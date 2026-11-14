/* THE CONSOLE'S PERMISSION GRID LOOK — a row per subject, a centred checkbox per column, an uppercase caption over
 * each column. The API key grid (`pages/settings/apiKeys/KeyPermissionGrid.tsx`) draws from it. */

import { FONT_WEIGHT, LETTER_SPACING, RADIUS, SIZE } from '../theme'

/** A row's floor: the checkbox's tap target. */
export const PERMISSION_ROW_MIN_HEIGHT = SIZE.tapTarget

/** One box's cell: the checkbox centred in its column. */
export const PERMISSION_CELL_SX = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  borderRadius: RADIUS.sm,
} as const

/** A column's heading over the boxes. */
export const PERMISSION_HEADING_SX = {
  color: 'text.secondary',
  fontWeight: FONT_WEIGHT.semibold,
  textAlign: 'center',
  textTransform: 'uppercase',
  letterSpacing: LETTER_SPACING.caps,
} as const

/** A subject row's rule above it. */
export const PERMISSION_ROW_RULE_SX = { borderTop: '1px solid', borderColor: 'divider' } as const
