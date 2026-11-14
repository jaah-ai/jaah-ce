import { useRef, type JSX, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import { LineChart, type LineChartProps } from '@mui/x-charts/LineChart'
import { useHasWidth } from '../hooks/useHasWidth'
import { clockTime, fmtTimestamp, shortDate } from '../lib/relativeTime'
import { TICK_STYLE } from './chartPalette'

/* ONE TIME-SERIES PLOT: a `role="img"` box named in words, drawn once it has width (`useHasWidth`: a hidden
 * tab's chart would lay out into a negative area), holding an x-charts line chart on a time axis with the
 * console's defaults (no animation or legend, horizontal gridlines, 12px ticks). What differs per caller is a
 * prop. The dashboard's bucket charts are point-scale and keep `ChartCard.tsx::ChartFrame`'s own gate. */

/** Plot height: 1% is more than a pixel, and two charts plus figures fit one screen. */
export const PLOT_PX = 168
/** The plot's margins outside the axes; an overlay strip under the plot is inset by the same amounts.
 *  `right`: the newest stamp sits ON the right edge and needs room for half its tick label. */
export const MARGIN = { top: 12, right: 28, bottom: 0, left: 0 } as const
/** The x-axis band under the plot: x-charts ellipsizes a tick label that does not fit its band; under 32px
 *  every stamp is blank. */
export const X_AXIS_PX = 32

/** A tick's stamp: the date once the window spans more than a day (every tick would be "00:00"), else the time. */
function tickStamp(at: Date, spanHours: number): string {
  return spanHours > 24 ? shortDate(at.toISOString()) : clockTime(at.toISOString())
}

export default function TimeSeriesPlot({
  label,
  testId,
  xs,
  spanHours,
  domain,
  yAxis,
  series,
  height = PLOT_PX,
  sx,
  children,
  ...dataAttrs
}: {
  label: string
  testId: string
  /** Each bucket's time, sorted. */
  xs: readonly Date[]
  /** The window's span in hours: ticks read as dates past a day, as clock times within one. */
  spanHours: number
  /** The axis's extent (the server's window); the data's own when absent. */
  domain?: { min: Date; max: Date }
  /** The y axis, less the shared tick style. */
  yAxis: NonNullable<LineChartProps['yAxis']>[number]
  series: LineChartProps['series']
  height?: number
  sx?: LineChartProps['sx']
  /** Overlays inside the chart (reference lines). */
  children?: ReactNode
  /** Further `data-*` facts on the plot box, for the tests. */
  [attr: `data-${string}`]: string | number | undefined
}): JSX.Element {
  const plotRef = useRef<HTMLDivElement>(null)
  const drawable = useHasWidth(plotRef)
  return (
    <Box ref={plotRef} role="img" aria-label={label} data-testid={testId} sx={{ height, minWidth: 0 }} {...dataAttrs}>
      {drawable && (
        <LineChart
          skipAnimation
          hideLegend
          height={height}
          margin={MARGIN}
          grid={{ horizontal: true }}
          xAxis={[{
            scaleType: 'time',
            data: xs,
            ...domain,
            tickLabelStyle: TICK_STYLE,
            height: X_AXIS_PX,
            valueFormatter: (at: Date, ctx) =>
              ctx.location === 'tick' ? tickStamp(at, spanHours) : fmtTimestamp(at.toISOString()),
          }]}
          yAxis={[{ tickLabelStyle: TICK_STYLE, ...yAxis }]}
          series={series}
          sx={sx}
        >
          {children}
        </LineChart>
      )}
    </Box>
  )
}
