import { useQuery, type QueryClient } from '@tanstack/react-query'
import { get } from '../../api'
import { TASK_TREE_QUERY_KEY } from '../../hooks/useTaskTree'
import type { FeatureSummary, TaskTree } from '../../types'

/* WHERE A TASK IS FILED — the reads and words every filing surface shares: the row menu's and task page's
 * "Move to feature…" (`MoveToFeatureDialog`), the editor breadcrumb's staged feature picker
 * (`TaskBreadcrumb`), and a duplicate's carried filing (`FilingTarget`). The writes stay on
 * the API: `PUT /api/tasks/{id}` `{feature_id}` files a task and its subtree into a feature of its own
 * project; `POST /api/tasks` takes `feature_id`. */

/** Where a create files: a project and, optionally, one of its features. */
export type FilingTarget = { projectId: number; featureId: string | null }

/** The cache key of `useFilingFeatures`; a filing or a feature write invalidates it. */
export const FILING_FEATURES_QUERY_KEY = ['features', 'filing'] as const

/** The features a task of `projectId` may be filed into: that project's ACTIVE features, where the project
 *  grants the caller `tasks:edit` (`GET /api/features?…&editable=true`). A shipped feature and another
 *  project's are never offered (AC-7.2, AC-1.3). `undefined` projectId (or `enabled` false) fetches nothing. */
export function useFilingFeatures(
  projectId: number | undefined,
  enabled = true,
): { features: FeatureSummary[] | undefined; failed: boolean } {
  const on = enabled && projectId !== undefined
  const query = useQuery({
    queryKey: [...FILING_FEATURES_QUERY_KEY, projectId],
    queryFn: () =>
      get<FeatureSummary[]>(`/api/features?project_id=${projectId}&status=active&editable=true`),
    enabled: on,
    staleTime: 15_000,
  })
  const features = on && Array.isArray(query.data) ? query.data : undefined
  return { features, failed: on && features === undefined && query.isError }
}

/** After a filing lands: the rail's tree (its counts) and every filing picker re-read. The board's own list
 *  and section totals are the host's `fetchTasks`, which it calls beside this. */
export function invalidateFilingQueries(client: QueryClient): void {
  void client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
  void client.invalidateQueries({ queryKey: FILING_FEATURES_QUERY_KEY })
}

/** A feature's name, wherever the caller can see it: the tree (active and shipped) first, then `features` (any
 *  `{id, name}` the caller already holds — a picker's list, a scope's feature). */
export function featureName(
  featureId: string | null | undefined,
  tree: TaskTree | undefined,
  features: ReadonlyArray<{ id: string; name: string }> = [],
): string | undefined {
  if (!featureId) return undefined
  for (const project of tree?.projects ?? []) {
    const hit = project.features.find(f => f.id === featureId)
    if (hit) return hit.name
  }
  return features.find(f => f.id === featureId)?.name
}
