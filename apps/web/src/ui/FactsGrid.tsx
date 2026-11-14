/* A FACTS GRID — the `<dl>` that lays a page's or card's `DetailField`s out as tiles: as many columns as
 * `minTile`-wide tiles fit (`auto-fill`, so a short list leaves its tracks empty rather than stretching three
 * facts across a card; the tile is capped at the container's own width, so a pane narrower than one tile does
 * not overflow), one column at phone width, where each field already puts its label beside its value. `line`
 * is the other shape, for a handful of short facts above something that must stay above the fold (a log, a
 * form): one wrapping row, each fact its own width, one fact per line on a phone — a wrapped row of pairs there
 * reads as "Persona Coder Priority Normal". `sx` merges last: a site's gap, padding, or its phone exception.
 *
 * `fit` is THE ONE STAT STRIP, of `Stat` tiles (`DetailField`s may sit in it too): the tiles stretch across the
 * row (`auto-fit`, each ≥ `minTile` PX) at every width, and `columns` pins that many equal ones from 1100px.
 *
 * Not `KeyValueList` (label-beside-value rows at body scale), and not a grid with its own breakpoints or
 * label-width rules (`OrgApproval`, `AuditEventPage`, the node page's stats). */
import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { SxProps, Theme } from '@mui/material/styles'
import { PHONE, STAT_GRID_WIDE, monoSx } from './listStyles'
import Tip from './Tip'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT } from '../theme'

type Shape =
  /** The narrowest tile (`'12rem'`, `'140px'`): wide enough for a column's typical value on one line. */
  | { minTile: string; line?: false; fit?: false; columns?: undefined }
  /** One wrapping row instead of a grid. */
  | { line: true; minTile?: undefined; fit?: false; columns?: undefined }
  /** The stat strip; `minTile` in px. */
  | { fit: true; minTile?: number; columns?: number; line?: false }

export function FactsGrid({ minTile, line, fit, columns, testId, sx, children }: Shape & {
  testId?: string
  sx?: SxProps<Theme>
  children: ReactNode
}): JSX.Element {
  return (
    <Box
      component="dl"
      data-testid={testId}
      sx={[
        fit
          ? {
            m: 0,
            display: 'grid',
            gridTemplateColumns: `repeat(auto-fit, minmax(${minTile ?? 104}px, 1fr))`,
            gap: 1,
            ...(columns ? { [STAT_GRID_WIDE]: { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` } } : null),
          }
          : { m: 0, minWidth: 0 },
        !fit && (line
          ? { display: 'flex', flexWrap: 'wrap', columnGap: 3, rowGap: 1, [PHONE]: { flexDirection: 'column' } }
          : {
            display: 'grid',
            gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${minTile}), 1fr))`,
            gap: 1.5,
            [PHONE]: { gridTemplateColumns: 'minmax(0, 1fr)' },
          }),
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Box>
  )
}

/* One `fit` tile, `data-metric={label}`: the label wraps rather than overflowing a neighbour and the value is pinned
 * to the tile's bottom, so a wrapped label never misaligns a row's figures. A measurement is `mono`; an absence is
 * prose (`absent`) — it must never look like a small number. `field-label` is the caption's handle; the value is
 * its next sibling. */
export function Stat({ label, value, testId, hint, mono, absent, large, exact, qualifier, wide }: {
  label: string
  value: ReactNode
  testId?: string
  /** Hover text for the tile; on the caption alone when `exact` owns the figure's tooltip. */
  hint?: string
  mono?: boolean
  absent?: boolean
  /** A headline figure (a balance, a total). */
  large?: boolean
  /** The unrounded figure when the tile abbreviates: the value's tooltip and accessible name. */
  exact?: string
  /** A caveat ON this number ('partial', what it excludes), in the warning tone. */
  qualifier?: string
  /** Spans the whole grid row and ellipsizes on one line (a model id has no compact form); pass `exact`. */
  wide?: boolean
}): JSX.Element {
  const figure = (
    <Typography
      data-testid={testId}
      component="dd"
      aria-label={exact ? `${label}: ${exact}` : undefined}
      variant={large ? 'h6' : 'body2'}
      sx={[
        { m: 0, fontWeight: FONT_WEIGHT.semibold, lineHeight: LINE_HEIGHT.snug, fontVariantNumeric: 'tabular-nums', overflowWrap: 'anywhere' },
        !large && { fontSize: FONT_SIZE.base },
        !!mono && !absent && monoSx,
        !!absent && { color: 'text.disabled', fontWeight: FONT_WEIGHT.regular },
        !!wide && { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
      ]}
    >
      {value}
    </Typography>
  )
  return (
    <Tip title={exact ? undefined : hint}><Box
      data-metric={label}
      sx={{ display: 'flex', flexDirection: 'column', gap: 0, minWidth: 0, ...(wide && { gridColumn: '1 / -1' }) }}
    >
      {/* `mb: auto` pins everything under the caption to the tile's bottom. */}
      <Tip title={exact ? hint : undefined}><Typography data-testid="field-label" component="dt" variant="caption" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.snug, mb: 'auto' }}>
        {label}
      </Typography></Tip>
      {exact ? <Tip describeChild={false} title={exact}>{figure}</Tip> : figure}
      {qualifier && (
        <Typography component="dd" variant="caption" sx={{ m: 0, color: 'warning.main', lineHeight: LINE_HEIGHT.snug }}>
          {qualifier}
        </Typography>
      )}
    </Box></Tip>
  )
}
