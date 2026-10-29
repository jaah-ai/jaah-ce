import { useState, type JSX, type ReactNode } from 'react'
import IconButton from '@mui/material/IconButton'
import Menu from '@mui/material/Menu'
import Stack from '@mui/material/Stack'
import Toolbar from '@mui/material/Toolbar'
import { MoreHorizIcon } from './icons'

/* The ONE filter/action strip above a table. The overflow menu is part of the primitive: every action
 * reachable at 1440px must stay reachable at 390px (AC4(f)) without wrapping, which would grow the height
 * above the first data row (AC5). Controls that fit stay; secondary ones collapse into a Menu.
 */
export function FilterBar({
  children,
  overflow,
  overflowLabel = 'More filters',
}: {
  children: ReactNode
  /** Secondary controls behind the ⋯ button at every width. The function form receives the closer:
   *  MUI `Menu` does not dismiss on item press, and an action must not leave the menu over the list it
   *  changed. Keep this a doc comment — it is the one union-typed prop. */
  overflow?: ReactNode | ((close: () => void) => ReactNode)
  /* The wordless ⋯ button's whole accessible name; override where the overflow holds actions, not filters. */
  overflowLabel?: string
}): JSX.Element {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)

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
      {overflow && (
        <>
          <IconButton
            aria-label={overflowLabel}
            aria-haspopup="true"
            aria-expanded={anchor ? true : undefined}
            onClick={e => setAnchor(e.currentTarget)}
          >
            <MoreHorizIcon />
          </IconButton>
          <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
            {typeof overflow === 'function' ? overflow(() => setAnchor(null)) : overflow}
          </Menu>
        </>
      )}
    </Toolbar>
  )
}

export default FilterBar
