import { useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Popover from '@mui/material/Popover'
import type { Theme } from '@mui/material/styles'
import { scopeLabel, type TaskScope } from '../../../lib/taskScope'
import type { TaskTree as TaskTreeData } from '../../../types'
import { schemed } from '../../../ui/listStyles'
import TaskTree from '../rail/TaskTree'

/* THE PHONE'S SCOPE PICKER, at the top of `/tasks`: a full-width button naming what the board shows, opening
 * the rail's own tree (`rail/TaskTree`, `touch` rows) in a popover below it. The tree navigates; a pick closes
 * it. Rendered by `PaneHeader` at phone width only — above it the sidebar is the scope control. */
export default function ScopeDropdown({ scope, tree }: { scope: TaskScope; tree: TaskTreeData | undefined }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const label = scopeLabel(scope, tree)

  return (
    <>
      <ButtonBase
        data-testid="scope-dropdown"
        aria-haspopup="dialog"
        aria-expanded={anchor !== null}
        aria-label={`${label} — change scope`}
        title={label}
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
          borderRadius: 1,
          typography: 'subtitle1',
          fontWeight: 600,
          textAlign: 'left',
          '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
        })}
      >
        <Box
          component="span"
          data-testid="scope-dropdown-label"
          sx={{ flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {label}
        </Box>
        <Box component="span" aria-hidden sx={{ flex: '0 0 auto', color: 'text.secondary' }}>
          ▾
        </Box>
      </ButtonBase>
      <Popover
        open={anchor !== null}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        slotProps={{
          paper: {
            'data-testid': 'scope-dropdown-tree',
            role: 'dialog',
            'aria-label': 'Change scope',
            sx: (t: Theme) => ({
              width: anchor?.offsetWidth,
              maxWidth: 'calc(100vw - 16px)',
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
