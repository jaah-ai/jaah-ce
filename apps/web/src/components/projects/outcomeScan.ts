import { useQuery } from '@tanstack/react-query'
import { get, post } from '../../api'
import { useAuth } from '../../auth'
import { planSurfaceShown } from '../../lib/planSwitches'

/* Project › Governance's outcome scan: the wire shapes of `GET`/`POST /api/projects/{id}/outcome-scan` (the
 * revert-and-fix scan the outcome ledger reads, `apps/api/app/routes/projects.py`). Owner/Admin only; the API is
 * the boundary. The interval itself is a project column (`outcome_scan_interval_minutes`), saved with the form. */

export type OutcomeScanStatus = {
  last_filed_at: string | null
  cursor_sha: string | null
  cursor_at: string | null
  /** The interval in force, and which tier set it. */
  interval_minutes: number
  interval_source: 'project' | 'org' | 'global'
  /** A git person is configured: without one no scan can run. */
  git_person: boolean
  /** The scan task in flight, if any. */
  open_task_id: number | null
  stale: boolean
}

export type OutcomeScanFiled = {
  filed: boolean
  task_id: number | null
  /** Why nothing was filed ("A scan is already running"); null when filed. */
  reason: string | null
  status: OutcomeScanStatus
}

/** The scan is the outcome ledger's: a tenant sees it only with agent performance on its plan, an operator always. */
export const useOutcomeScanShown = (): boolean => planSurfaceShown(useAuth().admin, 'agent_performance')

export const outcomeScanKey = (projectId: number) => ['project-outcome-scan', projectId] as const

export const getOutcomeScan = (projectId: number) => get<OutcomeScanStatus>(`/api/projects/${projectId}/outcome-scan`)

export const fileOutcomeScan = (projectId: number) => post<OutcomeScanFiled>(`/api/projects/${projectId}/outcome-scan`)

/** The status, read only when `enabled` (edit mode, Owner/Admin, a repository to scan) and the plan has agent
 *  performance (`useOutcomeScanShown`; a tenant without it is answered 403). */
export function useOutcomeScan(projectId: number | null, enabled: boolean) {
  const shown = useOutcomeScanShown()
  return useQuery({
    queryKey: outcomeScanKey(projectId ?? 0),
    queryFn: () => getOutcomeScan(projectId as number),
    enabled: enabled && shown && projectId !== null,
  })
}

/** The interval field's placeholder: what a blank inherits. Unknown while the project pins its own (or unread). */
export function inheritedInterval(status: OutcomeScanStatus | undefined): string {
  return status && status.interval_source !== 'project' ? String(status.interval_minutes) : 'Default'
}
