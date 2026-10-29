/* THE ORG THIS TAB ACTS AS (R4: an org switch moves every tab of the browser session; no tab keeps acting
 * as the old org).
 *
 * The session cookie names ONE org for the whole browser, so a switch in tab 1 silently re-points tab 2's
 * cookie while tab 2 still shows the old org's data. Two signals close that gap:
 *   · every `/api` request this tab sends carries `X-Jaah-Org: <the org it loaded with>`, and the event
 *     stream `?org=<id>`; the API answers 409 `org_switched` when the cookie now names another org
 *     (`apps/api/app/stale_tab_guard.py`), so the stale tab is refused rather than served as either org;
 *   · the switching tab posts on `BroadcastChannel('jaah-session')`, so the others learn at once instead
 *     of on their next request.
 * Either one raises `ORG_SWITCHED_EVENT`, which `components/StaleTabDialog.tsx` turns into a blocking
 * "reload" dialog.
 *
 * The pin comes ONLY from a live identity answer (`auth.tsx`), never the localStorage snapshot: the
 * snapshot is shared by every tab, so after a switch it already names the NEW org. Until the first live
 * answer lands nothing is pinned and requests go without the header — they are then served as whatever
 * org the cookie names, which is the org this freshly loaded tab is about to render anyway. */

import type { OrgRef } from '../types'
import { withActAs } from './actAs'
import { withActivity } from './activity'
import { createExternalStore, useExternalStore } from './externalStore'
import { withOrgScope } from './orgScope'

export const ORG_HEADER = 'X-Jaah-Org'
export const STREAM_ORG_PARAM = 'org'
export const SESSION_CHANNEL = 'jaah-session'
export const ORG_SWITCHED_EVENT = 'jaah:org-switched'
/** The API's 409 detail for a request whose `X-Jaah-Org` is no longer the session's org. */
export const ORG_SWITCHED = 'org_switched'

/** Who the session acts as now, as the API's 409 or the switching tab's broadcast names it. */
export type ActiveOrg = { id: string; name: string }

type SessionMessage = { type: typeof ORG_SWITCHED; org: ActiveOrg }

const tabOrgId = createExternalStore<string | null>(null)
let channel: BroadcastChannel | null = null

export const tabOrg = (): string | null => tabOrgId.get()

/** Pin (or re-pin) the org this tab acts as — from a live `/api/auth/me` or sign-in answer only. */
export function pinTabOrg(orgId: string | null | undefined): void {
  listen()
  tabOrgId.set(orgId || null)
}

/** The pinned org as React state: a stream opened before the pin reopens with `?org=` once it lands. */
export function useTabOrg(): string | null {
  return useExternalStore(tabOrgId)
}

/** `headers` plus `X-Jaah-Org` when this tab has pinned an org; the same object otherwise. */
export function withOrg(headers: HeadersInit | undefined): HeadersInit | undefined {
  const pinned = tabOrgId.get()
  if (!pinned) return headers
  const merged = new Headers(headers)
  merged.set(ORG_HEADER, pinned)
  return Object.fromEntries(merged.entries())
}

/** Every same-origin `/api` request's session headers — `api.ts` and the few raw `fetch` callers alike: the
 *  background marker (`lib/activity.ts`), the org this tab acts as, an operator's act-as org on every method
 *  (`lib/actAs.ts`), and — on an operator's read — the global org filter (`lib/orgScope.ts`). `method` is the
 *  request's (absent = GET, `fetch`'s default): pass it for anything else, or a write would carry the filter. */
export const sessionHeaders = (headers: HeadersInit | undefined, method?: string) =>
  withOrgScope(withActAs(withOrg(withActivity(headers))), method)

/** The event-stream URL with `?org=` when pinned (EventSource cannot send a header). */
export function withOrgParam(path: string): string {
  const pinned = tabOrgId.get()
  if (!pinned) return path
  return `${path}${path.includes('?') ? '&' : '?'}${STREAM_ORG_PARAM}=${encodeURIComponent(pinned)}`
}

/** Tell this tab's UI the session now acts as `org`. Ignored by a tab not yet pinned: it is still loading,
 *  and will load as whatever the session names. When `org` IS this tab's own (switched away and back), the
 *  listener closes its dialog rather than opens it — compare against `tabOrg()`. */
export function raiseOrgSwitched(org: ActiveOrg): void {
  if (typeof window === 'undefined' || tabOrgId.get() === null) return
  window.dispatchEvent(new CustomEvent<ActiveOrg>(ORG_SWITCHED_EVENT, { detail: org }))
}

/** Read a 409 body; raises the event when it is the stale-tab refusal. */
export function noticeOrgSwitched(status: number, body: unknown): void {
  if (status !== 409 || !body || typeof body !== 'object') return
  const { detail, active_org: active } = body as { detail?: unknown; active_org?: unknown }
  if (detail !== ORG_SWITCHED || !isActiveOrg(active)) return
  raiseOrgSwitched(active)
}

/** The switching tab's half: every OTHER tab of this browser hears it (a channel never echoes to its poster). */
export function announceOrgSwitch(org: OrgRef | ActiveOrg): void {
  const message: SessionMessage = { type: ORG_SWITCHED, org: { id: org.id, name: org.name } }
  try {
    const out = new BroadcastChannel(SESSION_CHANNEL)
    out.postMessage(message)
    out.close()
  } catch {
    /* No BroadcastChannel: the other tabs learn from their next request's 409. */
  }
}

function listen(): void {
  if (channel || typeof BroadcastChannel === 'undefined') return
  try {
    channel = new BroadcastChannel(SESSION_CHANNEL)
  } catch {
    return
  }
  channel.onmessage = (evt: MessageEvent<unknown>) => {
    const data = evt.data as Partial<SessionMessage> | null
    if (data?.type === ORG_SWITCHED && isActiveOrg(data.org)) raiseOrgSwitched(data.org)
  }
}

function isActiveOrg(v: unknown): v is ActiveOrg {
  return (
    typeof v === 'object' && v !== null &&
    typeof (v as ActiveOrg).id === 'string' && typeof (v as ActiveOrg).name === 'string'
  )
}

/** Test seam: forget the pin and the channel. */
export function resetTabOrgForTest(): void {
  tabOrgId.reset()
  channel?.close()
  channel = null
}
