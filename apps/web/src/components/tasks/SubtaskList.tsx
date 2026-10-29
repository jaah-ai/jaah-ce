import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { useMinuteTick } from '../../hooks/useMinuteTick'
import { toggled } from '../../lib/sets'
import { taskRowElementId } from '../../lib/taskDeepLink'
import type { PipelineTask } from '../../types'
import FeedList from '../../ui/feed/FeedList'
import type { FeedSection } from '../../ui/feed/types'
import { TASK_BOARD_CONTAINER_SX } from '../../ui/listStyles'
import { useOrgVisible } from '../../ui/OrgIndicator'
import { buildFeedTaskRow } from './listViews/feedRow'
import { toViewRow } from './listViews/rows'
import { epicChildGroups, prepareChildTitle } from './shared'

/* THE TASK PAGE'S SUBTASKS TAB: a task's children, one folding section per phase (`epicChildGroups`, the single
 * home for what a phase is), each child drawn as the board's own Activity row (`buildFeedTaskRow`) so a subtask
 * reads the same here as on `/tasks`. Read-only: no handler is passed, so no row offers a verb. Every phase
 * starts OPEN — the tab exists to show subtasks. The host keys this on the task id, so a navigation to another
 * task starts unfolded. The list is its OWN board query container: the row's slots (persona, cost, rating, org,
 * author) gate on `BOARD_NO_*`, and gated on a wider ancestor they would starve the title in a narrow pane. */
export default function SubtaskList({ task }: { task: PipelineTask }) {
  const navigate = useNavigate()
  const orgVisible = useOrgVisible()
  // The rows' ages are computed from `Date.now()` in `toViewRow`.
  useMinuteTick()
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())

  const sections: FeedSection[] = epicChildGroups(task.children ?? []).map(group => ({
    key: group.key,
    label: group.label,
    count: group.tasks.length,
    collapsed: collapsed.has(group.key),
    onToggle: () => setCollapsed(prev => toggled(prev, group.key)),
    rows: group.tasks.map(child => {
      const viewRow = toViewRow(child, task)
      // A Prepare stage's stored title is its epic's own; `<Stage>: <epic>` tells the stages apart.
      const title = prepareChildTitle(child, task.title)
      return buildFeedTaskRow({
        row: title === viewRow.title ? viewRow : { ...viewRow, title },
        depth: 0,
        leaf: true,
        deepLinked: false,
        elementId: taskRowElementId(child.id),
        handlers: {},
        busy: false,
        fetching: false,
        navigate,
        orgVisible,
      })
    }),
  }))

  return (
    <Box data-testid="subtask-list" sx={TASK_BOARD_CONTAINER_SX}>
      <FeedList sections={sections} ariaLabel="Subtasks" emptyMessage="No subtasks." />
    </Box>
  )
}
