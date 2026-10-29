import type { JSX, ReactNode } from 'react'
import type { SxProps, Theme } from '@mui/material/styles'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { monoSx } from './listStyles'

/* THE ONE STAT STRIP: a `<dl>` of caption-over-figure tiles that reflows (`auto-fit`, each tile ≥ `minTile`
 * px). The label wraps rather than overflowing a neighbour; the tile pins its value to the bottom, so a
 * wrapped label never misaligns a row's figures. A measurement is `mono`; an absence is prose (`absent`) —
 * it must never look like a small number. `ui/detail/DetailPage`'s `DetailField`s may sit in the grid too. */

export function StatGrid({ children, minTile = 104, columns, testId, sx }: {
  children: ReactNode
  minTile?: number
  /** Pinned to this many equal columns from 1100px. */
  columns?: number
  testId?: string
  sx?: SxProps<Theme>
}): JSX.Element {
  return (
    <Box
      component="dl"
      data-testid={testId}
      sx={[
        {
          m: 0,
          display: 'grid',
          gridTemplateColumns: `repeat(auto-fit, minmax(${minTile}px, 1fr))`,
          gap: 1,
          ...(columns ? { '@media (min-width:1100px)': { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` } } : null),
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Box>
  )
}

/** One tile. `field-label` is the caption's handle; the value is its next sibling. */
export function Stat({ label, value, testId, hint, mono, absent, large }: {
  label: string
  value: ReactNode
  testId?: string
  /** Hover text for the whole tile. */
  hint?: string
  mono?: boolean
  absent?: boolean
  /** A headline figure (a balance, a total). */
  large?: boolean
}): JSX.Element {
  return (
    <Box title={hint} sx={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between', gap: 0.25, minWidth: 0 }}>
      <Typography data-testid="field-label" component="dt" variant="caption" sx={{ color: 'text.secondary', lineHeight: 1.25 }}>
        {label}
      </Typography>
      <Typography
        data-testid={testId}
        component="dd"
        variant={large ? 'h6' : 'body2'}
        sx={[
          { m: 0, fontWeight: 600, lineHeight: 1.3, fontVariantNumeric: 'tabular-nums', overflowWrap: 'anywhere' },
          !large && { fontSize: '0.9375rem' },
          !!mono && !absent && monoSx,
          !!absent && { color: 'text.disabled', fontWeight: 400 },
        ]}
      >
        {value}
      </Typography>
    </Box>
  )
}
