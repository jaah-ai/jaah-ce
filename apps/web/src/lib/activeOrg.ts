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
 * "reload" dialog, and marks the tab LEFT BEHIND (`tabLeftBehind`): the choke point (`net/send.ts`) then sends
 * nothing more and holds every request — the refused one included — until the session acts as this tab's org
 * again (switched back) or the tab reloads, so no page ever shows the refusal as its own error.
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

/** Another tab of this browser now holds a different signed-in identity (`auth.tsx`). */
export const SESSION_CHANGED = 'session_changed'
export const SESSION_CHANGED_EVENT = 'jaah:session-changed'

type SessionMessage = { type: typeof ORG_SWITCHED; org: ActiveOrg } | { type: typeof SESSION_CHANGED }

const tabOrgId = createExternalStore<string | null>(null)
/** The org the session now acts as, while it is not this tab's own; null while this tab is current. */
const switchedTo = createExternalStore<ActiveOrg | null>(null)
/** Bumped by every org-switch signal: a refusal sent before the latest one may name an org the session has left. */
let switchSignals = 0
let channel: BroadcastChannel | null = null

export const tabOrg = (): string | null => tabOrgId.get()

/** Pin (or re-pin) the org this tab acts as — from a live `/api/auth/me` or sign-in answer only. */
export function pinTabOrg(orgId: string | null | undefined): void {
  listen()
  tabOrgId.set(orgId || null)
  if (switchedTo.get()?.id === orgId) switchedTo.set(null)
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
  switchSignals += 1
  switchedTo.set(org.id === tabOrgId.get() ? null : org)
  window.dispatchEvent(new CustomEvent<ActiveOrg>(ORG_SWITCHED_EVENT, { detail: org }))
}

/** The org a 409 body names when it is the stale-tab refusal, else null. Read by the choke point alone
 *  (`net/send.ts`), which raises it unless a newer signal came first (`switchSignalCount`). */
export function orgSwitchRefusal(status: number, body: unknown): ActiveOrg | null {
  if (status !== 409 || !body || typeof body !== 'object') return null
  const { detail, active_org: active } = body as { detail?: unknown; active_org?: unknown }
  return detail === ORG_SWITCHED && isActiveOrg(active) ? active : null
}

/** How many org-switch signals this tab has heard: compared across a request, it says whether a newer one landed. */
export const switchSignalCount = (): number => switchSignals

/** Has an org switch in another tab left this one behind (module header)? */
export const tabLeftBehind = (): boolean => switchedTo.get() !== null

/** `tabLeftBehind` as React state: the event stream closes while it holds and reopens once it does not. */
export function useTabLeftBehind(): boolean {
  return useExternalStore(switchedTo, org => org !== null)
}

/** Resolves once this tab is current (at once when it is); rejects with `signal`'s reason if it aborts first. */
export function whenTabCurrent(signal?: AbortSignal | null): Promise<void> {
  if (!tabLeftBehind()) return Promise.resolve()
  if (signal?.aborted) return Promise.reject(signal.reason)
  return new Promise((resolve, reject) => {
    const onAbort = () => { stop(); reject(signal?.reason) }
    const stop = switchedTo.subscribe(() => {
      if (tabLeftBehind()) return
      stop()
      signal?.removeEventListener('abort', onAbort)
      resolve()
    })
    signal?.addEventListener('abort', onAbort, { once: true })
  })
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

/** The tab whose signed-in identity just changed tells the others: their relay socket holds the old cookie
 *  (`net/relay.ts`). */
export function announceSessionChanged(): void {
  try {
    const out = new BroadcastChannel(SESSION_CHANNEL)
    out.postMessage({ type: SESSION_CHANGED } satisfies SessionMessage)
    out.close()
  } catch {
    /* No BroadcastChannel: nothing to tell. */
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
    const data = evt.data as { type?: unknown; org?: unknown } | null
    if (data?.type === ORG_SWITCHED && isActiveOrg(data.org)) raiseOrgSwitched(data.org)
    if (data?.type === SESSION_CHANGED && typeof window !== 'undefined') window.dispatchEvent(new Event(SESSION_CHANGED_EVENT))
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
  switchedTo.reset()
  switchSignals = 0
  channel?.close()
  channel = null
}
