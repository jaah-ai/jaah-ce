import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Switch from '@mui/material/Switch'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import {
  TASK_DATE_RANGES,
  dateRangeIsActive,
  dateRangeSummary,
  type TaskDateRangeId,
} from '../../lib/taskDateRange'
import type { TaskCreator, TaskSourceOption } from '../../types'
import { GROUP_BY_NONE, isTagGroupBy, type GroupByOption } from '../../lib/taskGrouping'
import { useOfficialZone } from '../../lib/officialZone'
import { useViewerMode } from '../../lib/viewerMode'
import { PHONE } from '../../ui/listStyles'
import ToolbarMenu, {
  CheckboxPage,
  GLYPH_BOX_SX,
  GLYPH_BUTTON_ON_SX,
  GLYPH_BUTTON_SX,
  moveWithinRadioGroup,
  PAGE_CAPTION_SX,
  PAGE_FIELD_SX,
  RadioPage,
  rovingTabIndex,
  TOOLBAR_BUTTON_SX,
  TOOLBAR_LABEL_SX,
  type ToolbarMenuItem,
} from '../../ui/ToolbarMenu'
import { creatorLabel, rosterOffersAChoice, toggleCreatorId } from '../../lib/taskCreators'
import NewButton from '../../ui/NewButton'
import { TemplatesIcon } from '../../ui/icons'
import AuthorBadge from './AuthorBadge'
import { STATE_TITLES, stateLabel } from './shared'
import { KNOWN_TASK_STATES } from '../../generated/taskTransitions'

// The `/tasks` pane header's action cluster: Focus, the Filter & view MENU and the New split button
// (New Chat is an entry in its ▾). It must fit ONE line at mobile width: each control collapses to its 44px
// glyph there (`TOOLBAR_LABEL_SX`).
// Not here: the selection (the sidebar; the phone's `pane/ScopeDropdown.tsx`), the connection indicator (one, in the Sidebar), Refresh (the SSE stream is live).
// ONE sliders button opens ONE drill-down panel: `ui/ToolbarMenu.tsx` owns its shape, focus moves, tap
// floor and `title`-not-on-screen help (and `RadioPage`/`CheckboxPage`). This file supplies only which
// settings exist, their value rows, options and the badge count. The badge counts what REMOVES ROWS from the
// menu (see `activeFilters`); each such filter is also a removable chip under the header (`pane/FilterChips.tsx`).
// Period is five pills (a pick is navigation-flavoured; a radio or select costs a step per comparison),
// labelled without numbers by operator decision; the day counts live in each `title` and the summary.
// `lib/taskDateRange.ts` single-homes the vocabulary and windows.

/* The Focus toggle is WORDLESS at every width, its own button beside the sliders (one tap for the strongest
 * filter), never a menu row. Off: `GLYPH_BUTTON_SX`; lit: `GLYPH_BUTTON_ON_SX` (`ui/ToolbarMenu.tsx`, which
 * says why the well is accent-tinted). */

/* The pulse: a centre dot inside two arcs, the "live" mark; filled centre when on, a ring when off, so the
 * glyph alone answers "is it on". `aria-hidden`: the button's name and `aria-pressed` carry it. */
function PulseIcon({ on }: { on: boolean }) {
  return (
    <Box
      component="svg"
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
      data-testid="toolbar-focus-glyph"
      data-on={on ? 'true' : 'false'}
      sx={{ width: 16, height: 16, display: 'block' }}
    >
      <Box
        component="path"
        d="M11.6 4.4a5.1 5.1 0 0 1 0 7.2M4.4 4.4a5.1 5.1 0 0 0 0 7.2"
        sx={{ fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' }}
      />
      {/* The ring keeps a 1.5 stroke: at r=2.4 a 2px stroke would make the OFF mark the heavier one. */}
      <Box
        component="circle"
        cx="8"
        cy="8"
        r={on ? 3 : 2.4}
        sx={on ? { fill: 'currentColor' } : { fill: 'none', stroke: 'currentColor', strokeWidth: 1.5 }}
      />
    </Box>
  )
}

/* The split button's halves: one pill, a hairline between them. The caret keeps the 44px phone floor. */
const SPLIT_MAIN_SX = { borderTopRightRadius: 0, borderBottomRightRadius: 0 } as const
const SPLIT_CARET_SX = {
  minWidth: 32,
  px: 0,
  borderTopLeftRadius: 0,
  borderBottomLeftRadius: 0,
  borderLeft: '1px solid rgba(var(--mui-palette-primary-contrastTextChannel) / 0.35)',
  [PHONE]: { minWidth: 44, minHeight: 44 },
} as const

/* ── PERIOD ──────────────────────────────────────────────────────────────────────────────────────── */

/* The pill row wraps: five words do not fit a 268px panel; a scroller hides options and truncation would
 * cut "Exact dates". */
const PERIOD_PILLS_SX = { display: 'flex', flexWrap: 'wrap', gap: 0.75, px: 1.5, py: 1.5 } as const

/* A real `<button>` for free keyboard access, so UA styling is reset: `appearance`, `m: 0`, and
 * `typography: 'body2'` (its longhands restore the face; `font: 'inherit'` does not). `minHeight` is 44px
 * at phone width, 32px above. */
const PERIOD_PILL_SX = {
  appearance: 'none',
  m: 0,
  font: 'inherit',
  typography: 'body2',
  lineHeight: 1,
  cursor: 'pointer',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  px: 1.25,
  minHeight: 32,
  borderRadius: 999,
  border: '1px solid',
  transition: 'background-color 120ms, border-color 120ms, color 120ms',
  /* Explicit focus ring: the stock one is low-contrast on the ACTIVE pill's `primary.main` fill. */
  '&:focus-visible': { outline: '2px solid', outlineColor: 'text.primary', outlineOffset: 2 },
  [PHONE]: { minHeight: 44, px: 1.5 },
} as const

/* The active preset is marked by fill, weight AND colour — never colour alone (WCAG 1.4.1); the fill
 * survives greyscale. `aria-checked` at the render site carries it to AT. */
const PERIOD_PILL_ACTIVE_SX = {
  fontWeight: 600,
  color: 'primary.contrastText',
  bgcolor: 'primary.main',
  borderColor: 'primary.main',
  '&:hover': { bgcolor: 'primary.dark', borderColor: 'primary.dark' },
} as const

const PERIOD_PILL_IDLE_SX = {
  fontWeight: 400,
  color: 'text.secondary',
  bgcolor: 'transparent',
  borderColor: 'divider',
  '&:hover': { bgcolor: 'action.hover', borderColor: 'text.disabled', color: 'text.primary' },
} as const

/* The two exact-date inputs stack at every width: side by side in a 268px panel each got 122px and the
 * year clipped (`08/01/20.`). Stacked each shows `08/01/2026`; the page grows ~40px only on request. */
const PERIOD_DATES_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 1,
  px: 1.5,
  pb: 1,
} as const

/* The tap floor is `PAGE_FIELD_SX`'s (`ui/ToolbarMenu.tsx`): on a native date input the finger lands on the
 * inner `<input>`, and a `minHeight` on a content-box input shrinks the date to three dots. Only layout
 * is left here. */
const PERIOD_DATE_SX = (theme: Theme) => ({
  ...PAGE_FIELD_SX(theme),
  flex: 1,
  minWidth: 0,
})

/* ── CREATOR ─────────────────────────────────────────────────────────────────── */

/* The host's half of `AuthorBadge`'s split, slightly smaller than the header strip's (it sits beside the
 * name). The row around it is `CheckboxPage`'s (`ui/ToolbarMenu.tsx`). */
const CREATOR_BADGE_SX = { fontSize: 10, fontWeight: 700, lineHeight: 1, minWidth: 20 } as const

export type NewMenuEntry = { id: 'task' | 'feature' | 'chat'; label: string; run: () => void }

export type TaskToolbarProps = {
  /* Focus: the board narrowed to work the operator can still act on. Drives `?focus=true` (`shared.tsx`'s
   * `buildTasksUrl`); which states survive is `apps/api/app/task_lifecycle.py`'s `_FOCUS_TOP_LEVEL_STATES`
   * (top-level rows) and `_FOCUS_STATES` (an expanded parent's children) — do not enumerate them here. Its own
   * wordless button beside the sliders (never a menu row). Not in the badge, which counts the chips' filters: its
   * own lit button says it is on. Clear leaves it alone. */
  focus: boolean
  onFocusChange: (focus: boolean) => void
  /* Clears every filter the badge counts, plus Search, in ONE step (one history entry); never Focus — the menu's Clear and the
   * chips' Clear all. Typed EXACT DATES survive: not a filter while a preset is active, and costly to retype. */
  onClearFilters: () => void
  /* The one "show me more rows" switch ("Hidden"): opts back in BOTH hidden SYSTEM rows
   * (janitor/compliance_fix/maturity/portfolio_drift/harvest/triage/adhoc) and `archived_at` rows; there is
   * no way to ask for one without the other. Drives `include_hidden`; `shared.tsx`'s `buildTasksUrl` maps it
   * to the request params. */
  showHidden: boolean
  onShowHiddenChange: (showHidden: boolean) => void
  /* The date window over `updated_at`; `'all'` is off. A real filter (counted in the badge). Words,
   * windows and their calendar (the control plane's zone) are `lib/taskDateRange.ts`'s. No `zone` prop:
   * the badge reads the same hook the page does. */
  dateRange: TaskDateRangeId
  /* `Exact dates` bounds (`YYYY-MM-DD`, `''` unset), kept while a preset is active so switching back does
   * not blank them. Either bound alone is valid. */
  dateFrom: string
  dateTo: string
  onDateRangeChange: (id: TaskDateRangeId) => void
  onDateFromChange: (value: string) => void
  onDateToChange: (value: string) => void
  /* Who created the rows. A real filter (badge, Clear). Multi-select: picks show the UNION; empty is off,
   * so there is no "Everyone" option. The header's `CreatorFilterBar.tsx` is the fast path (three badges and a
   * `+N`); this page is the full list, never hidden by viewport. */
  creators: TaskCreator[]
  /* Marks the operator's own row "(you)" (`creatorLabel`, `lib/taskCreators.ts`), as the strip does.
   * `null` before `/api/auth/me` resolves. */
  currentUserId: string | null
  creatorIds: string[]
  onCreatorIdsChange: (ids: string[]) => void
  /* Where the rows were filed from. A real filter (badge, Clear), multi-select like Creator: picks show the
   * UNION, empty is off. `taskSources` is the server's vocabulary (`GET /api/tasks/sources`, display order) and
   * the ONLY source of the words: a code is never printed. `[]` until it answers. */
  taskSources: TaskSourceOption[]
  sources: string[]
  onSourcesChange: (sources: string[]) => void
  /* One lifecycle state, `''` for every state (`lib/taskUrlFilters.ts`). A real filter (badge, Clear): with the
   * selection it narrows the board to one project's drafts for a bulk verb. */
  stateFilter: string
  onStateFilterChange: (state: string) => void
  /* The free-text term, `''` off, held VERBATIM (a trailing space is half-typed). A real filter (its chip,
   * Clear), typed in the header's box (`pane/TaskSearchBox.tsx`), not this menu. The SERVER matches: `?q=` on `GET /api/tasks`, case-insensitive ONE PHRASE over title OR body,
   * and a NUMBER (`N`, `#N`, `task #N`) also matches the task id and issue number, sorted first
   * (`apps/api/app/routes/tasks/listing.py`'s `list_tasks`). It can find rows outside the loaded window; its help
   * must not promise `/worktrees`'s every-word rule. */
  q: string
  onQChange: (q: string) => void
  /* Removes the term as a navigation (the chip's ✕), where `onQChange` is typing. */
  onQClear: () => void
  /* Group by — the board's sectioning (`lib/taskGrouping.ts` owns the vocabulary). A VIEW setting,
   * not in the badge. `groupBy` is the value the board groups by; `groupByOptions` what this selection
   * offers (`offeredGroupBys`), so the host decides Project-only-across-projects in one place. */
  groupBy: string
  onGroupByChange: (groupBy: string) => void
  groupByOptions: GroupByOption[]
  /* Optional; absence is the permission gate. `POST /api/tasks` declares `tasks:create`, which `tasks:view`
   * does not imply. The host withholds the handler (`Tasks.tsx`) and the button goes with it — no second
   * predicate, no disabled ＋. */
  onNewTask?: () => void
  /* The New split button's ▾ — every object this selection lets the user create (Task, Feature),
   * each entry present only where its permission holds (`pane/PaneHeader.tsx` builds them from
   * the tree's `can_*` flags). Empty or absent: no ▾, and ＋ stands alone. */
  newMenu?: NewMenuEntry[]
  /* Optional by the same mechanism; the host asks the narrower `useCanCreateChats`
   * (`hooks/useTaskAccess.ts`, single home). */
  onNewChat?: () => void
  /** Opens the template gallery, beside New task; absent = no button. */
  onFromTemplate?: () => void
}

export default function TaskToolbar(props: TaskToolbarProps) {
  const {
    focus,
    onFocusChange,
    onClearFilters,
    showHidden,
    onShowHiddenChange,
    dateRange,
    dateFrom,
    dateTo,
    onDateRangeChange,
    onDateFromChange,
    onDateToChange,
    creators,
    currentUserId,
    creatorIds,
    onCreatorIdsChange,
    taskSources,
    sources,
    onSourcesChange,
    stateFilter,
    onStateFilterChange,
    groupBy,
    onGroupByChange,
    groupByOptions,
    onNewTask,
    onNewChat,
    onFromTemplate,
    newMenu = [],
  } = props
  const [newAnchor, setNewAnchor] = useState<HTMLElement | null>(null)
  const newEntries: NewMenuEntry[] = onNewChat ? [...newMenu, { id: 'chat', label: 'Chat', run: onNewChat }] : newMenu
  /* The control plane's zone, from the same hook as `Tasks.tsx`, so badge and request agree. Used only for
   * the badge count: `dateRangeIsActive` answers from the REAL bounds so the badge never claims a narrowing
   * the request does not make. */
  const zone = useOfficialZone()
  const viewer = useViewerMode()
  const activeRange = TASK_DATE_RANGES.find(range => range.id === dateRange)
  const offerCreators = rosterOffersAChoice(creators)
  // The badge: one per filter NOT at its default (active filters, not hidden rows — the list is a bounded
  // window). It answers "why is this list short?", so it counts only what removes rows AND lives in the
  // menu: period, creator and source (ONE each however many picks), state, hidden — each with a chip
  // (`pane/FilterChips.tsx`). Not counted: Focus (its own lit button, no chip — a "2" beside one
  // chip would read as a lie), search (its box is on the header, `pane/TaskSearchBox.tsx`), the selection (the header
  // says it) and Group by (drawing only).
  const activeFilters =
    (showHidden ? 1 : 0) +
    (dateRangeIsActive(dateRange, dateFrom, dateTo, zone) ? 1 : 0) +
    (creatorIds.length > 0 ? 1 : 0) +
    (sources.length > 0 ? 1 : 0) +
    (stateFilter !== '' ? 1 : 0)

  /* Each row's value in the same words its page uses, so level one and the ticked option never differ. */
  const periodValue = activeRange?.label ?? 'Any time'
  /* The first PICKED creator in roster order plus a count, never "3 selected". It never says "Anyone" while
   * a pick is in force: a pick can outlive the roster (`GET /api/tasks/creators` for the current scope; `[]`
   * on first paint, `pages/Tasks.tsx`), so it names the count instead. */
  const pickedCreators = creators.filter(creator => creatorIds.includes(creator.id))
  const creatorValue =
    creatorIds.length === 0
      ? 'Anyone'
      : pickedCreators.length === 0
        ? `${creatorIds.length} ${creatorIds.length === 1 ? 'person' : 'people'}`
        : pickedCreators.length === 1
          ? creatorLabel(pickedCreators[0], currentUserId)
          : `${creatorLabel(pickedCreators[0], currentUserId)} +${pickedCreators.length - 1}`
  /* As the creator's: the first pick's label plus a count, and a bare count before the vocabulary answers. */
  const pickedSources = taskSources.filter(source => sources.includes(source.value))
  const sourceValue =
    sources.length === 0
      ? 'Any'
      : pickedSources.length === 0
        ? `${sources.length} ${sources.length === 1 ? 'source' : 'sources'}`
        : pickedSources.length === 1
          ? pickedSources[0].label
          : `${pickedSources[0].label} +${pickedSources.length - 1}`
  /* The host resolves `groupBy` against the options (`resolveGroupBy`); a tag group whose catalog is still
   * loading has no option yet, so its row says so rather than naming a group it cannot see. */
  const groupValue =
    groupByOptions.find(option => option.value === groupBy)?.label ?? (isTagGroupBy(groupBy) ? 'Tag group' : 'None')

  /* The settings, in badge order: those that change WHICH rows (period, creator, hidden), then
   * those that change how they are DRAWN (grouping). The switch ends the first run: it has no page.
   * Focus and Search are not here: each is its own control on the header (one decision, one control). Only
   * Hidden carries a `hint`: its word alone does not say which rows it adds.
   * THE SINGLE-CHOICE PAGES (Period, State, Group by) SHUT THE PANEL ON A PICK: left open, its invisible
   * backdrop swallowed the next click on the redrawn board. Creator and Source stay open for several picks. */
  const menuItems: ToolbarMenuItem[] = [
    {
      kind: 'page',
      id: 'period',
      label: 'Period',
      value: periodValue,
      closeOnPick: true,
      render: nav => (
        /* The pills (see the file header). `role="radiogroup"` makes them ONE setting; the dialog heading is the
         * accessible name. */
        <>
          <Box data-testid="toolbar-period" role="radiogroup" onKeyDown={moveWithinRadioGroup} aria-label="Period" sx={PERIOD_PILLS_SX}>
            {TASK_DATE_RANGES.map(range => (
              <Box
                key={range.id}
                component="button"
                type="button"
                role="radio"
                aria-checked={dateRange === range.id}
                tabIndex={rovingTabIndex(dateRange === range.id)}
                onClick={() => {
                  onDateRangeChange(range.id)
                  /* Shuts the panel on a preset, NOT on `exact`: that press just revealed the inputs to fill. */
                  if (range.id !== 'exact') nav.pick()
                }}
                /* The tooltip carries the day count the labels drop. */
                title={range.hint}
                data-testid={`toolbar-period-${range.id}`}
                sx={[
                  PERIOD_PILL_SX,
                  dateRange === range.id ? PERIOD_PILL_ACTIVE_SX : PERIOD_PILL_IDLE_SX,
                ]}
              >
                {range.label}
              </Box>
            ))}
          </Box>
          {/* The inputs appear only under "Exact dates"; their values survive leaving it. */}
          {dateRange === 'exact' && (
            <Box sx={PERIOD_DATES_SX}>
              <TextField
                type="date"
                label="From"
                value={dateFrom}
                onChange={e => onDateFromChange(e.target.value)}
                /* `shrink` pinned: a date input always paints its placeholder, which the label would overlap. */
                slotProps={{
                  htmlInput: { 'aria-label': 'Filter from date' },
                  inputLabel: { shrink: true },
                }}
                data-testid="toolbar-period-from"
                sx={PERIOD_DATE_SX}
              />
              <TextField
                type="date"
                label="To"
                value={dateTo}
                onChange={e => onDateToChange(e.target.value)}
                slotProps={{
                  htmlInput: { 'aria-label': 'Filter to date' },
                  inputLabel: { shrink: true },
                }}
                data-testid="toolbar-period-to"
                sx={PERIOD_DATE_SX}
              />
            </Box>
          )}
          {/* The window in words — kept on screen: it is the only place an operator learns an inverted pair filtered
          nothing. Sentence-form sibling of `dateRangePeriodLabel` (same module). */}
          <Typography component="p" data-testid="toolbar-period-summary" sx={PAGE_CAPTION_SX}>
            {dateRangeSummary(dateRange, dateFrom, dateTo)}
          </Typography>
        </>
      ),
    },
    /* Absent when there is nobody to choose between (`rosterOffersAChoice` in `lib/taskCreators.ts`, shared
     * with the header strip) — unless a pick is in force, or a lit badge would have no row to open (the
     * roster is `[]` on first paint). */
    ...(offerCreators || creatorIds.length > 0
      ? ([
          {
            kind: 'page',
            id: 'creator',
            label: 'Creator',
            value: creatorValue,
            /* The one multi-select page (`CheckboxPage`): it never calls `nav`, so each extra pick costs one press. */
            render: () => (
              <CheckboxPage
                testId="toolbar-creator"
                label="Creator"
                options={creators.map(creator => {
                  /* "(you)" is on the visible label too, not only the accessible name. */
                  const label = creatorLabel(creator, currentUserId)
                  return {
                    value: creator.id,
                    label,
                    /* The name, never the initials (the badge is `aria-hidden` at its source). */
                    ariaLabel: `Filter by ${label}`,
                    icon: (
                      <AuthorBadge
                        name={creator.name}
                        sx={CREATOR_BADGE_SX}
                        testId={`toolbar-creator-badge-${creator.id}`}
                      />
                    ),
                    /* The same count the header strip's tooltip carries. */
                    meta: String(creator.task_count),
                  }
                })}
                values={creatorIds}
                onToggle={id => onCreatorIdsChange(toggleCreatorId(creatorIds, id, creators))}
              />
            ),
          },
        ] satisfies ToolbarMenuItem[])
      : []),
    {
      kind: 'page',
      id: 'state',
      label: 'State',
      value: stateFilter === '' ? 'Any' : stateLabel(stateFilter),
      closeOnPick: true,
      render: nav => (
        <RadioPage
          testId="toolbar-state"
          label="State"
          options={[
            { value: '', label: 'Any', hint: 'Every state', testId: 'toolbar-state-any' },
            ...KNOWN_TASK_STATES.map(state => ({ value: state, label: stateLabel(state), hint: STATE_TITLES[state] ?? '', testId: `toolbar-state-${state}` })),
          ]}
          value={stateFilter}
          onPick={onStateFilterChange}
          nav={nav}
        />
      ),
    },
    /* Absent until the vocabulary answers, unless a pick is in force (so a lit badge always has its row). */
    ...(taskSources.length > 0 || sources.length > 0
      ? ([
          {
            kind: 'page',
            id: 'source',
            label: 'Source',
            value: sourceValue,
            render: () => (
              <CheckboxPage
                testId="toolbar-source"
                label="Source"
                options={taskSources.map(source => ({
                  value: source.value,
                  label: source.label,
                  ariaLabel: `Filter by ${source.label}`,
                }))}
                values={sources}
                /* Kept in vocabulary order, so equal picks write equal URLs whatever the click order. */
                onToggle={value => {
                  const picked = new Set(sources)
                  if (picked.has(value)) picked.delete(value)
                  else picked.add(value)
                  onSourcesChange(taskSources.filter(source => picked.has(source.value)).map(source => source.value))
                }}
              />
            ),
          },
        ] satisfies ToolbarMenuItem[])
      : []),
    /* Not on the Live Cast viewer: its API always lists hidden rows, so the switch would change nothing there. */
    ...(viewer
      ? []
      : [
          {
            /* Hidden is a switch, not a door (two states). Its `title` and accessible name name both classes — not
             * guessable from the word. */
            kind: 'row',
            id: 'hidden',
            label: 'Hidden',
            hint: 'System and archived tasks, among the finished ones',
            /* The testid is on the Switch ROOT: MUI types `slotProps.input` to reject `data-*`. Specs drive its
             * `switch` role. */
            control: (
              <Switch
                size="small"
                checked={showHidden}
                onChange={e => onShowHiddenChange(e.target.checked)}
                slotProps={{
                  input: {
                    'aria-label': 'Show hidden tasks (system and archived)',
                  },
                }}
                data-testid="toolbar-hidden"
              />
            ),
          } satisfies ToolbarMenuItem,
        ]),
    {
      kind: 'page',
      id: 'group',
      label: 'Group by',
      value: groupValue,
      closeOnPick: true,
      /* A radio list shutting the panel on a pick. Option hints are `title`s. Testids spell each
       * value (`toolbar-group-<value>`, `none` for None): e2e specs and a shared fixture reach them. */
      render: nav => (
        <RadioPage
          testId="toolbar-group-by"
          label="Group by"
          options={groupByOptions.map(option => ({
            ...option,
            testId: `toolbar-group-${option.value === GROUP_BY_NONE ? 'none' : option.value}`,
          }))}
          value={groupBy}
          onPick={onGroupByChange}
          nav={nav}
        />
      ),
    },
  ]

  return (
    /* The action cluster is ONE flex item. At phone width `pages/Tasks.tsx` dissolves the actions `Stack`
     * (`display: contents`; see `ui/PageHeader`), so bare buttons would wrap one at a time and orphan ＋ at the
     * left. Grouped they wrap together, `ml: 'auto'` ranging them right on whichever line.
     * The 4px gap keeps the phone line (`PageHeader`'s intended gap is inert under `display: contents`), and
     * keeps search floor + cluster on one line in a 1024px window's split pane (a ~348px board).
     * `flexWrap: 'wrap'` + the `maxWidth` cap: labelled buttons above the phone breakpoint can outgrow the
     * column; capped, the cluster breaks into right-ranged rows (`justifyContent: 'flex-end'`) instead of
     * clipping. */
    <Box
      data-testid="toolbar-actions"
      sx={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        justifyContent: 'flex-end',
        gap: '4px',
        // The right edge, on line one beside the title or on a line of its own.
        ml: 'auto',
        // Never shrinks (members are at the 44px floor; shrinking shreds labels). The cap lets it WRAP: `0 0 auto`
        // bases at max-content, and without a ceiling it overflows.
        flex: '0 0 auto',
        maxWidth: '100%',
      }}
    >
      {/* First the settings, then the create doors. */}

      {/* Focus, wordless at every width and immediately left of the Filter button. */}
      <Button
        variant="outlined"
        color="inherit"
        onClick={() => onFocusChange(!focus)}
        aria-pressed={focus}
        aria-label="Focus — hide finished tasks and queued subtasks"
        title="Focus — hide finished tasks and queued subtasks"
        data-testid="toolbar-focus"
        sx={focus ? GLYPH_BUTTON_ON_SX : GLYPH_BUTTON_SX}
      >
        {/* A `Box`, not `Typography` (wordless); `component="span"`: `<button>` takes phrasing content. */}
        <Box component="span" sx={GLYPH_BOX_SX}>
          <PulseIcon on={focus} />
        </Box>
      </Button>

      {/* One button for every setting; the panel is `ui/ToolbarMenu.tsx`'s, shared with `/worktrees`. */}
      {/* KEPT "Filter & view" after the List style page left: Group by still arranges the view rather
          than filtering rows, so "Filter" alone would hide where it lives; /worktrees' menu has the same name. */}
      <ToolbarMenu
        name="Filter and view"
        heading="Filter & view"
        hint="Period, creator, source, hidden and grouping"
        badgeCount={activeFilters}
        items={menuItems}
        onClear={onClearFilters}
      />

      {/* FROM TEMPLATE, beside New task for the same creator: the template gallery (`/templates`), where Start and
          Customize file the task. Wordless at phone width, like the other toolbar buttons. */}
      {onFromTemplate && (
        <Button
          onClick={onFromTemplate}
          variant="outlined"
          color="inherit"
          aria-label="From template"
          title="From template"
          data-testid="toolbar-from-template"
          sx={{ ...TOOLBAR_BUTTON_SX, gap: 0.75 }}
        >
          <Box component="span" sx={GLYPH_BOX_SX}><TemplatesIcon /></Box>
          <Typography component="span" variant="button" sx={TOOLBAR_LABEL_SX}>From template</Typography>
        </Button>
      )}

      {/* THE NEW SPLIT BUTTON — a bare ＋ is New Task (its name is on `aria-label`); ▾ lists every other object this
      selection lets the user create (`newEntries`: Feature, and Chat where `onNewChat` is offered). One control,
      two doors onto the same verb. With no Task permission the ▾ stands alone as "New". */}
      {(onNewTask || newEntries.length > 0) && (
        <Box data-testid="toolbar-new" sx={{ display: 'inline-flex', flex: '0 0 auto' }}>
          {onNewTask && (
            <NewButton
              label="New Task"
              onClick={onNewTask}
              testId="toolbar-add"
              sx={newEntries.length > 0 ? SPLIT_MAIN_SX : {}}
            />
          )}
          {newEntries.length > 0 && (
            <Button
              variant="contained"
              color="primary"
              onClick={e => setNewAnchor(e.currentTarget)}
              aria-label={onNewTask ? 'More to create' : 'New'}
              aria-controls={newAnchor ? 'toolbar-new-list' : undefined}
              aria-haspopup="menu"
              aria-expanded={newAnchor !== null}
              data-testid="toolbar-new-menu"
              sx={onNewTask ? SPLIT_CARET_SX : TOOLBAR_BUTTON_SX}
            >
              {!onNewTask && (
                <Typography component="span" variant="button" sx={TOOLBAR_LABEL_SX}>
                  New&nbsp;
                </Typography>
              )}
              <Box component="span" sx={GLYPH_BOX_SX}>▾</Box>
            </Button>
          )}
          <Menu
            id="toolbar-new-list"
            anchorEl={newAnchor}
            open={newAnchor !== null}
            onClose={() => setNewAnchor(null)}
            anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
            transformOrigin={{ vertical: 'top', horizontal: 'right' }}
          >
            {newEntries.map(entry => (
              <MenuItem
                key={entry.id}
                data-testid={`toolbar-new-${entry.id}`}
                onClick={() => {
                  setNewAnchor(null)
                  entry.run()
                }}
                sx={{ [PHONE]: { minHeight: 44 } }}
              >
                {entry.label}
              </MenuItem>
            ))}
          </Menu>
        </Box>
      )}
    </Box>
  )
}
