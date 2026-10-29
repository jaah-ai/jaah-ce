import { useMemo, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { alpha, type Theme } from '@mui/material/styles'
import { ChartsReferenceLine } from '@mui/x-charts/ChartsReferenceLine'
import { LineChart, lineClasses } from '@mui/x-charts/LineChart'
import type { JSX, ReactNode } from 'react'
import { get } from '../api'
import { useApiRead } from '../hooks/useApiRead'
import { useColorMode } from '../hooks/useColorMode'
import { useEventStream } from '../hooks/useEventStream'
import { useHasWidth } from '../hooks/useHasWidth'
import { useLiveRefetch } from '../hooks/useLiveRefetch'
import { formatBytes, kbSize } from '../lib/formatBytes'
import { categoricalSeries, TICK_STYLE } from '../ui/chartPalette'
import { RAW } from '../theme'
import type {
  NodeDiskEvent,
  NodeDiskGate,
  NodeMetricsPayload,
  NodeMetricsPoint,
} from '../types'
import Banner from '../ui/Banner'
import { NONE, NONE_SX } from '../ui/NoneMark'
import EmptyState from '../ui/EmptyState'
import LoadingState from '../ui/LoadingState'
import SectionCard from '../ui/SectionCard'
import StatusChip from '../ui/StatusChip'
import SegmentedControl from '../ui/SegmentedControl'
import { Stat, StatGrid } from '../ui/StatGrid'
import { clockTime, fmtTimestamp, humanAge, humanDuration } from '../lib/relativeTime'
import { scaleBitRate } from '../lib/bitRate'
import NodeSignals from './NodeSignals'
import NodeLoadMeter, { formatLoad, formatPct, formatRate, headlineLevel, levelTone } from './NodeLoadMeter'

/* ══ THE NODE'S METRICS TAB ═════════════════════════════════════════════════════════════════════════
 * The history behind the list's meter, from one read (`GET /api/nodes/{id}/metrics?hours=N`),
 * in `NodeHealth`'s self-fetching shape (shared `get`, generation guard, `Banner`, SSE tick).
 * Figures first: latest/min/max/mean as mono TEXT, the chart as context; each chart's `aria-label`
 * restates its summary (a plot is unreadable to assistive tech).
 * A percentage chart's Y axis is FIXED 0–100 (autoscaling makes 2-4% look like 88-94%). Network has no
 * percentage, so its two charts share one axis from 0 to a round ceiling over both series (`niceRateMax`),
 * and carry no guide lines: throughput is not pressure.
 * Guide lines come from the payload (`warn_pct`, `critical_pct`), never hardcoded; levels are the
 * server's (`types.ts` §NODE HOST METRICS).
 * Plots are `@mui/x-charts` `LineChart`s, as the operator dashboard's (`pages/dashboard/ChartCard.tsx`).
 * Load (against the cores), Pressure (three lines on one 0–100 axis — the one several-line chart,
 * each line named in the legend) and Swap charts; the latest host signals (`NodeSignals`) under the header.
 */

// ── The range selector ────────────────────────────────────────────────────────────────────────────

/* The server's `hours` values verbatim (`?hours=1|6|24|168`, clamped 1..168). The server widens the bucket
   with the range (`bucket_secs_for`); `bucket_secs` returns so the caption states what was averaged. */
const RANGES = [
  { hours: 1, label: '1h', spoken: 'the last hour' },
  { hours: 6, label: '6h', spoken: 'the last 6 hours' },
  { hours: 24, label: '24h', spoken: 'the last 24 hours' },
  { hours: 168, label: '7d', spoken: 'the last 7 days' },
] as const

/* 6h default, matching the route: holds a build and its aftermath while the 60s bucket is still raw. */
const DEFAULT_HOURS = 6

/** The range's phrase; falls back rather than throwing for an unknown `hours`. */
function spokenRange(hours: number): string {
  return RANGES.find(r => r.hours === hours)?.spoken ?? `the last ${hours} hours`
}

// ── The plotted series ────────────────────────────────────────────────────────────────────────────

/** A series off one point; `null` where that bucket never carried it. */
type Pick = (p: NodeMetricsPoint) => number | null

/* How big a hole breaks the line, in buckets — derived from the payload's `bucket_secs` (the bucket
   widens with the range). One missing bucket is bridged (a single dropped pass); MORE breaks, since
   interpolating an outage is a lie on a panel meant to spot quiet nodes.
   Known gap: points are `avg()` over their bucket with no row count on the wire, so at 7 days (2520s
   buckets) a sparsely reporting node draws a continuous line. Closing it needs a per-point coverage count
   plus a rendering policy — a payload change, deliberately not here. */
const GAP_BUCKETS = 2

/** One line of a several-line chart (Pressure): its own figures, colour and legend entry. */
type Line = { id: string; label: string; pick: Pick }

/** One series on a time axis. `null` in `ys` breaks the line (`connectNulls` is off). */
type Series = { xs: Date[]; ys: Array<number | null> }

/* Points → x/y, positioned by TIME (never by index: that once drew a stale node's recent hole on the far
 * LEFT). A gap wider than `GAP_BUCKETS` gets a `null` just after the last reading so the line breaks there;
 * a point whose own stamp will not parse is dropped and breaks the line too. */
export function seriesOf(points: NodeMetricsPoint[], pick: Pick, bucketSecs: number): Series {
  const gapMs = Math.max(1, bucketSecs * 1000) * GAP_BUCKETS
  const xs: Date[] = []
  const ys: Array<number | null> = []
  let previous: number | null = null
  const breakAfter = (at: number) => {
    xs.push(new Date(at + 1))
    ys.push(null)
  }
  for (const point of points) {
    const at = Date.parse(point.at)
    if (!Number.isFinite(at)) {
      if (previous !== null) breakAfter(previous)
      previous = null
      continue
    }
    if (previous !== null && at - previous > gapMs) breakAfter(previous)
    xs.push(new Date(at))
    ys.push(pick(point))
    previous = at
  }
  return { xs, ys }
}

// ── Sample gaps ───────────────────────────────────────────────────────────────────────────────────

/** A stretch with no reading: from the last bucket before the hole to the first after it (epoch ms). */
export type SampleGap = { from: number; to: number; label: string }

/** "15:03–15:12", or with dates where the hole spans a day ("Oct 16, 2026, 23:50 – Oct 17, 2026, 00:20"). */
function gapSpan(from: number, to: number): string {
  const a = new Date(from).toISOString()
  const b = new Date(to).toISOString()
  return new Date(from).toDateString() === new Date(to).toDateString()
    ? `${clockTime(a)}–${clockTime(b)}`
    : `${fmtTimestamp(a)} – ${fmtTimestamp(b)}`
}

/* Every hole wider than `GAP_BUCKETS` between two readings — `seriesOf`'s break rule, on the BUCKETS (not a
 * series' values), so it is computed once per payload and every chart marks the same stretches. A bucket that
 * carried some other series counts as a reading: a withheld figure is not an outage. Only holes BETWEEN
 * readings: before the first the node may not have existed, and after the last the stale banner speaks. */
export function gapsOf(points: NodeMetricsPoint[], bucketSecs: number): SampleGap[] {
  const gapMs = Math.max(1, bucketSecs * 1000) * GAP_BUCKETS
  const gaps: SampleGap[] = []
  let previous: number | null = null
  for (const point of points) {
    const at = Date.parse(point.at)
    if (!Number.isFinite(at)) continue
    if (previous !== null && at - previous > gapMs) {
      gaps.push({ from: previous, to: at, label: `No samples ${gapSpan(previous, at)}` })
    }
    previous = at
  }
  return gaps
}

// ── The figures ───────────────────────────────────────────────────────────────────────────────────

type Figures = { latest: number; min: number; max: number; mean: number }

/* Latest / min / max / mean — arithmetic, not a verdict. `null` for an empty series: a zero is a measurement. */
function figuresOf(points: NodeMetricsPoint[], pick: Pick): Figures | null {
  const values = points.map(pick).filter((v): v is number => v != null)
  if (values.length === 0) return null
  const total = values.reduce((sum, v) => sum + v, 0)
  return {
    latest: values[values.length - 1],
    min: Math.min(...values),
    max: Math.max(...values),
    mean: total / values.length,
  }
}

/* The network charts' shared ceiling, in bytes/s: the smallest 1/2/5 × 10ⁿ BITS per second at or above the
   busiest bucket of either series, so the top label is a round speed and both lines compare on one axis.
   Floored at 1 kbit/s so an idle link draws a flat line on the baseline rather than a 0-wide axis. */
export function niceRateMax(values: Array<number | null>): number {
  const peakBits = Math.max(0, ...values.filter((v): v is number => v != null && Number.isFinite(v))) * 8
  const floorBits = 1000
  if (peakBits <= floorBits) return floorBits / 8
  const step = 10 ** Math.floor(Math.log10(peakBits))
  const nice = [1, 2, 5, 10].map(m => m * step).find(m => m >= peakBits) ?? 10 * step
  return nice / 8
}

/* A y-axis tick for a network chart: bits per second with a one-letter prefix and no unit (`50M`, `2.5k`),
   so it fits the 34px gutter at the 11px type floor; the chart's caption names the unit. */
export function formatRateTick(bytesPerSec: number): string {
  const { value, prefix } = scaleBitRate(bytesPerSec)
  return `${Number(value.toFixed(value >= 10 ? 0 : 1))}${prefix}`
}

/* The runners chart's ceiling: the busiest bucket rounded up to an even count (so the half tick is whole),
   at least 2 so an idle node draws on the baseline rather than a 0-high axis. */
export function runnersMax(values: Array<number | null>): number {
  const peak = Math.max(0, ...values.filter((v): v is number => v != null && Number.isFinite(v)))
  return Math.max(2, 2 * Math.ceil(peak / 2))
}

/* The load chart's ceiling: the core count (a load equal to it is every core busy) or the busiest bucket,
   whichever is higher, rounded up to an even whole so the half tick is whole; at least 2. */
export function loadMax(values: Array<number | null>, cores: number | null): number {
  const peak = Math.max(cores ?? 0, ...values.filter((v): v is number => v != null && Number.isFinite(v)))
  return Math.max(2, 2 * Math.ceil(peak / 2))
}

/** A runner count: whole when whole (latest, min, max), one decimal for a mean. */
export function formatCount(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

/* An x-axis tick: clock time within a day, the date beyond. Local, locale-formatted; an axis tick, not an
   age, so the one thing here the browser may render itself. The tooltip carries the full stamp. */
function tickStamp(at: Date, hours: number): string {
  return hours > 24
    ? at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : clockTime(at.toISOString())
}

// ── Disk events on the disk chart ─────────────────────────────────────────────────────────────────

/* What the disk thresholds DO (`app/node_disk_events.py`): at warn the node runs a cleanup; at critical the
   gate pauses new work (no new runner or CI job) until usage drops. Words only — the figures stay the payload's. */
const DISK_GUIDE_MEANING = { warn: 'cleanup', critical: 'pause new work' } as const
const TMP_INODE_GUIDE_MEANING = { warn: 'flagged', critical: 'alert' } as const

/** Events closer than this share of the window (~9px at 360px) share one marker, so a burst stays one
 *  tappable target instead of a smear. */
const MARKER_MERGE_FRACTION = 0.03

/** Lines a merged marker's tooltip lists before "+N more". */
const MARKER_TOOLTIP_LINES = 6

type MarkerKind = 'reclaim' | 'pause' | 'resume'

/** One marker: a group of events at (about) one time. `frac` is its place across the window (0..1);
 *  `kind` is the group's most telling event. */
type ChartMarker = { at: Date; frac: number; kind: MarkerKind; lines: string[] }

/** The palette entry per kind: info for a cleanup, warning for a pause, success for a resume. */
const MARKER_PALETTE = { reclaim: 'info', pause: 'warning', resume: 'success' } as const

const MARKER_WORD: Record<MarkerKind, string> = {
  reclaim: 'cleanup',
  pause: 'paused',
  resume: 'resumed',
}

/** A pause outranks a resume outranks a cleanup when events share a marker. */
const MARKER_RANK: Record<MarkerKind, number> = { reclaim: 0, resume: 1, pause: 2 }

/** "Cleanup freed 7.2 GB · Oct 11, 2026, 01:07 PM". */
function markerLine(event: NodeDiskEvent): string {
  const at = (pct: number | null) => (pct == null ? '' : ` at ${formatPct(pct)} used`)
  const what =
    event.kind === 'pause' ? `New work paused${at(event.used_pct)}`
    : event.kind === 'resume' ? `New work resumed${at(event.used_pct)}`
    : `Cleanup freed ${formatBytes(event.total_freed_bytes)}`
  return `${what} · ${fmtTimestamp(event.at)}`
}

/* The events worth a marker — pauses, resumes, and reclaims that freed something (a no-op pass explains no
   drop) — inside `[window_start, window_end]`, merged when closer than `MARKER_MERGE_FRACTION`. */
function diskMarkers(events: readonly NodeDiskEvent[], windowStart: string, windowEnd: string): ChartMarker[] {
  const start = Date.parse(windowStart)
  const span = Date.parse(windowEnd) - start
  if (!Number.isFinite(start) || !Number.isFinite(span) || span <= 0) return []
  const placed = events
    .filter(e => e.kind !== 'reclaim' || e.total_freed_bytes > 0)
    .map(e => ({ event: e, frac: (Date.parse(e.at) - start) / span }))
    .filter(({ frac }) => Number.isFinite(frac) && frac >= 0 && frac <= 1)
    .sort((a, b) => a.frac - b.frac)

  const groups: Array<typeof placed> = []
  for (const item of placed) {
    const group = groups[groups.length - 1]
    if (group && item.frac - group[0].frac < MARKER_MERGE_FRACTION) group.push(item)
    else groups.push([item])
  }
  return groups.map(group => {
    const lines = group.map(({ event }) => markerLine(event))
    const frac = group.reduce((sum, g) => sum + g.frac, 0) / group.length
    return {
      at: new Date(start + frac * span),
      frac,
      kind: group.reduce<MarkerKind>(
        (best, g) => (MARKER_RANK[g.event.kind] > MARKER_RANK[best] ? g.event.kind : best),
        'reclaim',
      ),
      lines:
        lines.length > MARKER_TOOLTIP_LINES
          ? [...lines.slice(0, MARKER_TOOLTIP_LINES), `+${lines.length - MARKER_TOOLTIP_LINES} more`]
          : lines,
    }
  })
}

/** The gate in words: "New work: running" / "New work paused since …". */
function gateLine(gate: NodeDiskGate): string {
  return gate.state === 'paused'
    ? `New work paused since ${fmtTimestamp(gate.since)}`
    : 'New work: running'
}

// ── One chart ─────────────────────────────────────────────────────────────────────────────────────

/** Plot height: 1% is more than a pixel, and two charts plus figures fit one screen. */
const CHART_PX = 168
/** The y-axis band, wide enough for `100%` / `2.5k` at 12px. */
const AXIS_PX = 40
/** The plot's margins outside the axes; the marker strip under the plot is inset by the same amounts. */
// `right`: the newest stamp sits ON the right edge and needs room for half its tick label.
const MARGIN = { top: 12, right: 28, bottom: 0, left: 0 } as const
/** The x-axis band under the plot (tick labels). */
const X_AXIS_PX = 32

/* A HOLE IN THE DATA, said as a region, not only a broken line: at 168px the area fill either side made a
   one-line break read as a dark seam. Opaque paper first (so no fill or gridline shows through — nothing was
   measured there), then a diagonal hatch in the secondary text colour and dashed edges: "no data" in both
   themes without borrowing a status hue. At least 4px, so a short hole stays a tap target. */
const GAP_BAND_SX = {
  position: 'absolute',
  top: 0,
  bottom: 0,
  minWidth: 4,
  pointerEvents: 'auto',
  bgcolor: 'background.paper',
  backgroundImage: (theme: Theme) =>
    `repeating-linear-gradient(135deg, ${alpha(theme.palette.text.secondary, 0.28)} 0 1.5px, transparent 1.5px 6px)`,
  borderLeft: '1px dashed',
  borderRight: '1px dashed',
  borderColor: 'text.disabled',
  cursor: 'default',
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: -2 },
} as const
const GUIDE_LABEL_STYLE = { fontSize: 11 } as const

function MetricChart({
  title,
  unitNote,
  points,
  pick,
  hours,
  bucketSecs,
  windowStart,
  windowEnd,
  guides,
  format = formatPct,
  axisFormat,
  scaleMax = 100,
  guideMeaning,
  markers = [],
  gaps = [],
  note,
  lines,
  testId,
}: {
  title: string
  /** What the percentage is OF — "of 16 cores", "of 59.4 GB". Null when the node never reported it. */
  unitNote: string | null
  points: NodeMetricsPoint[]
  pick: Pick
  hours: number
  bucketSecs: number
  /** The x-axis domain, the payload's (`app/node_host_metrics.py::window_bounds`), never `Date.now()`. */
  windowStart: string
  windowEnd: string
  /** The server's thresholds for a percentage chart; `null` for a series no level judges (network). */
  guides: { warnPct: number; criticalPct: number } | null
  /** How one value reads, in the figures, the tooltip and the accessible name. */
  format?: (value: number) => string
  /** A shorter spelling for the y-axis ticks; required when `guides` is null. */
  axisFormat?: (value: number) => string
  /** The value at the top of the plot (100 for a percentage). */
  scaleMax?: number
  /** What each guide line DOES, said in a legend under the plot. */
  guideMeaning?: { warn: string; critical: string }
  /** Events marked at their time (the disk chart's cleanups and pauses). */
  markers?: ChartMarker[]
  /** The payload's sample holes (`gapsOf`), one set shared by every chart. */
  gaps?: SampleGap[]
  /** One line under the chart (the disk gate). */
  note?: ReactNode
  /** Several lines on the one axis (Pressure), each with its latest figure and a legend entry; `pick` is
   *  then unused. Lines no bucket carried are left out. */
  lines?: readonly Line[]
  testId: string
}): JSX.Element {
  const mode = useColorMode()
  const raw = RAW[mode]
  const plotRef = useRef<HTMLDivElement>(null)
  const drawable = useHasWidth(plotRef)
  const series = useMemo(() => seriesOf(points, pick, bucketSecs), [points, pick, bucketSecs])
  const figures = useMemo(() => figuresOf(points, pick), [points, pick])
  const newest = series.ys.reduce<number>((last, v, i) => (v == null ? last : i), -1)
  /* The several-line form: each line its own series (same buckets, so the same x positions) and figures.
     Colours are the dashboard's validated categorical set in its FIXED order by the line's place in `lines`
     (a fixed set, so colour still follows the line, and the first three hues are clearly distinct). */
  const multi = useMemo(() => {
    if (!lines) return null
    return lines.flatMap((line, slot) => {
      if (!points.some(p => line.pick(p) != null)) return []
      const own = seriesOf(points, line.pick, bucketSecs)
      return [{
        ...line,
        color: categoricalSeries(mode, slot),
        series: own,
        figures: figuresOf(points, line.pick),
        newest: own.ys.reduce<number>((last, v, i) => (v == null ? last : i), -1),
      }]
    })
  }, [lines, points, bucketSecs, mode])
  const xs = multi?.[0]?.series.xs ?? series.xs
  // The server's window, when usable; a broken payload falls back to the data's own extent.
  const start = Date.parse(windowStart)
  const end = Date.parse(windowEnd)
  const domain = Number.isFinite(start) && Number.isFinite(end) && end > start ? { min: new Date(start), max: new Date(end) } : {}
  /* Each hole's place across the plot (0..1), on the same domain the x axis draws: the server's window, else
     the data's extent. Clipped to it; a hole wholly outside draws nothing. */
  const bands = useMemo(() => {
    const windowed = Number.isFinite(start) && Number.isFinite(end) && end > start
    const times = xs.map(x => x.getTime())
    const lo = windowed ? start : Math.min(...times)
    const span = (windowed ? end : Math.max(...times)) - lo
    if (!Number.isFinite(span) || span <= 0) return []
    return gaps.flatMap(gap => {
      const left = Math.max(0, (gap.from - lo) / span)
      const right = Math.min(1, (gap.to - lo) / span)
      return right > left ? [{ ...gap, left, width: right - left }] : []
    })
  }, [gaps, xs, start, end])

  /* The chart's accessible name: the same four figures as printed, plus the scale and any thresholds. */
  const spoken =
    multi !== null
      ? `${title} over ${spokenRange(hours)}: ` +
        (multi.length === 0
          ? 'no readings.'
          : multi
              .map(line => `${line.label} latest ${format(line.figures!.latest)}, maximum ${format(line.figures!.max)}`)
              .join('; ') + `. Scale 0 to ${format(scaleMax)}.`) +
        bands.map(band => ` ${band.label}.`).join('')
      : figures === null
      ? `${title} over ${spokenRange(hours)}: no readings.`
      : `${title} over ${spokenRange(hours)}: latest ${format(figures.latest)}, ` +
        `minimum ${format(figures.min)}, maximum ${format(figures.max)}, mean ${format(figures.mean)}. ` +
        `Scale 0 to ${guides === null ? format(scaleMax) : '100 percent'}.` +
        (guides === null
          ? ''
          : guideMeaning
            ? ` ${formatPct(guides.warnPct)}: ${guideMeaning.warn}; ${formatPct(guides.criticalPct)}: ${guideMeaning.critical}.`
            : ` Warning threshold ${formatPct(guides.warnPct)}, critical ${formatPct(guides.criticalPct)}.`) +
        bands.map(band => ` ${band.label}.`).join('')
  const markerKinds = (['reclaim', 'pause', 'resume'] as const).filter(k => markers.some(m => m.kind === k))
  const tick = axisFormat ?? format

  return (
    <Box data-testid={testId} sx={{ display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="subtitle2" component="h3" sx={{ fontWeight: 600 }}>
          {title}
        </Typography>
        <Typography variant="caption" data-testid={`${testId}-caption`} sx={{ color: 'text.secondary' }}>
          {[unitNote, `${humanDuration(bucketSecs)} buckets`].filter(Boolean).join(' · ')}
        </Typography>
      </Box>

      {/* Figures above the plot (the answer first); `absence` prose when the window has no bucket. */}
      {multi !== null ? (
        multi.length === 0 ? (
          <Typography variant="body2" sx={{ color: 'text.disabled' }}>
            no readings in this window
          </Typography>
        ) : (
          <StatGrid>
            {multi.map(line => (
              <Stat
                key={line.id}
                mono
                label={line.label}
                value={format(line.figures!.latest)}
                hint={`Max ${format(line.figures!.max)} · mean ${format(line.figures!.mean)}`}
                testId={`${testId}-${line.id}-latest`}
              />
            ))}
          </StatGrid>
        )
      ) : figures === null ? (
        <Typography variant="body2" sx={{ color: 'text.disabled' }}>
          no readings in this window
        </Typography>
      ) : (
        <StatGrid>
          <Stat mono label="Latest" value={format(figures.latest)} testId={`${testId}-latest`} />
          <Stat mono label="Min" value={format(figures.min)} testId={`${testId}-min`} />
          <Stat mono label="Max" value={format(figures.max)} testId={`${testId}-max`} />
          <Stat mono label="Mean" value={format(figures.mean)} testId={`${testId}-mean`} />
        </StatGrid>
      )}

      {/* The plot and its gap bands share one box: the bands sit OVER the drawing area (inset by the plot's
          margins and axes), outside the `role="img"` plot so each stays a focusable, named target. */}
      <Box sx={{ position: 'relative', minWidth: 0 }}>
        <Box
          ref={plotRef}
          role="img"
          aria-label={spoken}
          data-testid={`${testId}-plot`}
          data-scale-max={guides === null ? scaleMax : 100}
          sx={{ height: CHART_PX, minWidth: 0 }}
        >
          {drawable && (
            <LineChart
              skipAnimation
              hideLegend
              height={CHART_PX}
              margin={MARGIN}
              grid={{ horizontal: true }}
              xAxis={[{
                scaleType: 'time',
                data: xs,
                ...domain,
                tickLabelStyle: TICK_STYLE,
                // x-charts ellipsizes a tick label that does not fit its band; under 32px every stamp is blank.
                height: X_AXIS_PX,
                valueFormatter: (at: Date, ctx) =>
                  ctx.location === 'tick' ? tickStamp(at, hours) : fmtTimestamp(at.toISOString()),
              }]}
              // Fixed: 0–100 for a percentage, 0–`scaleMax` shared by both network charts.
              yAxis={[{
                min: 0,
                max: guides === null ? scaleMax : 100,
                // Thirds of nothing: the threshold lines carry their own labels, so the ticks stay at 0/half/top.
                tickInterval: [0, (guides === null ? scaleMax : 100) / 2, guides === null ? scaleMax : 100],
                valueFormatter: (v: number) => (guides === null ? tick(v) : `${v}%`),
                tickLabelStyle: TICK_STYLE,
                width: AXIS_PX,
              }]}
              series={multi !== null
                ? multi.map(line => ({
                    id: line.id,
                    data: line.series.ys,
                    label: line.label,
                    color: line.color,
                    // No fill: overlapping areas would tint each other.
                    connectNulls: false,
                    showMark: ({ index }: { index: number }) => index === line.newest,
                    valueFormatter: (v: number | null) => (v == null ? '—' : format(v)),
                  }))
                : [{
                    data: series.ys,
                    label: title,
                    color: raw.primary,
                    area: true,
                    // Gaps stay gaps, never bridged or drawn as 0.
                    connectNulls: false,
                    // Only the newest reading is marked, so a one-point series is still visible.
                    showMark: ({ index }) => index === newest,
                    valueFormatter: v => (v == null ? '—' : format(v)),
                  }]}
              sx={{
                [`& .${lineClasses.area}`]: { fillOpacity: 0.14 },
                [`& .${lineClasses.line}`]: { strokeWidth: 2 },
              }}
            >
              {/* Each event's time as a faint vertical rule, so a drop lines up with its cause. */}
              {markers.map(marker => (
                <ChartsReferenceLine
                  key={`rule-${marker.frac}`}
                  x={marker.at}
                  lineStyle={{ stroke: raw[MARKER_PALETTE[marker.kind]], strokeOpacity: 0.5 }}
                />
              ))}
              {/* The thresholds at the server's percentages, dashed (annotation, not data). */}
              {guides !== null && (
                <>
                  <ChartsReferenceLine
                    y={guides.warnPct}
                    label={formatPct(guides.warnPct)}
                    labelAlign="start"
                    labelStyle={{ ...GUIDE_LABEL_STYLE, fill: raw.warning }}
                    lineStyle={{ stroke: raw.warning, strokeDasharray: '5 4', strokeWidth: 1.5 }}
                  />
                  <ChartsReferenceLine
                    y={guides.criticalPct}
                    label={formatPct(guides.criticalPct)}
                    labelAlign="start"
                    labelStyle={{ ...GUIDE_LABEL_STYLE, fill: raw.error }}
                    lineStyle={{ stroke: raw.error, strokeDasharray: '5 4', strokeWidth: 1.5 }}
                  />
                </>
              )}
            </LineChart>
          )}
        </Box>
        {bands.length > 0 && (
          <Box
            data-testid={`${testId}-gaps`}
            sx={{
              position: 'absolute',
              top: `${MARGIN.top}px`,
              bottom: `${X_AXIS_PX}px`,
              left: `${MARGIN.left + AXIS_PX}px`,
              right: `${MARGIN.right}px`,
              pointerEvents: 'none',
            }}
          >
            {bands.map(band => (
              <Tooltip key={band.from} title={band.label} enterTouchDelay={0} leaveTouchDelay={6000}>
                <Box
                  component="span"
                  tabIndex={0}
                  role="img"
                  aria-label={band.label}
                  data-testid={`${testId}-gap`}
                  sx={GAP_BAND_SX}
                  // Per band, so inline: one generated class per hole would only grow the stylesheet.
                  style={{ left: `${band.left * 100}%`, width: `${band.width * 100}%` }}
                />
              </Tooltip>
            ))}
          </Box>
        )}
      </Box>

      {/* The event ticks, in their own strip under the plot (inset to its drawing area), each a focusable
          target with a tap-opened tooltip saying what happened and when. Outside the `role="img"` plot, whose
          children assistive tech never reaches. */}
      {markers.length > 0 && (
        <Box sx={{ pl: `${MARGIN.left + AXIS_PX}px`, pr: `${MARGIN.right}px`, minWidth: 0 }}>
          <Box data-testid={`${testId}-markers`} sx={{ position: 'relative', height: 16 }}>
            {markers.map(marker => (
              <Tooltip
                key={`marker-${marker.frac}`}
                enterTouchDelay={0}
                leaveTouchDelay={6000}
                title={
                  <Box component="span" sx={{ display: 'flex', flexDirection: 'column' }}>
                    {marker.lines.map((line, i) => (
                      <span key={i}>{line}</span>
                    ))}
                  </Box>
                }
              >
                <Box
                  component="span"
                  tabIndex={0}
                  role="img"
                  aria-label={marker.lines.join('. ')}
                  data-testid={`${testId}-marker`}
                  data-kind={marker.kind}
                  data-count={marker.lines.length}
                  sx={{
                    position: 'absolute',
                    top: 0,
                    left: `${marker.frac * 100}%`,
                    transform: 'translateX(-50%)',
                    width: 14,
                    height: 16,
                    display: 'flex',
                    justifyContent: 'center',
                    borderRadius: 0.5,
                    cursor: 'default',
                    '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
                  }}
                >
                  <Box
                    component="span"
                    sx={{ width: 4, height: 12, mt: '2px', borderRadius: 1, bgcolor: `${MARKER_PALETTE[marker.kind]}.main` }}
                  />
                </Box>
              </Tooltip>
            ))}
          </Box>
        </Box>
      )}

      {/* What the lines and ticks mean, under the plot: wraps at 360px, never clipped. */}
      {(guideMeaning && guides !== null) || markerKinds.length > 0 || bands.length > 0 || (multi?.length ?? 0) > 1 ? (
        <Box
          data-testid={`${testId}-legend`}
          sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 1.5, rowGap: 0.25, alignItems: 'center', minWidth: 0 }}
        >
          {/* A several-line chart names each line: identity is never colour alone. */}
          {multi !== null && multi.length > 1 && multi.map(line => (
            <LegendItem
              key={line.id}
              swatch={<Box component="span" sx={{ width: 14, height: 3, borderRadius: 1, bgcolor: line.color }} />}
              text={line.label}
              testId={`${testId}-legend-${line.id}`}
            />
          ))}
          {guideMeaning && guides !== null && (
            <>
              <LegendItem
                swatch={<DashSwatch color="warning.main" />}
                text={`${formatPct(guides.warnPct)} ${guideMeaning.warn}`}
                testId={`${testId}-legend-warn`}
              />
              <LegendItem
                swatch={<DashSwatch color="error.main" />}
                text={`${formatPct(guides.criticalPct)} ${guideMeaning.critical}`}
                testId={`${testId}-legend-critical`}
              />
            </>
          )}
          {markerKinds.map(kind => (
            <LegendItem
              key={kind}
              swatch={
                <Box component="span" sx={{ width: 4, height: 10, borderRadius: 1, bgcolor: `${MARKER_PALETTE[kind]}.main` }} />
              }
              text={MARKER_WORD[kind]}
              testId={`${testId}-legend-${kind}`}
            />
          ))}
          {bands.length > 0 && (
            <LegendItem
              swatch={<Box component="span" sx={[GAP_BAND_SX, { position: 'static', display: 'inline-block', width: 14, height: 10 }]} />}
              text="no samples"
              testId={`${testId}-legend-gap`}
            />
          )}
        </Box>
      ) : null}

      {note}
    </Box>
  )
}

function DashSwatch({ color }: { color: string }): JSX.Element {
  return <Box component="span" sx={{ width: 14, borderTop: '2px dashed', borderColor: color }} />
}

function LegendItem({ swatch, text, testId }: { swatch: ReactNode; text: string; testId: string }): JSX.Element {
  return (
    <Box component="span" data-testid={testId} sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5 }}>
      {swatch}
      <Typography variant="caption" component="span" sx={{ color: 'text.secondary' }}>
        {text}
      </Typography>
    </Box>
  )
}

// ── The panel ─────────────────────────────────────────────────────────────────────────────────────

/** `minWidth: 0` is load-bearing: a `width: 100%` plot cannot shrink in a flex tab panel otherwise. */
const PANEL_ROOT_SX = { minWidth: 0 } as const

// `?? null`: CPU/RAM are null where withheld (`types.ts`); an older server's points carry no disk/net key.
const PICK_CPU = (p: NodeMetricsPoint): number | null => p.cpu_pct ?? null
const PICK_MEM = (p: NodeMetricsPoint): number | null => p.mem_pct ?? null
const PICK_DISK = (p: NodeMetricsPoint): number | null => p.disk_pct ?? null
const PICK_NET_RX = (p: NodeMetricsPoint): number | null => p.net_rx_bps ?? null
const PICK_NET_TX = (p: NodeMetricsPoint): number | null => p.net_tx_bps ?? null
const PICK_RUNNERS = (p: NodeMetricsPoint): number | null => p.runners ?? null
const PICK_TMP_INODES = (p: NodeMetricsPoint): number | null => p.tmp_inode_pct ?? null
// The host signals: optional on the wire (an older server sends none).
const PICK_LOAD = (p: NodeMetricsPoint): number | null => p.load_1 ?? null
const PICK_SWAP = (p: NodeMetricsPoint): number | null => p.swap_pct ?? null
const PICK_NONE = (): number | null => null
/* Pressure: `some avg60` per resource, three lines on one 0–100 axis (the same unit, so one chart). */
const PRESSURE_LINES: readonly Line[] = [
  { id: 'cpu', label: 'CPU', pick: p => p.psi_cpu_pct ?? null },
  { id: 'mem', label: 'Memory', pick: p => p.psi_mem_pct ?? null },
  { id: 'io', label: 'IO', pick: p => p.psi_io_pct ?? null },
]

const METRICS_ERROR = 'Failed to load this node’s metrics'
const NO_EVENTS: readonly NodeDiskEvent[] = []

export default function NodeMetrics({
  nodeId,
  diskGate = null,
  diskEvents = NO_EVENTS,
}: {
  nodeId: string
  /** The node's disk gate (`NodeOut.disk_gate`), said under the disk chart; null says nothing. */
  diskGate?: NodeDiskGate | null
  /** The disk chart's markers: `NodeDiskEvents`' list, read once by the page (`useNodeDiskEvents`). An
   *  annotation, so a failed read is simply no markers here (the Disk card carries the banner). */
  diskEvents?: readonly NodeDiskEvent[]
}): JSX.Element {
  const [hours, setHours] = useState(DEFAULT_HOURS)

  /* The payload survives a range change (everything renders from `payload.hours`, so the old chart stays
     correctly labelled until the new one lands) and a failed refetch (the last good chart stays under a
     banner, as `NodeHealth`). The page remounts this per node. */
  const { data: payload, error: failed, refresh } = useApiRead(
    ['node-metrics', nodeId, hours],
    () => get<NodeMetricsPayload>(`/api/nodes/${encodeURIComponent(nodeId)}/metrics?hours=${hours}`),
    METRICS_ERROR,
  )
  const error = failed ? METRICS_ERROR : ''
  const hasDisk = payload?.points.some(p => PICK_DISK(p) != null) ?? false
  // Once per payload, shared by every chart (one hole is one hole on all of them).
  const gaps = useMemo(() => (payload ? gapsOf(payload.points, payload.bucket_secs) : []), [payload])

  /* The live tick — any `node` event, as `NodeHealth`. Not narrowed: `nodeId` is the route's NAME while
   * emitters send the node's UUID, and `entity: 'node'` fires only on CRUD and config-snapshot changes
   * (not the ~5s observation pass), so there is no storm to narrow. */
  useLiveRefetch(useEventStream(), () => void refresh(), null, { entity: 'node' })

  /* The range selector: RADIO semantics — exactly one range applies (see `ui/SegmentedControl`). */
  const selector = (
    <SegmentedControl
      value={hours}
      options={RANGES.map(range => ({ value: range.hours, label: range.label }))}
      onChange={setHours}
      ariaLabel="Metrics range"
    />
  )

  /* `data-testid="node-metrics"` is on EVERY state, unsettled ones included, so a host's "renders here"
     assertion is not a race (as `ui/Banner`, `ui/EmptyState`). `PANEL_ROOT_SX` lets charts shrink. */
  if (payload === null) {
    return (
      <Box data-testid="node-metrics" sx={PANEL_ROOT_SX}>
        <SectionCard title="Metrics" actions={selector}>
          {error ? (
            <Banner severity="error" testId="metrics-load-error">
              {error}
            </Banner>
          ) : (
            <LoadingState rows={3} />
          )}
        </SectionCard>
      </Box>
    )
  }

  /* Never reported: the title row alone, its note on the header and no range picker — there is nothing to
     range over, and a card body saying so cost a phone ~120px. */
  if (payload.latest === null && payload.points.length === 0 && !error) {
    return (
      <Box data-testid="node-metrics" sx={PANEL_ROOT_SX}>
        <SectionCard
          title="Metrics"
          actions={
            <Typography variant="body2" data-testid="metrics-empty" sx={NONE_SX}>
              {NONE}
            </Typography>
          }
        />
      </Box>
    )
  }

  const { latest, points, warn_pct, critical_pct, bucket_secs, window_start, window_end } = payload
  /* The window every chart below shares; each adds only its own series, scale and guides. */
  const series = { points, hours: payload.hours, bucketSecs: bucket_secs, windowStart: window_start, windowEnd: window_end, gaps }
  const guides = { warnPct: warn_pct, criticalPct: critical_pct }
  // A chart only for a series some bucket in the window carried, so an older daemon shows no empty chart.
  const hasCpu = points.some(p => PICK_CPU(p) != null)
  const hasMem = points.some(p => PICK_MEM(p) != null)
  const hasNet = points.some(p => PICK_NET_RX(p) != null || PICK_NET_TX(p) != null)
  const hasTmpInodes = points.some(p => PICK_TMP_INODES(p) != null)
  const tmpInodeGuides = { warnPct: payload.tmp_inode_warn_pct, criticalPct: payload.tmp_inode_alarm_pct }
  const netMax = niceRateMax(points.flatMap(p => [PICK_NET_RX(p), PICK_NET_TX(p)]))
  // Absent from an older server's points; the node's own count, so present even where CPU/RAM are withheld.
  const hasRunners = points.some(p => PICK_RUNNERS(p) != null)
  const hasLoad = points.some(p => PICK_LOAD(p) != null)
  const hasSwap = points.some(p => PICK_SWAP(p) != null)
  const hasPressure = points.some(p => PRESSURE_LINES.some(line => line.pick(p) != null))
  // Buckets exist but carry no figure at all (every one withheld): an honest empty state, not bare axes.
  const hasSeries = hasCpu || hasMem || hasDisk || hasNet || hasRunners || hasTmpInodes || hasLoad || hasSwap || hasPressure
  const level = latest === null ? 'unknown' : headlineLevel(latest)

  return (
    <Box data-testid="node-metrics" sx={PANEL_ROOT_SX}>
      <SectionCard title="Metrics" actions={selector}>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          {error && (
            <Banner severity="error" testId="metrics-load-error">
              {error}
            </Banner>
          )}

          {/* ── The header: the list's own meter at full scale, so row and tab match. */}
          {latest !== null && (
            <Box
              sx={{
                display: 'flex',
                flexDirection: 'column',
                gap: 1.25,
                minWidth: 0,
              }}
            >
              {/* STALE, in words (grey is only a colour). The bound is the payload's `stale_bound_seconds`,
                  never re-derived: the server compares against `max(window_secs, FRESH_BOUND_SECONDS)`. */}
              {latest.stale && (
                <Banner severity="warning" testId="metrics-stale">
                  {`Last reported ${humanDuration(latest.age_seconds)} ago — past the ` +
                    `${humanDuration(latest.stale_bound_seconds)} freshness bound.`}
                </Banner>
              )}
              <Box
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1.5,
                  flexWrap: 'wrap',
                  minWidth: 0,
                }}
              >
                <Box sx={{ flex: '1 1 200px', minWidth: 160, maxWidth: 320 }}>
                  <NodeLoadMeter metrics={latest} variant="full" />
                </Box>
                {/* The list badge's figure, as a word, raised to `warn` by a /tmp inode flag; tone from
                    `statusTone.ts`. */}
                <StatusChip
                  label={level}
                  tone={levelTone(level)}
                  ariaLabel={`Overall load level: ${level}`}
                  data-status={level}
                />
              </Box>
              {/* What the bars cannot say: absolute figures, the mean's span, freshness (no repeat of the
                  percentages, nor of the core count and memory total the chart captions carry). */}
              <StatGrid>
                {/* The RAM tile only beside a RAM figure: where withheld, "not reported" would mislead. */}
                {latest.mem_pct != null && (
                  <Stat
                    mono
                    label="Memory used"
                    absent={latest.mem_total_kb === null}
                    value={
                      latest.mem_total_kb === null
                        ? 'not reported'
                        : kbSize((latest.mem_total_kb * latest.mem_pct) / 100)
                    }
                    testId="metrics-latest-mem"
                  />
                )}
                {/* Disk and network only when reported: an older daemon's node keeps its old strip. */}
                {latest.disk_free_kb != null && latest.disk_total_kb != null && (
                  <>
                    <Stat
                      mono
                      label="Disk free"
                      value={kbSize(latest.disk_free_kb)}
                      testId="metrics-disk-free"
                    />
                    <Stat
                      mono
                      label="Disk total"
                      value={kbSize(latest.disk_total_kb)}
                      testId="metrics-disk-total"
                    />
                  </>
                )}
                {latest.net_rx_bps != null && latest.net_tx_bps != null && (
                  <>
                    <Stat
                      mono
                      label="Network in"
                      value={formatRate(latest.net_rx_bps)}
                      testId="metrics-net-in"
                    />
                    <Stat
                      mono
                      label="Network out"
                      value={formatRate(latest.net_tx_bps)}
                      testId="metrics-net-out"
                    />
                  </>
                )}
                <Stat
                  mono
                  label="Averaged over"
                  value={humanDuration(latest.window_secs)}
                  testId="metrics-window"
                />
                <Stat mono label="Samples" value={String(latest.samples)} testId="metrics-samples" />
                <Stat
                  mono
                  label="Reported"
                  value={humanAge(latest.age_seconds)}
                  testId="metrics-age"
                />
              </StatGrid>
              {/* Load, swap, pressure and the busiest processes: latest, beside the figures above. */}
              <NodeSignals metrics={latest} />
            </Box>
          )}

          {/* ── THE HISTORY ────────────────────────────────────────────────────────────────────────── */}
          {points.length === 0 ? (
            /* A title, not an empty axis; the two empty reasons are named differently. */
            <EmptyState title={latest === null ? 'No metrics reported yet' : 'Nothing in this window'} />
          ) : !hasSeries ? (
            <EmptyState title="No history" />
          ) : (
            <>
              {hasCpu && (
                <MetricChart
                  title="CPU"
                  unitNote={latest?.cpu_count ? `% busy across ${latest.cpu_count} cores` : '% busy'}
                  {...series}
                  pick={PICK_CPU}
                  guides={guides}
                  testId="metrics-chart-cpu"
                />
              )}
              {hasMem && (
                <MetricChart
                  title="Memory"
                  unitNote={latest?.mem_total_kb ? `% used of ${kbSize(latest.mem_total_kb)}` : '% used'}
                  {...series}
                  pick={PICK_MEM}
                  guides={guides}
                  testId="metrics-chart-mem"
                />
              )}
              {/* Beside CPU and memory, so a load climb lines up with how many runners caused it. */}
              {hasRunners && (
                <MetricChart
                  title="Runners"
                  unitNote="sessions running"
                  {...series}
                  pick={PICK_RUNNERS}
                  guides={null}
                  format={formatCount}
                  scaleMax={runnersMax(points.map(PICK_RUNNERS))}
                  testId="metrics-chart-runners"
                />
              )}
              {/* Load against the cores (a load equal to the core count is every core busy), then what is
                  stalling — the pressure lines — and swap: the three signals that explain a slow box. */}
              {hasLoad && (
                <MetricChart
                  title="Load"
                  unitNote={latest?.cpu_count ? `1 min · ${latest.cpu_count} cores` : '1 min'}
                  {...series}
                  pick={PICK_LOAD}
                  guides={null}
                  format={formatLoad}
                  axisFormat={formatCount}
                  scaleMax={loadMax(points.map(PICK_LOAD), latest?.cpu_count ?? null)}
                  testId="metrics-chart-load"
                />
              )}
              {hasPressure && (
                <MetricChart
                  title="Pressure"
                  unitNote="% stalled"
                  {...series}
                  pick={PICK_NONE}
                  lines={PRESSURE_LINES}
                  guides={null}
                  scaleMax={100}
                  testId="metrics-chart-pressure"
                />
              )}
              {hasSwap && (
                <MetricChart
                  title="Swap"
                  unitNote={latest?.swap_total_kb ? `% used of ${kbSize(latest.swap_total_kb)}` : '% used'}
                  {...series}
                  pick={PICK_SWAP}
                  guides={null}
                  scaleMax={100}
                  testId="metrics-chart-swap"
                />
              )}
              {hasDisk && (
                <MetricChart
                  title="Disk"
                  unitNote={
                    latest?.disk_total_kb ? `% used of ${kbSize(latest.disk_total_kb)}` : '% used'
                  }
                  {...series}
                  pick={PICK_DISK}
                  guides={guides}
                  guideMeaning={DISK_GUIDE_MEANING}
                  markers={diskMarkers(diskEvents, window_start, window_end)}
                  note={
                    diskGate ? (
                      <Typography
                        variant="caption"
                        data-testid="metrics-disk-gate"
                        data-state={diskGate.state}
                        sx={{
                          color: diskGate.state === 'paused' ? 'warning.main' : 'text.secondary',
                          fontWeight: diskGate.state === 'paused' ? 600 : undefined,
                        }}
                      >
                        {gateLine(diskGate)}
                      </Typography>
                    ) : null
                  }
                  testId="metrics-chart-disk"
                />
              )}
              {hasTmpInodes && (
                <MetricChart
                  title="/tmp inodes"
                  unitNote="% used"
                  {...series}
                  pick={PICK_TMP_INODES}
                  guides={tmpInodeGuides}
                  guideMeaning={TMP_INODE_GUIDE_MEANING}
                  testId="metrics-chart-tmp-inodes"
                />
              )}
              {hasNet && (
                <>
                  <MetricChart
                    title="Network in"
                    unitNote="bit/s received"
                    {...series}
                    pick={PICK_NET_RX}
                    guides={null}
                    format={formatRate}
                    axisFormat={formatRateTick}
                    scaleMax={netMax}
                    testId="metrics-chart-net-in"
                  />
                  <MetricChart
                    title="Network out"
                    unitNote="bit/s sent"
                    {...series}
                    pick={PICK_NET_TX}
                    guides={null}
                    format={formatRate}
                    axisFormat={formatRateTick}
                    scaleMax={netMax}
                    testId="metrics-chart-net-out"
                  />
                </>
              )}
            </>
          )}
        </Box>
      </SectionCard>
    </Box>
  )
}
