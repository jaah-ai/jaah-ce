import Box from '@mui/material/Box'

/* THE CONSOLE'S ONE DISCLOSURE GLYPH: a caret, `▸` folded and `▾` open. Not `+`/`−`: beside an
   Add `+` a row's `+` read as "add", not "expand". The box's fixed width keeps the two carets (whose advance
   widths differ) from moving the label on toggle. No border: the glyph is the affordance in its 28x44 target,
   so the hover rule (`[data-testid="tree-toggle"]:hover > *`) recolours the glyph. */
export const DISCLOSURE_COLLAPSED = '▸'
export const DISCLOSURE_EXPANDED = '▾'

/** The glyph for a fold in `expanded` state — for a caller drawing its own box (a menu icon, a section head). */
export function disclosureGlyph(expanded: boolean): string {
  return expanded ? DISCLOSURE_EXPANDED : DISCLOSURE_COLLAPSED
}

const TOGGLE_GLYPH_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 20,
  height: 20,
  border: 0,
  borderRadius: 0,
  fontSize: '0.875rem',
  fontWeight: 600,
  lineHeight: 1,
  color: 'text.secondary',
} as const

/**
 * The SINGLE HOME for the console's collapse/expand glyph, so every tree
 * table (`components/WorktreePool.tsx` among them) agrees on "this section folds" (one fact, one file).
 *
 * `aria-hidden` on purpose: every caller wraps it in a real button whose `aria-label`/`aria-expanded`
 * carry the state, and that button owns the touch target; this box is deliberately 20px.
 */
export default function ExpandToggleGlyph({ expanded }: { expanded: boolean }) {
  return <Box component="span" aria-hidden="true" sx={TOGGLE_GLYPH_SX}>{disclosureGlyph(expanded)}</Box>
}
