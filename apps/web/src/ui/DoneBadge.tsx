import type { JSX } from 'react'
import { CHECK_PATH, Icon } from './icons'
import { RADIUS } from '../theme'

/** THE DONE BADGE: a ✓ in a success-filled circle — onboarding's "every step done" glyph, wherever a progress
 *  ring stands for the same progress (the widget, the sidebar's Setup button, the celebration). `aria-hidden`:
 *  the words beside it say "Done". */
export function DoneBadge({ size, testId }: { size: number; testId?: string }): JSX.Element {
  return (
    <Icon
      viewBox="0 0 24 24"
      size={size}
      testId={testId}
      sx={{
        flex: '0 0 auto',
        display: 'block',
        // The old `size / 8` px, rounded, folded down to the 0.5-unit spacing grid (6px units).
        p: Math.floor(Math.round(size / 8) / 3) / 2,
        borderRadius: RADIUS.round,
        bgcolor: 'success.main',
        fill: 'var(--mui-palette-success-contrastText)',
      }}
    >
      <path d={CHECK_PATH} />
    </Icon>
  )
}
