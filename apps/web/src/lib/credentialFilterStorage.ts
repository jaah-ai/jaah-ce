/* Persistence for the Credentials roster's VIEW: search text, four narrowings, and the view mode with its
 * open groups — remembered per operator across reloads and navigation (R2.8, R3.1).
 * Nine fields, three kinds: SIX are WIRE VALUES that narrow server-side; `sort` orders the answer; `mode` and `expanded` only decide
 * how the same answer is drawn. Pure of React (the `lib/taskFilterStorage.ts` precedent).
 * `localStorage`, unlike the Tasks board's `sessionStorage`: R2.8 must survive closing the tab. The cost
 * (a stale narrowing tomorrow) is paid by the on-screen strip showing every active filter and the
 * filtered empty state (R2.6) offering to clear them.
 * ADMIN-SCOPED key: `ownerIds` references rows another admin may not see, so an inherited filter
 * would read as an outage.
 * One store for the whole view (one read at mount, one write-through, no clobbering). To add a field: add
 * it to `CredentialView`, give it the WIDEST default in `DEFAULT_CREDENTIAL_VIEW`, add a per-field read in
 * `loadCredentialView`; older payloads then read the default. THE FALLBACK ALWAYS WIDENS: nothing
 * unaccounted-for may hide rows.
 * No `orgId` (R5.6): an operator's org narrowing comes from the global filter (`lib/orgScope.ts`); a
 * stale persisted `orgId` from an older payload is ignored, like any unknown field. */

import { readJsonObject, writeJson } from './safeStorage'
import { readExpandedPaths, readTreeMode, type TreeViewMode } from './treeViewStorage'
import { isUuidShaped } from './uuidShape'

/** The persisted view, one-for-one with the page's state. Every narrowing is the WIRE value `GET
 *  /api/credentials` takes (`type` slug, `ownerIds` actor uuids), so restoring is
 *  re-issuing the request. The two view fields go on no wire. */
export type CredentialView = {
  /** `?q=`, matched server-side over name AND description (R2.2). `''` is no search. */
  q: string
  /** A type SLUG as `?type=`, or `''`. Never a label: the server 422s an unknown slug. */
  type: string
  /** Repeated `?owner_id=`: USER uuids (names collide; see `CredentialOwnerOut`), the roster their union; `[]` is
   *  every owner. Ids only: the strip resolves their names (`GET /api/credentials/owners?ids=`). */
  ownerIds: string[]
  /** `?secrets=`: `''` or the API's `Literal["yes", "no"]`. */
  secrets: '' | 'yes' | 'no'
  /** `?health=`: `''` or the API's `RosterHealth` (expired, expiring within 7 days, never used, last test failed). */
  health: '' | CredentialHealth
  /** `?mine=true`: actor identity the browser cannot judge (see
   *  `pages/credentials/api.ts::listCredentials`). `false` sends nothing. */
  mine: boolean
  /** Flat or Tree (`name` split on `/`). Not a wire value (with `expanded`, never counted by
   *  `hasActiveCredentialFilters`). FLAT IS THE DEFAULT: it hides nothing behind a disclosure. */
  mode: TreeViewMode
  /** Group PATHS expanded in Tree mode — `['a', 'a/b']`, never indices, so expansion survives a filter
   *  change (R3.6; why: `nameTree.ts`'s `path` docblock).
   *  NEVER PRUNED against the roster: a filtered-out group comes back expanded (R3.6). Capped by
   *  `treeViewStorage.ts::readExpandedPaths`. An ARRAY, not a `Set`: a `Set` `JSON.stringify`s to `{}`. */
  expanded: string[]
  /** `?sort=`: the roster's server-side order (`CREDENTIAL_SORTS`). An ORDER, not a narrowing: it re-fetches
   *  like a filter but is never counted by `hasActiveCredentialFilters` nor dropped by `clearCredentialFilters`. */
  sort: CredentialSort
}

/** The route's `RosterSort` arms (`apps/api/app/routes/credentials/reads.py::_ROSTER_ORDER`). */
export const CREDENTIAL_SORTS = ['newest', 'oldest', 'name', 'type', 'org'] as const
export type CredentialSort = (typeof CREDENTIAL_SORTS)[number]

/** The route's `RosterHealth` arms (`apps/api/app/routes/credentials/roster.py`). */
export const CREDENTIAL_HEALTHS = ['expired', 'expiring', 'never_used', 'test_failed'] as const
export type CredentialHealth = (typeof CREDENTIAL_HEALTHS)[number]

/**
 * The UNFILTERED roster: first visit and the fallback for anything unreadable. Exported so tests and the
 * Clear control name the same object.
 */
export const DEFAULT_CREDENTIAL_VIEW: CredentialView = {
  q: '',
  type: '',
  ownerIds: [],
  secrets: '',
  health: '',
  mine: false,
  /* The widening fallback for the view fields: Flat, nothing expanded. */
  mode: 'flat',
  expanded: [],
  /* The route's own default, so a first visit sends nothing new. */
  sort: 'newest',
}

/**
 * The storage key, scoped to the signed-in admin's id; `null` gets its own `anon` bucket. `auth.tsx`
 * seeds `admin` from its snapshot, so the id is normally present on first render.
 */
export function credentialViewStorageKey(adminId: string | null): string {
  return `cockpit.credentials.view:${adminId ?? 'anon'}`
}

/**
 * The stored view, or the unfiltered roster for anything missing or unusable.
 * `getItem` AND `JSON.parse` sit inside the try: private-mode Safari and blocked storage throw on ACCESS.
 * Validation is PER FIELD (independent choices); only a non-object payload is discarded whole.
 * Shape only: a stale owner id cannot be detected here; the server answers an empty page and the
 * filtered empty state (R2.6) offers to clear it.
 */
export function loadCredentialView(adminId: string | null): CredentialView {
  const stored = readJsonObject('local', credentialViewStorageKey(adminId))
  if (!stored) return DEFAULT_CREDENTIAL_VIEW
  return {
    q: readString(stored.q),
    type: readString(stored.type),
    ownerIds: readOwnerIds(stored),
    // Closed vocabulary: the route declares `Literal["yes", "no"]` and 422s anything else, so a stored
    // `"true"` would fail every load.
    secrets: stored.secrets === 'yes' || stored.secrets === 'no' ? stored.secrets : '',
    // Closed vocabulary, for the same reason.
    health: CREDENTIAL_HEALTHS.find(health => health === stored.health) ?? '',
    // `typeof === 'boolean'`, not truthiness: a stored `"false"` is truthy.
    mine: typeof stored.mine === 'boolean' ? stored.mine : false,
    mode: readTreeMode(stored.mode),
    expanded: readExpandedPaths(stored.expanded),
    // Closed vocabulary: the route 422s anything else.
    sort: CREDENTIAL_SORTS.find(sort => sort === stored.sort) ?? 'newest',
  }
}

/** A stored narrowing, or `''` for a non-string. A type check only: a well-formed id naming nothing is as
 *  empty a page as a malformed one, and the route handles both. */
function readString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** The most owners a view holds: the bound `GET /api/credentials/owners?ids=` resolves in one request. */
export const MAX_OWNER_IDS = 50

/** `ownerIds`, or the single `ownerId` an older payload stored; anything not uuid-shaped dropped (the route 422s
 *  it), duplicates once. */
function readOwnerIds(stored: Record<string, unknown>): string[] {
  const raw = Array.isArray(stored.ownerIds) ? stored.ownerIds : [stored.ownerId]
  const ids = raw.filter(isUuidShaped)
  return [...new Set(ids)].slice(0, MAX_OWNER_IDS)
}

/**
 * Persist the view. Swallows a blocked-storage throw: failing to REMEMBER must never cost the view.
 */
export function saveCredentialView(adminId: string | null, view: CredentialView): void {
  writeJson('local', credentialViewStorageKey(adminId), view)
}

/** Whether any narrowing is active — R2.6's fork between the two empty states, decided on the VIEW, not
 *  `items.length`. `mode` and `expanded` remove no rows, so they must not appear below. */
export function hasActiveCredentialFilters(view: CredentialView): boolean {
  return Boolean(view.q || view.type || view.ownerIds.length > 0 || view.secrets || view.health || view.mine)
}

/** The fields that decide how the roster is DRAWN. Shared by `hasActiveCredentialFilters`, the page's
 *  writer (must not reset the pager or re-fetch) and `clearCredentialFilters` (must not reset them). */
export const CREDENTIAL_VIEW_ONLY_FIELDS: readonly (keyof CredentialView)[] = ['mode', 'expanded']

/**
 * Every NARROWING dropped, the view fields and the order kept — the filtered empty state's "Clear filters". Clearing to
 * `DEFAULT_CREDENTIAL_VIEW` would also reset Tree mode to Flat with every folder shut, and persist it.
 */
export function clearCredentialFilters(view: CredentialView): CredentialView {
  const cleared = { ...DEFAULT_CREDENTIAL_VIEW }
  for (const field of [...CREDENTIAL_VIEW_ONLY_FIELDS, 'sort'] as const) {
    // Per-field copy, not a spread: a new field is cleared by default and kept only once named above.
    Object.assign(cleared, { [field]: view[field] })
  }
  return cleared
}
