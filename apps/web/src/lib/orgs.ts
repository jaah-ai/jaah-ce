/* The one home of `GET /api/orgs`. It lives in `lib/`, not `pages/orgs/api.ts`, because `ui/OrgSelect`
 * consumes it and `ui/**` must not import `pages/**`. `Org` lives in `src/types.ts`.
 * THE ROSTER IS PAGED (`routes/orgs.py::list_orgs`): no reader holds every org. A picker searches
 * (`q`), a held value resolves its label by id (`ids`), and a URL's slugs resolve by search. */

import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { get } from '../api'
import { useDebouncedCallback } from '../hooks/useDebouncedCallback'
import type { Page, SortDir } from '../hooks/useListPage'
import type { Org } from '../types'
import { QUERY_DEBOUNCE_MS, QUERY_MAX_WAIT_MS } from '../ui/AsyncLookup'

export type OrgSort = 'name' | 'slug' | 'created'

export type OrgListQuery = {
  /** Name or slug, a substring match. */
  q?: string
  /** Exactly these orgs (≤ `ORG_IDS_MAX`), for naming values already held. */
  ids?: readonly string[]
  page?: number
  per?: number
  sort?: OrgSort
  dir?: SortDir
}

/** A typeahead's page: enough to scan, small enough to answer per keystroke. */
export const ORG_LOOKUP_PER = 20
/** The server's `?ids=` and `?per=` ceiling (`routes/orgs.py::_IDS_MAX`, `list_page.py`). */
export const ORG_IDS_MAX = 200

function orgsPath(query: OrgListQuery): string {
  const params = new URLSearchParams()
  const q = query.q?.trim()
  if (q) params.set('q', q)
  if (query.ids) params.set('ids', query.ids.join(','))
  if (query.page && query.page > 1) params.set('page', String(query.page))
  if (query.per) params.set('per', String(query.per))
  if (query.sort) {
    params.set('sort', query.sort)
    params.set('dir', query.dir ?? 'asc')
  }
  const out = params.toString().replace(/%2C/gi, ',')
  return `/api/orgs${out ? `?${out}` : ''}`
}

/** One page of the orgs this reader may see. Superadmin-only server-side (`routes/orgs.py`), so other callers
 *  must expect a 403 — `ui/OrgSelect` renders it as a field-level message. */
export const listOrgs = (query: OrgListQuery = {}, signal?: AbortSignal) =>
  get<Page<Org>>(orgsPath(query), signal ? { signal } : undefined)

/** A server-searched org box for a picker that draws its own list (an `Autocomplete`, a menu): `typed` is the
 *  box, `searched` the debounced query the server is asked (`ui/AsyncLookup`'s numbers, so every org search
 *  feels alike), `query` its page. `type` takes a keystroke; `clear` empties the box without a trailing read.
 *  A picked name echoed into the box is not a keystroke — callers pass only real input to `type`. */
export function useOrgSearch({ enabled = true, per = ORG_LOOKUP_PER }: { enabled?: boolean; per?: number } = {}) {
  const [typed, setTyped] = useState('')
  const [searched, setSearched] = useState('')
  const { trigger, cancel } = useDebouncedCallback(
    () => setSearched(typed.trim()),
    QUERY_DEBOUNCE_MS,
    QUERY_MAX_WAIT_MS,
  )
  const query = useQuery({
    queryKey: ['orgs', 'search', per, searched],
    queryFn: ({ signal }) => listOrgs({ q: searched, per }, signal),
    enabled,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const type = (text: string) => {
    setTyped(text)
    trigger()
  }
  const clear = () => {
    cancel()
    setTyped('')
    setSearched('')
  }
  return { query, typed, searched, type, clear }
}

/** The orgs behind `ids`, in the server's order; an id the reader cannot see (deleted, never existed) is simply
 *  absent. No ids, no request. More than one `?ids=` can carry is read in chunks. */
export async function getOrgsByIds(ids: readonly string[], signal?: AbortSignal): Promise<Org[]> {
  const unique = [...new Set(ids.filter(Boolean))]
  const chunks: string[][] = []
  for (let at = 0; at < unique.length; at += ORG_IDS_MAX) chunks.push(unique.slice(at, at + ORG_IDS_MAX))
  const pages = await Promise.all(chunks.map(chunk => listOrgs({ ids: chunk, per: ORG_IDS_MAX }, signal)))
  return pages.flatMap(page => page.items)
}

/** The orgs whose slug is exactly one of `slugs` (a shared `?orgs=` link). The API matches slugs by search, so
 *  each is one `q` read ordered by slug, kept only on an exact match; an unknown slug is absent. */
export async function getOrgsBySlugs(slugs: readonly string[], signal?: AbortSignal): Promise<Org[]> {
  const wanted = [...new Set(slugs.filter(Boolean))]
  const pages = await Promise.all(
    wanted.map(slug => listOrgs({ q: slug, per: ORG_IDS_MAX, sort: 'slug' }, signal)),
  )
  const found = new Map<string, Org>()
  pages.forEach((page, i) => {
    const hit = page.items.find(org => org.slug === wanted[i])
    if (hit) found.set(hit.id, hit)
  })
  return [...found.values()]
}

/* How many self-service orgs await the operator (`GET /api/orgs/pending-count`, operator-only). Here, not
 * `pages/orgs/api.ts`, because the rail's badge (`components/Sidebar.tsx`) reads it. */
export const getPendingOrgCount = () => get<{ count: number }>('/api/orgs/pending-count')

/** The badge's query key: a decision on the Orgs page invalidates it so the badge moves at once. */
export const PENDING_ORG_COUNT_KEY = ['orgs', 'pending-count'] as const
