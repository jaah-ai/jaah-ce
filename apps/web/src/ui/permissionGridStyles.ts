/* THE CONSOLE'S PERMISSION GRID LOOK — a row per subject, a centred checkbox per column, an uppercase caption over
 * each column. Shared by Settings' Jarvis tool grid (`pages/settings/jarvisTools/PermissionGrid.tsx`) and the API
 * key grid (`pages/settings/apiKeys/KeyPermissionGrid.tsx`), so the two read as one control. */

/** A row's floor: the checkbox's tap target. */
export const PERMISSION_ROW_MIN_HEIGHT = 44

/** One box's cell: the checkbox centred in its column. */
export const PERMISSION_CELL_SX = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  borderRadius: '4px',
} as const

/** A column's heading over the boxes. */
export const PERMISSION_HEADING_SX = {
  color: 'text.secondary',
  fontWeight: 600,
  textAlign: 'center',
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
} as const

/** A subject row's rule above it. */
export const PERMISSION_ROW_RULE_SX = { borderTop: '1px solid', borderColor: 'divider' } as const
