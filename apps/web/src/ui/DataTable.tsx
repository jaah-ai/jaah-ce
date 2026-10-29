import Paper from '@mui/material/Paper'
import Table from '@mui/material/Table'
import TableContainer from '@mui/material/TableContainer'
import type { JSX, ReactNode } from 'react'

/* The console's ONE table shell. Lists use `ui/feed/FeedList`; this is for what is genuinely two-axis
 * or a picker.
 * Deliberately a container, NOT a column-config abstraction: the caller composes `<TableHead>`/
 * `<TableBody>`; these are the most divergent tables, so a config API would grow a prop per caller.
 * `ariaLabel` is REQUIRED: an unnamed scrollable region is an AC11 defect.
 * No `sx`: heading caption and hover tint come from `theme.ts`; phone card, gutter reclaim, ellipsis,
 * mono and tap-target helpers come from `ui/listStyles.ts`. */
export function DataTable({
  children,
  ariaLabel,
  maxHeight,
  nested,
  testId,
}: {
  children: ReactNode
  ariaLabel: string
  /** Caps the scroll region — the ONLY thing that makes `stickyHeader` work: `overflowX: auto` forces
   *  this box to be the sticky `<th>`'s scroll container, and without a cap it never scrolls vertically. */
  maxHeight?: number | string
  /** Drop this container's own frame for a table inside one that already has one (e.g. `SectionCard`),
   *  avoiding a bordered box in a bordered box. Scroll and sticky header are unchanged. */
  nested?: boolean
  testId?: string
}): JSX.Element {
  const sx = { maxHeight, width: '100%', overflowX: 'auto' } as const
  const table = (
    <Table size="small" stickyHeader aria-label={ariaLabel}>
      {children}
    </Table>
  )
  /* Two branches, not a spread of `{component, variant}`: a conditional spread type-checks as neither
   * Paper nor Box shape. */
  return nested ? (
    <TableContainer data-testid={testId} sx={sx}>
      {table}
    </TableContainer>
  ) : (
    <TableContainer component={Paper} variant="outlined" data-testid={testId} sx={sx}>
      {table}
    </TableContainer>
  )
}

export default DataTable
