/* The board's react-query keys — one home, so a write that moves a row, a count or a feature invalidates
 * exactly the reads that show it. The tree's own key is `hooks/useTaskTree.ts`'s `TASK_TREE_QUERY_KEY`. */
import type { QueryClient } from '@tanstack/react-query'
import { TASK_TREE_QUERY_KEY } from '../../../hooks/useTaskTree'
import { isEnabled } from '../../../lib/editionFeatures'

/** The board's State counts (`pages/Tasks.tsx`'s `feedStateCounts`) sit under this prefix. */
export const PANE_QUERY_KEY = ['tasks', 'pane'] as const

/** A task's Live Cast state (`lib/liveCast.ts::useTaskLiveCast`): here, so a deleted task's is dropped in an edition
 *  without Live Cast's modules too. */
export const taskLiveCastKey = (taskId: number) => ['task-live-cast', taskId] as const

/** `GET /api/features/{id}`. */
export function featureQueryKey(featureId: string) {
  return ['features', featureId] as const
}

/** A DELETED task leaves every cache: its own keyed reads are REMOVED, never refetched (each would
 *  answer 404), and the lists and counts that drew it are invalidated once for the whole batch. */
export function forgetDeletedTasks(client: QueryClient, ids: readonly number[]): void {
  if (isEnabled('live_cast')) for (const id of ids) client.removeQueries({ queryKey: taskLiveCastKey(id) })
  void client.invalidateQueries({ queryKey: PANE_QUERY_KEY })
  void client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
}
