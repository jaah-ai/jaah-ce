import { useCallback, useMemo } from 'react'
import { useUrlFilters } from '../../../hooks/useUrlFilters'
import { HOST_STATES, type HostFilters } from '../hosts/api'
import { EMPTY_NODE_FILTERS, NODE_ENABLED_STATES, NODE_NODEVAULT_STATES, NODE_STATUSES, type NodeFilters } from './api'

/* The two rosters' filters live in the URL (`hooks/useUrlFilters.ts`): a pasteable "the offline nodes" link
 * (written with `replace`, so a filter click never buries Back), and one owner per value beside the `?tab=` key (`hooks/useUrlTab.ts`), which
 * `setFilters` leaves alone. A value outside its vocabulary reads as none: a stale bookmark must not send a
 * 422 the strip cannot show. Keys are the console's words — `pool`, `status`, `enabled`, `nodevault`, `q` — and the Hosts
 * tab's are prefixed (`hq`, `hstate`) so the two strips on one address never share a key. */

const oneOf = (vocabulary: readonly string[], raw: string | null): string =>
  raw !== null && vocabulary.includes(raw) ? raw : ''

export function readNodeFilters(params: URLSearchParams): NodeFilters {
  return {
    // Capped at the server's `max_length`: a bookmark past it would earn a 422 the strip cannot show.
    q: (params.get('q') ?? '').slice(0, 200),
    status: oneOf(NODE_STATUSES, params.get('status')),
    enabled: oneOf(NODE_ENABLED_STATES, params.get('enabled')),
    nodevault: oneOf(NODE_NODEVAULT_STATES, params.get('nodevault')),
    pool: params.get('pool') ?? '',
  }
}

export function readHostFilters(params: URLSearchParams): HostFilters {
  return { q: (params.get('hq') ?? '').slice(0, 200), state: oneOf(HOST_STATES, params.get('hstate')) }
}

/** The Nodes tab's filters: read through from the URL, patched one field at a time, cleared as one. */
export function useNodeFilters(): [NodeFilters, (patch: Partial<NodeFilters>) => void, () => void] {
  const [params, setFilters] = useUrlFilters()
  const filters = useMemo(() => readNodeFilters(params), [params])
  const patch = useCallback((next: Partial<NodeFilters>) => setFilters(next), [setFilters])
  const clear = useCallback(() => setFilters(EMPTY_NODE_FILTERS), [setFilters])
  return [filters, patch, clear]
}

/** The Hosts tab's, likewise. */
export function useHostFilters(): [HostFilters, (patch: Partial<HostFilters>) => void, () => void] {
  const [params, setFilters] = useUrlFilters()
  const filters = useMemo(() => readHostFilters(params), [params])
  const patch = useCallback(
    (next: Partial<HostFilters>) => setFilters({
      ...(next.q !== undefined ? { hq: next.q } : {}),
      ...(next.state !== undefined ? { hstate: next.state } : {}),
    }),
    [setFilters],
  )
  const clear = useCallback(() => setFilters({ hq: '', hstate: '' }), [setFilters])
  return [filters, patch, clear]
}
