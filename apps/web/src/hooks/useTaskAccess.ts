import { useCallback } from 'react'

import { useCan, useCanOnAnyProject, useCanOnProject } from '../auth'
import type { PipelineTask } from '../types'

/* ── The task WRITE keys, as the SERVER declares them ─────────────────────────────────────────────
 * `tasks:view` shows every task in a project the user holds a grant on; every MUTATION declares its own
 * key, so a read-only reader is a real principal. A task is NOT a grantable object: reaching it is the
 * grant on its PROJECT plus the `tasks:*` keys held there, for every task in it.
 * Keys (each asked PER ROW on the row's project: `tasks:edit` on B authorizes nothing on A):
 *   · `tasks:edit` — every mutation route but `DELETE`/`POST /api/tasks`: `PUT`/`PATCH /api/tasks/{id}`,
 *     `/start`, `/force-start`, `/answer`, `/answer-question`, `/ignore-question`, `/revise-question`,
 *     `/reset`, `/stop`, `/complete`, `/dependencies`, and the three attachment writes, all
 *     `@feature("tasks", "edit", object_type="task", object_params=("task_id",))`.
 *   · `tasks:delete` — `DELETE /api/tasks/{id}` alone.
 *   · `tasks:create` — `POST /api/tasks`, plus the widening below.
 * THE WIDENING: `tasks:create` also admits edit/delete of the caller's OWN task while still `draft`, per
 * row via `PipelineTask.authored_draft` (the server's own condition, serialised): `PUT`/`PATCH`, tags,
 * dependencies, attachment upload, `DELETE`. It NEVER unlocks a run verb — those need `tasks:edit` on the
 * row (`useCanRunTaskRow`); authoring a draft is not dispatching it.
 * Hooks: the three flat ones are the ROUTE-LEVEL FLOOR (does a surface offer the verb at all), never a
 * row answer. `useCanRunTaskRow(task)` = run verbs per row; `useCanRunTasksOnProject(id)` = the same
 * for a task with no row yet; `useCanEditTaskRow`/`useCanDeleteTaskRow` = their key per row OR
 * (`tasks:create` AND own draft). Pick by the route the control calls. A null task answers false.
 * No FLAT predicate implies any other, and none implies `tasks:view`; the per-row pair is the one place a
 * key is deliberately composed. One predicate per fact so ~10 call sites cannot drift. Superadmin is allow-all (`useCan`).
 * THE SERVER'S OWN PER-ROW VERDICT: `PipelineTask.may_edit` serialises `authz.editable_task_clause` and
 * STAYS THE AUTHORITY. A control is offered only where the door would admit: `may_edit === false` vetoes
 * the key-based arm of all three per-row predicates; `true` adds nothing; `null` (not computed, e.g. an
 * epic's nested children) and `undefined` keep the key answer; the `authored_draft` arm is never vetoed.
 * `mayEditVetoed` is the one reader.
 * ANOTHER ORG'S ROW: an operator edits it on that org's behalf, so no predicate here vetoes it. Every
 * write to such a row goes as its org for the one request (`lib/actAs.ts::useActAsRows`, held by the task page and the
 * board). The board's row ⋯ and the task page's ⋯ read the same answers.
 * Presentation only, never the security boundary — see `components/RequireFeature.tsx`. */

/** May this principal WRITE a task AT ALL — the flat `tasks:edit` union? The route-level floor
 * (`authz_guard`'s first check); for a row use `useCanRunTaskRow`.
 */
export function useCanEditTasks(): boolean {
  return useCan()('tasks:edit')
}

/** May this principal DELETE a task — `tasks:delete` only (`tasks:edit` does NOT pass). */
export function useCanDeleteTasks(): boolean {
  return useCan()('tasks:delete')
}

/** May this principal CREATE a task — `tasks:create` only? Gates "＋ New Task" and the inline new-task row
 * (`/tasks/new` is itself gated by `RequireFeature` in `App.tsx`).
 * This key alone although the inline row follows the create with `/start` (`tasks:edit`): a failed start
 * is reported honestly ("created but did not start … press Start").
 */
export function useCanCreateTasks(): boolean {
  return useCan()('tasks:create')
}

/** May this principal create a CHAT — `tasks:create` AND `worktrees:edit` on at least one project?
 * Composed because the product is: a chat is a live terminal filed as a task, and every reply goes
 * through the console relay (`routes/console.py`), which drops browser→node frames without
 * `worktrees:edit`. A `tasks:create`-only principal would get a runner parked at a prompt forever, holding
 * a lease — worse than a 403. PER-PROJECT, unlike the console's own flat `useCanDriveWorktreeSession`:
 * `create_task` (`routes/tasks/create.py`) demands `worktrees:edit` ON the chat's project. "At least one
 * project": whether the affordance may appear anywhere; the row's console item asks per row. The
 * `/tasks/new-chat` route is guarded on `tasks:create`.
 */
export function useCanCreateChats(): boolean {
  // Both hooks on every render: a `&&` short-circuit skipped the second while the identity loaded, and the
  // changed hook order crashed a host mounted before auth resolved.
  const canCreateTasks = useCanCreateTasks()
  const canOnAnyProject = useCanOnAnyProject()
  return canCreateTasks && canOnAnyProject('worktrees:edit')
}

/* The per-ROW row shape: `Pick`ed (`project_id`, `authored_draft`, `may_edit`) so a board row, detail
 * payload or fixture all fit. NULLABLE: hosts render before their task lands. No row: false. */
type AuthoredDraftRow = Pick<PipelineTask, 'authored_draft' | 'project_id' | 'may_edit'>

/* The veto (file header § THE SERVER'S OWN PER-ROW VERDICT). `=== false`: `null` and absent keep the key
 * answer. */
const mayEditVetoed = (task: AuthoredDraftRow | null | undefined): boolean => task?.may_edit === false

/** May this principal exercise a RUN verb on **this row** — Start, Stop, Reset, Complete, Go, Re-run,
 * the gate's answer/ignore?
 * `tasks:edit` on this row's PROJECT. `may_edit === false` vetoes (file header).
 * A null/undefined row answers false.
 */
export function useCanRunTaskRow(): (task: AuthoredDraftRow | null | undefined) => boolean {
  const canRunOnProject = useCanRunTasksOnProject()
  return useCallback(
    (task: AuthoredDraftRow | null | undefined) => !mayEditVetoed(task) && canRunOnProject(task?.project_id),
    [canRunOnProject],
  )
}

/** The same run answer for a PROJECT, for a task with no row yet: the create form's Go
 * (`components/tasks/TaskEditForm`) is `POST /api/tasks` then `/start`. `useCanRunTaskRow` delegates here.
 * An unresolved scope answers false (fail-closed).
 */
export function useCanRunTasksOnProject(): (projectId: number | null | undefined) => boolean {
  const canOnProject = useCanOnProjectOrNone()
  return useCallback(
    (projectId: number | null | undefined) => canOnProject('tasks:edit', projectId),
    [canOnProject],
  )
}

/** May this principal EDIT **this row** — content, tags, edges, attachments?
 * `useCanRunTaskRow`'s answer (with its veto), OR `tasks:create` on a row marked `authored_draft`. Never
 * re-derive authorship ("created_by is me"): `created_by` is a display name, and the flag is the server's
 * word. `=== true`, so an absent field refuses (fail-closed). Not for run verbs.
 * A callback because the answer varies per row and hooks cannot sit in `.map()`; `useCallback` keeps it
 * stable for memoised lists.
 */
export function useCanEditTaskRow(): (task: AuthoredDraftRow | null | undefined) => boolean {
  const canRunRow = useCanRunTaskRow()
  const canCreateTasks = useCanCreateTasks()
  return useCallback(
    (task: AuthoredDraftRow | null | undefined) => canRunRow(task) || (canCreateTasks && task?.authored_draft === true),
    [canRunRow, canCreateTasks],
  )
}

/** May this principal DELETE **this row**?
 * `tasks:delete` (with the `may_edit` veto — `DELETE` probes the same write clause), OR `tasks:create` on
 * an `authored_draft` row (`=== true`). `tasks:edit` still does NOT pass.
 */
export function useCanDeleteTaskRow(): (task: AuthoredDraftRow | null | undefined) => boolean {
  const canOnProject = useCanOnProjectOrNone()
  const canCreateTasks = useCanCreateTasks()
  return useCallback(
    (task: AuthoredDraftRow | null | undefined) =>
      (!mayEditVetoed(task) && canOnProject('tasks:delete', task?.project_id))
      || (canCreateTasks && task?.authored_draft === true),
    [canOnProject, canCreateTasks],
  )
}

/* `useCanOnProject` for a project that may not be known yet: an absent id answers false (an unloaded
 * scope is not a proven grant). */
function useCanOnProjectOrNone(): (key: 'tasks:edit' | 'tasks:delete', projectId: number | null | undefined) => boolean {
  const canOnProject = useCanOnProject()
  return useCallback(
    (key: 'tasks:edit' | 'tasks:delete', projectId: number | null | undefined) =>
      projectId !== null && projectId !== undefined && canOnProject(key, projectId),
    [canOnProject],
  )
}
