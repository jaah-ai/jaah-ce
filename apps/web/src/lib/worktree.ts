// Worktree display/liveness helpers and the Task→slot JOIN, shared by every surface that renders or
// addresses a slot. Here rather than on a page so no page imports another's route chunk.
import type { Worktree } from '../types'

// The Title, SHOWN AS IS: an in-flight operator edit wins, else `wt.title` VERBATIM. The CP resolves an
// absent Title (`apps/api/app/slot_title.py::resolved_title`), so `wt.title` is never null or `""`.
// NEVER read the node-sensed tmux name (`types.ts` keeps it for older-node compat) and never compose a
// default: no node-sensed value becomes a Title, and the CP must be the only opinion.
export function resolveTitle(wt: Worktree, pending?: string): string {
  return pending ?? wt.title
}

// A folder slot: a project with no repository, so branch/ahead/behind/dirty arrive null and NO git cell,
// row, filter or action applies (`routes/portfolio.py::WorktreeOut.has_repo`). Absent is a git worktree:
// a cached roster can predate the field.
export function isFolderSlot(wt: Worktree): boolean {
  return wt.has_repo === false
}

// "A live session was observed in this slot" — tested as PRESENCE, never by enumerating status values.
// `session_status` is the verbatim `claude agents --json` status, so its value set is the CLI's; NULL/""
// is the one "no session" value (apps/api/app/account_runners.py's module docstring is the single home).
// An {idle, busy} allowlist once dropped `waiting` slots from the chevrons and quota badge.
export function hasLiveSession(wt: Worktree): boolean {
  return !!wt.session_status
}

/** May a console be OPENED on this slot — NOT `hasLiveSession` alone.
 *
 * `session_status` is empty for a runner wedged during startup (e.g. on a CLI prompt), which is exactly
 * when an operator needs a console. So: something live to ATTACH to, or `mayCreate` (the caller's
 * conjunction of the session key and `wt.may_edit`) — the console resolves-or-creates and takes no hold.
 * A `worktrees:view` principal is offered a console only where one is live. */
export function canOpenConsoleOn(wt: Worktree, { mayCreate }: { mayCreate: boolean }): boolean {
  return hasLiveSession(wt) || mayCreate
}

/** How long a held slot may sit with NOTHING ever sensed before a surface warns. Five minutes: well past
 * the CP's own 90s launch grace (`apps/api/app/runner_liveness.py::LAUNCH_SETTLE_SECONDS`). A WARNING
 * threshold only — nothing here frees, kills or re-dispatches. */
export const UNSIGHTED_RUNNER_WARN_MS = 5 * 60 * 1000

/** How long this slot has been held with NOTHING ever sensed in it, or null.
 *
 * A PAGE AFFORDANCE ("how long silent"), not a liveness verdict — that is the CP's
 * (`apps/api/app/runner_liveness.py`).
 *
 * Measured from `holder_since` (wire name of `worktrees.state_since`; see `types.ts`), NOT
 * `last_seen_live`, which only advances on a sighting and is null forever on the silent slot.
 *
 * NULL IS "NOT THIS FAULT": a sensed session; a prior sighting this occupancy (`last_seen_live` —
 * `app/slot_state.py` blanks it on every re-arm), which is a different fault; a missing or unparseable
 * `holder_since`; or a FUTURE stamp (clock skew), rather than a negative age. */
export function unsightedRunnerAgeMs(wt: Worktree, nowMs: number): number | null {
  if (hasLiveSession(wt)) return null
  if (wt.last_seen_live) return null
  if (!wt.holder_since) return null
  const since = Date.parse(wt.holder_since)
  if (Number.isNaN(since)) return null
  const age = nowMs - since
  return age < 0 ? null : age
}

// The task id a slot's BRANCH names, or null — the SUBORDINATE key for the join below.
//
// A runner names its own `{prefix}fleet/task-<id>`, so this reads whatever ref is on disk.
// The retired `-N` attempt suffix stays parseable: old refs still sit on real trees.
// Strict otherwise, because a WRONG id points a console at another run: anchored at the END on a whole
// `/`-delimited segment, digits only. Any prefix is accepted ahead of `fleet/` (the browser does not
// hold `branch_prefix`). The `fleet/` segment is REQUIRED: `wip/task-12` or `task-7` are branches a HUMAN
// can name on a `user`-held slot, and must not resolve as a dispatch claim.
const BRANCH_TASK_ID = /(?:^|\/)fleet\/task-(\d+)(?:-\d+)?$/

export function taskIdFromBranch(branch: string | null | undefined): number | null {
  const match = branch?.match(BRANCH_TASK_ID)
  return match ? Number(match[1]) : null
}

// THE ONE JOIN from a Task to the pool slot a runner is working it in, behind every "this task has a
// runner, here is its console" affordance.
//
// Keyed on `task_id` (the slot's EXACT task reference). NOT on a GitHub issue number (`worktree.number` is
// a pool slot id) and NOT gated on the stack (`stack_state === 'up'`): a console attaches to the slot's
// tmux window (`apps/jaahd/console.py::resolve_or_create_console_target`), never the stack (measured:
// 5 of 5 task-bound slots on the fleet read `stack_state = null`).
//
// `task_id` is AUTHORITATIVE but not always present, hence the SUBORDINATE branch key — OPT-IN
// (`branchFallback`), asked for by the CONSOLE resolver alone. The 🌐 STACK LINK uses this join too but
// admits `ready`/`awaiting_response` roots, and an undispatched task must resolve NO slot; an always-on
// fallback would link a stale branch into another task's stack.
//
// A NULL `task_id` IS NOT "NEVER STAMPED": release nulls it (`app/slot_state.py::release`,
// `jaahd/commands.py`; cleanup in `app/slot_columns.py`) while parking the tree off its branch is
// best-effort. So the fallback needs BOTH guards:
//   · A FREE SLOT'S BRANCH IS NOT A CLAIM (`namesTaskByBranch`). The stamped key is left ungated — a
//     dangling pointer is what `task_active` describes.
//   · A NON-NULL `task_id` WINS OUTRIGHT, across rows too (the two-pass `find`): a re-leased slot carries
//     the next task's id before its branch catches up.
//
// The REACHABILITY test is the caller's and participates IN the `find`: there is no FK behind
// `worktrees.task_id`, so several rows can match a `(project, task_id)` pair and a stale one must not win
// and then fail the test. `projectName` is required: `undefined` (unresolved project) yields no slot.
// `task_active` is NOT consulted — the caller holds the Task row.
export function worktreeForTask(
  taskId: number,
  projectName: string | undefined,
  worktrees: readonly Worktree[],
  isReachable: (wt: Worktree) => boolean = () => true,
  { branchFallback = false }: { branchFallback?: boolean } = {},
): Worktree | undefined {
  if (!projectName) return undefined
  const candidate = (w: Worktree) => w.project === projectName && isReachable(w)
  /* TWO PASSES, NOT ONE `find` OVER A DISJUNCTION: `find` returns the first match in roster order, so a
     branch-only row sorting earlier would beat the stamped row. */
  const stamped = worktrees.find(w => candidate(w) && w.task_id === taskId)
  if (stamped || !branchFallback) return stamped
  return worktrees.find(w => candidate(w) && w.task_id == null && namesTaskByBranch(w, taskId))
}

/* The branch key's gate: only `state === 'task'` speaks for a `fleet/task-N` branch. A `user`-held slot
   is a person's, who may check out any branch, so a stale checkout there is never that task's runner. */
function namesTaskByBranch(w: Worktree, taskId: number): boolean {
  if (w.state !== 'task') return false
  return taskIdFromBranch(w.branch) === taskId
}

// "This task has a runner whose console can be opened" — the one caller that asks for the branch
// fallback (the unstamped-yet Running badge). ONLY A TASK-HELD SLOT: `hasLiveSession` is true for a
// human's own console session too, so `state === 'task'` keeps a `user` slot out.
export function runnerWorktreeFor(
  taskId: number,
  projectName: string | undefined,
  worktrees: readonly Worktree[],
): Worktree | undefined {
  // TASK-HELD, NOT SENSED-LIVE: the CP's answer to "which slot is this task's runner in" is occupancy, so
  // a wedged startup with no sensed session still resolves. `free`/`user` slots are refused.
  return worktreeForTask(
    taskId, projectName, worktrees, w => w.state === 'task', { branchFallback: true },
  )
}
