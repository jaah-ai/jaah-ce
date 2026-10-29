// The Task page's handlers: gate-round writes, the ⋯'s task verbs, the runner console doors and the form's saves.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiDetail, del, post } from '../../api'
import { useConfirm } from '../../components/ConfirmDialog'
import { useConsoleDock } from '../../components/ConsoleDock'
import { CONTROLS, startIntentFor } from '../../components/tasks/controls'
import type { RoundSend } from '../../components/tasks/GateSteps'
import { PANE_QUERY_KEY, forgetDeletedTasks } from '../../components/tasks/pane/queryKeys'
import { postRoundAnswer } from '../../components/tasks/roundAnswer'
import { taskOverflowItems, taskRowActionFlags, type TaskRowActionHandlers } from '../../components/tasks/rowActions'
import { deescapeMarkdownText, displayTitle, LIVE_STATES } from '../../components/tasks/shared'
import { useTaskActions } from '../../components/tasks/useTaskActions'
import { lastBoardScope } from '../../lib/boardScope'
import { taskDeepLinkPath } from '../../lib/taskDeepLink'
import { duplicateTask } from '../../lib/taskDuplicate'
import { hasLiveSession, resolveTitle } from '../../lib/worktree'
import { queryClient } from '../../queryClient'
import type { PipelineTask } from '../../types'
import type { OverflowItem } from '../../ui/OverflowMenu'
import type { TaskTab } from './taskDetailConstants'
import type { TaskDetailAccess } from './useTaskDetailAccess'
import type { TaskDetailDerived } from './useTaskDetailDerived'
import type { TaskDetailRead } from './useTaskDetailLoad'
import { isEnabled } from '../../lib/editionFeatures'

export type TaskDetailActions = ReturnType<typeof useTaskDetailActions>

export const SAVED_NOT_RESTARTED = 'Saved; not restarted.'

/* A mutation's response, held over the loaded row. Responses omit `children`, so the loaded ones stay.
   `tags` needs the same rescue: mutation routes serialise `[]` (indistinguishable from untagged), and the
   tag editor builds a FULL-REPLACE payload from this state — unrescued, the next pick silently DELETES
   every other group's tags. `?.length` because the value to reject is `[]`; only `PUT /tags` and
   `POST /retag` clear tags, and neither lands here. */
export function keepLoaded(prev: PipelineTask | null, updated: PipelineTask): PipelineTask {
  return {
    ...updated,
    children: updated.children ?? prev?.children,
    tags: updated.tags?.length ? updated.tags : prev?.tags,
  }
}

export function useTaskDetailActions({
  id, isCreate, viewer, read, access, derived, ratingFolded, setRatingOpen, selectTab, titleEditable = false,
  onTitleRegenerated,
}: {
  id: string | undefined
  isCreate: boolean
  viewer: boolean
  read: TaskDetailRead
  access: TaskDetailAccess
  derived: TaskDetailDerived
  ratingFolded: boolean
  setRatingOpen: (open: boolean) => void
  selectTab: (tab: TaskTab, pin?: boolean) => void
  /** May this viewer change the title (`useTaskDetailFiling`'s `titleEditable`)? Offers "Re-generate title". */
  titleEditable?: boolean
  /** Re-stages the trail's title after a re-generate: a dirty form keeps its seeded draft, which Save would write back. */
  onTitleRegenerated?: (title: string) => void
}) {
  const { task, setTask, load, setDeleting, keepsHandedRead, setFormDirty } = read
  const { otherOrg, canEditTasks, canCreateTasks, canDeleteThisTask, canRunThisTask, awaitingApproval } = access
  const { runnerProjectName, runnerWorktree, consoleRunnerWorktree } = derived
  const navigate = useNavigate()
  // Post-save action failures from the edit form. On the PAGE because the form remounts on its own
  // writes (see the banner).
  const [actionError, setActionError] = useState('')
  // A Go that ended as a save (`onEditKept`): said above the tabs until dismissed, the next save, or another task.
  const [savedNotice, setSavedNotice] = useState('')
  useEffect(() => setSavedNotice(''), [task?.id])
  // A page action (the ⋯'s verbs) in flight: the ⋯ disables until it lands.
  const [acting, setActing] = useState(false)
  // The in-app confirm modal, never `window.confirm`: a suppressed browser dialog fires the guarded
  // action with no intent recorded (`components/ConfirmDialog`).
  const [confirm, confirmDialog] = useConfirm()
  // Local submit/error state for this page's own gate-answer form.
  const [submitting, setSubmitting] = useState(false)
  const [answerError, setAnswerError] = useState('')
  /* Revise's own error line: `answerError` renders on the Answer tab, Revise is clicked on the Q&A
     subtab. */
  const [reviseError, setReviseError] = useState('')
  // The row menu's two filing dialogs, opened from this page's ⋯ (`pageActionItems`).
  const [filingOpen, setFilingOpen] = useState(false)
  const [waitingOpen, setWaitingOpen] = useState(false)

  // A's gate error over B's form: the error line belongs to one task (`/tasks/A → /tasks/B` keeps this instance).
  useEffect(() => {
    if (keepsHandedRead()) return
    setAnswerError('')
  }, [id])

  // The strip's select, for the callbacks below (Revise lands on Answer), without re-keying them on it.
  const selectTabRef = useRef<(tab: TaskTab, pin?: boolean) => void>(selectTab)
  selectTabRef.current = selectTab
  // The task the page shows NOW, so a re-generate answering after a navigation stages nothing on the next task.
  const shownIdRef = useRef(id)
  shownIdRef.current = id
  // The Web Console lives in the shell (components/ConsoleDock.tsx); this page only picks the slot.
  const { open: openConsole } = useConsoleDock()

  // Create and Save land on the task LIST with the saved row REVEALED (`taskDeepLinkPath`) — the board
  // is bounded and tree-shaped, so a bare `/tasks` may hide it — on the scope the board last showed in this
  // tab (`lib/boardScope.ts::lastBoardScope`), never moved to the row's project; only a tab with
  // no earlier board falls back to the row's own project. `replace: true` so Back skips the form.
  // The pane's cached rows predate this save and are persisted, fresh for their `staleTime`: invalidated, they
  // refetch on landing; left alone, the list (and a reload of it) kept the board without the row just created.
  const backToList = useCallback(
    (taskId: number, projectId: number) => {
      void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
      navigate(taskDeepLinkPath(taskId, projectId, lastBoardScope()), { replace: true })
    },
    [navigate],
  )

  /* Answering is DONE once nothing is outstanding: the page returns to the list (row revealed, as Save)
     at once — it does not wait out the delivery, and does not fall back to the Task tab's editor. While
     other rounds stay open it remains on Answer. Reads the RESPONSE, not the stale row. */
  const leaveIfAnswered = useCallback(
    (updated: PipelineTask) => {
      if ((updated.questions ?? []).some(q => q.status === 'open')) return
      backToList(updated.id, updated.project_id)
    },
    [backToList],
  )

  /* ONE round write (answer, answer-question, ignore-question, revise-question): busy, clear the error
     line(s), post, hold the row (`keepLoaded`), then `after` with the RESPONSE. `submitting` is SHARED by
     all four: they act on the same rounds from different tabs, and a private busy flag would allow two
     mutations on one round (the server would 409). `error` is cleared first and takes the failure;
     `alsoClear` is another tab's banner, cleared but never written. */
  const writeRound = useCallback(async (
    send: (taskId: number) => Promise<PipelineTask>,
    { error, alsoClear, fallback, after }: {
      error: (message: string) => void
      alsoClear?: (message: string) => void
      fallback: string
      after: (updated: PipelineTask) => void
    },
  ) => {
    if (!task) return
    setSubmitting(true)
    error('')
    alsoClear?.('')
    try {
      const updated = await send(task.id)
      setTask(keepLoaded(task, updated))
      after(updated)
    } catch (err) {
      error(apiDetail(err, fallback))
    } finally {
      setSubmitting(false)
    }
  }, [task])

  // Posts a human answer via the shared `answer` control (controls.ts owns the endpoint). On success
  // `leaveIfAnswered` returns to the list when nothing is outstanding; on failure the form stays.
  const submitAnswer = useCallback((answer: string, comment?: string, attachmentIds?: number[]) =>
    writeRound(taskId => post<PipelineTask>(CONTROLS.answer.path(taskId), {
      // An empty answer with files is a file-only answer: omitted, and the server words it. `comment` is
      // its own field (see GateForm's `AnswerControl`), omitted when empty.
      ...(answer || !attachmentIds ? { answer } : {}),
      ...(comment ? { comment } : {}),
      ...(attachmentIds ? { attachment_ids: attachmentIds } : {}),
    }), { error: setAnswerError, fallback: 'Failed to post answer', after: leaveIfAnswered }),
  [writeRound, leaveIfAnswered])

  /* Sends every open round's outcome from the Answer tab's pages (`GateSteps`) in one write: each answer via
     `postRoundAnswer`, each skip via POST /tasks/{id}/ignore-question, in page order. The server resumes the
     Task once NOTHING is outstanding, so the last post delivers them all. A failure stops the run: the rounds
     already posted have settled, the rest stay open with their drafts. Clears the Q&A trail's Revise banner
     too: it lives on another tab and nothing else would clear it once the rounds are settled. */
  const submitRounds = useCallback((sends: RoundSend[]) =>
    writeRound(async taskId => {
      let updated: PipelineTask | undefined
      try {
        for (const s of sends) {
          updated = s.ignore
            ? await post<PipelineTask>(`/api/tasks/${taskId}/ignore-question`, { question_id: s.questionId })
            : await postRoundAnswer(taskId, s.answer, s.questionId, s.comment, s.attachmentIds)
        }
      } catch (err) {
        // The rounds posted before the failure have settled: show that, so a retry sends only the rest.
        const partial = updated
        if (partial) setTask(prev => keepLoaded(prev, partial))
        throw err
      }
      if (updated === undefined) throw new Error('Nothing to send')
      return updated
    }, {
      error: setAnswerError, alsoClear: setReviseError, fallback: 'Failed to send answers', after: leaveIfAnswered,
    }),
  [writeRound, leaveIfAnswered, setTask])

  /* REVISE one resolved round via POST /tasks/{id}/revise-question: it REOPENS the round, so every
     existing answer path then works unchanged. The error is its own, since `answerError` renders on the
     Answer tab. On success jump to the Answer tab, where the reopened round is answerable. */
  const submitReviseQuestion = useCallback((questionId: number) =>
    writeRound(taskId => post<PipelineTask>(`/api/tasks/${taskId}/revise-question`, { question_id: questionId }), {
      error: setReviseError, fallback: 'Failed to revise answer', after: () => selectTabRef.current('answer'),
    }),
  [writeRound])

  /* A filing, a wait or a ⋯ write returns the row; the page holds it (children and tags rescued, as `onSaved`
     does) — unless it lands after the page moved to another id (the route reuses this page). */
  const holdUpdated = (updated: PipelineTask) =>
    setTask(prev => prev !== null && prev.id !== updated.id ? prev : keepLoaded(prev, updated))
  /* The waiting banner's ×: drops the reason, keeps the state. */
  const dismissWaiting = async () => {
    if (task === null) return
    setActionError('')
    try {
      holdUpdated(await del<PipelineTask>(`/api/tasks/${task.id}/wait-on-customer`))
    } catch (err) {
      setActionError(apiDetail(err, 'Could not clear Waiting on customer'))
    }
  }
  /* One write from the ⋯: busy, clear the error line, hold the returned row (the SSE refetch confirms), name
     a failure. The row is the loaded one, not an argument: the board's model (`useTaskActions`) is per row. */
  const runPageAction = async (_t: PipelineTask, failure: string, write: () => Promise<PipelineTask | void>) => {
    setActing(true)
    setActionError('')
    setSavedNotice('')
    try {
      const updated = await write()
      if (updated) holdUpdated(updated)
    } catch (err) {
      setActionError(apiDetail(err, failure))
    } finally {
      setActing(false)
    }
  }
  /* The runner console for this row, from the header pill and the Runner field's 🪟 alike. Never on the Live
     Cast viewer: a visitor has no console door, and the ConsoleDock would open a WebSocket outside the live API
     fence (the live API also sends `microvm` null and the pool roster empty — this is belt and braces). */
  // No MicroVM without the feature (`microvm`): the community edition has none.
  const microvm = isEnabled('microvm') ? task?.microvm : null
  const openRunnerConsole = viewer ? undefined : consoleRunnerWorktree ? () => openConsole({
    project: consoleRunnerWorktree.project,
    projectId: consoleRunnerWorktree.project_id,
    number: consoleRunnerWorktree.number,
    // `nodeId`: (project, number) names one slot PER NODE; without it the relay may attach another
    // node's same-numbered slot, and the node-addressed Title editor cannot show.
    nodeId: consoleRunnerWorktree.node_uuid,
    // Fail-closed server verdict, as in `components/WorktreePool.tsx`; the console ANDs it with its own
    // key predicate.
    mayEdit: consoleRunnerWorktree.may_edit,
    title: resolveTitle(consoleRunnerWorktree),
    // The row's own session answer at click time (the memo's arms do not prove one); see
    // `ConsolePane.tsx`'s SLOT_EMPTY_REASON.
    hadLiveSession: hasLiveSession(consoleRunnerWorktree),
  }) : microvm && runnerProjectName && task ? () => openConsole({
    // A MicroVM task holds no slot: the relay attaches its VM by `view` (number 0); the CP resolves the
    // node from the VM row and never creates.
    project: runnerProjectName,
    projectId: task.project_id,
    number: 0,
    nodeId: microvm.node,
    view: `vm:${microvm.vm_id}`,
    mayEdit: microvm.may_edit,
    title: task?.title,
    // The detail route reports only a RUNNING VM.
    hadLiveSession: true,
  }) : undefined
  /* "Open in chat" from the Agent tab: the slot's console, which shows a chat-capable session's Chat view on its
     own. A MicroVM view has no Chat, so only the slot's console offers it. */
  const openRunnerChat = consoleRunnerWorktree ? openRunnerConsole : undefined

  /* ── THE ⋯: EVERY TASK ACTION THE BOARD ROW OFFERS ─────────────────────────────────────
     Same flags, items and gates as the board's row menu (`rowActions.tsx` decides which a task offers and
     why), minus Edit — this page IS the editor. It is the page's only home for Stop and Delete/Archive.
     Offered with unsaved edits too (an operator decision): a write that returns the row re-keys the form. */
  /* The board's verbs (`useTaskActions`: same confirms, routes and preempt dialog) under this page's model.
     Delete: `DELETE /api/tasks/{id}` (any state; `tasks:delete`, or a `tasks:create` author's own
     draft); which rows offer it is `rowActions.tsx` `showCleanup`, gated on `canDeleteThisTask`. On success
     go to plain `/tasks` (the row is gone, no `?task=` to reveal); on failure `actionError` and the ⋯ re-enables. */
  const actions = useTaskActions({
    confirm,
    run: runPageAction,
    isBusy: () => acting,
    withBusy: async (_t, fn) => {
      setActing(true)
      try { await fn() } finally { setActing(false) }
    },
    deleteDialog: (t, verb) => ({
      title: `${verb} task`,
      // Named in its own identity block, so a misclick cannot confirm without seeing which task.
      identity: `${t.id}: ${t.title}`,
      /* Names the cascade: `delete_task_core` removes children via the `parent_task_id` ON DELETE CASCADE
         self-FK without asking again. Counted from the loaded row. */
      message: (
        <>
          {verb} “<strong>{displayTitle(t.title, { showPersona: true })}</strong>”? The task
          {(t.children?.length ?? 0) > 0 && <> and its {t.children?.length} subtask
            {t.children?.length === 1 ? '' : 's'}</>} and its attachments are deleted for good —
          this cannot be undone.{LIVE_STATES.has(t.state) && <> It will be stopped.</>}
        </>
      ),
      failure: `Failed to ${verb.toLowerCase()} task`,
    }),
    /* A refused DELETE resumes the reads, and re-reads the row once: events heard while paused were dropped. */
    onDeleting: pending => {
      setDeleting(pending)
      if (!pending) void load()
    },
    onDeleted: () => {
      if (task !== null) forgetDeletedTasks(queryClient, [task.id])
      navigate('/tasks')
    },
  })
  const pageActions = task === null || isCreate
    ? null
    : (() => {
        const handlers: TaskRowActionHandlers = {
          onStart: canEditTasks ? (t, intent) => void actions.start(t, intent) : undefined,
          onStop: canEditTasks ? t => void actions.stop(t) : undefined,
          onReset: canEditTasks ? t => void actions.reset(t) : undefined,
          onComplete: canEditTasks ? t => void actions.complete(t) : undefined,
          onDelete: canDeleteThisTask ? t => void actions.confirmDelete(t) : undefined,
          /* Another org's row: the editor opens acting as it, so the copy files there, as New task does. */
          onDuplicate: canCreateTasks ? t => duplicateTask(t, otherOrg, navigate) : undefined,
          duplicateAsOf: () => otherOrg,
          onMoveToFeature: canEditTasks ? () => setFilingOpen(true) : undefined,
          onWaitOnCustomer: canEditTasks ? () => setWaitingOpen(true) : undefined,
          canRunRow: () => canRunThisTask,
          startBlockedReason: awaitingApproval,
          worktreeFor: () => runnerWorktree,
          consoleWorktreeFor: () => consoleRunnerWorktree,
          onOpenConsole: () => openRunnerConsole?.(),
        }
        const variant = task.parent_task_id === null ? 'root' : 'leaf'
        const flags = taskRowActionFlags(task, variant, handlers)
        return {
          // Named as the trail shows the title (de-escaped), like the ⋯ itself (`TaskDetailHeader`).
          items: taskOverflowItems(task, deescapeMarkdownText(task.title), variant, handlers, flags),
          // The pill's Start is the overflow's Start, gated the same way.
          onPillStart: flags.showStart && !flags.startBlockedReason
            ? () => void actions.start(task, startIntentFor(task, variant))
            : undefined,
          // Beside the pill's Start, on the same gate, while a MicroVM node holds the task's last VM.
          onStartElsewhere: flags.showStart && !flags.startBlockedReason && isEnabled('microvm') && task.vm_hold_node
            ? () => void actions.startElsewhere(task, startIntentFor(task, variant))
            : undefined,
        }
      })()
  /* Re-generate title: the auto-title model over the current body, overwriting the title. Page-only, like
     Rate result; on the title route, so an operator gets it on another org's row too. */
  const regenerateTitle = task !== null && titleEditable
    ? () => void runPageAction(task, 'Could not re-generate the title',
      async () => {
        const updated = await post<PipelineTask>(`/api/tasks/${task.id}/title/regenerate`)
        /* Only onto the task it was asked for, and only a NEW title: an unchanged answer (the model gave nothing)
           leaves a title the operator typed but has not saved. */
        if (String(updated.id) === shownIdRef.current && updated.title !== task.title) onTitleRegenerated?.(updated.title)
        return updated
      })
    : null
  const pageActionItems: OverflowItem[] = [
    ...(pageActions?.items ?? []),
    ...(regenerateTitle
      ? [{
          key: 'regenerate-title', icon: '↻', label: 'Re-generate title', ariaLabel: 'Re-generate title',
          testId: 'task-regenerate-title', onClick: regenerateTitle,
        }]
      : []),
    ...(ratingFolded
      ? [{ key: 'rate', icon: '☆', label: 'Rate result', ariaLabel: 'Rate result', testId: 'task-rating-fold', onClick: () => setRatingOpen(true) }]
      : []),
  ]

  // The edit form's Save and Go return to the LIST; `setTask` first so the page holds the saved row regardless.
  const onEditSaved = (saved: PipelineTask) => {
    setActionError('')
    setSavedNotice('')
    // `tags` rescued beside `children` (see `submitAnswer`).
    setTask(prev => ({
      ...saved,
      children: saved.children ?? prev?.children,
      tags: saved.tags?.length ? saved.tags : prev?.tags,
    }))
    backToList(saved.id, saved.project_id)
  }
  /* A Go whose late reset confirm was cancelled (the row went running during the save): the save landed and
     the runner is untouched, so the page STAYS on the saved row — the form re-keys on it, clean — and says what
     happened, where Save would have left for the list. A row now waiting on a question pins Task (its gate's tab
     would otherwise take the page; the pin goes with the gate, `useTaskDetailTabs`). */
  const onEditKept = (saved: PipelineTask) => {
    setActionError('')
    setFormDirty(false)
    setTask(prev => keepLoaded(prev, saved))
    setSavedNotice(SAVED_NOT_RESTARTED)
    if (saved.state === 'awaiting_response') selectTabRef.current('task', true)
  }

  return {
    confirm, confirmDialog, submitting, answerError, reviseError, actionError, setActionError, acting, filingOpen,
    setFilingOpen, waitingOpen, setWaitingOpen, backToList, submitAnswer, submitRounds,
    submitReviseQuestion, holdUpdated, dismissWaiting, openRunnerConsole, openRunnerChat, preemptDialog: actions.preemptDialog,
    pageActions, pageActionItems, onEditSaved, onEditKept, savedNotice, setSavedNotice,
  }
}
