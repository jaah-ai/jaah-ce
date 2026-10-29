import { useMemo, useRef } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { LineChart } from '@mui/x-charts/LineChart'
import type { JSX } from 'react'
import { get } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { useColorMode } from '../../hooks/useColorMode'
import { useHasWidth } from '../../hooks/useHasWidth'
import { formatBytes } from '../../lib/formatBytes'
import { clockTime, fmtTimestamp } from '../../lib/relativeTime'
import { assignColors, TICK_STYLE } from '../../ui/chartPalette'
import type { TaskSessionUsage as SessionUsage, TaskSessionUsagePayload, TaskSessionUsagePoint } from '../../types'
import Banner from '../../ui/Banner'
import { formatCorePct } from '../NodeLoadMeter'

/* ══ A TASK'S SESSIONS, RAM AND CPU OVER TIME ═══════════════════════════════════════════════
 * `GET /api/tasks/{id}/session-usage?hours=N`, N the run's own span (its start to now, whole hours, 1..168 —
 * the retention window), so a short run gets the server's narrow buckets rather than two 42-min points: one
 * line per session on two small
 * charts, RAM then CPU (% of one core, top-style). Two charts, not two axes on one. Nothing drawn when no
 * session reported. Each line breaks where its session sent nothing (`null`, never 0). Colours are the
 * dashboard's validated categorical set, keyed by the session (colour follows the entity). A legend names
 * each line when there are two or more; the figures (peaks) are text above the plots.
 * A reading with no neighbour (a lone bucket, or the newest) carries a mark, so a one-point line is visible.
 * Re-read every minute while the task runs; the span is taken at each read, so it widens with the run.
 */

/** The route's ceiling (`?hours` ≤ 168): the retention window, and the span when the start is unknown. */
const MAX_HOURS = 168

/** Whole hours from `startedAt` to now, rounded up, clamped 1..168; 168 when unknown or unparseable. */
export function spanHours(startedAt: string | null | undefined, now = Date.now()): number {
  const start = startedAt ? Date.parse(startedAt) : NaN
  if (!Number.isFinite(start)) return MAX_HOURS
  return Math.min(MAX_HOURS, Math.max(1, Math.ceil((now - start) / 3_600_000)))
}

/** Indexes that get a mark: the newest reading, and any reading with no reading either side (no line to see). */
export function markedIndexes(ys: ReadonlyArray<number | null>): Set<number> {
  const marked = new Set<number>()
  let newest = -1
  ys.forEach((v, i) => {
    if (v == null) return
    newest = i
    if (ys[i - 1] == null && ys[i + 1] == null) marked.add(i)
  })
  if (newest >= 0) marked.add(newest)
  return marked
}
const POLL_MS = 60_000
const CHART_PX = 120
const MARGIN = { top: 8, right: 24, bottom: 0, left: 0 } as const
const LOAD_ERROR = 'Failed to load session usage'

type Pick = (p: TaskSessionUsagePoint) => number | null

/** A line's legend name: its node, plus the session's first characters when one node ran several. */
function lineLabel(s: SessionUsage, sameNode: boolean): string {
  // The server sends `name: ""` once the node is deleted (the peak outlives it).
  const node = s.node?.name || 'Deleted node'
  return sameNode && s.session_id ? `${node} · ${s.session_id.slice(0, 8)}` : node
}

/** Every session's stamps on one sorted axis; each line is `null` where its session has no bucket. */
function alignedSeries(sessions: readonly SessionUsage[], pick: Pick) {
  const times = [...new Set(sessions.flatMap(s => s.points.map(p => Date.parse(p.at)).filter(Number.isFinite)))].sort(
    (a, b) => a - b,
  )
  const lines = sessions.map(s => {
    const byTime = new Map(s.points.map(p => [Date.parse(p.at), pick(p)]))
    return times.map(t => byTime.get(t) ?? null)
  })
  return { xs: times.map(t => new Date(t)), lines }
}

function SessionChart({
  title,
  sessions,
  pick,
  format,
  labels,
  colors,
  testId,
  floorMax,
  axisWidth,
}: {
  title: string
  sessions: readonly SessionUsage[]
  pick: Pick
  format: (v: number) => string
  labels: string[]
  colors: string[]
  testId: string
  /** The y axis reaches at least this, so near-zero readings don't stretch to fill it (and repeat one tick label). */
  floorMax?: number
  axisWidth: number
}): JSX.Element {
  const plotRef = useRef<HTMLDivElement>(null)
  const drawable = useHasWidth(plotRef)
  const { xs, lines } = useMemo(() => alignedSeries(sessions, pick), [sessions, pick])
  const marks = useMemo(() => lines.map(markedIndexes), [lines])
  const dataMax = Math.max(0, ...lines.flat().filter((v): v is number => v != null))
  const spanHours = xs.length > 1 ? (xs[xs.length - 1].getTime() - xs[0].getTime()) / 3_600_000 : 0
  const spoken = `${title}: ` + sessions
    .map((_s, i) => {
      const values = lines[i].filter((v): v is number => v != null)
      return values.length ? `${labels[i]} peak ${format(Math.max(...values))}` : `${labels[i]} no readings`
    })
    .join('; ')
  return (
    <Box data-testid={testId} sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0 }}>
      <Typography variant="caption" sx={{ color: 'text.secondary', fontWeight: 600 }}>{title}</Typography>
      <Box ref={plotRef} role="img" aria-label={spoken} data-testid={`${testId}-plot`} sx={{ height: CHART_PX, minWidth: 0 }}>
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
              tickLabelStyle: TICK_STYLE,
              height: 28,
              valueFormatter: (at: Date, ctx) =>
                ctx.location === 'tick'
                  ? spanHours > 24
                    ? at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
                    : clockTime(at.toISOString())
                  : fmtTimestamp(at.toISOString()),
            }]}
            yAxis={[{
              min: 0,
              ...(floorMax != null ? { max: Math.max(floorMax, dataMax) } : {}),
              valueFormatter: (v: number) => format(v),
              tickLabelStyle: TICK_STYLE,
              width: axisWidth,
              tickNumber: 3,
            }]}
            series={lines.map((data, i) => ({
              data,
              label: labels[i],
              color: colors[i],
              connectNulls: false,
              showMark: ({ index }: { index: number }) => marks[i].has(index),
              valueFormatter: (v: number | null) => (v == null ? '—' : format(v)),
            }))}
          />
        )}
      </Box>
    </Box>
  )
}

const PICK_RSS: Pick = p => (p.rss_kb == null ? null : p.rss_kb * 1024)
const PICK_CPU: Pick = p => p.cpu_pct ?? null
const bytes = (v: number): string => formatBytes(v)

export function TaskSessionUsageView({ sessions }: { sessions: readonly SessionUsage[] }): JSX.Element | null {
  const mode = useColorMode()
  const drawn = sessions.filter(s => s.points.length > 0)
  if (drawn.length === 0) return null
  const keys = drawn.map(s => `${s.node?.id ?? ''}/${s.session_id}`)
  const palette = assignColors(mode, keys)
  const colors = keys.map(k => palette.get(k) as string)
  const nodes = drawn.map(s => s.node?.id ?? '')
  const labels = drawn.map(s => lineLabel(s, nodes.filter(n => n === (s.node?.id ?? '')).length > 1))

  return (
    <Box data-testid="task-session-usage" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
      {drawn.length > 1 && (
        <Box data-testid="task-session-usage-legend" sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 1.5, rowGap: 0.25 }}>
          {labels.map((label, i) => (
            <Box key={keys[i]} component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, minWidth: 0 }}>
              <Box component="span" sx={{ width: 14, height: 3, borderRadius: 1, bgcolor: colors[i], flex: '0 0 auto' }} />
              <Typography variant="caption" component="span" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
                {label}
              </Typography>
            </Box>
          ))}
        </Box>
      )}
      <Box
        sx={{
          display: 'grid',
          gap: 2,
          gridTemplateColumns: 'minmax(0, 1fr)',
          '@media (min-width:900px)': { gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' },
        }}
      >
        <SessionChart title="RAM" sessions={drawn} pick={PICK_RSS} format={bytes} labels={labels} colors={colors} testId="task-session-ram" axisWidth={68} />
        <SessionChart title="CPU" sessions={drawn} pick={PICK_CPU} format={formatCorePct} labels={labels} colors={colors} testId="task-session-cpu" axisWidth={52} floorMax={100} />
      </Box>
    </Box>
  )
}

export default function TaskSessionUsage({ taskId, startedAt = null, live = false }: {
  taskId: number
  /** The run's start (`TaskRunMetrics.started_at`); null reads the whole retention window. */
  startedAt?: string | null
  live?: boolean
}): JSX.Element | null {
  const { data, error } = useApiRead(
    ['task-session-usage', taskId, startedAt],
    () => get<TaskSessionUsagePayload>(`/api/tasks/${taskId}/session-usage?hours=${spanHours(startedAt)}`),
    LOAD_ERROR,
    { refetchInterval: live ? POLL_MS : false },
  )
  if (error && !data) return <Banner severity="error" testId="task-session-usage-error">{LOAD_ERROR}</Banner>
  return data ? <TaskSessionUsageView sessions={data.sessions ?? []} /> : null
}
