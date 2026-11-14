// The Task page's Details tab: everything ABOUT the run — Summary · Outcome · Metrics · Timeline · Versions · Review, each drawn only with
// something to show (`useTaskDetailTabs`).
import Box from '@mui/material/Box'
import { TabPanel, Tabs } from '../../components/Tabs'
import type { RunMetricsState } from '../../components/tasks/TaskRunMetrics'
import TaskVersions from '../../components/tasks/TaskVersions'
import type { Admin } from '../../auth'
import type { PipelineTask } from '../../types'
import { DetailSection } from '../../ui/detail/DetailPage'
import ActivityLog, { type ActivityLogEntry } from './ActivityLog'
import { TaskMetricsPanel } from './TaskDetailTabs'
import TaskOutcomePanel from './TaskOutcomePanel'
import TaskReviewFindingsPanel from './TaskReviewFindingsPanel'
import TaskSummaryPanel from './TaskSummaryPanel'
import { FEED_SX, type TaskTab } from './taskDetailConstants'
import type { TaskDetailAccess } from './useTaskDetailAccess'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { TaskDetailDerived } from './useTaskDetailDerived'
import type { TaskDetailTabsState } from './useTaskDetailTabs'

/* The two append-only histories (Timeline, Versions) SHARE the one `task-page-feed` scrollport (only one is visible),
   keeping the fit doctrine's single panel-level port; Summary and Metrics are sections, as on any panel. `lazy`
   (keyed by task): nothing here mounts until Details is first opened; what the header and strip need of it (the
   which subtabs exist; the Timeline's control-plane notices) is read by the page (`useTaskDetailTabs`). */
export default function TaskDetailsTabPanel({ current, task, activity, runMetrics, admin, viewer, script, formDirty, gateLogHref, access, derived, tabState, actions }: {
  current: TaskTab
  task: PipelineTask
  activity: ActivityLogEntry[]
  runMetrics: RunMetricsState
  admin: Admin | null
  viewer: boolean
  script: boolean
  formDirty: boolean
  gateLogHref: string | null
  access: TaskDetailAccess
  derived: TaskDetailDerived
  tabState: TaskDetailTabsState
  actions: TaskDetailActions
}) {
  const { detailsSubtabs, detailsSubtab, setDetailsSubtab, hasOutcome, outcomes, hasRunMetrics, hasTimeline, notices, hasVersions, bodyVersions, hasFindings, findings } = tabState
  const feedShown = detailsSubtab === 'timeline' || detailsSubtab === 'versions'
  return (
    <TabPanel key={task.id} id="details" current={current} idPrefix="task" lazy>
      <Tabs tabs={detailsSubtabs} tab={detailsSubtab} onSelect={setDetailsSubtab} label="Task details" idPrefix="details" />

      <TaskSummaryPanel
        current={detailsSubtab}
        task={task}
        admin={admin}
        viewer={viewer}
        script={script}
        formDirty={formDirty}
        access={access}
        derived={derived}
        tabState={tabState}
        actions={actions}
      />
      {/* Guarded like the strip, so no hidden panel dangles an `aria-labelledby`. */}
      {hasOutcome && <TaskOutcomePanel current={detailsSubtab} read={outcomes} />}
      {hasRunMetrics && <TaskMetricsPanel current={detailsSubtab} runMetrics={runMetrics} />}

      <Box data-details-feed hidden={!feedShown}>
        <DetailSection testId="task-page-panel" fill>
          {/* SANCTIONED SCROLLPORT 3 of 4 (`FEED_SX`). */}
          <Box data-testid="task-page-feed" sx={FEED_SX}>
            {hasTimeline && (
              <TabPanel id="timeline" current={detailsSubtab} idPrefix="details">
                <ActivityLog
                  entries={activity}
                  notices={notices}
                  epicId={task.id}
                  childTasks={task.children ?? []}
                  projectId={task.project_id}
                  onOpenDetails={() => setDetailsSubtab('summary')}
                  onOpenVersions={() => setDetailsSubtab('versions')}
                  hasVersions={hasVersions}
                  script={script}
                  gateLogHref={gateLogHref}
                  currentState={task.state}
                />
              </TabPanel>
            )}

            {/* The description's version history (read-only), still written by every save and refresh. The
                read is the page's (`useTaskDetailTabs`), handed down; it mounts for `tasks:view` readers too. */}
            {hasVersions && (
              <TabPanel id="versions" current={detailsSubtab} idPrefix="details">
                <TaskVersions task={task} read={bodyVersions} />
              </TabPanel>
            )}
          </Box>
        </DetailSection>
      </Box>
      {hasFindings && <TaskReviewFindingsPanel current={detailsSubtab} read={findings} />}
    </TabPanel>
  )
}
