import { useQuery } from '@tanstack/react-query'
import { get } from '../api'
import type { NodeRecord } from '../types'
import { useEventStream } from './useEventStream'
import { useStreamedQuery } from './useStreamedQuery'

/* The roster (`GET /api/nodes`, a `nodes:view` read — `apps/api/app/routes/nodes/reads.py::list_nodes`) under ONE
   key, so every surface naming a node serves it from the same cache: a slot's label, the audit filter, a
   host's rows, a NodeLink's attach dialog, a project's placement. NOT the Nodes tab's rows: those are one
   searched, filtered PAGE at a time (`pages/nodes/roster/api.ts`), so a fleet of thousands never
   lands whole in the browser for a list. Live on `node` events, a 30s poll while the stream is down.
   `enabled: false` is a DEGRADATION, never an error: a denied account (or a surface not yet needing names)
   gets a terminal empty roster rather than a spinner or a 403 per event and poll. */
export const NODES_KEY = ['nodes-list']
export const listNodes = () => get<NodeRecord[]>('/api/nodes')
const POLL_MS = 30_000

export function useNodesRoster(enabled = true) {
  const stream = useEventStream()
  return useStreamedQuery(stream, { pollMs: POLL_MS, entity: 'node' }, { queryKey: NODES_KEY, queryFn: listNodes, enabled })
}

/* The nodes a picker may offer to take work: every one but a Multi-Node Host, which runs none
   (the API refuses work on a host) and is offered only where its nodes are made (Nodes > Hosts). */
export const workNodes = (nodes: readonly NodeRecord[]): NodeRecord[] =>
  nodes.filter(n => n.hosting !== 'multi_node_host')

/* The same roster, busiest first (`?order=activity`, `apps/api/app/activity_rank.py`): what a create wizard
   pre-picks from (`lib/topPicks.ts`). Under `NODES_KEY`, so a roster invalidation refetches it too; not live. */
export function useNodesByActivity(enabled = true) {
  return useQuery({
    queryKey: [...NODES_KEY, 'activity'],
    queryFn: () => get<NodeRecord[]>('/api/nodes?order=activity'),
    enabled,
  })
}
