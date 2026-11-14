import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get } from '../api'
import { useCan } from '../auth'
import { useDebouncedCallback } from './useDebouncedCallback'
import { useEventStream } from './useEventStream'
import { isPollingPaused } from '../lib/pollGate'
import { STREAM_DOWN_POLL_MS, STREAM_SAFETY_POLL_MS, useEventsSinceSeen } from './useLiveRefetch'

// Composite health for the "Live" badge: api (GET /api/health/components), sse (this browser's stream
// from useEventStream(), not the endpoint's `sse`), jaahd (node presence; "unknown" = not asserted).
// useEventStream is shared per path, so this hook's own call opens no extra connection. While the stream
// is connected the API is evidently reachable, so the endpoint is re-read only on a `node` event, an
// overrun or a resume (debounced), and every 5 min (`STREAM_SAFETY_POLL_MS`: a node reads offline only
// once its reconnect grace ends, which sends no event); otherwise it polls every 30s (`useLiveRefetch.ts::STREAM_DOWN_POLL_MS`),
// skipping ticks in a hidden tab, plus the default refetch on return once stale.

export type ComponentHealth = {
  // `'unknown'` = this account lacks `system:view` (routes/health.py 403s), so nothing is known. NOT
  // `false`, which means "down" and would paint every standard user's console amber.
  api: boolean | 'unknown'
  sse: boolean
  jaahd: 'online' | 'offline' | 'unknown'
}

export type HealthState = {
  overall: 'connected' | 'degraded' | 'reconnecting' | 'down'
  components: ComponentHealth
}

type HealthComponentsResponse = {
  api: boolean
  sse: boolean
  jaahd: { status: 'online' | 'offline' | 'unknown'; last_seen: string | null }
}

const HEALTH_QUERY_KEY = ['health-components'] as const

type FetchedHealth = Pick<ComponentHealth, 'api' | 'jaahd'>

// Failed/pending fetch reads as api:false → degraded, never a false "Live".
const INITIAL_FETCHED: FetchedHealth = { api: false, jaahd: 'unknown' }

const UNASSERTED_FETCHED: FetchedHealth = { api: 'unknown', jaahd: 'unknown' }

export function useHealth(): HealthState {
  const stream = useEventStream()
  const streamState = stream.state
  // Without `system:view` the endpoint 403s forever — don't poll, don't claim to know.
  const can = useCan()
  const canReadComponents = can('system:view')
  const client = useQueryClient()
  const { data, isError } = useQuery({
    queryKey: HEALTH_QUERY_KEY,
    queryFn: () => get<HealthComponentsResponse>('/api/health/components'),
    enabled: canReadComponents,
    refetchInterval: streamState === 'connected' ? STREAM_SAFETY_POLL_MS : STREAM_DOWN_POLL_MS,
  })
  // A hidden tab only marks it stale; the refetch on return reads it.
  const { trigger } = useDebouncedCallback(
    () =>
      void client.invalidateQueries(
        { queryKey: HEALTH_QUERY_KEY, refetchType: isPollingPaused() ? 'none' : 'active' },
        { cancelRefetch: false },
      ),
    1_000,
    5_000,
  )
  useEventsSinceSeen(stream, (events, overrun) => {
    if (canReadComponents && (overrun || events.some(event => event.entity === 'node'))) trigger()
  })
  // Unreachable → api down, but keep last-known jaahd across one transient error.
  const fetched: FetchedHealth = data ? { api: !isError && data.api, jaahd: data.jaahd.status } : INITIAL_FETCHED

  const sse = streamState === 'connected'
  const known = canReadComponents ? fetched : UNASSERTED_FETCHED
  const components: ComponentHealth = { ...known, sse }

  let overall: HealthState['overall']
  if (streamState === 'down') {
    overall = 'down'
  } else if (streamState === 'reconnecting') {
    overall = 'reconnecting'
  } else {
    // An 'unknown' component does NOT degrade — it is simply not asserted.
    overall = known.api !== false && known.jaahd !== 'offline' ? 'connected' : 'degraded'
  }

  return { overall, components }
}
