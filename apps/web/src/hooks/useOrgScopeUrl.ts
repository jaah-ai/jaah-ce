import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useLayoutEffect, useMemo } from 'react'
import { useLocation, useNavigate, type Location } from 'react-router-dom'
import { useAuth } from '../auth'
import { useListDetail } from '../components/ListDetail'
import { getOrgsByIds, getOrgsBySlugs } from '../lib/orgs'
import {
  readScopeParam,
  refreshScope,
  resolveScopeSlugs,
  scopeMatchesSlugs,
  scopeRoster,
  setOrgScope,
  useOrgScope,
  writeScopeParam,
  type OrgScope,
} from '../lib/orgScope'

/* THE ROSTER IS PAGED: nothing here holds every org. The held scope is re-read by its ids, a URL's
 * slugs by search (`lib/orgs.ts`), and the picker searches (`ui/OrgScopeControl.tsx`). `GET /api/orgs` is not
 * tenant-owned, so the filter never narrows any of these reads. */

/* Both reads cache the orgs already NAMED as the filter names them (`scopeRoster`), under keys of their own —
   `ScopeOrg[]`, not `Org[]` — so the picker can seed the slugs a commit writes (`useApplyOrgScope`). */
const sortedKey = (values: readonly string[]) => [...new Set(values)].sort()
const scopeIdsKey = (ids: readonly string[]) => ['orgs', 'scope-ids', sortedKey(ids)] as const
const scopeSlugsKey = (slugs: readonly string[]) => ['orgs', 'scope-slugs', sortedKey(slugs)] as const

/** The orgs behind `ids`, named as the filter names them; `orgs` undefined until read. */
export function useScopeOrgsByIds(ids: readonly string[], enabled: boolean) {
  const query = useQuery({
    queryKey: scopeIdsKey(ids),
    queryFn: async ({ signal }) => scopeRoster(await getOrgsByIds(ids, signal)),
    enabled: enabled && ids.length > 0,
    staleTime: 60_000,
  })
  return { ...query, orgs: query.data }
}

/** The orgs a URL's `?orgs=` slugs name, named as the filter names them. */
function useScopeOrgsBySlugs(slugs: readonly string[], enabled: boolean) {
  const query = useQuery({
    queryKey: scopeSlugsKey(slugs),
    queryFn: async ({ signal }) => scopeRoster(await getOrgsBySlugs(slugs, signal)),
    enabled: enabled && slugs.length > 0,
    staleTime: 60_000,
  })
  return { ...query, orgs: query.data }
}

/** The address bar's location. Under the list/detail split the shell's `<Routes>` renders the BACKGROUND list
 *  (`components/ListDetail.tsx`), so `useLocation()` above the pane answers the list's address, not the URL —
 *  and the URL is where `?orgs=` lives. */
function useAddress(): Location {
  const located = useLocation()
  return useListDetail()?.detailLocation ?? located
}

/** Has the address bar already moved past `location`? The browser router stamps each history entry's key into
 *  `history.state`, so a newer key there is a navigation still in flight (`v7_startTransition`) — a `<Navigate>`
 *  redirect, or this hook's own write. Carrying the scope into the stale location would revert it: `/hosts`
 *  stranded blank at `/hosts?orgs=…`. The effect re-runs once that navigation commits. A router
 *  that keeps no key there (the memory router) never reads as moved on. */
function addressMovedOn(location: Location): boolean {
  const live = (window.history.state as { key?: unknown } | null)?.key
  return typeof live === 'string' && live !== location.key
}

/** The address as `useOrgScopeUrl` (mounted once, for the whole session) last committed it: read by a write that
 *  lands after an `await`, when the caller's own render-time location may be stale or its component gone. */
let liveAddress: Location | null = null

/* KEEPS `?orgs=` AND THE REMEMBERED SCOPE IN STEP (`lib/orgScope.ts`), for an operator; a no-op for anyone
 * else, whose crafted `?orgs=` is simply never read (R5.8). Mounted once, in `App.tsx::AuthLayout`. The ADDRESS
 * is the one driver: the picker only navigates (`useApplyOrgScope`), and this effect selects what the committed
 * address names — so a navigation still in flight (`v7_startTransition`) is never mistaken for a URL to revert to.
 *   · The URL names a set → it wins: resolve its slugs, select them, and remember them. Unknown slugs are
 *     dropped and the address rewritten to what was actually selected, so it never claims a filter it is not.
 *     `?orgs=` (empty) is All, and needs no roster.
 *   · The URL has no parameter → restore the remembered set INTO the address (`replace`), so an in-app link
 *     that drops the query still shows, and shares, the scope it renders.
 *   · The held set is re-read by id (`?ids=`): a deleted org is dropped (an id the API no longer knows filters
 *     nothing, so the chip would lie), a renamed one relabelled.
 * Returns whether the page may render: `false` only while a URL names orgs the scope does not hold yet and
 * the read resolving their slugs is in flight — rendering then would fetch, and paint, under the wrong scope. */
export function useOrgScopeUrl(): boolean {
  const { admin } = useAuth()
  const operator = admin?.is_operator === true
  const location = useAddress()
  const navigate = useNavigate()
  const scope = useOrgScope()
  const slugs = operator ? readScopeParam(location.search) : null
  const settled = slugs === null || scopeMatchesSlugs(scope, slugs)
  const heldIds = useMemo(() => (scope ?? []).map(org => org.id), [scope])
  const held = useScopeOrgsByIds(heldIds, operator)
  const named = useScopeOrgsBySlugs(slugs && !settled ? slugs : [], operator)
  const namedDone = named.isSuccess || named.isError

  useLayoutEffect(() => {
    liveAddress = location
  }, [location])

  useLayoutEffect(() => {
    if (!operator) return
    const replaceWith = (next: OrgScope) =>
      navigate({ pathname: location.pathname, search: writeScopeParam(location.search, next), hash: location.hash }, { replace: true })
    if (slugs === null) {
      // No parameter: carry the remembered set into the address. All needs no parameter.
      const kept = held.orgs ? refreshScope(scope, held.orgs) : scope
      if (kept !== scope) setOrgScope(kept)
      if (kept && !addressMovedOn(location)) replaceWith(kept)
      return
    }
    if (slugs.length === 0) {
      setOrgScope(null)
      return
    }
    const against = settled ? held.orgs : named.orgs
    if (!against) return
    const resolved = settled ? refreshScope(scope, against) : resolveScopeSlugs(slugs, against)
    setOrgScope(resolved)
    if (!scopeMatchesSlugs(resolved, slugs)) replaceWith(resolved)
    // `slugs` and `settled` derive from `location.search` and `scope`, both in the list.
  }, [operator, location.pathname, location.search, location.hash, scope, held.orgs, named.orgs, navigate]) // eslint-disable-line react-hooks/exhaustive-deps

  /* Held only until the slugs are resolved. Once they are, the layout effect above selects them before paint (a
     page's first fetch under the old scope is then reset), so a Back across scopes never remounts the page. A
     read that failed resolves nothing: render under the scope already held. */
  return !operator || settled || slugs?.length === 0 || namedDone
}

/** Put `next` in the address as a new history entry (Back returns to the previous scope); `useOrgScopeUrl`
 *  selects it once the navigation commits. The picker already holds the orgs it names, so their slugs are
 *  seeded as resolved: a commit selects in the same render, never holding the page on a slug search. */
export function useApplyOrgScope(): (next: OrgScope) => void {
  const location = useAddress()
  const applyAt = useApplyOrgScopeAt()
  return useCallback((next: OrgScope) => applyAt(location, next), [applyAt, location])
}

/** `useApplyOrgScope`'s write, against the address it is handed. */
function useApplyOrgScopeAt(): (at: Location, next: OrgScope) => void {
  const navigate = useNavigate()
  const client = useQueryClient()
  return useCallback(
    (at: Location, next: OrgScope) => {
      if (next) client.setQueryData(scopeSlugsKey(next.map(org => org.slug)), [...next])
      const search = writeScopeParam(at.search, next)
      if (search !== at.search) navigate({ pathname: at.pathname, search, hash: at.hash })
    },
    [navigate, client],
  )
}

/** After an operator files a record into `orgId` (Add node, a new node group): a filter that leaves that org out
 *  gains it, so the new record and its page are not "Hidden by the org filter". A no-op under All, for an
 *  org already selected, and for anyone else (who holds no filter). A failed read leaves the filter as it was. */
export function useRevealOrg(): (orgId: string) => Promise<void> {
  const location = useAddress()
  const applyAt = useApplyOrgScopeAt()
  const client = useQueryClient()
  const scope = useOrgScope()
  return useCallback(
    async (orgId: string) => {
      if (!orgId || !scope || scope.some(org => org.id === orgId)) return
      try {
        const named = await client.fetchQuery({
          queryKey: scopeIdsKey([orgId]),
          queryFn: async ({ signal }) => scopeRoster(await getOrgsByIds([orgId], signal)),
          staleTime: 60_000,
        })
        const org = named.find(o => o.id === orgId)
        /* The address as it is NOW, not as it was before the read: a dialog closed meanwhile has dropped its
           own parameters, and writing the earlier search back would reopen it. */
        if (org) applyAt(liveAddress ?? location, [...scope, org])
      } catch {
        // The record is still reachable under All: nothing to undo.
      }
    },
    [applyAt, client, scope, location],
  )
}
