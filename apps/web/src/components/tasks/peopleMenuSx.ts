/* The task-people popover's frame — a filter field over a scrolling list of person rows — shared by the board's
 * assignee strip overflow (`./AssigneeFilterBar`) and a row's assign lookup (`./TaskPeople`), so both menus are
 * one shape. Each row is a `<button>`: `UNSTYLED_BUTTON_SX` already gives it the pointer cursor. */
import { ELLIPSIS_SX, UNSTYLED_BUTTON_SX } from '../../ui/listStyles'
import { RADIUS, grid } from '../../theme'

export const MENU_SX = { display: 'flex', flexDirection: 'column', minWidth: grid(40), maxWidth: grid(53), py: 1 } as const

export const SEARCH_SX = {
  mx: 1.5, mb: 1, px: 1, py: 0, border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.sm, typography: 'body2',
} as const

export const LIST_SX = { maxHeight: grid(53), overflowY: 'auto', px: 0.5 } as const

export const ITEM_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  width: '100%',
  ...UNSTYLED_BUTTON_SX,
  px: 1,
  py: 0.5,
  borderRadius: RADIUS.sm,
  textAlign: 'left',
  color: 'text.primary',
  '&:hover': { bgcolor: 'action.hover' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: -2 },
} as const

/* The row's name takes the width and ellipsizes. */
export const NAME_SX = { flex: '1 1 auto', ...ELLIPSIS_SX } as const

/* "Loading…", an error, or "No … matches": one muted line in the list's place. */
export const NOTE_SX = { px: 2, py: 1, color: 'text.secondary' } as const
