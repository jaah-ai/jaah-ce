import { useQuery } from '@tanstack/react-query'
import { get } from '../api'
import { useCan } from '../auth'
import { useEventStream } from './useEventStream'

// Composite health for the "Live" badge: api (GET /api/health/components), sse (this browser's stream
// from useEventStream(), not the endpoint's `sse`), jaahd (node presence; "unknown" = not asserted).
// useEventStream is shared per path, so this hook's own call opens no extra connection. Refresh is a
// 10s poll while the tab is visible (react-query skips interval ticks in a hidden tab), plus one on return.

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

const POLL_INTERVAL_MS = 10_000

type FetchedHealth = Pick<ComponentHealth, 'api' | 'jaahd'>

// Failed/pending fetch reads as api:false → degraded, never a false "Live".
const INITIAL_FETCHED: FetchedHealth = { api: false, jaahd: 'unknown' }

const UNASSERTED_FETCHED: FetchedHealth = { api: 'unknown', jaahd: 'unknown' }

export function useHealth(): HealthState {
  const { state: streamState } = useEventStream()
  // Without `system:view` the endpoint 403s forever — don't poll, don't claim to know.
  const can = useCan()
  const canReadComponents = can('system:view')
  const { data, isError } = useQuery({
    queryKey: ['health-components'],
    queryFn: () => get<HealthComponentsResponse>('/api/health/components'),
    enabled: canReadComponents,
    refetchInterval: POLL_INTERVAL_MS,
    refetchOnWindowFocus: 'always',
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
