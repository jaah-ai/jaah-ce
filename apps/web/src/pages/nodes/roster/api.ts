import { setPageParams, type ListPageState } from '../../../hooks/useListPage'

/* THE NODES TAB'S READ: `GET /api/nodes/page` (`apps/api/app/routes/nodes/reads.py::list_nodes_page`) — the roster
 * searched, filtered, sorted and paged (numbered, `hooks/useListPage.ts`) SERVER-SIDE, so a fleet of thousands
 * costs the browser one page at a time. The full list (`hooks/useNodesRoster.ts`,
 * `GET /api/nodes`) stays for every surface that needs NAMES — a slot's label, the audit filter, a host's own
 * record — and is no longer the Nodes tab's.
 * Every control on the strip is a query parameter the server applies to the page AND `total`; nothing here
 * filters. The console's words (`pool`) map to the wire's (`group`) at this one seam. */

export const NODE_STATUSES = ['online', 'offline', 'unknown'] as const
export const NODE_ENABLED_STATES = ['enabled', 'draining', 'disabled'] as const
/** `routes/nodes/reads.py::_ROSTER_NODEVAULT_STATES`: holds NodeVault keys now, or not (never reported included). */
export const NODE_NODEVAULT_STATES = ['capable', 'not_capable'] as const

export type NodeFilters = {
  /** Name, description or tag substring, or an id / identity-fingerprint prefix, as the server matches it. '' = none. */
  q: string
  /** One of `NODE_STATUSES`, or ''. */
  status: string
  /** One of `NODE_ENABLED_STATES`, or ''. */
  enabled: string
  /** One of `NODE_NODEVAULT_STATES`, or ''. */
  nodevault: string
  /** A node group's uuid (the console's word is Pool), or ''. */
  pool: string
}

export const EMPTY_NODE_FILTERS: NodeFilters = { q: '', status: '', enabled: '', nodevault: '', pool: '' }

export const hasNodeFilters = (filters: NodeFilters): boolean => Object.values(filters).some(Boolean)

/** The server's sorts (`routes/nodes/reads.py::_ROSTER_SORTS`); without one, its name-then-key order. */
export const NODE_SORTS = ['name', 'status', 'seen', 'version'] as const

export const NODE_PAGE_KEY = ['nodes-page'] as const

/** The page's path: only set filters travel (an empty `?status=` is a 422); page/per/sort/dir always ride
 *  along (`hooks/useListPage.ts::setPageParams`). */
export function nodePagePath(filters: NodeFilters, page: ListPageState): string {
  const params = new URLSearchParams()
  if (filters.q.trim()) params.set('q', filters.q.trim())
  if (filters.status) params.set('status', filters.status)
  if (filters.enabled) params.set('enabled', filters.enabled)
  if (filters.nodevault) params.set('nodevault', filters.nodevault)
  if (filters.pool) params.set('group', filters.pool)
  return `/api/nodes/page?${setPageParams(params, page).toString()}`
}
