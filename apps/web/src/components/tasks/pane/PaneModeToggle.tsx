import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { GLYPH_BOX_SX, GLYPH_BUTTON_ON_SX, GLYPH_BUTTON_SX } from '../../../ui/ToolbarMenu'

/* FLAT / TREE — a roster's STRUCTURE, the ONE control that decides it (one-decision-one-control): ONE
 * wordless on/off button at every width, pressed = Tree. The glyph shows the CURRENT structure; tooltip and
 * name are "Tree view", and `aria-pressed` carries the state to AT. Geometry is `GLYPH_BUTTON_SX` (the
 * sliders' box, 44×44 at phone width), lit like Focus (`GLYPH_BUTTON_ON_SX`). Its hosts (Credentials,
 * Playbooks) map their own `tree` spelling onto `'hierarchy'` at the call site. */

export type PaneView = 'flat' | 'hierarchy'

/* A boxed list (flat) vs a root box branching to two child boxes (tree). Neither is bare horizontal rules:
   three rules read as the Filter & view sliders beside it. */
function ViewGlyph({ view }: { view: PaneView }) {
  return (
    <Box
      component="svg"
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
      data-testid="pane-mode-glyph"
      data-view={view}
      sx={{ width: 16, height: 16, display: 'block' }}
    >
      <Box
        component="path"
        d={
          view === 'flat'
            ? 'M2.5 2.5h11v11h-11zM2.5 6.17h11M2.5 9.83h11'
            : 'M1.75 1.75h4.5v3.5h-4.5zM9.25 6.25h5v3.5h-5zM9.25 11h5v3.5h-5zM4 5.25v7.5h5.25M4 8h5.25'
        }
        sx={{ fill: 'none', stroke: 'currentColor', strokeWidth: 1.75, strokeLinecap: 'round', strokeLinejoin: 'round' }}
      />
    </Box>
  )
}

export default function PaneModeToggle({ value, onChange }: { value: PaneView; onChange: (next: PaneView) => void }) {
  const tree = value === 'hierarchy'
  return (
    <Button
      variant="outlined"
      color="inherit"
      onClick={() => onChange(tree ? 'flat' : 'hierarchy')}
      aria-pressed={tree}
      aria-label="Tree view"
      title="Tree view"
      data-testid="pane-mode-toggle"
      sx={tree ? GLYPH_BUTTON_ON_SX : GLYPH_BUTTON_SX}
    >
      <Box component="span" sx={GLYPH_BOX_SX}>
        <ViewGlyph view={value} />
      </Box>
    </Button>
  )
}
