import { useCallback, useEffect, useState } from 'react'
import { apiDetail, get } from '../api'

// GET /api/console/nodes/{node}/windows — the fleet tmux session's windows, input to the layout designer.
// Plumbing windows are filtered node-side; do not re-filter here. Rows arrive in window-index order.
// `id` (tmux `@N`, as in `worktrees.tmux_window_id`) is the identity a saved layout keys on. `index` is
// display-only: `renumber-windows` is on, so indexes shift and get reused. Never persist it.
export type FleetWindow = {
  id: string
  index: number
  name: string
}

type FleetWindowsResponse = { windows: FleetWindow[] }

export type FleetWindowsState = {
  windows: FleetWindow[]
  loading: boolean
  // Set when the CP could not answer. A 404 means unknown node OR no node grant: the route answers the
  // same message for both so it is not an existence oracle. Do not make the server name the grant.
  error: string | null
  // True once the state answers the node currently asked for. `loading` is false before dispatch too,
  // and a latched boolean would read settled on the first render asking about a real node after the
  // no-node case — so it compares against the node the state was produced for.
  settled: boolean
  refresh: () => Promise<void>
}

export function useFleetWindows(nodeId: string | null | undefined): FleetWindowsState {
  const [windows, setWindows] = useState<FleetWindow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // `undefined` = no answer yet; distinct from the `null` a no-node console settles on.
  const [settledFor, setSettledFor] = useState<string | null | undefined>(undefined)

  const refresh = useCallback(async () => {
    if (!nodeId) {
      // No node yet: stay empty rather than request `/api/console/nodes/undefined/windows`.
      setWindows([])
      setError(null)
      setSettledFor(null)
      return
    }
    setLoading(true)
    try {
      const r = await get<FleetWindowsResponse>(
        `/api/console/nodes/${encodeURIComponent(nodeId)}/windows`,
      )
      setWindows(r.windows ?? [])
      setError(null)
    } catch (err) {
      setWindows([])
      setError(apiDetail(err, 'could not list this node’s windows'))
    } finally {
      setLoading(false)
      setSettledFor(nodeId)
    }
  }, [nodeId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { windows, loading, error, settled: settledFor === (nodeId ?? null), refresh }
}
