// Who may do what on the Task page: the row's edit/run/delete answers, the board's flat keys, another org's lock.
import { useCan } from '../../auth'
import {
  useCanCreateTasks,
  useCanDeleteTaskRow,
  useCanEditTaskRow,
  useCanEditTasks,
  useCanRunTaskRow,
} from '../../hooks/useTaskAccess'
import { useActAsRows } from '../../lib/actAs'
import { useAwaitingApprovalReason } from '../../lib/orgLifecycle'
import type { PipelineTask } from '../../types'
import { useOtherOrg } from '../../ui/OrgIndicator'

export type TaskDetailAccess = ReturnType<typeof useTaskDetailAccess>

export function useTaskDetailAccess(task: PipelineTask | null) {
  /* ── ANOTHER ORG'S ROW ─────────────────────────────────────────────────────────────────
     An operator edits another org's task on that org's behalf: every write below the row — each panel's, the ⋯'s,
     the answer, comments, attachments — goes as its org for the one request (`lib/actAs.ts::useActAsRows`), and the
     page's `ActingForNotice` says so. A new feature filed from Move… is that org's too (`MoveToFeatureDialog`). A picker
     offers only rows a write may target: the operator's own org's projects, or on another org's row that org's. */
  const otherOrg = useOtherOrg(task?.org)
  useActAsRows(task === null ? [] : [{ path: `/api/tasks/${task.id}`, org: otherOrg }])
  const can = useCan()
  /* ── `tasks:view` NO LONGER IMPLIES `tasks:edit` ────────────────────────────────────────────────
     This page can be open on a task nobody here may write. One rule, panel by panel: the content stays,
     the controls go — each mixed panel takes `readOnly`; the upload dropzone (pure control) is not
     mounted. The strip keeps its tabs either way. Key-per-route: `hooks/useTaskAccess.ts`; presentation
     not gate: `components/RequireFeature.tsx`. */
  /* ── AND THE SAME QUESTION ASKED OF **THIS ROW**, because the server now answers it per row ────────
     A `tasks:create` holder may edit and delete their own `authored_draft` row, so content and run
     rights split, verb by verb, by which route each control posts to:
       · `canEditThisTask` — form fields and Save, breadcrumb edits, tags, dependency edges, attachment
         upload; falls back to `tasks:create` on an authored draft.
       · `canRunThisTask` — every RUN verb (gate answer/ignore, **Go**, Re-run), never widened; the ROW
         answer, not the flat key. */
  const canEditThisTask = useCanEditTaskRow()(task)
  const canRunThisTask = useCanRunTaskRow()(task)
  /* ── …AND THE THIRD KEY, WHICH IS NEITHER OF THOSE TWO: may they DELETE this row? ─────────────────
     `DELETE /api/tasks/{id}` declares `tasks:delete`. The ROW predicate, not flat `useCanDeleteTasks`,
     so a `tasks:create` author can delete their own draft here (`hooks/useTaskAccess.ts`). */
  const canDeleteThisTask = useCanDeleteTaskRow()(task)
  /* The ⋯'s flat keys, as the board hands its row verbs out (`pages/Tasks.tsx` `rowActions`): lifecycle on
     `tasks:edit` (`canRunThisTask` narrows to this row), Duplicate on `tasks:create`. */
  const canEditTasks = useCanEditTasks()
  const canCreateTasks = useCanCreateTasks()
  // A pending org starts nothing: Start renders disabled with this reason.
  const awaitingApproval = useAwaitingApprovalReason()
  return {
    otherOrg, can, canEditThisTask, canRunThisTask, canDeleteThisTask, canEditTasks, canCreateTasks, awaitingApproval,
  }
}
