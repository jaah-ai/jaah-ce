// What the Task page derives from its loaded row: the lane-run flag, the runner's slots, why it failed, Live Cast.
import { useMemo } from 'react'
import { useConsoleChatTask } from '../../components/ConsoleDock'
import { LANE_PERSONA, laneArtifactHref, latestGateLog } from '../../components/tasks/laneRun'
import { isEnabled } from '../../lib/editionFeatures'
import { useMinuteTick } from '../../hooks/useMinuteTick'
import { runnerWorktreeFor, UNSIGHTED_RUNNER_WARN_MS, unsightedRunnerAgeMs, worktreeForTask } from '../../lib/worktree'
import { consoleStateLive } from '../../components/tasks/rowActions'
import type { Artifact, OrgRef, PipelineTask, Project, Worktree } from '../../types'
import type { ActivityLogEntry } from './ActivityLog'

export type TaskDetailDerived = ReturnType<typeof useTaskDetailDerived>

/* ── The note kinds that carry a task's TERMINAL REASON ───────────────────────────────────────────────
   Full stored kinds, byte-identical to the writer: `task_note:placement_refused` is written only by
   `_fail_unplaceable_tasks` in `apps/api/app/lease_drain.py`. The reader reassembles the kind from
   `category`/`action` so the two sides stay one grep apart. Add future terminal causes HERE only; never
   widen to note kinds in general (every content row would hoist into the banner). See `terminalCause`. */
const TERMINAL_CAUSE_NOTE_KINDS: ReadonlySet<string> = new Set(['task_note:placement_refused'])

// A5/R5: the worktree link shows only while a dispatched runner holds one.
const IN_PROGRESS_STATES = new Set(['ready', 'active', 'awaiting_response'])

export function useTaskDetailDerived({ task, activity, artifacts, projects, worktrees, isCreate, deleting, otherOrg }: {
  task: PipelineTask | null
  activity: ActivityLogEntry[]
  artifacts: Artifact[]
  projects: Project[]
  worktrees: Worktree[]
  isCreate: boolean
  deleting: boolean
  otherOrg: OrgRef | null
}) {
  // The task whose Chat view the open console shows, if any: the Agent tab then draws no second live feed.
  const consoleChatTask = useConsoleChatTask()
  /* A merge-lane run's newest gate log, linked from its runner rows on the Timeline (its Artifacts tab holds it). */
  const gateLog = isEnabled('merge_lane') && task?.persona === LANE_PERSONA ? latestGateLog(artifacts) : null
  /* A lane run (the merge lane) is filed by the CP: its body is read, never edited or rated. */
  const script = task !== null && isEnabled('merge_lane') && task.persona === LANE_PERSONA
  const gateLogHref = task && gateLog ? laneArtifactHref(task.id, gateLog.id) : null

  /* Live Cast's override control asks only for a saved, standing task whose project's org holds the product
     (`live_cast_offered`); the control itself is an org admin's (`lib/liveCastSurface.ts::TaskLiveCastControl`). */
  const liveCastAsked = task !== null && !isCreate && !deleting
    && projects.find(p => p.id === task.project_id)?.live_cast_offered === true
  // Another org's task is read and written as that org.
  const liveCastOrg = otherOrg

  // The roster is keyed by NAME; a Task carries `project_id`.
  const runnerProjectName = useMemo(
    () => (task ? projects.find(p => p.id === task.project_id)?.name : undefined),
    [task, projects],
  )

  // The stack link's worktree (`lib/worktree.ts::worktreeForTask`): only a stack the operator can open.
  const runnerWorktree = useMemo(() => {
    if (!task || !IN_PROGRESS_STATES.has(task.state)) return undefined
    return worktreeForTask(task.id, runnerProjectName, worktrees,
      wt => wt.stack_state === 'up' && !!wt.urls?.['web'])
  }, [task, runnerProjectName, worktrees])

  /* A coarse clock for the age threshold: 30s against 5 minutes. */
  const nowTick = useMinuteTick(30_000)

  /* ── A LAUNCHED RUNNER THAT HAS NEVER REPORTED ─────────────────────────────────────────────
     A `ready` task holding a slot = a runner was LAUNCHED (leasing happens only at dispatch) and never
     claimed `active` — e.g. stuck on a startup prompt. Five minutes: well past the CP's 90s launch grace
     (`apps/api/app/runner_liveness.py::LAUNCH_SETTLE_SECONDS`); threshold and age clock are single-homed
     in `lib/worktree.ts`. A WARNING only — the CP stays the judge. The state test is inside the memo so
     no reader inherits the slot without the condition. Keyed on `nowTick` because nothing else moves on
     this fault. */
  const wedgedRunnerSlot = useMemo(() => {
    if (!task || task.state !== 'ready') return undefined
    /* The strong key only, no `branchFallback`: for a `ready` task a stale branch could resolve another
       task's slot (`worktreeForTask`). */
    const slot = worktreeForTask(task.id, runnerProjectName, worktrees, w => w.state === 'task')
    if (!slot) return undefined
    const age = unsightedRunnerAgeMs(slot, Date.now())
    if (age === null || age < UNSIGHTED_RUNNER_WARN_MS) return undefined
    // `since` carried out because TypeScript cannot see that a non-null age proves `holder_since` parsed.
    const since = slot.holder_since
    return since ? { slot, since } : undefined
  }, [task, runnerProjectName, worktrees, nowTick])

  // The slot whose CONSOLE this task can open: whichever slot the task HOLDS, at any state — the
  // runner exists while it holds one, Asking (alive inside its question call) and a wedged launch included.
  // Occupancy, not a sensed session (`lib/worktree.ts::runnerWorktreeFor`): on a wedged slot the console IS
  // the diagnosis. Wider than the board's live-state rule (`components/tasks/rowActions.tsx` `consoleSlotFor`)
  // on purpose: there an Asking pill presses to this page. The unstamped-launch branch fallback only while
  // that rule says the runner is live: off it, a stale `fleet/task-N` checkout could name another task's
  // slot. The button still passes the slot's `may_edit`, which the console ANDs with its own key predicate.
  const consoleRunnerWorktree = useMemo(() => {
    if (task === null) return undefined
    return consoleStateLive(task)
      ? runnerWorktreeFor(task.id, runnerProjectName, worktrees)
      : worktreeForTask(task.id, runnerProjectName, worktrees, w => w.state === 'task')
  }, [task, runnerProjectName, worktrees])

  /* ── The one feed row that says WHY this task's run died, hoisted out of the feed ────────────────────
     `system_failed` notifications reach nothing without a Slack credential, and `awaiting_response_body`
     renders only under `gated` — which a failed row almost never is (`questions` rides the detail payload
     only for `awaiting_response` or `triage`, routes/tasks/). A failed triage row with a stale round
     renders the sentence twice; accepted, never suppressed. Rules: the LAST match (feed is ascending);
     this task's own rows only (epic feeds roll up children — `ActivityLog`'s owner idiom); a blank summary
     yields nothing; and the match must postdate the newest own `task:active` row, by POSITION (a refusal
     is written at dispatch; an older one belongs to a superseded attempt). No `active` row = all qualify. */
  const terminalCause = useMemo(() => {
    if (task === null || task.state !== 'system_failed') return null
    const own = (e: ActivityLogEntry) => {
      const ownerId = typeof e.data.id === 'number' ? e.data.id : Number(e.data.id)
      return !(Number.isFinite(ownerId) && ownerId !== task.id)
    }
    // Reverse scan: `findLastIndex` is ES2023 and this app's `lib` is ES2022. `-1` = nothing to postdate.
    let lastRunStart = -1
    for (let i = activity.length - 1; i >= 0; i -= 1) {
      const e = activity[i]
      if (e.category === 'transition' && e.action === 'active' && own(e)) { lastRunStart = i; break }
    }
    return activity.filter((e, i) => (
      i > lastRunStart
      && e.category === 'note'
      && TERMINAL_CAUSE_NOTE_KINDS.has(`task_note:${e.action}`)
      && own(e)
      && e.summary.trim() !== ''
    )).at(-1) ?? null
  }, [task, activity])

  /* The banner's content: the feed note when one matched (it carries the server's label), else the reason
     the failure itself stored (`failure_reason`, served only on `system_failed`) — the one copy every
     failure path writes, so a first-time owner never meets a bare "Failed" chip. One banner, never two:
     a placement refusal stores the note's own sentence as its reason. */
  const failureCause = useMemo(() => {
    if (terminalCause !== null) return terminalCause
    const reason = task?.state === 'system_failed' ? task.failure_reason?.trim() : undefined
    // Writers store a clause (`end_attempt`'s reasons start lower-case); a banner shows a sentence.
    return reason ? { label: 'Why it failed', summary: reason.charAt(0).toUpperCase() + reason.slice(1) } : null
  }, [task, terminalCause])

  return {
    consoleChatTask, script, gateLogHref, liveCastAsked, liveCastOrg, runnerProjectName, runnerWorktree, wedgedRunnerSlot,
    consoleRunnerWorktree, failureCause,
  }
}
