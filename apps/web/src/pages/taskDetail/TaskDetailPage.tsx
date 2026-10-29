// The Task page at /tasks/:id (and /tasks/new): the route-mode switch composing the page's hooks and sections.
import { useNavigate, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import { useAuth } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import TaskBreadcrumb from '../../components/tasks/TaskBreadcrumb'
import { featureName } from '../../components/tasks/featureFiling'
import { featureIconFor } from '../../lib/featureIcon'
import { LANE_PERSONA } from '../../components/tasks/laneRun'
import MoveToFeatureDialog from '../../components/tasks/MoveToFeatureDialog'
import WaitOnCustomerDialog from '../../components/tasks/WaitOnCustomerDialog'
import { useTaskCreateDefaults } from '../../components/tasks/taskDefaults'
import { useCanEditTaskRow } from '../../hooks/useTaskAccess'
import { LiveRefetchPausedContext } from '../../hooks/useLiveRefetch'
import { useViewerMode } from '../../lib/viewerMode'
import DetailStatus from '../../ui/detail/DetailStatus'
import { DetailPage } from '../../ui/detail/DetailPage'
import { ActingForNotice } from '../../ui/OrgIndicator'
import { OrgScopeInline } from '../../ui/OrgScopeControl'
import TaskActivityPanel from './TaskActivityPanel'
import TaskCreateView from './TaskCreateView'
import TaskDetailBanners from './TaskDetailBanners'
import { TaskDetailCrumbs, TaskDetailHeaderActions, taskTitleAside } from './TaskDetailHeader'
import TaskDetailsPanel from './TaskDetailsPanel'
import {
  TaskAgentPanel,
  TaskAnswerPanel,
  TaskArtifactsPanel,
  TaskBodyPanel,
  TaskCommentsPanel,
  TaskDetailTabStrip,
  TaskMetricsPanel,
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
import { useTaskPageViewport } from './useTaskPageViewport'

// The Task page at /tasks/:id: FULL-HEIGHT, TABBED, NON-SCROLLING — header and tab strip fixed, the
// active panel takes the rest of `.app-main`.
//
// Tabs use the shared `components/Tabs` shell. Keep-mounted `[hidden]` panels are LOAD-BEARING: an
// unmount-on-switch would discard an in-progress gate answer or description edit.
//
//   answer    (conditional, FIRST, auto-selected) the gate form, iff the task is at a gate
//   task      the Description and the task's settings — content only
//   subtasks  (conditional) the tree table in page mode
//   artifacts (conditional) runner-handed documents (sandboxed HTML, markdown)
//   details   everything ABOUT the run: fields, runner, unverified-done warning, Outcome, run metrics,
//             Finisher notes, Blocks, attachments
//   agent     (conditional) the ACP run's live event stream (`TaskActivity`)
//   activity  append-only histories: Timeline · Q&A · Versions
//
// Panels FIT rather than scroll: exactly FOUR panel-level scrollports are sanctioned (Subtasks rows,
// Description, the Activity feed — one port shared by its subtabs — and the Agent tab's live event stream),
// each unbounded by construction.
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
  /* The public Live Cast viewer (`lib/viewerMode.ts`) sees this page as a console `tasks:view` reader does, minus
     the comment composer and the operator's exceptions: people, cross-task edges, the compliance
     manifest. The write controls are already gone by permission (no `AuthProvider` there). */
  const viewer = useViewerMode()
  const nav = useTaskDetailNavState(isCreate, isChat)
  /* The operator's `task_default_*` settings for the CREATE surface (fences single-homed in
     `components/tasks/taskDefaults`). `loaded` gates the HANDOVER, not the render, so the form seeds
     once from real values; the page never waits. */
  const createDefaults = useTaskCreateDefaults(isCreate)
  const read = useTaskDetailLoad({ id, isCreate, duplicateOf: nav.duplicateOf })
  const { task, activity, artifacts, loading, notFound, error, runMetricsId, runMetrics } = read
  const access = useTaskDetailAccess(task)
  const { otherOrg, canEditThisTask, canRunThisTask } = access
  // The loading trail asks it of the board's row: will its crumbs be pickers once loaded?
  const canEditRow = useCanEditTaskRow()
  const pageRef = useTaskPageViewport({ hasTask: task !== null, isCreate, loading, settled: runMetrics.settled })
  const derived = useTaskDetailDerived({
    task, activity, artifacts, projects: read.projects, worktrees: read.worktrees, isCreate, deleting: read.deleting, otherOrg,
  })
  const { script } = derived
  const tabState = useTaskDetailTabs({
    task, isCreate, artifacts, runMetrics, setArtifactsEverShown: read.setArtifactsEverShown, canRunThisTask, script,
    formDirty: read.formDirty,
  })
  const { activeTab } = tabState
  const filing = useTaskDetailFiling({
    id, isCreate, task, formTask: read.formTask, setTask: read.setTask, formDirty: read.formDirty, cachedRow: read.cachedRow, projects: read.projects, loading, keepsHandedRead: read.keepsHandedRead, otherOrg, nav,
    canEditThisTask, script,
  })
  const actions = useTaskDetailActions({
    id, isCreate, viewer, read, access, derived, ratingFolded: tabState.ratingFolded, setRatingOpen: tabState.setRatingOpen,
    selectTab: tabState.selectTab, titleEditable: filing.titleEditable, onTitleRegenerated: filing.setDraftTitle,
  })

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
       does too; a task's page leaves it to the shell's strip above the page, where it no longer
       crowds a long title's trail onto three lines. */
    const loadingTrail = isCreate ? (
      <Box sx={LOADING_TRAIL_SX}>
        <Box sx={{ minWidth: 0, maxWidth: '100%', flex: '1 1 10rem' }}>{loadingCrumbs}</Box>
        <OrgScopeInline />
      </Box>
    ) : loadingCrumbs
    // The create surface keeps the plain status: its shell is the form's, one tab, drawn once the roster lands.
    if (isCreate) return <DetailStatus trail={loadingTrail} loading />
    /* THE PAGE'S SHELL WHILE IT LOADS, not a bare "Loading…" under the trail — single-homed in
       `TaskPageSkeleton`, which the shell's task pane also draws while this chunk downloads. */
    return <TaskPageSkeleton trail={loadingTrail} />
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
        breadcrumb={taskCrumbs}
        titleAside={taskTitleAside(task, admin)}
        actions={<TaskDetailHeaderActions task={task} viewer={viewer} canRunThisTask={canRunThisTask} derived={derived} tabState={tabState} actions={actions} />}
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

        <MoveToFeatureDialog
          task={actions.filingOpen ? task : null}
          tree={filing.taskTree}
          onClose={() => actions.setFilingOpen(false)}
          onMoved={actions.holdUpdated}
        />
        <WaitOnCustomerDialog
          task={actions.waitingOpen ? task : null}
          onClose={() => actions.setWaitingOpen(false)}
          onDone={actions.holdUpdated}
        />
        {actions.preemptDialog}

        <TaskDetailBanners
          task={task}
          canRunThisTask={canRunThisTask}
          stalledAnswer={tabState.stalledAnswer}
          read={read}
          derived={derived}
          actions={actions}
        />

        {tabState.gated && (
          <TaskAnswerPanel current={activeTab} task={task} activity={activity} canRunThisTask={canRunThisTask} actions={actions} />
        )}
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
          onOpenAnswer={tabState.answerable ? () => tabState.selectTab('answer') : undefined}
        />
        {/* Guarded like the strip, so no hidden panel dangles an `aria-labelledby`. */}
        {tabState.hasSubtasks && <TaskSubtasksPanel current={activeTab} task={task} />}
        {tabState.hasArtifacts && (
          <TaskArtifactsPanel current={activeTab} task={task} artifacts={artifacts} everShown={read.artifactsEverShown} />
        )}
        <TaskCommentsPanel
          current={activeTab}
          task={task}
          readOnly={viewer}
          onCountChange={tabState.setCommentCount}
        />
        <TaskDetailsPanel
          current={activeTab}
          task={task}
          admin={admin}
          viewer={viewer}
          script={script}
          formDirty={read.formDirty}
          access={access}
          derived={derived}
          tabState={tabState}
          actions={actions}
        />
        {tabState.hasAgentSessions && (
          <TaskAgentPanel
            current={activeTab}
            task={task}
            inChat={derived.consoleChatTask === task.id}
            onOpenInChat={actions.openRunnerChat}
          />
        )}
        {tabState.hasRunMetrics && <TaskMetricsPanel current={activeTab} runMetrics={runMetrics} />}
        <TaskActivityPanel
          current={activeTab}
          task={task}
          activity={activity}
          script={script}
          gateLogHref={derived.gateLogHref}
          canRunThisTask={canRunThisTask}
          tabState={tabState}
          actions={actions}
        />
      </DetailPage>
    </LiveRefetchPausedContext.Provider>
  )
}
