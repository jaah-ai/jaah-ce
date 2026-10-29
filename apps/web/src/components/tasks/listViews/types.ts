/* The `/tasks` board's one list renderer, the Activity feed (`./ActivityFeedView.tsx`): its props and the
 * bundles the host hands it. Every field below `deepLinkedId` is OPTIONAL: an ABSENT field is the permission
 * gate (no handler, no affordance).
 */
import type { PipelineTask, TaskTree, TaskTreeCounts } from '../../../types'
import type { ExpandedEntry } from '../shared'
import type { InlineTaskCreateInput } from '../useInlineTaskCreate'
import type { TaskTreeGrouping } from '../taskTree'
import type { TaskRowActionHandlers } from '../rowActions'
import type { PickStore } from '../../../lib/pickStore'

/** The board's pick for its bulk verbs (`pages/Tasks.tsx`): which top-level rows may be picked, which are (`store`,
 *  which each tick reads for its own row — `lib/pickStore.ts` says why), whether any is (`picking`), and
 *  `onToggle` to change one. `onDrawn` hands the
 *  host every pickable row the feed draws NOW, so a pick that left the board (deleted, started, collapsed away)
 *  is dropped and the rest act on fresh rows. */
export type PaneSelection = {
  store: PickStore<PipelineTask>
  picking: boolean
  isSelectable: (task: PipelineTask) => boolean
  onToggle: (task: PipelineTask) => void
  onDrawn: (tasks: readonly PipelineTask[]) => void
}

/* The board's `TaskTreeGrouping`, aliased (not re-declared): two copies of a discriminated union
   could accept shapes the flattener rejects. */
export type TaskListGrouping = TaskTreeGrouping

/* Group by State's two server-backed facts: the section totals
   (`GET /api/tasks/counts`, so a header agrees with the rail) and Done's fold
   (`task_done_expanded`). */
export type TaskListStateGrouping = {
  /** `undefined` until the counts land: a header then shows no number rather than the loaded window. */
  counts: TaskTreeCounts | undefined
  doneExpanded: boolean
  /** Absent = Done does not fold (a principal who cannot write preferences). */
  onToggleDone?: () => void
}

/* The inline create row's bundle — `InlineTaskCreateInput`, documented there; a view only forwards it.
   All-or-nothing: its ABSENCE is the `tasks:create` gate (`POST /api/tasks` needs a key `tasks:view` does not
   imply). */
export type TaskListCreate = InlineTaskCreateInput

/* The feed's props. `tasks` is the page's already-filtered top-level rows, never a second fetch. */
export type TaskListViewProps = {
  tasks: PipelineTask[]
  loading?: boolean
  /** The deep-linked row, highlighted. */
  deepLinkedId?: number | null

  /** The board's sectioning: a built-in (`state`, `feature`, `recency`, …) or a tag-group id
   *  (`lib/taskGrouping.ts`). */
  groupBy?: string
  /** Present only while `groupBy` names a tag group the catalog can speak for. The host MEMOIZES it:
   *  views memoize sectioning on it and the recency cut reads `Date.now()`. Hence the section toggle is
   *  a sibling prop, not a member. */
  grouping?: TaskListGrouping
  /** Row verbs + the two worktree resolvers (`TaskRowActionHandlers`); absent withholds. */
  actions?: TaskRowActionHandlers
  /** Rows with a write in flight — their controls go busy. */
  submitting?: ReadonlySet<number>
  /** Open epics and their fetched detail (or error); the host owns fetch and persistence. */
  expanded?: Record<number, ExpandedEntry>
  /** Epics whose detail fetch is in flight. */
  fetching?: Set<number>
  /** Whether the board is focused: narrow an expanded epic's CHILDREN (top-level `tasks` are filtered server-side — `setFocusParam`, `components/tasks/shared.tsx`).
   *  Read through `isFocusRow` (../shared). */
  focus?: boolean
  /** Whether any row-removing filter is on (the URL's or Focus): an empty list then says the filters emptied
   *  it, in a sentence, rather than that there is nothing. */
  filtered?: boolean
  onToggleTask?: (id: number) => void
  /** Present while `groupBy` is State; absent, State sections still draw, without totals and with Done open. Done
   *  folds only while `onToggleDone` is present. */
  stateGrouping?: TaskListStateGrouping
  /** The rail's tree: names and orders Feature sections. */
  tree?: TaskTree
  /** A tag section's fold (`''` = Untagged) with current state. Absent = no fold. */
  onToggleSection?: (tagId: string, collapsed: boolean) => void
  /** The inline create affordance, or absent for a principal who may not create. */
  create?: TaskListCreate
  /** The board's pick for its bulk verbs (`PaneSelection`). Absent = no ticks. The feed ticks the TOP-LEVEL rows
   *  it draws for which `isSelectable` answers, and reports them through `onDrawn` so a pick folded or scrolled
   *  out of sight is dropped. */
  selection?: PaneSelection
}
