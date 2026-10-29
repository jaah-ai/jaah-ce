/* AN OPERATOR ACTING AS A TENANT ORG: this ONE tab served as that org's Owner seat — every route,
 * every plane — until it leaves. The server half is `apps/api/app/act_as.py`.
 *
 * THE HEADER IS THE WHOLE STATE. While acting, every `/api` request this tab sends carries
 * `X-Jaah-Act-As: <org id>`, on every method (`lib/activeOrg.ts::sessionHeaders`) — unlike the org filter
 * (`lib/orgScope.ts`), which rides reads only. The API honours it from an operator alone. The session cookie
 * is untouched, so the other tabs stay the operator's.
 *
 * Where it lives: this tab's sessionStorage, so a reload keeps acting and no other tab ever does.
 *
 * What a change does (`enterActAs`/`exitActAs`): the in-memory query cache is cleared — keys do not carry the
 * org, so no answer may paint across the change — and `auth.tsx` re-reads the identity, holding the shell until
 * it lands. While acting, the two localStorage copies every tab shares stay the operator's: the query cache is
 * neither restored nor saved (`queryClient.ts::pauseQueryPersistence`) and the identity snapshot is neither
 * seeded from nor written (`auth.tsx`).
 *
 * A target the API will not act as (unknown, disabled, the operator org) answers 409 `act_as_unavailable` on
 * every request; the tab leaves act-as and says so once (`noticeActAsUnavailable`, `components/ActAsBar.tsx`). */

import { useLayoutEffect, useRef } from 'react'
import { pauseQueryPersistence, queryClient } from '../queryClient'
import type { OrgRef } from '../types'
import { createExternalStore, useExternalStore } from './externalStore'
import { readJson, removeKey, writeJson } from './safeStorage'
import { clearTaskPageSeeds } from './taskPageSeed'

export const ACT_AS_HEADER = 'X-Jaah-Act-As'
/** The API's 409 detail for a target it will not act as. */
export const ACT_AS_UNAVAILABLE = 'act_as_unavailable'
const STORAGE_KEY = 'jaah.act-as'
/* Where this tab was when it entered act-as: Exit goes back there (`actAsReturnTo`). Its own key, so the stored
 * org stays exactly an `ActAsOrg`. */
const RETURN_KEY = 'jaah.act-as.from'

/** The org acted as: the id rides the header, the name the bar, the slug nothing yet. */
export type ActAsOrg = OrgRef

function isActAsOrg(v: unknown): v is ActAsOrg {
  return (
    typeof v === 'object' && v !== null &&
    typeof (v as ActAsOrg).id === 'string' && (v as ActAsOrg).id !== '' &&
    typeof (v as ActAsOrg).slug === 'string' && typeof (v as ActAsOrg).name === 'string'
  )
}

function readStored(): ActAsOrg | null {
  const stored = readJson('session', STORAGE_KEY)
  return isActAsOrg(stored) ? { id: stored.id, slug: stored.slug, name: stored.name } : null
}

/* Same-origin paths only (not `//host` nor `/\host`): a return URL is never trusted to leave the console. */
const inConsolePath = (v: unknown): v is string => typeof v === 'string' && /^\/(?![/\\])/.test(v)

function readReturnTo(): string | null {
  const stored = readJson('session', RETURN_KEY)
  return inConsolePath(stored) ? stored : null
}

// Seeded after creation, so the test seam's reset lands on null rather than on whatever this tab had stored.
const acting = createExternalStore<ActAsOrg | null>(null)
acting.set(readStored())
let returnTo: string | null = acting.get() ? readReturnTo() : null
/* The org a refusal ended act-as for, until the notice is dismissed. */
const refused = createExternalStore<ActAsOrg | null>(null)
// Before the first render, so `PersistQueryClientProvider` never restores the operator's cache into an acting tab.
pauseQueryPersistence(acting.get() !== null)

export const actingAs = (): ActAsOrg | null => acting.get()

/** The in-console URL this tab entered act-as from, or null (unknown, or not acting). */
export const actAsReturnTo = (): string | null => returnTo

/** The org this tab acts as, as React state. */
export function useActingAs(): ActAsOrg | null {
  return useExternalStore(acting)
}

/** The org a refusal just ended act-as for, as React state (the notice), until `dismissActAsNotice`. */
export function useActAsRefused(): ActAsOrg | null {
  return useExternalStore(refused)
}

export function dismissActAsNotice(): void {
  refused.set(null)
}

/** `headers` plus `X-Jaah-Act-As` while this tab acts; the same object otherwise. */
export function withActAs(headers: HeadersInit | undefined): HeadersInit | undefined {
  const org = acting.get()
  if (!org) return headers
  const merged = new Headers(headers)
  merged.set(ACT_AS_HEADER, org.id)
  return Object.fromEntries(merged.entries())
}

/** ONE REQUEST as `org`: the header alone, for an operator's single write on another org's row (a
 *  Delete) without switching the tab. Nothing for `null` (the row is the viewer's own org). */
export function actAsOnce(org: OrgRef | null): HeadersInit | undefined {
  return org ? { [ACT_AS_HEADER]: org.id } : undefined
}

/** ONE READ as `org`, as arguments to spread after the path: `get(path, ...readAsOnce(org))`. Nothing for
 *  `null`, so the viewer's own read is sent exactly as before. */
export function readAsOnce(org: OrgRef | null): [] | [RequestInit] {
  return org ? [{ headers: { [ACT_AS_HEADER]: org.id } }] : []
}

/** ONE WRITE as `org`, as `actAsOnce`'s header to spread after a verb's body: `post(path, body, ...writeAsOnce(org))`. */
export function writeAsOnce(org: OrgRef | null): [] | [HeadersInit] {
  return org ? [{ [ACT_AS_HEADER]: org.id }] : []
}

/* ── ANOTHER ORG'S ROW, WRITTEN AS THAT ORG ─────────────────────────────────────────────────────────────────────
 * An operator edits another org's project, task or account in place, on that org's behalf. A page
 * showing such a row HOLDS its path (`useActAsRows`): while held, every write `api.ts` sends to that path or below
 * it (`/api/projects/7`, `/api/projects/7/env/…`) carries the row's org for that one request (`actAsOnce`'s header),
 * whichever tab, panel or dialog sends it. Reads stay the operator's, and so does every write elsewhere (Change
 * org…, support, the operator's own routes). The longest held path wins; a tab acting as an org sends that org. */
const heldRows = new Map<string, OrgRef[]>()

const pathOf = (url: string): string => url.split(/[?#]/, 1)[0]

/** Hold `path`'s writes as `org` until the returned release is called. */
export function holdRowOrg(path: string, org: OrgRef): () => void {
  const holds = heldRows.get(path) ?? []
  holds.push(org)
  heldRows.set(path, holds)
  return () => {
    const at = holds.indexOf(org)
    if (at >= 0) holds.splice(at, 1)
    if (holds.length === 0 && heldRows.get(path) === holds) heldRows.delete(path)
  }
}

/** The org a write to `url` goes as (the longest held path at or above it), or null: a read, or no held row. */
export function heldRowOrg(url: string, method: string | undefined): OrgRef | null {
  if (!method || ['GET', 'HEAD'].includes(method.toUpperCase())) return null
  const path = pathOf(url)
  let best: string | null = null
  for (const held of heldRows.keys()) {
    if ((path === held || path.startsWith(`${held}/`)) && (best === null || held.length > best.length)) best = held
  }
  const holds = best === null ? undefined : heldRows.get(best)
  return holds?.[holds.length - 1] ?? null
}

/** `headers` plus the held row's org on a write below a held path (`heldRowOrg`); the same object otherwise. A
 *  caller's own act-as header stands. */
export function withHeldRowOrg(url: string, init: RequestInit): HeadersInit | undefined {
  const org = heldRowOrg(url, init.method)
  if (!org) return init.headers
  const merged = new Headers(init.headers)
  if (!merged.has(ACT_AS_HEADER)) merged.set(ACT_AS_HEADER, org.id)
  return Object.fromEntries(merged.entries())
}

/** Hold each row whose `org` is another org's (`useOtherOrg`'s answer; `null` holds nothing) while mounted. */
export function useActAsRows(rows: ReadonlyArray<{ path: string; org: OrgRef | null }>): void {
  const held = rows.filter((row): row is { path: string; org: OrgRef } => row.org !== null)
  const key = held.map(row => `${row.path}\n${row.org.id}`).join('\n')
  const latest = useRef(held)
  latest.current = held
  // A layout effect: held before any child's mount-time effect could write.
  useLayoutEffect(() => {
    const releases = latest.current.map(row => holdRowOrg(row.path, row.org))
    return () => releases.forEach(release => release())
  }, [key])
}

function change(next: ActAsOrg | null): void {
  if (next) writeJson('session', STORAGE_KEY, next)
  else {
    removeKey('session', STORAGE_KEY)
    removeKey('session', RETURN_KEY)
    returnTo = null
  }
  // Cleared before persistence resumes, so the first save after leaving holds none of the org's answers.
  queryClient.clear()
  clearTaskPageSeeds()
  pauseQueryPersistence(next !== null)
  // Published last: a render under this notify must already see the cleared cache and the stored org.
  acting.set(next)
}

/** Act as `org` in this tab. `auth.tsx` re-reads the identity as its Owner seat. Exit returns to the page this
 *  was pressed on; a switch from one org to another keeps the first page, the operator's own. */
export function enterActAs(org: ActAsOrg): void {
  refused.set(null)
  if (!acting.get()) {
    const here = `${window.location.pathname}${window.location.search}${window.location.hash}`
    returnTo = inConsolePath(here) ? here : null
    if (returnTo) writeJson('session', RETURN_KEY, returnTo)
  }
  change({ id: org.id, slug: org.slug, name: org.name })
}

/** Leave act-as: this tab is the operator's again. A no-op when not acting. */
export function exitActAs(): void {
  if (!acting.get()) return
  change(null)
}

/** Read a 409 body; leaves act-as, with a notice, when it is the refusal of the org acted as. */
export function noticeActAsUnavailable(status: number, body: unknown): void {
  if (status !== 409 || !acting.get() || !body || typeof body !== 'object') return
  if ((body as { detail?: unknown }).detail !== ACT_AS_UNAVAILABLE) return
  refused.set(acting.get())
  change(null)
}

/** Test seam: forget the org, the notice and the stored copy. */
export function resetActAsForTest(): void {
  acting.reset()
  returnTo = null
  refused.reset()
  heldRows.clear()
  pauseQueryPersistence(false)
  removeKey('session', STORAGE_KEY)
  removeKey('session', RETURN_KEY)
}
