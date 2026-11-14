// The Task page's header: the breadcrumb trail (its `<h1>`) and the action cluster, the record's org chip in it.
import { useRef, type ComponentType, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import { Link as RouterLink } from 'react-router-dom'
import { get } from '../../api'
import { currentOrgId, type Admin } from '../../auth'
import { featureIconFor } from '../../lib/featureIcon'
import { featureName } from '../../components/tasks/featureFiling'
import RatingBadge from '../../components/tasks/RatingBadge'
import SuggestionActions from '../../components/tasks/SuggestionActions'
import { RowActionsOverflow } from '../../components/tasks/rowActions'
import { deescapeMarkdownText, TaskStatusIndicator } from '../../components/tasks/shared'
import TaskBreadcrumb from '../../components/tasks/TaskBreadcrumb'
import { TaskLiveCastControl } from '../../lib/liveCastSurface'
import type { PipelineTask } from '../../types'
import { OrgAside } from '../../ui/OrgIndicator'
import { useOrgScopeOverflowItem } from '../../ui/OrgScopeControl'
import { ConsoleIcon } from '../../ui/icons'
import TaskIncidentChip from './TaskIncidentChip'
import TrackerIssueChip from '../../components/trackers/TrackerIssueChip'
import { SIZE } from '../../theme'
import { PHONE } from '../../ui/listStyles'
import { PAGE_OVERFLOW_SX } from './taskDetailConstants'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { TaskDetailDerived } from './useTaskDetailDerived'
import type { TaskDetailFiling } from './useTaskDetailFiling'
import type { TaskTrailNames } from './useTaskDetailLoad'
import type { TaskDetailTabsState } from './useTaskDetailTabs'
import Tip from '../../ui/Tip'
import { editionModule } from '../../lib/editionModule'
import { isEnabled } from '../../lib/editionFeatures'

/* The hosted app's "Live" chip (`components/hostedApps/TaskLiveLink.tsx`, `easy_mode_hosting`). */
const TaskLiveLink = editionModule(isEnabled('easy_mode_hosting'),
  () => import.meta.glob<ComponentType<{ link: PipelineTask['live_link'] }>>(
    '../../components/hostedApps/TaskLiveLink.tsx', { eager: true, import: 'default' }))

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
    crumbProjects, stagedProjectId, stageProject, projectCrumbEditable, crumbPickRefusal, draftTitle, onTitleChange,
    titleEditable, stagedFeatureId, taskTree, filingFeatures, stageFeature, moveError, openNewFeature,
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
          ? crumbProjects.filter(p => p.id === stagedProjectId || crumbPickRefusal(p) === null)
          : crumbProjects}
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
        onNewFeature={openNewFeature}
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

/** The record's org chip, drawn in the header's cluster before the state pill (on the title's row, not under the trail
 * where a title-less header's aside wrapped to a second line in the side pane), or `null` when it would not draw. The
 * loading skeleton draws it in the same place (`TaskPageSkeleton`'s `org`), so nothing shifts when the page lands. */
export function taskOrgChip(task: PipelineTask, admin: Admin | null): ReactNode {
  /* The RECORD's own tenant. `task.org` only: a null org is a real state (`types.ts::OrgRef`), never
     backfilled from `admin.org`; it draws no chip (a bare "Org —" read as part of the title). Not when the record is
     the viewer's OWN org: the chip would only repeat where they already stand. `hideOnPhone`: mobile web must not
     show the org name. */
  if (!task.org || (admin && task.org.id === currentOrgId(admin))) return null
  return <OrgAside label="Org" org={task.org} testId="task-page-org" chipTestId="task-page-org-chip" hideOnPhone />
}

/** The cluster beside the state it answers: Live Cast, the folded rating, the record's org, the state pill, the
 * console door and the ⋯. */
export function TaskDetailHeaderActions({ task, org, viewer, canRunThisTask, derived, tabState, actions }: {
  task: PipelineTask
  /** `taskOrgChip`'s chip, or `null`. */
  org: ReactNode
  viewer: boolean
  /** May this viewer act on the row (`useTaskDetailAccess`)? The console door is theirs alone. */
  canRunThisTask: boolean
  derived: TaskDetailDerived
  tabState: TaskDetailTabsState
  actions: TaskDetailActions
}) {
  const { liveCastAsked, liveCastOrg, consoleRunnerWorktree, script } = derived
  const { foldedRating } = tabState
  const { pageActions, openRunnerConsole, acting, pageActionItems } = actions
  /* A ready child's epic: its hold or unmet prerequisites read Held/Waiting on the pill as on the row (`toViewRow`'s
     `parent`). Read only while it can change the word; until it lands the pill reads unrefined by it. */
  const parentId = task.parent_task_id
  const parent = useQuery({
    queryKey: ['task-parent-gate', parentId],
    queryFn: () => get<PipelineTask>(`/api/tasks/${parentId}`),
    enabled: parentId !== null && task.state === 'ready' && !script,
    retry: false,
  }).data
  // The name the trail shows (`TaskBreadcrumb`'s de-escaped title), so "More actions for …" matches the header.
  const shownTitle = deescapeMarkdownText(task.title)
  /* THE PHONE'S ORG FILTER, IN THE ⋯: the shell's strip took a whole row above Back and the trail, pushing a long
     title onto three lines; on the trail's row the chip crowded it the same way. The item names the scope. */
  const clusterRef = useRef<HTMLDivElement | null>(null)
  const orgScope = useOrgScopeOverflowItem(() => clusterRef.current)
  const overflowItems = orgScope.item ? [...pageActionItems, orgScope.item] : pageActionItems
  return (
    <Box ref={clusterRef} sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
      <TaskLiveCastControl taskId={task.id} asked={liveCastAsked} org={liveCastOrg} />
      {/* A draft or paused pill starts the task, as on the board; otherwise it opens the web console when the
          task holds a runner slot (`taskIndicatorAction`). */}
      {/* The hosted app's address: on an epic, and on a task whose change merged (`TaskOut.live_link`). */}
      {!viewer && TaskLiveLink && task.live_link && <TaskLiveLink link={task.live_link} />}
      {/* The incident this task was filed for (Production QA), when one was; another org's viewer reads none. */}
      {!viewer && <TaskIncidentChip taskId={task.id} />}
      {/* The tracker issue the task is linked to, started there or picked by hand alike. */}
      {task.tracker_issue && <TrackerIssueChip issue={task.tracker_issue} />}
      {foldedRating && foldedRating.count > 0 && (
        <RatingBadge average={foldedRating.average} count={foldedRating.count} testId="task-page-rating-badge" />
      )}
      {org}
      {/* A Jaah suggestion's Accept and Discard, before the pill they move. */}
      {pageActions && <SuggestionActions task={task} title={shownTitle} handlers={pageActions.handlers} busy={acting} />}
      {/* WHY IT WAITS rides the icon pill's hover and name: a chip beside it would split one fact in two. */}
      <TaskStatusIndicator task={task} name={shownTitle} parentTransitionMode={parent?.transition_mode} parentUnmetPrerequisites={parent?.unmet_prerequisite_count} onStart={pageActions?.onPillStart} onOpenConsole={openRunnerConsole} consoleSlot={consoleRunnerWorktree ?? undefined} waitReasonHover iconOnly />
      {/* The pill says why it waits; a hold the VIEWER can fix (their own revoked git login) gets its door here. */}
      {task.wait_fix_url && task.wait_fix_url.startsWith('/') && (
        <Button size="small" variant="outlined" component={RouterLink} to={task.wait_fix_url} data-testid="task-wait-fix">
          Reconnect
        </Button>
      )}
      {pageActions?.onStartElsewhere && (
        <Tip title={`Leave ${task.vm_hold_node} and start on another node`}><Button
          size="small"
          variant="outlined"
          disabled={acting}
          onClick={pageActions.onStartElsewhere}
          data-testid="task-start-elsewhere"
        >
          Start fresh elsewhere
        </Button></Tip>
      )}
      {/* THE CONSOLE DOOR, BESIDE THE STATE IT ANSWERS: it lived in the Details tab's Runner field,
          so from any other tab the console was a hidden press on the pill. The slot the task holds at any
          state, or its running MicroVM; a second `&& running` here would withdraw it from an Asking run. */}
      {/* Only for a viewer who may act on the row: another org's, or a read-only seat, gets no door here. */}
      {openRunnerConsole && canRunThisTask && (
        <Tip title="Open web console"><IconButton
          size="small"
          // AC6.4: the aria-label keeps the `#`.
          aria-label={`Open web console for task #${task.id}`}
          onClick={openRunnerConsole}
          data-testid="task-page-console"
          // The 44px touch floor on the phone, as the ⋯ beside it.
          sx={{ [PHONE]: { width: SIZE.tapTarget, height: SIZE.tapTarget } }}
        >
          <ConsoleIcon />
        </IconButton></Tip>
      )}
      <RowActionsOverflow name={shownTitle} busy={acting} items={overflowItems} sx={PAGE_OVERFLOW_SX} />
      {orgScope.picker}
    </Box>
  )
}
