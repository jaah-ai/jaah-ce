/* THE OPERATOR'S GLOBAL ORG FILTER (R5): which orgs an operator's console is narrowed to — All, or
 * any set. A FILTER on reads, never a scope: it changes no create's org and grants or removes no power (R5.7).
 *
 * Where it lives:
 *   · the URL, `?orgs=<slug>,<slug>` — shareable, reload- and back/forward-stable. `?orgs=` (empty) is an
 *     explicit All, so stepping back across a "show all orgs" lands on All rather than on the remembered set;
 *   · this browser, per operator (`lib/safeStorage.ts`), so a URL WITHOUT the parameter restores the last
 *     choice. A URL carrying it wins and becomes the remembered choice. `hooks/useOrgScopeUrl.ts` keeps the
 *     two in step; `ui/OrgScopeControl.tsx` is the picker.
 *
 * What it does: every GET/HEAD this tab sends as an operator carries `X-Jaah-Org-Scope: <id>,<id>` (through
 * `lib/activeOrg.ts::sessionHeaders`, the one helper every `/api` caller uses), and the API narrows every list,
 * count, badge and dashboard to those orgs (`apps/api/app/authz/guard.py::_requested_org_filter`). A non-operator
 * never sends it, and the API ignores it from one anyway (R5.8). The SSE stream is not filtered: its events
 * are invalidation hints, and every refetch they cause is.
 *
 * A change resets every query and drops the persisted cache — the org-switch precedent (`lib/orgSwitch.ts`):
 * query keys do not carry the scope, so an answer fetched under the old one must never paint under the new. */

import { dropPersistedQueries, queryClient } from '../queryClient'
import type { Org, OrgRef } from '../types'
import { createExternalStore, useExternalStore } from './externalStore'
import { readJson, writeJson } from './safeStorage'

export const ORG_SCOPE_HEADER = 'X-Jaah-Org-Scope'
export const ORG_SCOPE_PARAM = 'orgs'
const STORAGE_PREFIX = 'jaah.org-scope.'

/** One selected org: the id rides the header, the slug the URL, the name the chip. */
export type ScopeOrg = OrgRef
/** The selected orgs, or `null` for All orgs. Never an empty array. */
export type OrgScope = readonly ScopeOrg[] | null

let viewerId: string | null = null
const scope = createExternalStore<OrgScope>(null)

const storageKey = (viewer: string) => `${STORAGE_PREFIX}${viewer}`

function isScopeOrg(v: unknown): v is ScopeOrg {
  return (
    typeof v === 'object' && v !== null &&
    typeof (v as ScopeOrg).id === 'string' && typeof (v as ScopeOrg).slug === 'string' &&
    typeof (v as ScopeOrg).name === 'string'
  )
}

/** `orgs` deduplicated by id; `null` when nothing is left (All). */
function normalize(orgs: readonly ScopeOrg[] | null | undefined): OrgScope {
  if (!orgs) return null
  const byId = new Map<string, ScopeOrg>()
  for (const org of orgs) if (isScopeOrg(org) && !byId.has(org.id)) byId.set(org.id, { id: org.id, slug: org.slug, name: org.name })
  return byId.size ? [...byId.values()] : null
}

function readRemembered(viewer: string): OrgScope {
  const stored = readJson('local', storageKey(viewer))
  return Array.isArray(stored) ? normalize(stored.filter(isScopeOrg)) : null
}

/** Do two scopes select the same orgs (order aside)? */
export function sameScope(a: OrgScope, b: OrgScope): boolean {
  if (a === null || b === null) return a === b
  if (a.length !== b.length) return false
  const ids = new Set(a.map(org => org.id))
  return b.every(org => ids.has(org.id))
}

/** Who the filter belongs to: the operator's admin id, or `null` for anyone else (no filter, no header).
 *  Called by `auth.tsx` whenever the identity changes — the snapshot it seeds from included, so the first
 *  requests of a warm reload already carry the remembered scope. Loads that operator's remembered choice. */
export function bindOrgScopeViewer(viewer: string | null): void {
  if (viewer === viewerId) return
  viewerId = viewer
  const next = viewer ? readRemembered(viewer) : null
  if (sameScope(next, scope.get())) return
  scope.set(next)
}

export const currentOrgScope = (): OrgScope => scope.get()

/** Select `next` (`null` = All), remember it, and refetch everything under it. Returns whether it changed.
 *  Ignored for a non-operator: there is no filter to set. */
export function setOrgScope(next: readonly ScopeOrg[] | null): boolean {
  if (!viewerId) return false
  const normalized = normalize(next)
  const changed = !sameScope(normalized, scope.get())
  // Same orgs under a new name or slug (a rename): relabel and remember, but refetch nothing.
  if (!changed && JSON.stringify(normalized) === JSON.stringify(scope.get())) return false
  writeJson('local', storageKey(viewerId), normalized ?? [])
  if (changed) {
    dropPersistedQueries()
    void queryClient.resetQueries()
  }
  // Published last: a render under this notify must already see the dropped queries and the remembered scope.
  scope.set(normalized)
  return changed
}

/** The scope as React state. */
export function useOrgScope(): OrgScope {
  return useExternalStore(scope)
}

const READS = new Set(['GET', 'HEAD'])

/** `headers` plus `X-Jaah-Org-Scope` for an operator's filtered read; the same object otherwise. A write never
 *  carries it (R5.7): the filter must not steer which org a create lands in. */
export function withOrgScope(headers: HeadersInit | undefined, method: string | undefined): HeadersInit | undefined {
  const orgs = scope.get()
  if (!viewerId || !orgs || !READS.has((method ?? 'GET').toUpperCase())) return headers
  const merged = new Headers(headers)
  // A caller's own header wins: `UNFILTERED_READ`.
  if (merged.has(ORG_SCOPE_HEADER)) return headers
  merged.set(ORG_SCOPE_HEADER, orgs.map(org => org.id).join(','))
  return Object.fromEntries(merged.entries())
}

/** The init of ONE read that names a record whatever the filter: an empty header is All server-side, and
 *  `withOrgScope` keeps a header its caller set. For a by-id read of a record the page shows regardless of the
 *  filter — an operator setting's credential, which lives in org zero. Ignored for a non-operator. */
export const UNFILTERED_READ: RequestInit = { headers: { [ORG_SCOPE_HEADER]: '' } }

/** The slugs `?orgs=` names, `[]` for an explicit All (`?orgs=`), or `null` when the parameter is absent. */
export function readScopeParam(search: string): string[] | null {
  const raw = new URLSearchParams(search).get(ORG_SCOPE_PARAM)
  if (raw === null) return null
  return [...new Set(raw.split(',').map(slug => slug.trim()).filter(Boolean))]
}

/** `search` with `?orgs=` naming `next` — empty for All, so the address says All rather than "whatever was
 *  remembered". Other parameters are kept. Returns `''` or a string starting with `?`. */
export function writeScopeParam(search: string, next: OrgScope): string {
  const params = new URLSearchParams(search)
  params.set(ORG_SCOPE_PARAM, next ? next.map(org => org.slug).join(',') : '')
  // Commas are the list separator and legal in a query; keep them readable rather than `%2C`.
  const out = params.toString().replace(/%2C/gi, ',')
  return out ? `?${out}` : ''
}

/** How the operator plane is named in the picker, the chip and the URL's resolved scope. */
export const OPERATOR_PLANE_LABEL = 'Operator'

/** The roster as the filter names and orders it: the operator plane first (as "Operator"), then by name. One
 *  order for the picker and for a scope read from a URL, so a shared link draws the same chip as picking. */
export function scopeRoster(roster: readonly Org[]): ScopeOrg[] {
  return [...roster]
    .sort((a, b) => a.is_operator_plane === b.is_operator_plane ? a.name.localeCompare(b.name) : a.is_operator_plane ? -1 : 1)
    .map(org => ({ id: org.id, slug: org.slug, name: org.is_operator_plane ? OPERATOR_PLANE_LABEL : org.name }))
}

/** The orgs of `roster` (in its order) whose slug `slugs` names; unknown slugs are dropped (none left is All). */
export function resolveScopeSlugs(slugs: readonly string[], roster: readonly OrgRef[]): OrgScope {
  const wanted = new Set(slugs)
  return normalize(roster.filter(org => wanted.has(org.slug)))
}

/** `current` re-read against `roster` by id: a deleted org is dropped, a renamed one relabelled. A remembered
 *  scope naming only orgs that no longer exist would otherwise draw a chip while the API, finding none of its
 *  ids, filters nothing. */
export function refreshScope(current: OrgScope, roster: readonly OrgRef[]): OrgScope {
  if (!current) return null
  const ids = new Set(current.map(org => org.id))
  return normalize(roster.filter(org => ids.has(org.id)))
}

/** Does `scope` select exactly the orgs `slugs` names? */
export function scopeMatchesSlugs(current: OrgScope, slugs: readonly string[]): boolean {
  if (current === null) return slugs.length === 0
  if (current.length !== slugs.length) return false
  const mine = new Set(current.map(org => org.slug))
  return slugs.every(slug => mine.has(slug))
}

/** The orgs, by name, for prose: "Acme", "Acme and Org B", "Acme, Org B and 2 more". */
export function scopeNames(current: readonly ScopeOrg[]): string {
  const names = current.map(org => org.name)
  if (names.length <= 2) return names.join(' and ')
  if (names.length === 3) return `${names[0]}, ${names[1]} and ${names[2]}`
  return `${names[0]}, ${names[1]} and ${names.length - 2} more`
}

/** An empty list's title naming the orgs the filter narrowed it to ("No nodes yet" → "No nodes in Acme"), so
 *  an empty page is never mistaken for an empty fleet; `title` unchanged under All. */
export function scopedTitle(title: string, current: OrgScope): string {
  return current ? `${title.replace(/(?: yet)?\.?$/, '')} in ${scopeNames(current)}` : title
}

/** Test seam: forget the viewer and the scope. */
export function resetOrgScopeForTest(): void {
  viewerId = null
  scope.reset()
}
