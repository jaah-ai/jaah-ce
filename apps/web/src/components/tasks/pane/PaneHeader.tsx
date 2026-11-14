import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { scopeLabel, type TaskScope } from '../../../lib/taskScope'
import type { TaskAssignee, TaskTree } from '../../../types'
import { usePhone } from '../../../hooks/usePhone'
import { ABOVE_PHONE, BOARD_NO_ASSIGNEES } from '../../../ui/listStyles'
import PageHeader from '../../../ui/PageHeader'
import { VISUALLY_HIDDEN } from '../../../ui/visuallyHidden'
import { SearchIcon } from '../../../ui/icons'
import { GLYPH_BOX_SX, GLYPH_BUTTON_ON_SX, GLYPH_BUTTON_SX } from '../../../ui/ToolbarMenu'
import { OrgScopeInline } from '../../../ui/OrgScopeControl'
import AssigneeFilterBar from '../AssigneeFilterBar'
import TaskToolbar, { type TaskToolbarProps } from '../TaskToolbar'
import FilterChips, { type ChipSelectAll } from './FilterChips'
import ScopeDropdown from './ScopeDropdown'
import TaskSearchBox from './TaskSearchBox'
import { SIZE, grid } from '../../../theme'

/* THE `/tasks` PANE HEADER — what the main pane is showing and every control over it:
 *   · the selection's name as the page `<h1>`, VISUALLY HIDDEN at every width (the sidebar already shows
 *     it, highlighted) — `scopeLabel`, or "Tasks" on All projects; on the phone, where the sidebar is a
 *     bar, the `ScopeDropdown` above the header says it;
 *   · the SEARCH box, leading the row (`TaskSearchBox`), then the assignee strip (`AssigneeFilterBar`, the fast
 *     path; not on the phone);
 *   · the action cluster: Focus, Filter & view, ＋ / ▾ (New Task; Task, From template and Chat in the ▾ —
 *     `TaskToolbar`);
 *   · a chip per active filter, with Clear all (`FilterChips`).
 * The page owns the state (all of it the URL's but Focus); this component owns the layout. */

/* ── Geometry, applied to `ui/PageHeader` from the outside through its `data-slot` hooks (`sx` on this wrapper;
   both are REDUCTIONS) ──
   · ABOVE THE PHONE IT IS ONE LINE: search, assignee strip and action cluster share the row. The
     title+aside group bases at 0 with a `min-content` floor, so the row SHRINKS before it wraps — `PageHeader`'s
     `1 1 auto` bases the group at its max-content and breaks the line while there is still room. As the board
     narrows the assignee strip goes first (`BOARD_NO_ASSIGNEES`; the same filter is in Filter & view), then the
     search box shrinks to its floor (`TaskSearchBox`). Only a board too narrow for search floor + actions
     (~410px, a console docked to its limit) still wraps, onto a second line rather than overflowing.
   · AT PHONE WIDTH IT IS ONE LINE, drawn here without `PageHeader`: the scope dropdown takes the
     spare width, then an operator's org-scope chip, the search button and the action cluster — 36px buttons
     (`PHONE_BAR_SX`). The search box opens on a line of its own under it, from
     the button or whenever a term is set (a box beside the cluster left ~2 characters of a term at 390px).
     The assignee strip is not drawn there (the same filter is in Filter & view). */
const TITLE_ROW_LEAD = '& > h1, & > [data-slot="heading"]'

/* THE PHONE'S HEADER LINE: the project picker, the search button and the action cluster share ONE line; the picker
   takes the spare width and ellipsises, the buttons keep theirs. An operator's org-scope chip (`OrgScopeInline`, which
   suppresses the shell's own strip while mounted — `App.tsx`'s `OrgScopeBar`) comes last, without its "Orgs" word: it
   stays on the line where both names fit whole, and wraps under the picker where they do not (at 390px, two pickers
   beside the buttons read "All pro…" and "Orgs A…"). Every
   control is 36px here, not the 44px floor (an owner decision): four stacked 44px rows had pushed
   the list half a screen down. Ancestor selectors, so they outrank each button's own phone floor. */
const PHONE_CONTROL_PX = 36
const PHONE_BAR_SX = {
  pb: 1,
  mb: 1,
  borderBottom: '1px solid',
  borderColor: 'divider',
} as const
const PHONE_SCOPE_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 0.5,
  flexWrap: 'wrap',
  rowGap: 1,
  minWidth: 0,
  // Its floor holds "All projects" whole, so a chip that would squeeze it wraps instead.
  '& > [data-testid="scope-dropdown"]': { flex: '1 1 0%', minWidth: grid(21), minHeight: PHONE_CONTROL_PX, px: 1 },
  '& > [data-testid="org-scope-inline"] .MuiButtonBase-root': { minHeight: PHONE_CONTROL_PX },
  '& .org-scope-word': { display: 'none' },
  '& .MuiButton-root': { minWidth: PHONE_CONTROL_PX, minHeight: PHONE_CONTROL_PX, height: PHONE_CONTROL_PX, py: 0 },
  '& [data-testid="toolbar-new-menu"]': { minWidth: SIZE.avatar },
  '& > [data-testid="toolbar-actions"]': { flexWrap: 'nowrap', ml: 0 },
} as const
/* The search line under it: the box takes the whole width (`TaskSearchBox` flexes in a flex parent). */
const PHONE_SEARCH_ROW_SX = { display: 'flex', mt: 1, minWidth: 0 } as const
const PHONE_SEARCH_ROW_ID = 'pane-search-row'
const HEADER_SX = {
  '& > [data-slot="page-header"]': {
    [ABOVE_PHONE]: {
      '& > [data-slot="lead"]': {
        flex: '1 1 0%',
        minWidth: 'min-content',
        /* `PageHeader`'s aside row, one level down: one line, and its lead (the visually hidden `<h1>`, or a
           feature's empty heading column) out of flow, so the row's gap does not open a blank before the search
           box. Named, not `:first-child`: emotion flags that as unsafe under SSR, and `:first-of-type` would also
           take the search box, the first `div` beside the `<h1>`. */
        '& > [data-slot="title-row"]': { flexWrap: 'nowrap', [TITLE_ROW_LEAD]: { position: 'absolute' } },
      },
    },
  },
  [BOARD_NO_ASSIGNEES]: {
    [ABOVE_PHONE]: { '& [data-testid="assignee-filter-bar"]': { display: 'none' } },
  },
} as const


export default function PaneHeader({
  scope,
  tree,
  assignees,
  currentUserId,
  toolbar,
  selectAll,
}: {
  scope: TaskScope
  tree: TaskTree | undefined
  /** The assignee strip's roster, already ordered by the page. */
  assignees: TaskAssignee[]
  currentUserId: string | null
  /** Everything the action cluster reads. */
  toolbar: TaskToolbarProps
  /** Select all beside the filter chips (`FilterChips` says why); absent where the board offers no pick. */
  selectAll?: ChipSelectAll
}) {
  /* The phone's search line: open from its button or once a term narrows the list, and open until its button
     closes it — emptying the box (a debounced `q=''`) must not pull it from under the typing hand. Focused only
     when its button opened it, never on a load with `?q=`. Synced to `q` during render (React's "adjusting
     state when a prop changes"), so the line is there on the first frame. */
  const [searchOpen, setSearchOpen] = useState(toolbar.q !== '')
  const [focusSearch, setFocusSearch] = useState(false)
  const [seenQ, setSeenQ] = useState(toolbar.q)
  if (toolbar.q !== seenQ) {
    setSeenQ(toolbar.q)
    if (toolbar.q !== '') setSearchOpen(true)
  }
  const phone = usePhone({ noSsr: true })
  const searchShown = searchOpen || toolbar.q !== ''
  /* The `<h1>`, visually hidden — the sidebar's highlighted row names the selection (the phone's dropdown does
     there), so a visible copy only cost the search row its width. */
  const title = (
    <Box component="span" data-testid="pane-title" sx={VISUALLY_HIDDEN}>
      {scope.kind === 'all' ? 'Tasks' : scopeLabel(scope, tree)}
    </Box>
  )

  if (phone) {
    // Closing the line drops the term with it: a hidden box must not keep narrowing the list.
    const toggleSearch = () => {
      if (!searchShown) {
        setSearchOpen(true)
        setFocusSearch(true)
        return
      }
      setSearchOpen(false)
      setFocusSearch(false)
      if (toolbar.q !== '') toolbar.onQClear()
    }
    return (
      <Box data-testid="pane-header">
        <Box data-testid="pane-bar" sx={PHONE_BAR_SX}>
          <Box data-testid="pane-scope-row" sx={PHONE_SCOPE_ROW_SX}>
            <ScopeDropdown scope={scope} tree={tree} />
            <Button
              variant="outlined"
              color="inherit"
              onClick={toggleSearch}
              aria-label={searchShown ? 'Close search' : 'Search'}
              aria-expanded={searchShown}
              aria-controls={searchShown ? PHONE_SEARCH_ROW_ID : undefined}
              data-testid="task-search-toggle"
              sx={searchShown ? GLYPH_BUTTON_ON_SX : GLYPH_BUTTON_SX}
            >
              <Box component="span" sx={GLYPH_BOX_SX}>
                <SearchIcon size={SIZE.iconSm} />
              </Box>
            </Button>
            <TaskToolbar {...toolbar} />
            <OrgScopeInline />
          </Box>
          <Box component="h1" sx={{ m: 0 }}>
            {title}
          </Box>
          {searchShown && (
            <Box id={PHONE_SEARCH_ROW_ID} sx={PHONE_SEARCH_ROW_SX}>
              <TaskSearchBox
                value={toolbar.q}
                onChange={toolbar.onQChange}
                onClear={toolbar.onQClear}
                autoFocus={focusSearch}
                hintBelow
              />
            </Box>
          )}
        </Box>
        <FilterChips {...toolbar} selectAll={selectAll} />
      </Box>
    )
  }

  return (
    <Box data-testid="pane-header" sx={HEADER_SX}>
      <PageHeader
        testId="page-header"
        title={title}
        /* SEARCH, then THE ASSIGNEE PICKER, leading the header row: both say WHICH rows. The picker draws
           nothing on an empty roster, and is not drawn on the phone (one line; Filter & view has it). */
        titleAside={
          <>
            <TaskSearchBox value={toolbar.q} onChange={toolbar.onQChange} onClear={toolbar.onQClear} />
            <AssigneeFilterBar
              assignees={assignees}
              currentUserId={currentUserId}
              selectedIds={toolbar.assigneeIds}
              onChange={toolbar.onAssigneeIdsChange}
            />
          </>
        }
        actions={<TaskToolbar {...toolbar} />}
      />
      <FilterChips {...toolbar} selectAll={selectAll} />
    </Box>
  )
}
