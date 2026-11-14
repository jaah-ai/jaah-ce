import type { JSX } from 'react'
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import { RADIUS } from '../theme'
import Tip from './Tip'

/* THE ONE STACKED BAR: proportions as segments laid end to end on an optional track, each `value / total` of
 * the width (`total` defaults to the segments' sum, so they fill the track). One SVG across a BAR_W×1 viewBox
 * with `preserveAspectRatio="none"` (`NodeMetrics`' reason): a 0.1% segment is a whole unit, the caller's `sx`
 * sets the size and rounds the ends, and a segment's `title` is its hover. Never `<text>` inside — the scale is
 * not uniform — and never colour alone: `label` names the split in words (`role="img"`), and every caller
 * prints the figures beside it. No `label` means `aria-hidden`, for a bar whose figures sit beside it already.
 *
 * `gap` is the other shape — discrete steps, each its own rounded pill a fixed gap apart (a checklist's "how
 * many are left"). A scaled viewBox can hold neither a pixel gap nor a round end per segment, so it lays out as
 * a flex row instead. Fills are CSS colours or a theme read: `styleFunctionSx` does not resolve `fill`. Not
 * `NodeLoadMeter` (phone scales and a readout slot). */

/** viewBox width. */
const BAR_W = 1000

type Paint = string | ((theme: Theme) => string)

export type StackedSegment = {
  key: string
  value: number
  fill: Paint
  /** An alpha, not a second colour, so one declaration is right in both schemes. */
  opacity?: number
  title?: string
}

const paint = (fill: Paint, theme: Theme): string => (typeof fill === 'function' ? fill(theme) : fill)

export default function StackedBar({
  segments,
  total = segments.reduce((sum, s) => sum + s.value, 0),
  label,
  title,
  track,
  separated = false,
  gap,
  segmentAttr = 'segment',
  testId,
  sx,
}: {
  segments: readonly StackedSegment[]
  total?: number
  /** The split in words; omitted, the bar is `aria-hidden`. */
  label?: string
  /** The whole bar's hover. */
  title?: string
  /** Drawn under the segments; an empty bar is the bare track. */
  track?: { fill: Paint; opacity?: number }
  /** A hairline in the card's ground between segments. */
  separated?: boolean
  /** Discrete steps this far apart (theme spacing), each a rounded pill; `track`, `total` and `separated` do not apply. */
  gap?: number
  /** Each segment carries `data-{segmentAttr}={key}`. */
  segmentAttr?: string
  testId?: string
  sx?: SxProps<Theme>
}): JSX.Element {
  const a11y = label === undefined ? { 'aria-hidden': true } : { role: 'img', 'aria-label': label }
  const dataKey = `data-${segmentAttr}`

  if (gap !== undefined) {
    return (
      <Tip title={title}><Box {...a11y} data-testid={testId} sx={[{ display: 'flex', gap }, ...(Array.isArray(sx) ? sx : [sx])]}>
        {segments.map(segment => (
          <Tip key={segment.key} title={segment.title}><Box
            {...{ [dataKey]: segment.key }}
            sx={theme => ({
              flex: segment.value,
              height: '100%',
              borderRadius: RADIUS.md,
              bgcolor: paint(segment.fill, theme),
              opacity: segment.opacity,
              transition: theme.transitions.create('background-color'),
            })}
          /></Tip>
        ))}
      </Box></Tip>
    )
  }

  let cursor = 0
  return (
    <Tip title={title}><Box
      component="svg"
      {...a11y}
      data-testid={testId}
      viewBox={`0 0 ${BAR_W} 1`}
      preserveAspectRatio="none"
      sx={[{ display: 'block', width: '100%', overflow: 'hidden' }, ...(Array.isArray(sx) ? sx : [sx])]}
    >
      {track && (
        <Box
          component="rect"
          x={0}
          y={0}
          width={BAR_W}
          height={1}
          sx={theme => ({ fill: paint(track.fill, theme), fillOpacity: track.opacity })}
        />
      )}
      {total > 0 &&
        segments.map(segment => {
          const width = (segment.value / total) * BAR_W
          const x = cursor
          cursor += width
          if (!(width > 0)) return null
          return (
            <Tip key={segment.key} title={segment.title}><Box
              component="rect"
              {...{ [dataKey]: segment.key }}
              x={x}
              y={0}
              width={width}
              height={1}
              /* `vector-effect` keeps the separator a hairline under the steep vertical scale. */
              sx={theme => ({
                fill: paint(segment.fill, theme),
                fillOpacity: segment.opacity,
                ...(separated && { stroke: theme.vars.palette.background.paper, strokeWidth: 1, vectorEffect: 'non-scaling-stroke' }),
              })}
            /></Tip>
          )
        })}
    </Box></Tip>
  )
}
