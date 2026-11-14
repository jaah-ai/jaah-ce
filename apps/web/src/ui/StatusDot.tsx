import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import type { JSX } from 'react'
import type { Tone } from './StatusChip'
import { TONE_COLOR } from './statusTone'
import { RADIUS } from '../theme'

/* The console's one status dot (when and how to use it: style guide § Controls), decorative by default. `color`
 * (a palette path or CSS colour) overrides `tone`; `hollow` draws a 2px ring; `pulse` adds one expanding
 * ring, off under reduced motion. */
const PULSE_SX = {
  '@keyframes statusDotPulse': { from: { transform: 'scale(1)', opacity: 0.45 }, '70%, to': { transform: 'scale(3.2)', opacity: 0 } },
  '&::after': { content: '""', position: 'absolute', inset: 0, borderRadius: RADIUS.round, bgcolor: 'inherit', animation: 'statusDotPulse 1.9s ease-out infinite' },
  '@media (prefers-reduced-motion: reduce)': { '&::after': { animation: 'none', opacity: 0 } },
} as const

export default function StatusDot({ tone = 'neutral', color, size = 8, pulse, hollow, sx, 'aria-hidden': ariaHidden = true, ...rest }: {
  tone?: Tone
  color?: string
  size?: number | string
  pulse?: boolean
  hollow?: boolean
  title?: string
  role?: string
  'aria-label'?: string
  sx?: SxProps<Theme>
  'aria-hidden'?: boolean
  'data-testid'?: string
  'data-state'?: string
  'data-tone'?: string
}): JSX.Element {
  const ink = color ?? TONE_COLOR[tone]
  return (
    <Box
      component="span"
      aria-hidden={ariaHidden || undefined}
      {...rest}
      sx={[
        { position: 'relative', display: 'inline-block', flex: 'none', width: size, height: size, borderRadius: RADIUS.round, boxSizing: 'border-box' },
        hollow ? { border: '2px solid', borderColor: ink } : { bgcolor: ink },
        pulse ? PULSE_SX : {},
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    />
  )
}
