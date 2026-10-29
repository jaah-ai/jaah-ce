import { useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get } from '../api'
import { useAuth } from '../auth'
import { applyMutes, readMuted } from '../lib/taskMute'
import type { TaskTree } from '../types'
import { useDebouncedCallback } from './useDebouncedCallback'
import { useEventStream } from './useEventStream'
import { useEventsSinceSeen } from './useLiveRefetch'
import { isPageHidden } from './useHiddenDeferral'

/** The one cache key for `GET /api/tasks/tree`; a write that moves a count or a node invalidates it. */
export const TASK_TREE_QUERY_KEY = ['tasks', 'tree'] as const

/* The entities whose console events move the tree: a task's state or filing moves counts; a feature or
   project write adds, renames or ships a node. */
const TREE_ENTITIES = new Set(['task', 'feature', 'project'])

/** The task rail's tree (`apps/api/app/task_tree.py`), with the user's mutes applied (`lib/taskMute.ts`): every
 *  reader sees muted nodes marked and left out of their parents' counts. Polled every 30s and refreshed on the console event
 *  stream (debounced, so a burst of transitions costs one read). `tree` is `undefined` while disabled, loading
 *  or failed; `failed` says which, so a caller waiting on it can stop waiting. */
export function useTaskTree(enabled: boolean): { tree: TaskTree | undefined; failed: boolean } {
  const client = useQueryClient()
  const query = useQuery({
    queryKey: TASK_TREE_QUERY_KEY,
    queryFn: () => get<TaskTree>('/api/tasks/tree'),
    enabled,
    staleTime: 15_000,
    refetchInterval: 30_000,
  })
  const stream = useEventStream()
  /* Every mounted reader (the sidebar AND the page) hears the same event: `cancelRefetch: false` lets the second
     invalidation join the first one's read instead of cancelling it for a fresh one. A hidden tab only marks the
     tree stale; React Query's focus refetch reads it on return (`hooks/useHiddenDeferral.ts`). */
  const { trigger } = useDebouncedCallback(
    () =>
      void client.invalidateQueries(
        { queryKey: TASK_TREE_QUERY_KEY, refetchType: isPageHidden() ? 'none' : 'active' },
        { cancelRefetch: false },
      ),
    1_000,
    5_000,
  )
  useEventsSinceSeen(stream, (events, overrun) => {
    if (enabled && (overrun || events.some(event => TREE_ENTITIES.has(event.entity)))) trigger()
  })
  const raw = enabled && query.data && Array.isArray(query.data.projects) ? query.data : undefined
  const { admin } = useAuth()
  const mutedKey = readMuted(admin?.preferences).join(' ')
  const tree = useMemo(() => (raw && mutedKey !== '' ? applyMutes(raw, mutedKey.split(' ')) : raw), [raw, mutedKey])
  return { tree, failed: enabled && raw === undefined && query.isError }
}
