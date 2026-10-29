import { useEventStream } from '../../../hooks/useEventStream'
import { useStreamedQuery } from '../../../hooks/useStreamedQuery'
import { HOSTS_KEY, listAllHosts } from './api'

const POLL_MS = 30_000

/* The roster's hosts (`GET /api/hosts`, `nodes:view` like the roster itself). Live: a `node` event (a machine
   joined, a node was added or removed) and a `node_order` event (an Add node order moved a stage) each
   refetch, so a host row's stage chip follows the order after its dialog is closed (R15c). Polled only while
   the stream is down, as the roster is. */
export function useHosts() {
  const stream = useEventStream('/api/stream', ['node', 'node_order'])
  return useStreamedQuery(stream, { pollMs: POLL_MS }, {
    queryKey: HOSTS_KEY,
    queryFn: listAllHosts,
    // A failed read falls back to the flat roster at once; retrying would hold the page on a skeleton.
    retry: false,
  })
}
