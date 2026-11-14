import Box from '@mui/material/Box'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, grid } from '../theme'
import type { SxProps, Theme } from '@mui/material/styles'

/** The coin's tones, the theme's palette: red for a failure or what waits on you, amber for a question, blue for work
 *  in motion, grey for a plain count. */
export type CountTone = 'error' | 'warning' | 'info' | 'neutral'

const TONE_SX: Record<CountTone, { color: string; backgroundColor: string }> = {
  error: { color: 'error.contrastText', backgroundColor: 'error.main' },
  warning: { color: 'warning.contrastText', backgroundColor: 'warning.main' },
  info: { color: 'info.contrastText', backgroundColor: 'info.main' },
  neutral: { color: 'common.white', backgroundColor: 'grey.600' },
}

/* `spinning`: a ring turning round the coin, for a count of things in motion (merging, deploying). It stands still
 * under `prefers-reduced-motion`; the number stays readable either way. */
/** The ring itself, for an `&::after` of a `position: relative` host (`StepGlyph.tsx` draws the same one). */
export const SPIN_RING_SX = {
  content: '""',
  position: 'absolute',
  inset: `calc(-1 * ${grid(0.5)})`,
  borderRadius: RADIUS.round,
  border: '2px solid',
  borderColor: 'currentColor',
  borderTopColor: 'transparent',
  borderRightColor: 'transparent',
  '@keyframes spinRing': { to: { transform: 'rotate(360deg)' } },
  animation: 'spinRing 1.1s linear infinite',
  '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
} as const
const spinSx = (ring: string) => ({ position: 'relative', '&::after': { ...SPIN_RING_SX, color: ring } }) as const

/** THE count coin: a small filled count beside a label ("Tickets 2"), capped at `max`+ (50 unless the host says);
 *  nothing at 0. Aria-hidden: the host control's accessible name carries the number in words. `sx` is merged after
 *  the coin's own. */
export default function CountPill({ count, tone = 'error', spinning = false, max = 50, testId, sx }: {
  count: number; tone?: CountTone; spinning?: boolean; max?: number; testId?: string; sx?: SxProps<Theme>
}) {
  if (count <= 0) return null
  return (
    <Box
      component="span"
      data-testid={testId}
      data-tone={tone}
      aria-hidden="true"
      sx={[
        {
          flex: '0 0 auto',
          minWidth: grid(2.5),
          height: grid(2.5),
          py: 0,
          px: 0.5,
          borderRadius: RADIUS.round,
          fontSize: FONT_SIZE.xxs,
          fontWeight: FONT_WEIGHT.bold,
          // A one-line box centred in the pill, so the digits sit mid-height without a px line height.
          lineHeight: LINE_HEIGHT.tight,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
          boxSizing: 'border-box',
          ...TONE_SX[tone],
        },
        ...(spinning ? [spinSx(TONE_SX[tone].backgroundColor)] : []),
        ...(sx === undefined ? [] : Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {count > max ? `${max}+` : count}
    </Box>
  )
}
