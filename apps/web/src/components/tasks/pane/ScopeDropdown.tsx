import { useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Popover from '@mui/material/Popover'
import type { Theme } from '@mui/material/styles'
import { scopeLabel, type TaskScope } from '../../../lib/taskScope'
import type { TaskTree as TaskTreeData } from '../../../types'
import { schemed, ELLIPSIS_SX } from '../../../ui/listStyles'
import TaskTree from '../rail/TaskTree'
import { FONT_WEIGHT, RADIUS, grid } from '../../../theme'
import Tip from '../../../ui/Tip'

/* THE PHONE'S SCOPE PICKER, at the top of `/tasks`: a full-width button naming what the board shows, opening
 * the rail's own tree (`rail/TaskTree`, `touch` rows) in a full-width popover below it. The tree navigates; a pick closes
 * it. Rendered by `PaneHeader` at phone width only — above it the sidebar is the scope control. */
export default function ScopeDropdown({ scope, tree }: { scope: TaskScope; tree: TaskTreeData | undefined }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const label = scopeLabel(scope, tree)

  return (
    <>
      <Tip title={label}><ButtonBase
        data-testid="scope-dropdown"
        aria-haspopup="dialog"
        aria-expanded={anchor !== null}
        aria-label={`${label} — change scope`}
        onClick={e => setAnchor(e.currentTarget)}
        sx={(t: Theme) => ({
          display: 'flex',
          width: '100%',
          minWidth: 0,
          minHeight: t.custom.tapTargetPx,
          justifyContent: 'space-between',
          gap: 1,
          px: 1.5,
          border: '1px solid',
          borderColor: 'divider',
          borderRadius: RADIUS.sm,
          typography: 'subtitle1',
          fontWeight: FONT_WEIGHT.semibold,
          textAlign: 'left',
          '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
        })}
      >
        <Box
          component="span"
          data-testid="scope-dropdown-label"
          sx={{ flex: '1 1 auto', ...ELLIPSIS_SX }}
        >
          {label}
        </Box>
        <Box component="span" aria-hidden sx={{ flex: '0 0 auto', color: 'text.secondary' }}>
          ▾
        </Box>
      </ButtonBase></Tip>
      <Popover
        open={anchor !== null}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        marginThreshold={16}
        slotProps={{
          paper: {
            'data-testid': 'scope-dropdown-tree',
            role: 'dialog',
            'aria-label': 'Change scope',
            sx: (t: Theme) => ({
              /* The whole screen less the page gutters, never the trigger's width: the trigger shares its row
               * with the toolbar, and a list that narrow breaks project names letter by letter. The 32px is the
               * Popover's `marginThreshold` on each side. */
              width: `calc(100vw - ${grid(5)})`,
              maxHeight: '70vh',
              overflowY: 'auto',
              py: 0.5,
              ...schemed(t, c => ({ backgroundColor: c.bg, color: c.text })),
            }),
          } as object,
        }}
      >
        <TaskTree tree={tree} touch onNavigate={() => setAnchor(null)} />
      </Popover>
    </>
  )
}
