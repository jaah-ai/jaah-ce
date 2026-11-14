import { isEnabled } from '../../lib/editionFeatures'
import { planSurfaceShown } from '../../lib/planSwitches'
import type { Admin } from '../../auth'
import type { PermissionKey } from '../../types'

/* Audit's two project tabs, Merge lanes and Memory: who is offered each. ONE rule read by the shell's strip
 * (`AuditPage.tsx`) and the rail's Audit row (`components/Sidebar.tsx`), so a reader the strip offers a tab to always
 * has the row that reaches it. Both read `projects:view` (`routes/merge_lane_rows.py`, `routes/project_memory.py`). */

type Can = (key: PermissionKey) => boolean

/** Merge lanes: with the lane in the edition (`merge_lane`). */
export function mergeLanesOffered(can: Can): boolean {
  return can('projects:view') && isEnabled('merge_lane')
}

/** Memory: with project memory in the edition, on the plan — or, off it, for an editor, whose Purge by `mem-N` stays
 *  open (erasure is never plan-gated). */
export function memoryOffered(admin: Admin | null | undefined, can: Can): boolean {
  return can('projects:view') && isEnabled('project_memory')
    && (planSurfaceShown(admin, 'project_memory_enabled') || can('projects:edit'))
}
