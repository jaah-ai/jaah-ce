import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import { useId, type JSX } from 'react'

/* THE ONE PROGRESS RING: a track and an arc drawn clockwise from twelve o'clock. The arc's extent rides
 * the dash OFFSET (one computed number), which `e2e/tasks-feed.spec.ts` reads to check an epic's arc
 * against its rollup. Colours are hand-typed `var(--mui-palette-*)`: `styleFunctionSx` does not resolve
 * `stroke`. A variant is a geometry plus a paint; the size comes from `size` or the caller's `sx`. */
const VARIANTS = {
  /** The feed disc's epic ring: edge to edge on the disc, a divider track. */
  feed: { box: 20, r: 9, stroke: 2, cap: 'butt', track: { stroke: 'var(--mui-palette-divider)' } },
  /** The task rail's: stronger track — at 14px on the rail's tinted ground the divider all but vanishes. */
  rail: { box: 14, r: 5, stroke: 2, cap: 'butt', track: { stroke: 'var(--mui-palette-text-disabled)', opacity: 0.6 } },
  /** Onboarding's header glyph: thick, round-capped, in the Help header's hues so the two windows read as
   *  one family, easing forward as steps finish. The rail's track strength: on the divider a short arc
   *  with no visible circle reads as a spinner. */
  gradient: { box: 32, r: 12, stroke: 4, cap: 'round', track: { stroke: 'var(--mui-palette-text-disabled)', opacity: 0.6 } },
} as const

/** "7 of 10 done" as a percentage, rounded; nothing to do reads as 0. */
export function progressPct(progress: { done: number; total: number }): number {
  return progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0
}

export type ProgressRingVariant = keyof typeof VARIANTS

/** `aria-hidden`: the words beside the ring ("N of M") carry the reading; `title` is the mouse hint. */
export function ProgressRing({ pct, variant, size, testId, title, sx }: {
  /** 0–100; clamped for drawing, echoed raw on `data-pct`. */
  pct: number
  variant: ProgressRingVariant
  /** Rendered width and height in px; omit to size it by `sx`. */
  size?: number
  testId: string
  title?: string
  sx?: SxProps<Theme>
}): JSX.Element {
  const gradientId = `progress-ring-${useId().replace(/:/g, '')}`
  const { box, r, stroke, cap, track } = VARIANTS[variant]
  const c = box / 2
  const circumference = 2 * Math.PI * r
  const frac = Math.min(1, Math.max(0, pct / 100))
  const gradient = variant === 'gradient'
  return (
    <Box
      component="svg"
      viewBox={`0 0 ${box} ${box}`}
      aria-hidden="true"
      data-testid={testId}
      data-pct={pct}
      sx={[
        size === undefined ? null : { width: size, height: size, flex: '0 0 auto', display: 'block' },
        { '& .ring-track': track, '& .ring-arc': gradient ? {} : { stroke: 'var(--mui-palette-success-main)' } },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {title && <title>{title}</title>}
      {gradient && (
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2={box} y2={box} gradientUnits="userSpaceOnUse">
            <stop offset="0" stopColor="#38bdf8" />
            <stop offset="0.55" stopColor="#3b82f6" />
            <stop offset="1" stopColor="#a855f7" />
          </linearGradient>
        </defs>
      )}
      <circle className="ring-track" cx={c} cy={c} r={r} fill="none" strokeWidth={stroke} />
      <Box
        component="circle"
        className="ring-arc"
        cx={c}
        cy={c}
        r={r}
        fill="none"
        stroke={gradient ? `url(#${gradientId})` : undefined}
        strokeWidth={stroke}
        /* A zero-length dash still paints a round cap: a lone dot at twelve o'clock, read as a spinner's
           head. Nothing done draws no arc at all. */
        strokeLinecap={frac === 0 ? 'butt' : cap}
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - frac)}
        transform={`rotate(-90 ${c} ${c})`}
        sx={gradient ? {
          transition: 'stroke-dashoffset 400ms cubic-bezier(.2, .8, .2, 1)',
          '@media (prefers-reduced-motion: reduce)': { transition: 'none' },
        } : undefined}
      />
    </Box>
  )
}
