// The Task page at /tasks/:id (and /tasks/new): the route-mode switch composing the page's hooks and sections.
import { useEffect, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { useViewportIs } from '../../hooks/useViewportWidth'
import { useAuth } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import TaskBreadcrumb from '../../components/tasks/TaskBreadcrumb'
import { featureName } from '../../components/tasks/featureFiling'
import { deescapeMarkdownText, TaskStatusIndicator } from '../../components/tasks/shared'
import { featureIconFor } from '../../lib/featureIcon'
import { LANE_PERSONA } from '../../components/tasks/laneRun'
import { useTaskCreateDefaults } from '../../components/tasks/taskDefaults'
import { useCanEditTaskRow } from '../../hooks/useTaskAccess'
import { LiveRefetchPausedContext } from '../../hooks/useLiveRefetch'
import { markTaskSeen } from '../../lib/taskSeen'
import { useViewerMode } from '../../lib/viewerMode'
import { SIZE, grid } from '../../theme'
import DetailStatus from '../../ui/detail/DetailStatus'
import { DetailPage } from '../../ui/detail/DetailPage'
import { ActingForNotice } from '../../ui/OrgIndicator'
import { OrgScopeInline, useStripStandIn } from '../../ui/OrgScopeControl'
import TaskDetailsTabPanel from './TaskDetailsTabPanel'
import TaskCreateView from './TaskCreateView'
import TaskDetailBanners from './TaskDetailBanners'
import { TaskDetailCrumbs, TaskDetailHeaderActions, taskOrgChip } from './TaskDetailHeader'
import {
  TaskAgentPanel,
  TaskArtifactsPanel,
  TaskBodyPanel,
  TaskQAPanel,
  TaskCommentsPanel,
  TaskDetailTabStrip,
  TaskRoutingPanel,
  TaskSubtasksPanel,
} from './TaskDetailTabs'
import { LOADING_TRAIL_SX, PAGE_SX } from './taskDetailConstants'
import TaskPageSkeleton, { TaskTrailSkeleton } from './TaskPageSkeleton'
import { useTaskDetailAccess } from './useTaskDetailAccess'
import { useTaskDetailActions } from './useTaskDetailActions'
import { useTaskDetailDerived } from './useTaskDetailDerived'
import { savedProjectCrumbEditable, useTaskDetailFiling } from './useTaskDetailFiling'
import { useTaskDetailLoad } from './useTaskDetailLoad'
import { useTaskDetailNavState } from './useTaskDetailNavState'
import { useTaskDetailTabs } from './useTaskDetailTabs'
import { useStartRefusedToast } from './useStartRefusedToast'
import { useTaskPageViewport } from './useTaskPageViewport'
import { useTaskBoardScope } from './useTaskBoardScope'
import { parseScope, scopeHref } from '../../lib/taskScope'

// The Task page at /tasks/:id: FULL-HEIGHT, TABBED, NON-SCROLLING — header and tab strip fixed, the
// active panel takes the rest of `.app-main`.
//
// Tabs use the shared `components/Tabs` shell. Keep-mounted `[hidden]` panels are LOAD-BEARING: an
// unmount-on-switch would discard an in-progress gate answer or description edit.
//
//   task      the Description and the task's settings — content only
//   subtasks  (conditional) the tree table in page mode
//   artifacts (conditional) runner-handed documents (sandboxed HTML, markdown)
//   qa        (conditional, auto-selected at a gate) the gate form at a gate, else the Q&A trail
//   comments  comments
//   agent     (conditional) the ACP run's live event stream (`TaskActivity`)
//   details   everything ABOUT the run, one subtab each (only those with data): Summary (fields, runner,
//             unverified-done warning, Outcome, Finisher notes, Blocks, attachments) · Outcome (the ledger) · Metrics
//             · Timeline · Versions · Review (review lanes' findings)
//
// Panels FIT rather than scroll: exactly FOUR panel-level scrollports are sanctioned (Subtasks rows,
// Description, the history feed — one port shared by Details' Timeline and Versions, the same on Q&A's
// trail — and the Agent tab's live event stream), each unbounded by construction.
// Anything else scrolling is a defect (`e2e/task-detail-fit.spec.ts` sweeps per tab and subtab). The one
// nested exception: each Versions snapshot `<pre>` is bounded at 150px.
//
// `--task-page-vv-height` is set by `useTaskPageViewport` and consumed in `PAGE_SX`; they move together
// because the `var()` fallback would make a split silent.
//
// Live via the shared `task` SSE stream; an unknown id renders a not-found panel.
//
// The hooks run in the order their effects must: the read (`useTaskDetailLoad`) first, its per-task resets ahead
// of the artifacts latch (`useTaskDetailTabs`) and the create seeds (`useTaskDetailFiling`); handlers last.

const NO_OP = () => {}
/** At or below this viewport the task is a full screen of its own (never beside the list): it carries Back. */
const BACK_MAX_PX = 768
const narrowViewport = (width: number) => width <= BACK_MAX_PX

export default function TaskDetailPage(
  { mode = 'edit' }: { mode?: 'create' | 'create-chat' | 'edit' } = {},
) {
  // CONTRACT-5/AD-13: `/tasks/new` renders THIS page in create mode (one New/Edit UI): no fetch, and the
  // strip carries only the `task` tab. `'create-chat'` (`/tasks/new-chat`) is the same shell and form — a
  // chat is a task with the `advisor` persona; only these two lines and the form's `chat` prop branch on it.
  const isCreate = mode !== 'edit'
  const isChat = mode === 'create-chat'
  const navigate = useNavigate()
  const { id } = useParams<{ id: string }>()
  const { admin } = useAuth()
  /* FULL SCREEN ON A PHONE OR NARROW WINDOW, WITH A WAY BACK: to the page it was opened from, else the list. */
  const narrow = useViewportIs(narrowViewport)
  /* The public Live Cast viewer (`lib/viewerMode.ts`) sees this page as a console `tasks:view` reader does, minus
     the comment composer and the operator's exceptions: people, cross-task edges, the compliance
     manifest. The write controls are already gone by permission (no `AuthProvider` there). */
  const viewer = useViewerMode()
  const nav = useTaskDetailNavState(isCreate, isChat)
  const startRefusedToast = useStartRefusedToast()
  /* The operator's `task_default_*` settings for the CREATE surface (fences single-homed in
     `components/tasks/taskDefaults`). `loaded` gates the HANDOVER, not the render, so the form seeds
     once from real values; the page never waits. */
  const createDefaults = useTaskCreateDefaults(isCreate)
  const read = useTaskDetailLoad({ id, isCreate, duplicateOf: nav.duplicateOf })
  const { task, activity, artifacts, loading, notFound, error, runMetricsId, runMetrics } = read
  const access = useTaskDetailAccess(task)
  /* OPENING A FINISHED TASK ENDS ITS "NOT YET SEEN" MARK for this viewer (`lib/taskSeen.ts`): once per task, retried
     on the next read only if the report failed. */
  const seenRef = useRef<number | null>(null)
  useEffect(() => {
    if (!task?.done_unseen || seenRef.current === task.id) return
    const taskId = task.id
    seenRef.current = taskId
    markTaskSeen(taskId).catch(() => {
      if (seenRef.current === taskId) seenRef.current = null
    })
  }, [task?.id, task?.done_unseen])
  const { otherOrg, canEditThisTask, canRunThisTask } = access
  /* THE BOARD BESIDE THIS TASK, kept on its URL so a reload or a shared link opens the same pipeline. */
  // The loaded row only: whose org it is (`otherOrg`) is known from it, never from the board's cached copy.
  const boardScope = useTaskBoardScope(task?.project_id, isCreate || viewer || task === null || otherOrg !== null)
  const goBack = () => ((window.history.state as { idx?: number } | null)?.idx ?? 0) > 0
    ? navigate(-1)
    : navigate(boardScope ? scopeHref(parseScope(boardScope)) : '/tasks')
  // The loading trail asks it of the board's row: will its crumbs be pickers once loaded?
  const canEditRow = useCanEditTaskRow()
  const pageRef = useTaskPageViewport({ hasTask: task !== null, isCreate, loading, settled: runMetrics.settled })
  const derived = useTaskDetailDerived({
    task, activity, artifacts, projects: read.projects, worktrees: read.worktrees, isCreate, deleting: read.deleting, otherOrg,
  })
  const { script } = derived
  const tabState = useTaskDetailTabs({
    task, isCreate, activity, artifacts, runMetrics, setArtifactsEverShown: read.setArtifactsEverShown, script,
    formDirty: read.formDirty,
  })
  const { activeTab } = tabState
  const filing = useTaskDetailFiling({
    id, isCreate, task, formTask: read.formTask, setTask: read.setTask, cachedRow: read.cachedRow, projects: read.projects, loading, keepsHandedRead: read.keepsHandedRead, otherOrg, nav,
    canEditThisTask, script, formDirty: read.formDirty,
  })
  const actions = useTaskDetailActions({
    id, isCreate, viewer, read, access, derived, selectTab: tabState.selectTab, titleEditable: filing.titleEditable, onTitleRegenerated: filing.setDraftTitle,
  })

  /* The phone's org strip steps aside for a task this page shows or is about to: its filter is the header's ⋯ item
     (`TaskDetailHeaderActions`). Not for a create (its chip is on the crumb row) nor a task not found or failed, which
     the filter may be what hides. */
  useStripStandIn(!isCreate && !notFound && !error && (loading || task !== null))

  const taskCrumbs = (
    <TaskDetailCrumbs
      filing={filing}
      task={task}
      isCreate={isCreate}
      isChat={isChat}
      viewer={viewer}
      projectsPending={read.projectsPending}
      trail={read.trail}
    />
  )

  /* Loading/not-found/error cannot name a project or title: the PLAIN trail, still carrying the id and
     the `<h1>`, so PageHeader gets NO `title` there (two level-1 headings otherwise). */
  const crumbs = (
    <Breadcrumb
      items={[{ label: 'Tasks', to: '/tasks' }, { label: id ? `${id}` : 'Task' }]}
      currentComponent="h1"
    />
  )

  if (loading || (runMetricsId !== null && !runMetrics.settled) || (isCreate && filing.prefillPending)) {
    /* THE HEADING NEVER CHANGES UNDER THE READER: a bare "7355" swapped for "acme › 7355: Title" once the
       row landed. While loading, the title crumb is a skeleton; the id stays the `<h1>`'s accessible name. */
    const { cachedRow } = read
    // Once the page's own read has landed (the run metrics still hold it), its trail names stand in for the roster's.
    const named = read.trail !== null && read.trail.taskId === cachedRow?.id ? read.trail : null
    /* THE CRUMBS DO NOT SHIFT WHEN THEIR PICKERS ARRIVE: a crumb that will be a picker for this viewer
       (`savedProjectCrumbEditable`, asked of the board's row; `script` as `useTaskDetailDerived` reads it) holds its
       ▾'s width while loading. */
    const pickerPending = cachedRow !== undefined
      && savedProjectCrumbEditable(canEditRow(cachedRow), cachedRow.persona === LANE_PERSONA, cachedRow.parent_task_id)
    const loadingCrumbs = isCreate || !id ? crumbs : cachedRow ? (
      <TaskBreadcrumb
        projects={read.projects}
        projectId={cachedRow.project_id}
        onProjectChange={NO_OP}
        projectEditable={false}
        projectPending={read.projectsPending}
        projectLabel={named?.project_name ?? undefined}
        pickerPending={pickerPending}
        taskId={cachedRow.id}
        title={cachedRow.title}
        onTitleChange={NO_OP}
        titleEditable={false}
        featureId={cachedRow.feature_id ?? null}
        featureLabel={featureName(cachedRow.feature_id, filing.taskTree) ?? named?.feature_name ?? undefined}
        featureIcon={featureIconFor(filing.taskTree, cachedRow.feature_id ?? null)}
      />
    ) : <TaskTrailSkeleton id={id} />
    /* THE PHONE'S ORG-SCOPE CHIP: the create surface draws it on its crumb row (`TaskCreateView`), so its loading trail
       does too; a task's page carries it in its ⋯ (`useStripStandIn` above), off the row Back and the trail share. */
    const loadingTrail = isCreate ? (
      <Box sx={LOADING_TRAIL_SX}>
        <Box sx={{ minWidth: 0, maxWidth: '100%', flex: `1 1 ${grid(26.5)}` }}>{loadingCrumbs}</Box>
        <OrgScopeInline />
      </Box>
    ) : loadingCrumbs
    // The create surface keeps the plain status: its shell is the form's, one tab, drawn once the roster lands.
    if (isCreate) return <DetailStatus trail={loadingTrail} loading />
    /* THE PAGE'S SHELL WHILE IT LOADS, not a bare "Loading…" under the trail — single-homed in
       `TaskPageSkeleton`, which the shell's task pane also draws while this chunk downloads. A cold open from the board
       heads it with the board's row whole — trail, org chip and an inert state pill, drawn as the loaded header draws
       them (`TaskDetailHeader`), so the same row lands with no swap; the actions, tabs and body wait for the page's read. */
    if (!cachedRow) return <TaskPageSkeleton trail={loadingTrail} />
    return (
      <TaskPageSkeleton
        trail={loadingTrail}
        org={taskOrgChip(cachedRow, admin)}
        state={<TaskStatusIndicator task={cachedRow} name={deescapeMarkdownText(cachedRow.title)} waitReasonHover iconOnly />}
      />
    )
  }

  // ── /tasks/new (A1b/AC1c.1, AD-13) ──────────────────────────────────────────────────────────────
  // Same shell and form, ONE tab. After the loading branch (the roster must land), before not-found/error.
  if (isCreate) {
    return (
      <TaskCreateView
        pageRef={pageRef}
        taskCrumbs={taskCrumbs}
        isChat={isChat}
        prefilledBody={nav.prefilledBody}
        createDefaults={createDefaults}
        read={read}
        filing={filing}
      />
    )
  }

  if (notFound || error || !task) {
    return (
      <DetailStatus
        trail={crumbs}
        notFound={notFound && 'Task not found'}
        error={error || 'Failed to load task detail'}
        locate={{ objectType: 'task', objectId: id }}
      />
    )
  }

  // AC6.3's "bare number then the title" is the trail's job; the header carries trail + state badge.
  return (
    /* A row whose DELETE is in flight: no panel re-reads it on its own delete event (`LiveRefetchPausedContext`). */
    <LiveRefetchPausedContext.Provider value={read.deleting}>
      <DetailPage
        ref={pageRef}
        measure="fill"
        testId="task-page"
        headerTestId="task-page-header"
        breadcrumb={narrow ? (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }}>
            <Button size="small" onClick={goBack} data-testid="task-page-back" sx={{ minWidth: 0, minHeight: SIZE.tapTarget, flexShrink: 0 }}>
              Back
            </Button>
            <Box sx={{ minWidth: 0, flex: 1 }}>{taskCrumbs}</Box>
          </Box>
        ) : taskCrumbs}
        actions={<TaskDetailHeaderActions task={task} org={taskOrgChip(task, admin)} viewer={viewer} canRunThisTask={canRunThisTask} derived={derived} tabState={tabState} actions={actions} />}
        sx={PAGE_SX}
      >
        {otherOrg && (
          <Box sx={{ flex: '0 0 auto' }}>
            <ActingForNotice org={otherOrg} testId="task-page-acting-for" />
          </Box>
        )}
        <TaskDetailTabStrip task={task} canVote={admin != null} tabState={tabState} />

        {/* The Details tab's Delete/Archive confirm; portalled, at the page root. */}
        {actions.confirmDialog}

        {actions.preemptDialog}
        {startRefusedToast}

        <TaskDetailBanners
          task={task}
          canRunThisTask={canRunThisTask}
          unheardAnswer={tabState.hasQA ? null : tabState.stalledAnswer}
          read={read}
          derived={derived}
          actions={actions}
        />

        <TaskBodyPanel
          current={activeTab}
          task={read.formTask ?? task}
          liveTask={task}
          script={script}
          projects={read.projects}
          canEditThisTask={canEditThisTask}
          canRunThisTask={canRunThisTask}
          filing={filing}
          actions={actions}
          onDirtyChange={read.setFormDirty}
          onCancel={() => navigate('/tasks')}
          canVote={admin != null}
          tabState={tabState}
        />
        {/* Guarded like the strip, so no hidden panel dangles an `aria-labelledby`. */}
        {tabState.hasSubtasks && <TaskSubtasksPanel current={activeTab} task={task} />}
        {tabState.hasArtifacts && (
          <TaskArtifactsPanel current={activeTab} task={task} artifacts={artifacts} everShown={read.artifactsEverShown} />
        )}
        {tabState.hasQA && (
          <TaskQAPanel
            current={activeTab}
            task={task}
            gated={tabState.gated}
            activity={activity}
            canRunThisTask={canRunThisTask}
            stalledAnswer={tabState.stalledAnswer}
            actions={actions}
          />
        )}
        <TaskCommentsPanel
          current={activeTab}
          task={task}
          readOnly={viewer}
          onCountChange={tabState.setCommentCount}
        />
        {tabState.hasAgentSessions && (
          <TaskAgentPanel
            current={activeTab}
            task={task}
            inChat={derived.consoleChatTask === task.id}
            onOpenInChat={actions.openRunnerChat}
          />
        )}
        {tabState.hasRouting && (
          <TaskRoutingPanel
            current={activeTab}
            task={task}
            onAccepted={() => void read.load()}
            onChange={() => tabState.selectTab('task')}
          />
        )}
        <TaskDetailsTabPanel
          current={activeTab}
          task={task}
          activity={activity}
          runMetrics={runMetrics}
          admin={admin}
          viewer={viewer}
          script={script}
          formDirty={read.formDirty}
          gateLogHref={derived.gateLogHref}
          access={access}
          derived={derived}
          tabState={tabState}
          actions={actions}
        />
      </DetailPage>
    </LiveRefetchPausedContext.Provider>
  )
}
