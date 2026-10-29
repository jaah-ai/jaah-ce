import { useState } from 'react'
import Box from '@mui/material/Box'
import useMediaQuery from '@mui/material/useMediaQuery'
import { scopeLabel, type TaskScope } from '../../../lib/taskScope'
import type { TaskCreator, TaskTree } from '../../../types'
import { ABOVE_PHONE, BOARD_NO_CREATORS, PHONE, PHONE_QUERY } from '../../../ui/listStyles'
import PageHeader from '../../../ui/PageHeader'
import { VISUALLY_HIDDEN } from '../../../ui/visuallyHidden'
import { OrgScopeInline } from '../../../ui/OrgScopeControl'
import CreatorFilterBar from '../CreatorFilterBar'
import TaskToolbar, { type NewMenuEntry, type TaskToolbarProps } from '../TaskToolbar'
import FilterChips, { type ChipSelectAll } from './FilterChips'
import NewNodeDialog, { creatableFor } from './NewNodeDialog'
import ScopeDropdown from './ScopeDropdown'
import TaskSearchBox from './TaskSearchBox'

/* THE `/tasks` PANE HEADER — what the main pane is showing and every control over it:
 *   · the selection's name as the page `<h1>`, VISUALLY HIDDEN at every width (the sidebar already shows
 *     it, highlighted) — `scopeLabel`, or "Tasks" on All projects; on the phone, where the sidebar is a
 *     bar, the `ScopeDropdown` above the header says it. A feature scope draws none, `FeatureHeader`'s name is
 *     the `<h1>` there;
 *   · the SEARCH box, leading the row (`TaskSearchBox`), then the creator strip (`CreatorFilterBar`, the fast
 *     path; not on the phone);
 *   · the action cluster: Focus, Filter & view, ＋ / ▾ (New Task; Feature and Chat in the ▾ — `TaskToolbar`);
 *   · a chip per active filter, with Clear all (`FilterChips`).
 * The page owns the state (all of it the URL's but Focus); this component owns the layout and the New
 * Feature dialog, whose entry it derives from the tree's `can_*` flags. */

/* ── Geometry, applied to `ui/PageHeader` from the outside through its `data-slot` hooks (`sx` on this wrapper;
   both are REDUCTIONS) ──
   · ABOVE THE PHONE IT IS ONE LINE: search, creator strip and action cluster share the row. The
     title+aside group bases at 0 with a `min-content` floor, so the row SHRINKS before it wraps — `PageHeader`'s
     `1 1 auto` bases the group at its max-content and breaks the line while there is still room. As the board
     narrows the creator strip goes first (`BOARD_NO_CREATORS`; the same filter is in Filter & view), then the
     search box shrinks to its floor (`TaskSearchBox`). Only a board too narrow for search floor + actions
     (~410px, a console docked to its limit) still wraps, onto a second line rather than overflowing.
   · AT PHONE WIDTH IT IS ONE LINE TOO: the selection's name is the dropdown's (above, sharing its row
     with an operator's org-scope chip — `PHONE_SCOPE_ROW_SX`);
     the header row is the search box, growing, then the action cluster ranged right. The creator strip is not
     drawn there (the same filter is in Filter & view). `PageHeader`'s `actions` slot is `display: contents`, so the toolbar's own cluster is
     the item (see `TaskToolbar`). */
const TITLE_ROW_LEAD = '& > h1, & > [data-slot="heading"]'

/* THE PHONE'S SCOPE ROW: the project picker and an operator's org-scope chip (`OrgScopeInline`, which
   suppresses the shell's own strip while mounted — `App.tsx`'s `OrgScopeBar`) share ONE line, not two stacked:
   the picker takes the spare width, never under half the line; the chip keeps its natural width and ellipsises its
   name past that.
   A tenant has no chip, so the picker keeps the whole line. */
const PHONE_SCOPE_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  mb: 1,
  minWidth: 0,
  '& > [data-testid="scope-dropdown"]': { flex: '1 1 0%', minWidth: '50%' },
} as const
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
    [PHONE]: {
      flexWrap: 'nowrap',
      columnGap: '0.5rem',
      justifyContent: 'flex-start',
      pb: '0.375rem',
      mb: '6px',
      borderBottom: '1px solid',
      borderColor: 'divider',
      '& > [data-slot="actions"]': { display: 'contents' },
      '& > [data-slot="lead"]': {
        flex: '1 1 0%',
        minWidth: 0,
        '& > [data-slot="title-row"]': { flexWrap: 'nowrap', [TITLE_ROW_LEAD]: { position: 'absolute' } },
      },
    },
  },
  [BOARD_NO_CREATORS]: {
    [ABOVE_PHONE]: { '& [data-testid="creator-filter-bar"]': { display: 'none' } },
  },
} as const


export default function PaneHeader({
  scope,
  tree,
  onNavigate,
  creators,
  currentUserId,
  toolbar,
  selectAll,
}: {
  scope: TaskScope
  tree: TaskTree | undefined
  /** Move the selection (a push: Back walks it). */
  onNavigate: (next: TaskScope) => void
  /** The creator strip's roster, already ordered by the page. */
  creators: TaskCreator[]
  currentUserId: string | null
  /** Everything the action cluster reads; `newMenu` is this component's to add. */
  toolbar: Omit<TaskToolbarProps, 'newMenu'>
  /** Select all beside the filter chips (`FilterChips` says why); absent where the board offers no pick. */
  selectAll?: ChipSelectAll
}) {
  const [creating, setCreating] = useState(false)
  const phone = useMediaQuery(PHONE_QUERY)
  /* The `<h1>`: none on a feature (FeatureHeader's), and visually hidden everywhere else — the sidebar's
     highlighted row names the selection (the phone's dropdown does there), so a visible copy only cost the
     search row its width. */
  const title =
    scope.kind === 'feature' ? undefined : (
      <Box component="span" data-testid="pane-title" sx={VISUALLY_HIDDEN}>
        {scope.kind === 'all' ? 'Tasks' : scopeLabel(scope, tree)}
      </Box>
    )

  /* New ▾: each object where the user may create it here — Task on `tasks:create` (the page withholds
     `onNewTask` otherwise), Feature where a project grants `tasks:create`. */
  const newMenu: NewMenuEntry[] = [
    ...(toolbar.onNewTask ? [{ id: 'task' as const, label: 'Task', run: toolbar.onNewTask }] : []),
    ...(creatableFor(scope, tree).length > 0
      ? [{ id: 'feature' as const, label: 'Feature', run: () => setCreating(true) }]
      : []),
  ]
  // A ▾ offering only what ＋ already does is a second control for one choice: none. Chat (added by the
  // toolbar from `onNewChat`) is a second object, so with it the ▾ lists Task too.
  const splitMenu = newMenu.length === 1 && newMenu[0].id === 'task' && !toolbar.onNewChat ? [] : newMenu

  return (
    <Box data-testid="pane-header" sx={HEADER_SX}>
      {phone && (
        <Box data-testid="pane-scope-row" sx={PHONE_SCOPE_ROW_SX}>
          <ScopeDropdown scope={scope} tree={tree} />
          <OrgScopeInline />
        </Box>
      )}
      <PageHeader
        testId="page-header"
        title={title}
        /* SEARCH, then THE CREATOR PICKER, leading the header row: both say WHICH rows. The picker draws
           nothing on an empty roster, and is not drawn on the phone (one line; Filter & view has it). */
        titleAside={
          <>
            <TaskSearchBox value={toolbar.q} onChange={toolbar.onQChange} onClear={toolbar.onQClear} />
            {!phone && (
              <CreatorFilterBar
                creators={creators}
                currentUserId={currentUserId}
                selectedIds={toolbar.creatorIds}
                onChange={toolbar.onCreatorIdsChange}
              />
            )}
          </>
        }
        actions={<TaskToolbar {...toolbar} newMenu={splitMenu} />}
      />
      <FilterChips {...toolbar} selectAll={selectAll} />
      {creating && (
        <NewNodeDialog
          scope={scope}
          tree={tree}
          onClose={() => setCreating(false)}
          onCreated={next => {
            setCreating(false)
            onNavigate(next)
          }}
        />
      )}
    </Box>
  )
}
