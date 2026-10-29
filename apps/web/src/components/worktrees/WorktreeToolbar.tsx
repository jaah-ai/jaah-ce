import ToolbarMenu, {
  CheckboxPage,
  RadioPage,
  SearchPage,
  searchRowValue,
  type ToolbarMenuItem,
} from '../../ui/ToolbarMenu'
import {
  EMPTY_WORKTREE_FILTERS,
  activeWorktreeFilterCount,
  type GitFilter,
  type OccupancyFilter,
  type SessionFilter,
  type StackFilter,
  type WorktreeFilters,
} from '../../lib/worktreeFilters'
import {
  GROUP_BY_FLAT,
  GROUP_BY_NODE_PROJECT,
  GROUP_BY_PROJECT_NODE,
  type WorktreeGroupBy,
} from '../../lib/worktreeGrouping'

/* ── The Worktrees page's ONE header control ──
 *
 * Eight settings behind one wordless button via `ui/ToolbarMenu` — the same drill-down component as
 * `/tasks` (operator's instruction: re-use and improve, never copy). Level one is one fixed-height line
 * per setting, still one screenful on a phone.
 *
 * GROUPING SHARES THE MENU BUT NOT THE BADGE: the badge counts only what REMOVES ROWS; both are the same
 * gesture ("change how I read this list").
 *
 * THE OPTION LISTS COME FROM THE ROWS, NOT THE ROSTER: `nodeIds`/`projects` are facets of the unfiltered
 * slot payload. `/api/nodes` lists slotless nodes and is EMPTY for a `worktrees:view`-only reader.
 */

/* EVERY PAGE BODY (radio, checkbox, `SearchPage`) is `ui/ToolbarMenu.tsx`'s; what
   stays here is this page's data declarations, the placeholder and how THIS list matches. */

/** A multi-select's level-one value: the COUNT once past one ("4 nodes" is exact at 268px; a joined list
 *  ellipsizes into nothing). */
function manyValue<V extends string>(picked: readonly V[], labelOf: (v: V) => string, noun: string): string {
  if (picked.length === 0) return 'All'
  if (picked.length === 1) return labelOf(picked[0])
  return `${picked.length} ${noun}s`
}

/* ONE BUILDER PER PAGE SHAPE, so a setting is a line of data, not a restated `render`. */

/** A single-select setting: the row shows the picked option's word (the first option is the off state). */
function radioItem<V extends string | null>({ id, label, testId, options, value, onPick }: {
  id: string
  label: string
  testId: string
  options: { value: V; label: string; hint: string }[]
  value: V
  onPick: (next: V) => void
}): ToolbarMenuItem {
  return {
    kind: 'page',
    id,
    label,
    value: options.find(o => o.value === value)?.label ?? options[0].label,
    render: nav => (
      <RadioPage testId={testId} label={label} options={options} value={value} onPick={onPick} nav={nav} />
    ),
  }
}

/** A multi-select setting: the row shows `manyValue`'s word or count. */
function checkItem<V extends string>({ id, label, testId, options, values, onToggle, labelOf, noun, emptyNote }: {
  id: string
  label: string
  testId: string
  options: { value: V; label: string }[]
  values: readonly V[]
  onToggle: (value: V) => void
  labelOf: (v: V) => string
  noun: string
  emptyNote?: string
}): ToolbarMenuItem {
  return {
    kind: 'page',
    id,
    label,
    value: manyValue(values, labelOf, noun),
    render: () => (
      <CheckboxPage
        testId={testId}
        label={label}
        options={options}
        values={values}
        onToggle={onToggle}
        emptyNote={emptyNote}
      />
    ),
  }
}

const GROUP_OPTIONS: { value: WorktreeGroupBy; label: string; hint: string }[] = [
  {
    value: GROUP_BY_PROJECT_NODE,
    label: 'Project → node',
    hint: '',
  },
  {
    value: GROUP_BY_NODE_PROJECT,
    label: 'Node → project',
    hint: '',
  },
  {
    value: GROUP_BY_FLAT,
    label: 'Flat',
    hint: '',
  },
]

const OCCUPANCY_OPTIONS: { value: OccupancyFilter; label: string }[] = [
  { value: 'free', label: 'Free' },
  { value: 'task', label: 'Held by a task' },
  { value: 'user', label: 'Held by a person' },
]

const SESSION_OPTIONS: { value: SessionFilter | null; label: string; hint: string }[] = [
  { value: null, label: 'Any', hint: '' },
  { value: 'live', label: 'Live', hint: '' },
  { value: 'none', label: 'None', hint: '' },
]

const STACK_OPTIONS: { value: StackFilter | null; label: string; hint: string }[] = [
  { value: null, label: 'Any', hint: '' },
  { value: 'up', label: 'Up', hint: '' },
  { value: 'down', label: 'Down', hint: '' },
  {
    value: 'not-built',
    label: 'Not built',
    hint: 'A slot the node has never probed matches none of these.',
  },
]

const GIT_OPTIONS: { value: GitFilter | null; label: string; hint: string }[] = [
  { value: null, label: 'Any', hint: '' },
  { value: 'dirty', label: 'Dirty', hint: '' },
  { value: 'diverged', label: 'Diverged', hint: '' },
  {
    value: 'clean',
    label: 'Clean',
    hint: 'A tree the node has never scanned matches none of these.',
  },
]

export default function WorktreeToolbar({
  filters,
  onFiltersChange,
  groupBy,
  onGroupByChange,
  nodeIds,
  projects,
  nodeLabelFor,
}: {
  filters: WorktreeFilters
  onFiltersChange: (next: WorktreeFilters) => void
  groupBy: WorktreeGroupBy
  onGroupByChange: (next: WorktreeGroupBy) => void
  /** The facets of the UNFILTERED rows (see the header). */
  nodeIds: readonly string[]
  projects: readonly string[]
  nodeLabelFor: (nodeId: string) => string
}) {
  const patch = (next: Partial<WorktreeFilters>) => onFiltersChange({ ...filters, ...next })

  const toggle = <T extends string>(list: readonly T[], value: T): T[] =>
    list.includes(value) ? list.filter(v => v !== value) : [...list, value]

  const items: ToolbarMenuItem[] = [
    radioItem({
      id: 'group', label: 'Group by', testId: 'worktree-group-by',
      options: GROUP_OPTIONS, value: groupBy, onPick: onGroupByChange,
    }),
    checkItem({
      id: 'node', label: 'Node', testId: 'worktree-filter-node',
      options: nodeIds.map(id => ({ value: id, label: nodeLabelFor(id) })),
      values: filters.node, onToggle: value => patch({ node: toggle(filters.node, value) }),
      labelOf: nodeLabelFor, noun: 'node',
      /* A REAL STATE: facets of the loaded rows can be empty before the payload lands or for a reader
         with nothing, and blank would look broken. (Occupancy's options are a constant.) */
      emptyNote: 'Nothing to choose from — no slots are loaded.',
    }),
    checkItem({
      id: 'project', label: 'Project', testId: 'worktree-filter-project',
      options: projects.map(name => ({ value: name, label: name })),
      values: filters.project, onToggle: value => patch({ project: toggle(filters.project, value) }),
      labelOf: name => name, noun: 'project',
      emptyNote: 'Nothing to choose from — no slots are loaded.',
    }),
    checkItem({
      id: 'occupancy', label: 'Occupancy', testId: 'worktree-filter-occupancy',
      options: OCCUPANCY_OPTIONS,
      values: filters.occupancy, onToggle: value => patch({ occupancy: toggle(filters.occupancy, value) }),
      labelOf: v => OCCUPANCY_OPTIONS.find(o => o.value === v)?.label ?? v, noun: 'kind',
    }),
    radioItem({
      id: 'session', label: 'Session', testId: 'worktree-filter-session',
      options: SESSION_OPTIONS, value: filters.session, onPick: next => patch({ session: next }),
    }),
    radioItem({
      id: 'stack', label: 'Stack', testId: 'worktree-filter-stack',
      options: STACK_OPTIONS, value: filters.stack, onPick: next => patch({ stack: next }),
    }),
    radioItem({
      id: 'git', label: 'Git', testId: 'worktree-filter-git',
      options: GIT_OPTIONS, value: filters.git, onPick: next => patch({ git: next }),
    }),
    {
      kind: 'page',
      id: 'search',
      label: 'Search',
          /* The TERM itself on the row (`searchRowValue`); the header has no box. */
      value: searchRowValue(filters.q),
      render: () => (
        <SearchPage
          testId="worktree-filter-search"
          label="Search"
          value={filters.q}
          onChange={q => patch({ q })}
          placeholder="e.g. #3, task-42, node-a"
          /* THIS PAGE'S matching rule: `matchesQuery` AND-s terms over the loaded rows. */
          caption="Every word must match. Case is ignored."
        />
      ),
    },
  ]

  return (
    <ToolbarMenu
      name="Filter and view"
      heading="Filter & view"
      hint="Filter & view"
      badgeCount={activeWorktreeFilterCount(filters)}
      items={items}
      /* CLEAR DISCHARGES EXACTLY WHAT THE BADGE PROMISES: filters reset, grouping left alone. */
      onClear={() => onFiltersChange(EMPTY_WORKTREE_FILTERS)}
    />
  )
}
