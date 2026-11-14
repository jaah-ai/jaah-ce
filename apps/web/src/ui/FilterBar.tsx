import type { JSX, ReactNode } from 'react'
import Stack from '@mui/material/Stack'
import Toolbar from '@mui/material/Toolbar'

/* The ONE filter/action strip above a table. Every action reachable at 1440px must stay reachable at 390px
 * (AC4(f)) without wrapping, which would grow the height above the first data row (AC5): controls that fit
 * stay; secondary ones collapse into a `ui/OverflowMenu` the caller places in its row (`ui/BulkActionBar`).
 */
export function FilterBar({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Toolbar
      variant="dense"
      disableGutters
      /* `minHeight: 0` beats MUI's 48px dense floor; height is measured (AC5). A REDUCTION. */
      sx={{ minHeight: 0, py: 0.5, gap: 1, flexWrap: 'wrap' }}
    >
      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        spacing={1}
        useFlexGap
        sx={{ flexWrap: 'wrap', alignItems: { xs: 'stretch', sm: 'center' }, flex: '1 1 auto', minWidth: 0 }}
      >
        {children}
      </Stack>
    </Toolbar>
  )
}

export default FilterBar
