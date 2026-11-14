// The Task page's tabs: the strip (the rating beside it) and the panels small enough to sit together.
import { useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { TabPanel, Tabs } from '../../components/Tabs'
import TaskActivity from '../../components/TaskActivity'
import RoutingPanel from '../../components/routing/RoutingPanel'
import RichTextRenderer from '../../components/richtext/RichTextRenderer'
import GateForm from '../../components/tasks/GateForm'
import QATrail from '../../components/tasks/QATrail'
import type { StalledAnswer } from '../../lib/answerPickup'
import StalledAnswerBanner from './StalledAnswerBanner'
import SubtaskList from '../../components/tasks/SubtaskList'
import TaskArtifacts from '../../components/tasks/TaskArtifacts'
import TaskComments from '../../components/tasks/TaskComments'
import TaskEditForm from '../../components/tasks/TaskEditForm'
import TaskRating from '../../components/tasks/TaskRating'
import { RunMetricsView, type RunMetricsState } from '../../components/tasks/TaskRunMetrics'
import TaskSessionUsage from '../../components/tasks/TaskSessionUsage'
import { isChatTask } from '../../components/tasks/taskPersona'
import type { Artifact, PipelineTask, Project } from '../../types'
import { DetailSection } from '../../ui/detail/DetailPage'
import { RADIUS } from '../../theme'
import { FEED_SX, RATING_SLOT_SX, TABS_ROW_SX, type DetailsSubtab, type TaskTab } from './taskDetailConstants'
import { gateParkEvent, type ActivityLogEntry } from './ActivityLog'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { TaskDetailFiling } from './useTaskDetailFiling'
import type { TaskDetailTabsState } from './useTaskDetailTabs'

/* Every panel on this page wraps the shared detail-page control (`ui/detail/DetailPage.tsx`, where the fill measure
   and panel reasoning are single-homed). No `DetailSection` here is `divided`. Every panel passes
   `testId="task-page-panel"`, and titled ones `headingLevel="h3"` (they sit inside a tab panel). */

/* ── THE TAB-STRIP ROW: the strip, and on a DONE task the rating control on its right ─────────
   The strip's own wrapper becomes the row so `PAGE_SX`'s `& > [data-testid="task-page-tabs"]` rules still hit
   it. Only for `isRatableTask` (single home; the server 409s likewise). Folded, it is a field on the Task tab
   (`TaskBodyPanel`). */
export function TaskDetailTabStrip({ task, canVote, tabState }: {
  task: PipelineTask
  /** Any signed-in viewer may vote (`tasks:view`, server-enforced); a vote must be attributable. */
  canVote: boolean
  tabState: TaskDetailTabsState
}) {
  const { setTabsRow, tabs, activeTab, selectTab, rateable, ratingFolded } = tabState
  return (
    <Box data-testid="task-page-tabs" ref={setTabsRow} sx={TABS_ROW_SX}>
      <Tabs
        tabs={tabs}
        tab={activeTab}
        onSelect={selectTab}
        label="Task sections"
        idPrefix="task"
        /* The tabs that fit stay; the rest (the rarer Agent, Details at the end) go behind More, so a phone reaches
           every tab without scrolling the strip. */
        overflowMenu
      />
      {rateable && !ratingFolded && (
        <Box data-testid="task-rating-slot" sx={RATING_SLOT_SX}>
          {/* Any signed-in viewer may vote (`tasks:view`, server-enforced); a vote must be attributable. */}
          <TaskRating taskId={task.id} canVote={canVote} />
        </Box>
      )}
    </Box>
  )
}

/* ── q&a ────────────────────────────────────────────────────────────────────────────────────
    What was asked and how it was settled. At a gate: the gate's form, whose History is the settled trail. Otherwise
    the DB-sourced Q&A trail alone. AD-7: mounted from the tab's first visit on (`lazy`, keyed by task) so a half-typed
    answer survives tab trips.
    AC2.7: where a gated task or subtask is answered. `onSendRounds` = the open rounds as pages, `onSubmit` =
    single-shot `/answer` with no `questions` history. The form mounts on `gated`, not `answerable`: readers get it
    read-only. */
export function TaskQAPanel({ current, task, gated, activity, canRunThisTask, stalledAnswer, actions }: {
  current: TaskTab
  task: PipelineTask
  gated: boolean
  /** An answer the agent never picked up (`useStalledAnswer`), or `null`. */
  stalledAnswer: StalledAnswer | null
  activity: ActivityLogEntry[]
  canRunThisTask: boolean
  actions: TaskDetailActions
}) {
  const { submitting, answerError, submitAnswer, submitRounds, submitReviseQuestion, reviseError } = actions
  /* The empty gate's next step, other query keys kept (`hooks/useUrlTab.ts`): Details › Timeline, where the gate's
     park event is — never a second empty page. */
  const [searchParams] = useSearchParams()
  const timelineHref = new URLSearchParams(searchParams)
  timelineHref.set('tab', 'details')
  timelineHref.set('details', 'timeline')
  /* Revise: not for chats (the server 409s). */
  const onRevise = isChatTask(task) ? undefined : submitReviseQuestion
  const qaEntries = task.qa_entries ?? []
  /* GateForm's History lists settled ROUNDS; a single-question gate (no `questions` rows) draws none, so its earlier
     trail goes under the form instead of vanishing. */
  const trailBelowGate = gated && (task.questions ?? []).length === 0 && qaEntries.length > 0
  const trail = (
    <QATrail
      entries={qaEntries}
      /* Live rows beside the snapshots; absent on the wire = no rounds. */
      questions={task.questions ?? []}
      submitting={submitting}
      error={reviseError}
      /* `canRunThisTask` (reopening is a RUN verb), not `answerable`: Revise must work once every round is settled. */
      onRevise={canRunThisTask ? onRevise : undefined}
    />
  )
  return (
    <TabPanel key={task.id} id="qa" current={current} idPrefix="task" lazy>
      <DetailSection testId="task-page-panel" fill>
        <StalledAnswerBanner task={task} canRunThisTask={canRunThisTask} stalledAnswer={stalledAnswer} actions={actions} />
        {gated && (
          <GateForm
            task={task}
            submitting={submitting}
            /* Straight off the row (`answer_delivering` in types.ts); no local copy. */
            delivering={task.answer_delivering ?? false}
            error={answerError || undefined}
            onSubmit={submitAnswer}
            onSendRounds={submitRounds}
            /* `readOnly` covers readers, so `canRunThisTask` is not re-checked. */
            onReviseQuestion={onRevise}
            reviseError={reviseError || undefined}
            /* Content stays, controls go. The callbacks are still passed (unreachable when read-only).
               The ROW answer, project-scoped. Another org's question is answered as that org. */
            readOnly={!canRunThisTask}
            trailLink={{ to: `?${timelineHref.toString()}`, label: 'Details › Timeline' }}
            parkEvent={gateParkEvent(activity, task.id) ?? undefined}
          />
        )}
        {/* SANCTIONED SCROLLPORT 3 of 4 (`FEED_SX`): the trail is as unbounded as Details' feed. */}
        {(!gated || trailBelowGate) && <Box data-testid="task-page-feed" sx={FEED_SX}>{trail}</Box>}
      </DetailSection>
    </TabPanel>
  )
}

/* A script run's Task tab (the merge lane): the body the CP wrote, read-only, with none of the form's Depth,
 * Involvement or Persona controls — nothing there is the operator's to set. The read-only box `TaskSpecFields`
 * draws round its renderer. */
function ScriptTaskBody({ body }: { body: string }) {
  return (
    <Box
      data-testid="task-page-script-body"
      role="region"
      aria-label="Description"
      sx={{ border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.md, bgcolor: 'background.paper', p: 2, minWidth: 0 }}
    >
      <RichTextRenderer content={body} />
    </Box>
  )
}

/* ── task ───────────────────────────────────────────────────────────────────────────────────
    The same `TaskEditForm` `/tasks/new` renders, in edit mode, so the two cannot drift. Mounted from the tab's first
    visit (`lazy`): a task opened on Q&A or Artifacts fetches none of the form's pickers, and a form never shown
    is never dirty. */
/* CONTENT ONLY: Outcome and the unverified-done warning live on Details › Summary. */
/* `task` is the page's `formTask` (`useTaskDetailLoad`): the row it opened on while it holds unsaved edits.
   `liveTask` is the page's live row, which Go is decided from. */
export function TaskBodyPanel({
  current, task, liveTask, script, projects, canEditThisTask, canRunThisTask, filing, actions, onDirtyChange, onCancel, canVote, tabState,
}: {
  current: TaskTab
  task: PipelineTask
  liveTask: PipelineTask
  script: boolean
  projects: Project[]
  canEditThisTask: boolean
  canRunThisTask: boolean
  filing: TaskDetailFiling
  actions: TaskDetailActions
  onDirtyChange: (dirty: boolean) => void
  onCancel: () => void
  /** Any signed-in viewer may vote (`TaskDetailTabStrip`'s `canVote`). */
  canVote: boolean
  tabState: TaskDetailTabsState
}) {
  const { rateable, ratingFolded, onRated } = tabState
  const { draftTitle, draftProjectId, draftFeatureId } = filing
  const { onEditSaved, onEditKept, setActionError } = actions
  return (
    <TabPanel key={task.id} id="task" current={current} idPrefix="task" lazy>
      <DetailSection testId="task-page-panel" fill>
        {/* SANCTIONED SCROLLPORT 1 of 4 is inside this form (`data-testid="task-page-description"`).
            `key` = id (A→B keeps the instance; B must not open with A's edits) + `updated_at` (a runner's
            concurrent edit must not be silently overwritten on the next Save). */}
        {script ? (
          <>
            <ScriptTaskBody body={task.body ?? ''} />
            {/* No form here, so a folded rating sits under the body (the form's own field otherwise). */}
            {rateable && ratingFolded && (
              <Box role="group" aria-label="Rating" data-testid="task-page-rating-field" sx={{ mt: 2 }}>
                <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>Rating</Typography>
                <TaskRating taskId={task.id} canVote={canVote} onRated={onRated} />
              </Box>
            )}
          </>
        ) : <TaskEditForm
          key={`${task.id}:${task.updated_at}`}
          mode="edit"
          task={task}
          liveTask={liveTask}
          projects={projects}
          // Save and Go return to the LIST (`onEditSaved`).
          onSaved={onEditSaved}
          // A Go whose save landed but whose transition failed; lifted to outlive the remount.
          onActionError={setActionError}
          // A Go that ended as a save (its late reset confirm cancelled): stays here and says so.
          onSavedNotRestarted={onEditKept}
          // The Discard button; the prop keeps its name as this page's contract with the form.
          onCancel={onCancel}
          /* Still mounts for a reader: read-only, it is how `tasks:view` reads the task at all. */
          readOnly={!canEditThisTask}
          /* Go is Save + start: an authored-draft author may Save but not start, which `readOnly` cannot
             express. `canStart` takes Go and the prerequisite panel's Re-run. */
          canStart={canRunThisTask}
          // Title and Project live in the breadcrumb, but their values ride this submit.
          titleDraft={draftTitle ?? undefined}
          projectIdDraft={draftProjectId ?? undefined}
          featureIdDraft={draftFeatureId}
          onDirtyChange={onDirtyChange}
          // Folded off the strip, the rating is one of this tab's fields; `onRated` feeds the header's badge.
          rating={rateable && ratingFolded ? <TaskRating taskId={task.id} canVote={canVote} onRated={onRated} /> : undefined}
        />}
      </DetailSection>
    </TabPanel>
  )
}

/* ── subtasks ───────────────────────────────────────────────────────────────────────────────
    The children by phase, in the board's own Activity rows; read-only. */
/* Guarded by `hasSubtasks` like the strip, so no hidden panel dangles an `aria-labelledby`. */
export function TaskSubtasksPanel({ current, task }: { current: TaskTab; task: PipelineTask }) {
  return (
    <TabPanel key={task.id} id="subtasks" current={current} idPrefix="task" lazy>
      <DetailSection testId="task-page-panel" fill>
        {/* SANCTIONED SCROLLPORT 2 of 4 (see `PAGE_SX`). */}
        {task && <SubtaskList key={task.id} task={task} />}
      </DetailSection>
    </TabPanel>
  )
}

/* ── artifacts ──────────────────────────────────────────────────────────────────────────────
    What a runner handed back, read on the task that produced it. The frame is SANDBOXED WITH NO
    SAME-ORIGIN ACCESS (see `TaskArtifacts`): agent-authored content, admin-session origin. The
    panel mounts under `hasArtifacts` (ARIA pairing); the frame only after the latch
    (`artifactsEverShown`), since a hidden iframe still fetches its `src`. */
export function TaskArtifactsPanel({ current, task, artifacts, everShown }: {
  current: TaskTab
  task: PipelineTask
  artifacts: Artifact[]
  /** The latch (`artifactsEverShown`): the frame mounts only once the tab has shown. */
  everShown: boolean
}) {
  return (
    <TabPanel id="artifacts" current={current} idPrefix="task">
      <DetailSection testId="task-page-panel" fill>
        {/* `key` = task id, so the child's selection state cannot outlive a task by construction. */}
        {everShown && (
          <TaskArtifacts key={task.id} taskId={task.id} artifacts={artifacts} />
        )}
      </DetailSection>
    </TabPanel>
  )
}

/* ── comments ───────────────────────────────────────────────────────────────────────────────
    A place to WRITE: `tasks:view` may post, so no gate here; edit/delete rules travel per row
    (`app/routes/task_comments.py`). Always mounted, never `lazy`: it reports its count to the tab label. */
export function TaskCommentsPanel({ current, task, readOnly, onCountChange }: {
  current: TaskTab
  task: PipelineTask
  readOnly: boolean
  onCountChange: (count: number) => void
}) {
  return (
    <TabPanel id="comments" current={current} idPrefix="task">
      <DetailSection testId="task-page-panel" fill>
        {/* `key` is the task id so a half-typed draft cannot outlive the task it was about. */}
        <TaskComments
          key={task.id}
          taskId={task.id}
          readOnly={readOnly}
          onCountChange={onCountChange}
          active={current === 'comments'}
        />
      </DetailSection>
    </TabPanel>
  )
}

/* ── agent ──────────────────────────────────────────────────────────────────────────────────
    The ACP sessions' live event stream. `sessions:view` lists the sessions; the events carry the
    agent's text, so they are `sessions:edit` (as the transcript download). While the console already shows
    this run's Chat view the tab draws no second live feed: it points there (`inChat`). `lazy`: a tab never opened
    polls no transcript (whether the tab exists is `useTaskAgentSessions`', read by the strip). */
export function TaskAgentPanel({ current, task, inChat, onOpenInChat }: {
  current: TaskTab
  task: PipelineTask
  inChat: boolean
  onOpenInChat: (() => void) | undefined
}) {
  const running = task.state === 'active'
  return (
    <TabPanel key={task.id} id="agent" current={current} idPrefix="task" lazy>
      {/* Its own `DetailSection` (fill), as the other panels' wrapper. SANCTIONED SCROLLPORT 4 of 4 is its event list. */}
      <TaskActivity
        key={task.id}
        taskId={task.id}
        live={running}
        fill
        inChat={inChat}
        onOpenInChat={onOpenInChat}
      />
    </TabPanel>
  )
}

/* ── routing ────────────────────────────────────────────────────────────────────────────────
    What the agent router decided for the task's next dispatch and every earlier one; `lazy`, so a tab never opened
    reads nothing. "Use this" writes the task's runner (`onAccepted` re-reads the row); "Change" opens the Task tab's
    form (`onChange`), where the runner is edited. */
export function TaskRoutingPanel({ current, task, onAccepted, onChange }: {
  current: TaskTab
  task: PipelineTask
  onAccepted: () => void
  onChange: () => void
}) {
  return (
    <TabPanel key={task.id} id="routing" current={current} idPrefix="task" lazy>
      <DetailSection testId="task-page-panel" title="Routing" headingLevel="h3">
        <RoutingPanel taskId={task.id} onAccepted={onAccepted} onChange={onChange} />
      </DetailSection>
    </TabPanel>
  )
}

/* ── details › metrics ───────────────────────────────────────────────────────────────────────
    Task Run Metrics — computed server-side for ANY task (`GET …/run-metrics`, read once above as
    `runMetrics`), live while running. Mounted under `hasRunMetrics` like the strip (ARIA pairing). */
export function TaskMetricsPanel({ current, runMetrics }: { current: DetailsSubtab; runMetrics: RunMetricsState }) {
  const metrics = runMetrics.metrics
  /* The per-session chart reads only once some session reported a peak: no peak, no buckets. */
  const sessionsReported = metrics != null &&
    (metrics.peak_rss_kb != null || metrics.peak_cpu_pct != null || metrics.cpu_seconds != null)
  return (
    <TabPanel id="metrics" current={current} idPrefix="details">
      <DetailSection testId="task-page-panel" title="Task Run Metrics" headingLevel="h3">
        <RunMetricsView {...runMetrics} />
        {sessionsReported && (
          <TaskSessionUsage
            key={metrics.task_id}
            taskId={metrics.task_id}
            startedAt={metrics.started_at}
            live={metrics.in_progress}
          />
        )}
      </DetailSection>
    </TabPanel>
  )
}
