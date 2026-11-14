import { useQuery } from '@tanstack/react-query'
import { get } from '../api'
import { useAuth } from '../auth'
import { ROSTER_STALE_MS } from '../lib/cachedRead'
import { oneOrgRead } from '../lib/orgScope'
import type { TagGroup } from '../types'

/* THE TAG-GROUP CATALOG (`GET /api/tags/groups`, ACTIVE groups in `position` order), one read for the board's
 * Group by (`pages/Tasks.tsx`) and the task's tag editor (`components/tasks/TaskTags.tsx`). KEYED ON THE ADMIN:
 * a tag group is a reference a principal may not be able to read, so an in-place admin switch never keeps the
 * previous vocabulary or reconciles against it — `isSuccess` is a catalog THIS admin was answered. A failed read
 * is not an answer (`isSuccess` stays false, the held rows stay): "no active groups" and "unreachable" look
 * alike, and one transient 500 must not delete a chosen grouping. Within `ROSTER_STALE_MS` a mount answers from
 * cache (Settings › Tags edits it without invalidating this key, so a minute, not five; the board passes
 * `refetchOnMount: 'always'` and so re-reads per visit); never persisted, so no reload reconciles against
 * yesterday's catalog. Only the top level is shape-checked: callers own `tags ?? []`.
 *
 * `orgId` READS ONE ORG'S CATALOGUE: the task editor passes its task's org, so an operator under "All orgs" (or a
 * filter naming other orgs) is offered only the groups that task's tag write can resolve — the PUT validates
 * against the task's org, and another org's tag id answers 422 "tag … not found". It rides `lib/orgScope.ts::oneOrgRead`. Keyed on it, so two tasks of different orgs never share an answer. */
export function useTagGroups({ enabled = true, refetchOnMount, orgId }: { enabled?: boolean; refetchOnMount?: 'always'; orgId?: string | null } = {}) {
  const { admin } = useAuth()
  return useQuery({
    queryKey: ['task-tag-groups', admin?.id ?? null, orgId ?? null],
    queryFn: async () => {
      const rows = orgId
        ? await get<TagGroup[]>('/api/tags/groups', oneOrgRead(orgId))
        : await get<TagGroup[]>('/api/tags/groups')
      return Array.isArray(rows) ? rows : []
    },
    enabled,
    refetchOnMount,
    staleTime: ROSTER_STALE_MS,
    meta: { persist: false },
  })
}
