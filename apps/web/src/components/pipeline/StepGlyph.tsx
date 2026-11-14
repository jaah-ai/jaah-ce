/* A PIPELINE STEP'S ONE MARK (`StageStrip.tsx`, `ReleaseStep.tsx`): the step's icon, a coin on its corner with the count
 * it carries, and — while the lane works — a ring turning round it. One mark per step: the icon IS the count and the
 * state, never an icon beside separate coins. The ring stands still under `prefers-reduced-motion`. */
import type { JSX } from 'react'
import Box from '@mui/material/Box'
import CountPill, { SPIN_RING_SX, type CountTone } from '../../ui/CountPill'
import { FONT_SIZE, RADIUS, grid, tint, type PaletteKey } from '../../theme'

export default function StepGlyph({ icon, active = false, count = 0, tone = 'info', error = false, accent }: {
  icon: JSX.Element
  /** The lane is working: the ring turns. */
  active?: boolean
  /** The coin's number; none at 0. */
  count?: number
  /** The coin's tone; a working lane with no coin takes it on its icon and ring when it warns (a held queue). */
  tone?: CountTone
  /** The step's last run failed: the icon turns red. */
  error?: boolean
  /** The step's own colour (`STEP_ACCENT`): at rest the icon takes it on a tinted disc. */
  accent?: PaletteKey
}): JSX.Element {
  const resting = !error && !active && accent !== undefined
  return (
    <Box
      component="span"
      data-testid="step-glyph"
      data-active={active}
      data-error={error}
      aria-hidden="true"
      sx={{
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: grid(3),
        height: grid(3),
        fontSize: FONT_SIZE.md,
        borderRadius: RADIUS.round,
        color: error ? 'error.main' : active ? (tone === 'warning' ? 'warning.main' : 'info.main')
          : accent ? `${accent}.main` : 'text.secondary',
        ...(resting ? { bgcolor: tint(accent, 0.16) } : {}),
        ...(active ? { '&::after': SPIN_RING_SX } : {}),
      }}
    >
      {icon}
      <CountPill count={count} tone={tone} testId="step-glyph-coin"
        sx={{ position: 'absolute', top: `calc(-1 * ${grid(1)})`, right: `calc(-1 * ${grid(1)})`, zIndex: 1 }} />
    </Box>
  )
}
