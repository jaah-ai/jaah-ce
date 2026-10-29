// The Task page's Activity tab: its Timeline · Q&A · Versions subtabs over one shared feed scrollport.
import Box from '@mui/material/Box'
import { TabPanel, Tabs } from '../../components/Tabs'
// One Q&A trail component shared by the Answer tab's History and Activity › Q&A.
import QATrail from '../../components/tasks/QATrail'
import { isChatTask } from '../../components/tasks/taskPersona'
import TaskVersions from '../../components/tasks/TaskVersions'
import type { PipelineTask } from '../../types'
import { DetailSection } from '../../ui/detail/DetailPage'
import EmptyState from '../../ui/EmptyState'
import ActivityLog, { type ActivityLogEntry } from './ActivityLog'
import { ACTIVITY_TABS, type TaskTab } from './taskDetailConstants'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { TaskDetailTabsState } from './useTaskDetailTabs'

/* Three append-only histories, one subtab each, so the long feed does not bury the short trails. They SHARE the one
   `task-page-feed` scrollport (only one is visible), keeping the fit doctrine's single panel-level port. `timeline`
   is the default. */
export default function TaskActivityPanel({ current, task, activity, script, gateLogHref, canRunThisTask, tabState, actions }: {
  current: TaskTab
  task: PipelineTask
  activity: ActivityLogEntry[]
  script: boolean
  gateLogHref: string | null
  canRunThisTask: boolean
  tabState: TaskDetailTabsState
  actions: TaskDetailActions
}) {
  const { activityTab, setActivityTab, selectTab } = tabState
  const { submitting, reviseError, submitReviseQuestion } = actions
  const children = task.children ?? []
  const qaEntries = task.qa_entries ?? []
  return (
    <TabPanel id="activity" current={current} idPrefix="task">
      <DetailSection testId="task-page-panel" fill>
        <Tabs
          tabs={ACTIVITY_TABS}
          tab={activityTab}
          onSelect={setActivityTab}
          label="Task history"
          idPrefix="history"
        />

        {/* SANCTIONED SCROLLPORT 3 of 4: an append-only audit feed is unbounded by construction.
            No `overscroll-behavior: contain`: at phone width this grows to its content and the shell
            is what scrolls, and a contained non-scroller swallows that gesture. See PAGE_SX's
            "WHY NO SCROLLPORT ON THIS PAGE CONTAINS ITS OVERSCROLL". */}
        <Box
          data-testid="task-page-feed"
          sx={{
            display: 'flex',
            flexDirection: 'column',
            gap: 1.75,
            flex: '1 1 auto',
            minHeight: '8rem',
            overflowY: 'auto',
          }}
        >
          <TabPanel id="timeline" current={activityTab} idPrefix="history">
            <ActivityLog
              entries={activity}
              epicId={task.id}
              childTasks={children}
              projectId={task.project_id}
              onOpenDetails={() => selectTab('details')}
              script={script}
              gateLogHref={gateLogHref}
              currentState={task.state}
            />
          </TabPanel>

          {/* The DB-sourced record of what was asked and how it was settled (the live gate is on `answer`). */}
          <TabPanel id="qa" current={activityTab} idPrefix="history">
            {qaEntries.length > 0
              ? (
                <QATrail
                  entries={qaEntries}
                  /* Live rows beside the snapshots; absent on the wire = no rounds. */
                  questions={task.questions ?? []}
                  submitting={submitting}
                  error={reviseError}
                  /* `canRunThisTask` (reopening is a RUN verb), not `answerable` (which folds in `gated`,
                     false exactly when every round is settled and Revise must work). Not for chats (the
                     server 409s). */
                  onRevise={canRunThisTask && !isChatTask(task) ? submitReviseQuestion : undefined}
                />
              )
              : <EmptyState title="No questions yet." />}
          </TabPanel>

          {/* The description's version history (read-only), still written by every save and refresh. The
              app's ONLY reader of `GET …/body-versions`, so it mounts for `tasks:view` readers too. */}
          <TabPanel id="versions" current={activityTab} idPrefix="history">
            <TaskVersions task={task} />
          </TabPanel>
        </Box>
      </DetailSection>
    </TabPanel>
  )
}
