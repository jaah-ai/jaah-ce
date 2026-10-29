import { type ReactNode, useCallback, useState } from 'react'
import { del, get, post } from '../../api'
import { actAsOnce } from '../../lib/actAs'
import { useOtherOrgOf } from '../../ui/OrgIndicator'
import type { ConfirmOptions } from '../ConfirmDialog'
import { blockedReach, GitReachText, gitReachPath, type ProjectGitReach } from '../GitReach'
import Toast from '../../ui/Toast'
import type { DispatchForecast, PipelineTask } from '../../types'
import {
  can as canTransition,
  CONTROLS,
  DISPATCH_ROUTES,
  type ForceStartBody,
  startBody,
  type StartIntent,
} from './controls'
import PreemptDialog, { preemptForecast } from './PreemptDialog'
import { type PushIdentity, usePushIdentity } from './PushIdentityDialog'
import { isArchiveRow, resetConfirmOptions, stopConfirmOptions, TASK_ACTIONS } from './shared'

/* THE FIVE TASK VERBS THE BOARD ROW AND THE TASK PAGE SHARE — Start (with its push-identity and preempt dialogs), Stop, Reset,
   Complete and Delete/Archive: one confirm wording, one route, one body per verb. Each page keeps its own
   busy/error model and its own after-effects (replace a row in place, refetch, navigate), handed in here. */

export type TaskActionsOptions = {
  confirm: (options: ConfirmOptions) => Promise<boolean>
  /** The page's busy/error model: run `write` with the row busy, the error line cleared first, a rejection
   *  shown under `failure`'s fallback (never rejects). The page may hold the row `write` resolves. */
  run: (task: PipelineTask, failure: string, write: () => Promise<PipelineTask | void>) => Promise<void>
  /** Whether a row is mid-action (the board: `submitting.has(id)`; the page: `acting`). */
  isBusy: (task: PipelineTask) => boolean
  /** Busy around a non-write (the forecast probe): no error line is touched. */
  withBusy: (task: PipelineTask, fn: () => Promise<void>) => Promise<void>
  /** The returned row replaces the task in place, for a verb that keeps it where it is (start, reset, force-start). */
  onRow?: (updated: PipelineTask) => void
  /** The row moved sections (stop, complete, force-start's stopped task): refetch (board) or nothing (page relies on SSE). */
  onMoved?: () => Promise<void> | void
  /** The Delete/Archive confirm's words and failure label; `verb` is `TASK_ACTIONS.delete.verb` or `.archive.verb`
   *  by `isArchiveRow`, the single predicate shared with the row's button. */
  deleteDialog: (task: PipelineTask, verb: string) => Pick<ConfirmOptions, 'title' | 'identity' | 'message'> & { failure: string }
  /** After the DELETE landed: refetch (board) or leave for `/tasks` (page). */
  onDeleted: () => Promise<void> | void
  /** The DELETE is in flight (`true`) or was refused (`false`): the page stops reading a row about to vanish. */
  onDeleting?: (deleting: boolean) => void
}

/* THE ONE TASK DELETE every door sends (the row's ⋯, the page's ⋯, the board's bulk Delete). An operator's row in
   another org goes as that org for this one request (`actAsOnce`): the menu offers Delete there already, and a
   separate Act-as click first only made it work. The server re-checks the act-as seat as ever. */
export function useDeleteTask(): (task: PipelineTask) => Promise<void> {
  const otherOrgOf = useOtherOrgOf()
  return useCallback(
    (task: PipelineTask) => {
      const asOrg = actAsOnce(otherOrgOf(task.org))
      return asOrg ? del(`/api/tasks/${task.id}`, asOrg) : del(`/api/tasks/${task.id}`)
    },
    [otherOrgOf],
  )
}

export function useTaskActions({
  confirm, run, isBusy, withBusy, onRow, onMoved, deleteDialog, onDeleted, onDeleting,
}: TaskActionsOptions): {
  start: (task: PipelineTask, intent: StartIntent) => Promise<void>
  /** Start that releases the task's held VM and places it on another node (`fresh_elsewhere`). */
  startElsewhere: (task: PipelineTask, intent: StartIntent) => Promise<void>
  stop: (task: PipelineTask) => Promise<void>
  reset: (task: PipelineTask) => Promise<void>
  complete: (task: PipelineTask) => Promise<void>
  confirmDelete: (task: PipelineTask) => Promise<void>
  /** The push-identity ask, for a page's own Start door (the board's priority force-start); its dialog is in
   *  `preemptDialog`. */
  askPushIdentity: ReturnType<typeof usePushIdentity>['ask']
  /** Page-mounted like `confirmDialog`: a row-owned modal would unmount when a refetch re-keys the rows.
   *  Carries the push-identity dialog and Start's git-preflight toast too, so every page rendering it gets all three. */
  preemptDialog: ReactNode
} {
  /* WHICH ROW'S PREEMPT DIALOG IS OPEN, with its forecast and the pressed intent, so "Just queue it" posts EXACTLY
     that start. Carries its own row: the page route reuses one component across ids, so the dialog must not act
     on whichever loaded next. */
  const [preempting, setPreempting] = useState<{
    task: PipelineTask
    intent: StartIntent
    forecast: DispatchForecast
    pushAs: PushIdentity
  } | null>(null)
  const deleteTask = useDeleteTask()
  const pushIdentity = usePushIdentity()
  /* START'S GIT PREFLIGHT: after a start lands, whether the starter's credentials can push there
     (`components/GitReach.tsx`). Start never waits on it and a failed read says nothing; a `blocked` answer is a
     warning toast with the fix, since the runner would otherwise find out only when its push or PR is refused. */
  const [reachWarning, setReachWarning] = useState<{ reach: ProjectGitReach; seq: number } | null>(null)
  // Only the starter's own answer: an operator pushing as someone else (`push_as_id`) would be told about themself.
  const checkGitReach = (task: PipelineTask, pushAs: PushIdentity) => {
    if (task.has_repo === false || pushAs.push_as_id) return
    get<ProjectGitReach>(gitReachPath(task.project_id))
      .then(reach => {
        const blocked = blockedReach(reach)
        if (blocked) setReachWarning({ reach: blocked, seq: Date.now() })
      })
      .catch(() => undefined)
  }

  // The POST every door into Start shares (direct start, the dialog's "Just queue it"). A DB write
  // only, idempotent, so no confirm and no 409 case; the returned task replaces the row. The body
  // DECLARES the intent, always explicitly — `startIntentFor` is the single answer.
  const postStart = (task: PipelineTask, intent: StartIntent, pushAs: PushIdentity, freshElsewhere = false) =>
    run(task, 'Failed to start task', async () => {
      const body = freshElsewhere ? { ...startBody(intent, pushAs), fresh_elsewhere: true } : startBody(intent, pushAs)
      const updated = await post<PipelineTask>(CONTROLS.start.path(task.id), body)
      onRow?.(updated)
      checkGitReach(updated, pushAs)
      return updated
    })

  // The unified idempotent Start (the registry's `start` path) for ANY task: an epic (draft → first go + fan-out;
  // otherwise reactivate) or a subtask/legacy row (dispatch; a `done` row re-runs fresh).
  // IT PREDICTS FIRST: a start with no free slot sits "Queued", so a non-epic row asks `dispatch-forecast`, and
  // when blocked the `PreemptDialog` offers `force-start` (the server re-queues the stopped task). Skipped for an
  // epic container and for `queue` intents. A FAILED FORECAST FALLS THROUGH TO THE ORDINARY START — a courtesy
  // must never block the primary verb.
  // AN ACTING OPERATOR IS ASKED WHOM TO PUSH AS before anything (`usePushIdentity`; `{}` at once otherwise); Cancel
  // starts nothing, and the answer rides whichever POST follows.
  async function start(task: PipelineTask, intent: StartIntent) {
    // BUSY FOR THE PROBE TOO, or a double click probes twice and opens two dialogs.
    if (isBusy(task)) return
    const pushAs = await pushIdentity.ask({ projectId: task.project_id, title: task.title, assigneeId: task.assigned_to_id })
    if (!pushAs) return
    if (task.persona !== 'epic' && intent !== 'queue') {
      // The probe starts once; busy wraps its wait, and the value is read off the same promise.
      const probe = preemptForecast(task.id)
      await withBusy(task, async () => { await probe })
      const forecast = await probe
      if (forecast) {
        setPreempting({ task, intent, forecast, pushAs })
        return
      }
    }
    await postStart(task, intent, pushAs)
  }

  // Start fresh elsewhere: the server drops the held VM and picks another node, so there is no slot to forecast
  // and no preempt dialog; a 409 (no other node) lands on the error line as Start's does.
  async function startElsewhere(task: PipelineTask, intent: StartIntent) {
    if (isBusy(task)) return
    const pushAs = await pushIdentity.ask({ projectId: task.project_id, title: task.title, assigneeId: task.assigned_to_id })
    if (!pushAs) return
    await postStart(task, intent, pushAs, true)
  }

  // One POST: stop the chosen row and start this one in its slot. The stopped task moved too (`onMoved`).
  async function forceStart(task: PipelineTask, intent: StartIntent, stopTaskId: number, pushAs: PushIdentity) {
    setPreempting(null)
    await run(task, 'Failed to force-start task', async () => {
      const body: ForceStartBody = { stop_task_id: stopTaskId, ...startBody(intent, pushAs) }
      const updated = await post<PipelineTask>(DISPATCH_ROUTES.forceStart(task.id), body)
      onRow?.(updated)
      await onMoved?.()
      return updated
    })
  }

  // Stop: parks a stoppable Task into `paused` (one atomic conditional UPDATE). Not a force-kill — apps/api never
  // calls gh/git; the live runner reconciles on its own signal. Confirms first (it interrupts work).
  async function stop(task: PipelineTask) {
    /* A QUEUED ROW HAS NO RUNNER, so the confirm must not promise one. Asked of the transition table: among rows
       Stop accepts, the one that can ALSO still Start is the one nothing was dispatched against. */
    const queued = canTransition(task, 'start')
    if (!(await confirm(stopConfirmOptions(task.title, queued)))) return
    // Stop is idempotent (200 when already stopped), so any error here is real (network / 404).
    await run(task, 'Failed to stop task', async () => {
      const updated = await post<PipelineTask>(CONTROLS.stop.path(task.id))
      await onMoved?.()
      return updated
    })
  }

  // `complete` — the direct `→ done` transition, a one-click close posting NO body.
  // MOVED, not replaced in place: the row crosses the board's halves in the server's order (as Stop and delete do);
  // patched in place it would sit above the gap indefinitely on a quiet stream.
  async function complete(task: PipelineTask) {
    await run(task, 'Failed to complete task', async () => {
      const updated = await post<PipelineTask>(CONTROLS.complete.path(task.id))
      await onMoved?.()
      return updated
    })
  }

  // Reset: return a system_failed/paused/done non-epic Task to a clean `draft` WITHOUT
  // dispatching. A DB write; the returned draft replaces the row; a 409/400 lands on the error line.
  // IT CONFIRMS FIRST: Reset also clears the outcome and soft-deletes the attempt's Q&A rounds, report and stage
  // output. The dialog names what goes, worded by the shared `resetConfirmOptions` (also the edit form's Go).
  async function reset(task: PipelineTask) {
    if (!(await confirm(resetConfirmOptions(task.title, { epic: task.persona === 'epic' })))) return
    await run(task, 'Failed to reset task', async () => {
      const updated = await post<PipelineTask>(CONTROLS.reset.path(task.id))
      onRow?.(updated)
      return updated
    })
  }

  // Delete (any state; a live row's runner is stopped) with the in-app confirm (R3). Also the row's Archive for a
  // `done`/legacy/orphaned row (`delete_task`, tasks.py): one DELETE, but the dialog echoes the clicked verb.
  async function confirmDelete(task: PipelineTask) {
    const verb = isArchiveRow(task) ? TASK_ACTIONS.archive.verb : TASK_ACTIONS.delete.verb
    const { failure, ...words } = deleteDialog(task, verb)
    if (!(await confirm({ ...words, confirmLabel: verb, destructive: true }))) return
    onDeleting?.(true)
    await run(task, failure, async () => {
      try {
        await deleteTask(task)
      } catch (err) {
        onDeleting?.(false)
        throw err
      }
      await onDeleted()
    })
  }

  const preemptDialog = (
    <>
      {pushIdentity.dialog}
      {preempting && (
        <PreemptDialog
          open
          target={preempting.task}
          forecast={preempting.forecast}
          onForceStart={stopId => void forceStart(preempting.task, preempting.intent, stopId, preempting.pushAs)}
          onQueue={() => {
            setPreempting(null)
            void postStart(preempting.task, preempting.intent, preempting.pushAs)
          }}
          onCancel={() => setPreempting(null)}
        />
      )}
      <Toast
        toast={reachWarning && { text: <GitReachText reach={reachWarning.reach} />, severity: 'warning', seq: reachWarning.seq }}
        onClose={() => setReachWarning(null)}
        testId="start-git-reach-toast"
      />
    </>
  )

  return { start, startElsewhere, stop, reset, complete, confirmDelete, askPushIdentity: pushIdentity.ask, preemptDialog }
}
