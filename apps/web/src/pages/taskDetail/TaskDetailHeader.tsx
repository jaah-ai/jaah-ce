// The Task page's header: the breadcrumb trail (its `<h1>`), the record's org aside and the action cluster.
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import { currentOrgId, type Admin } from '../../auth'
import { featureIconFor } from '../../lib/featureIcon'
import { featureName } from '../../components/tasks/featureFiling'
import RatingBadge from '../../components/tasks/RatingBadge'
import { RowActionsOverflow } from '../../components/tasks/rowActions'
import { deescapeMarkdownText, TaskStatusIndicator } from '../../components/tasks/shared'
import TaskBreadcrumb from '../../components/tasks/TaskBreadcrumb'
import { ControlMessagesChip } from '../../components/tasks/TaskControlMessages'
import { TaskLiveCastControl } from '../../lib/liveCastSurface'
import type { PipelineTask } from '../../types'
import { OrgAside } from '../../ui/OrgIndicator'
import { ConsoleIcon } from '../../ui/icons'
import { CONTROL_MESSAGES_ANCHOR, PAGE_OVERFLOW_SX, PHONE } from './taskDetailConstants'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { TaskDetailDerived } from './useTaskDetailDerived'
import type { TaskDetailFiling } from './useTaskDetailFiling'
import type { TaskTrailNames } from './useTaskDetailLoad'
import type { TaskDetailTabsState } from './useTaskDetailTabs'

/* ── THE TRAIL IS THE TITLE ─────────────────────────────────────────────────────────────────────────
   `{Project} › [{Feature}] › {id}: {title}`, the page `<h1>` on both the create and the edit surface. */
export function TaskDetailCrumbs({ filing, task, isCreate, isChat, viewer, projectsPending, trail = null }: {
  filing: TaskDetailFiling
  task: PipelineTask | null
  isCreate: boolean
  isChat: boolean
  viewer: boolean
  projectsPending: boolean
  /** `GET …/page`'s names, for the crumbs the roster or the task tree has not resolved yet. */
  trail?: TaskTrailNames | null
}) {
  const {
    pickerProjects, stagedProjectId, stageProject, projectCrumbEditable, crumbPickRefusal, draftTitle, onTitleChange,
    titleEditable, stagedFeatureId, taskTree, filingFeatures, stageFeature, moveError,
  } = filing
  /* The trail's names answer only for the row and filing they were read for: a staged move names its target from
     the roster and the tree, as ever. */
  const named = !isCreate && trail !== null && task !== null && trail.taskId === task.id ? trail : null
  const projectLabel = named && named.projectId === stagedProjectId ? named.project_name ?? undefined : undefined
  const trailFeature = named && stagedFeatureId !== null && named.featureId === stagedFeatureId
    ? named.feature_name ?? undefined
    : undefined
  return (
    <>
      <TaskBreadcrumb
        /* A create target lists only projects this viewer may create in; a move keeps every row, its
           refusal chip saying why the one wanted is dead. */
        projects={isCreate
          ? pickerProjects.filter(p => p.id === stagedProjectId || crumbPickRefusal(p) === null)
          : pickerProjects}
        projectId={stagedProjectId}
        onProjectChange={stageProject}
        // Create resolves its project silently (the seed effect above); the picker overrides it (AC2.3
        // "no gates"), live for anyone on the create surface (already `tasks:create`-gated).
        projectEditable={projectCrumbEditable}
        // The Live Cast viewer's project is its router's root: the crumb goes back to its task list.
        projectTo={viewer ? '/' : undefined}
        projectPending={projectsPending}
        projectLabel={projectLabel}
        // Which rows the API would accept (see `crumbPickRefusal`).
        pickRefusal={crumbPickRefusal}
        taskId={isCreate ? undefined : task?.id}
        // `TaskCreate` has no `title` (derived from the body), so create surfaces name themselves; this
        // crumb IS the `<h1>`, so a chat is headed "New Chat".
        title={isCreate ? (isChat ? 'New Chat' : 'New Task') : (draftTitle ?? task?.title ?? '')}
        onTitleChange={onTitleChange}
        titleEditable={titleEditable}
        featureId={stagedFeatureId}
        featureLabel={featureName(stagedFeatureId, taskTree, filingFeatures) ?? trailFeature}
        featureIcon={featureIconFor(taskTree, stagedFeatureId)}
        features={filingFeatures}
        onFeatureChange={f => stageFeature(f?.id ?? null, f?.project_id)}
        featureEditable={projectCrumbEditable}
      />
      {/* A crumb move the server refused (`stageProject`), under the trail it reverted; cleared by the next pick. */}
      {moveError && (
        <Typography role="alert" variant="body2" data-testid="task-crumb-move-error" sx={{ color: 'error.main' }}>
          {moveError}
        </Typography>
      )}
    </>
  )
}

/** The loaded header's `titleAside`, or `undefined` when neither part would draw (the header then lays out as a
 * plain trail). */
export function taskTitleAside(task: PipelineTask, admin: Admin | null): ReactNode | undefined {
  // The record is the viewer's OWN org's: its org chip would only repeat where they already stand.
  const ownOrgRecord = Boolean(admin && task.org && task.org.id === currentOrgId(admin))
  /* The RECORD's own tenant. `task.org` only: a null org is a real state (`types.ts::OrgRef`), never
     backfilled from `admin.org`; it draws no chip (a bare "Org —" read as part of the title).
     `hideOnPhone`: mobile web must not show the org name. Not when the record is the viewer's OWN
     org: the chip would only repeat where they already stand. */
  /* No aside at all when neither part would draw (the header then lays out as a plain trail). */
  return ownOrgRecord || !task.org ? undefined : (
    <>
      {!ownOrgRecord && task.org && (
        <OrgAside
          label="Org"
          org={task.org}
          testId="task-page-org"
          chipTestId="task-page-org-chip"
          hideOnPhone
        />
      )}
    </>
  )
}

/** The cluster beside the state it answers: Live Cast, the messages chip, the folded rating, the state pill, the
 * console door and the ⋯. */
export function TaskDetailHeaderActions({ task, viewer, canRunThisTask, derived, tabState, actions }: {
  task: PipelineTask
  viewer: boolean
  /** May this viewer act on the row (`useTaskDetailAccess`)? The console door is theirs alone. */
  canRunThisTask: boolean
  derived: TaskDetailDerived
  tabState: TaskDetailTabsState
  actions: TaskDetailActions
}) {
  const { liveCastAsked, liveCastOrg, consoleRunnerWorktree } = derived
  const { controlMessageCount, selectTab, foldedRating } = tabState
  const { pageActions, openRunnerConsole, acting, pageActionItems } = actions
  // The name the trail shows (`TaskBreadcrumb`'s de-escaped title), so "More actions for …" matches the header.
  const shownTitle = deescapeMarkdownText(task.title)
  return (
    <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
      <TaskLiveCastControl taskId={task.id} asked={liveCastAsked} org={liveCastOrg} />
      {/* A draft or paused pill starts the task, as on the board; otherwise it opens the web console when the
          task holds a runner slot (`taskIndicatorAction`). */}
      {!viewer && (
        <ControlMessagesChip
          count={controlMessageCount}
          onClick={() => {
            selectTab('details')
            // After the tab's panel is shown, so the anchor has a box to scroll to.
            requestAnimationFrame(() => document.getElementById(CONTROL_MESSAGES_ANCHOR)?.scrollIntoView?.({ block: 'start' }))
          }}
        />
      )}
      {foldedRating && foldedRating.count > 0 && (
        <RatingBadge average={foldedRating.average} count={foldedRating.count} testId="task-page-rating-badge" />
      )}
      {/* WHY IT WAITS, IN THE BADGE: a hover alone hid it, and a chip beside it split one fact in two. */}
      <TaskStatusIndicator task={task} name={shownTitle} onStart={pageActions?.onPillStart} onOpenConsole={openRunnerConsole} consoleSlot={consoleRunnerWorktree ?? undefined} spellWaitReason />
      {pageActions?.onStartElsewhere && (
        <Button
          size="small"
          variant="outlined"
          disabled={acting}
          title={`Leave ${task.vm_hold_node} and start on another node`}
          onClick={pageActions.onStartElsewhere}
          data-testid="task-start-elsewhere"
        >
          Start fresh elsewhere
        </Button>
      )}
      {/* THE CONSOLE DOOR, BESIDE THE STATE IT ANSWERS: it lived in the Details tab's Runner field,
          so from any other tab the console was a hidden press on the pill. The slot the task holds at any
          state, or its running MicroVM; a second `&& running` here would withdraw it from an Asking run. */}
      {/* Only for a viewer who may act on the row: another org's, or a read-only seat, gets no door here. */}
      {openRunnerConsole && canRunThisTask && (
        <IconButton
          size="small"
          // AC6.4: the aria-label keeps the `#`.
          aria-label={`Open web console for task #${task.id}`}
          title="Open web console"
          onClick={openRunnerConsole}
          data-testid="task-page-console"
          // The 44px touch floor on the phone, as the ⋯ beside it.
          sx={{ [`@media (max-width:${PHONE - 1}px)`]: { width: 44, height: 44 } }}
        >
          <ConsoleIcon />
        </IconButton>
      )}
      <RowActionsOverflow name={shownTitle} busy={acting} items={pageActionItems} sx={PAGE_OVERFLOW_SX} />
    </Box>
  )
}
