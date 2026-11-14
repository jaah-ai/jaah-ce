import { useCallback, useEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { ApiError, get } from '../../api'
import { useEventStream } from '../../hooks/useEventStream'
import { useEventsSinceSeen } from '../../hooks/useLiveRefetch'
import { useHiddenDeferral } from '../../hooks/useHiddenDeferral'
import { touchesScope } from '../../lib/taskEventScope'
import type { TaskRunMetrics as RunMetrics } from '../../types'
import { capitalize, compactCount } from '../../lib/humanize'
import { fmtPriceKey } from '../../lib/models'
import { humanDuration } from '../../lib/relativeTime'
import { formatBytes } from '../../lib/formatBytes'
import { formatCostUsd } from '../../lib/taskCost'
import { formatCorePct } from '../NodeLoadMeter'
import { takeTaskPageSeed } from '../../lib/taskPageSeed'
import { Banner } from '../../ui/Banner'
import { Stat, FactsGrid } from '../../ui/FactsGrid'
import { grid } from '../../theme'
import { FEED_META } from '../../ui/listStyles'
import Tip from '../../ui/Tip'

// Details › Metrics' "Task Run Metrics" panel: total run time, agentic time, tokens in/out and token cost
// for ANY task, from `GET /api/tasks/{id}/run-metrics`. Fetched here (`useTaskRunMetrics`) because these
// are computed server-side from state history + captured usage (unlike the Finisher-authored
// `task.metrics`). The hook is exported so `pages/taskDetail/useTaskDetailLoad.ts` can read it ONCE and both gate the tab
// (`hasRunToShow`) and render the panel (`RunMetricsView`). Fetch shape is
// `TaskAttachmentsField`/`TaskDependencies`': shared `get`, a generation guard, `Banner` on failure,
// nothing before the first response.
//
// ── HONESTY IS THIS PANEL'S WHOLE JOB ────────────────────────────────────────────────────────────────
// Every figure can be absent for a reason that is not zero (`types.ts`): `token_status: 'missing'`,
// `cost_status: 'unavailable'`, `clock_status`, `unpriced_models`. Never render those as `0`/`$0.00`
// (absence is not a state). A MEASUREMENT IS MONO, AN ABSENCE IS
// PROSE, so an absence never looks like a small number.

// `compactCount` in the tile: exact counts (~20 chars) would wrap inside a number at 390px. The exact figures are
// the tooltip and accessible name.

// Narrow an unknown payload (as `TaskDependencies`' guard): unrecognised → "nothing to show". Checks the
// five fields the render reads without its own null check.
function isRunMetrics(data: unknown): data is RunMetrics {
  const d = data as Partial<RunMetrics> | null
  return (
    !!d &&
    typeof d.in_progress === 'boolean' &&
    typeof d.clock_status === 'string' &&
    typeof d.token_status === 'string' &&
    typeof d.cost_status === 'string' &&
    Array.isArray(d.unpriced_models)
  )
}

/* One tile's figure: a measurement (mono) or its absence (prose) — exactly one (`ui/FactsGrid`'s `Stat`). */
function figure(value: string | null, absence: string): { value: string; absent: boolean; mono: true } {
  return { value: value ?? absence, absent: value === null, mono: true }
}

export interface RunMetricsState {
  metrics: RunMetrics | null
  error: string
  /** The first answer for THIS task has arrived (a figure, nothing, or a failure). */
  settled: boolean
  /** Read at call time: the latest read has landed and no live re-read is pending — `metrics` is what a fresh
   *  read would answer (the task page hands it across a remount, `lib/taskPageSeed.ts`). */
  isCurrent: () => boolean
}

/* What one answer said, and for which task: keyed so a task switch never shows the previous task's figures
   (or its tab) for even one render. */
interface Answer {
  taskId: number
  metrics: RunMetrics | null
  error: string
}

/* An answer already in hand for `taskId` (the task page's `…/page` bundle part, or a handed-over read): the panel's
   answer until its own read lands, so the caller commits it with the task instead of a render later. */
export interface RunMetricsSeed {
  taskId: number
  data: unknown
}

/* The panel's read, live. `taskId` null (create mode, a non-numeric id) reads nothing. */
export function useTaskRunMetrics(taskId: number | null, seed?: RunMetricsSeed | null): RunMetricsState {
  const [answer, setAnswer] = useState<Answer | null>(null)

  // Generation guard: mount and live refetches can resolve out of order.
  const fetchGenRef = useRef(0)

  // The on-screen ids, mirrored into a ref so the live effect can filter without depending on `answer`
  // (which its own refetch sets — an infinite loop).
  const scopeRef = useRef<ReadonlySet<number> | null>(null)
  /* A 404 is final: the task was deleted, and no later event brings it back (else a deleted task's page
     re-reads this route on every fleet event). Cleared only by a new `taskId`. */
  const goneRef = useRef(false)
  // The latest read's generation once landed, and a live re-read heard but not yet started (`isCurrent`).
  const landedGenRef = useRef(0)
  const pendingRef = useRef(false)

  const load = useCallback(async () => {
    const gen = ++fetchGenRef.current
    pendingRef.current = false
    if (taskId === null) {
      scopeRef.current = null
      return
    }
    try {
      const data = takeTaskPageSeed<RunMetrics>(taskId, 'run_metrics') ?? await get<RunMetrics>(`/api/tasks/${taskId}/run-metrics`)
      if (gen !== fetchGenRef.current) return
      const recognised = isRunMetrics(data)
      scopeRef.current = recognised ? new Set(data.scope_task_ids) : null
      goneRef.current = false
      landedGenRef.current = gen
      setAnswer({ taskId, metrics: recognised ? data : null, error: '' })
    } catch (err) {
      if (gen !== fetchGenRef.current) return
      if (err instanceof ApiError && err.status === 404) goneRef.current = true
      // A failed read leaves no scope to trust, so any event refetches.
      scopeRef.current = null
      setAnswer({ taskId, metrics: null, error: 'Failed to load run metrics' })
    }
  }, [taskId])

  useEffect(() => {
    goneRef.current = false
    void load()
  }, [load])

  /* LIVE (as `TaskDependencies`): `in_progress` totals are measured to NOW. An in-scope `task` event
     recomputes (`touchesScope` over `scope_task_ids`: each refetch is a recursive-subtree CTE plus a
     transition-trail read) — never after a 404, and a hidden tab once on its return. */
  const stream = useEventStream('/api/stream', ['task'])
  const reload = useHiddenDeferral(() => {
    if (!goneRef.current) void load()
  })
  useEventsSinceSeen(stream, (events, overrun) => {
    if (taskId === null || goneRef.current) return
    if (!overrun && !events.some(event => event.entity === 'task' && touchesScope(event, scopeRef.current))) return
    pendingRef.current = true
    reload()
  })

  /* Its own read (which takes the same part off the page seed) replaces the seed's answer with the same figures, so the
     handover never redraws; a task switch never shows the seed of another id. */
  const current = answer !== null && answer.taskId === taskId
    ? answer
    : seed && taskId !== null && seed.taskId === taskId
      ? { taskId, metrics: isRunMetrics(seed.data) ? seed.data : null, error: '' }
      : null
  const isCurrent = useCallback(() => landedGenRef.current === fetchGenRef.current && !pendingRef.current, [])
  return { metrics: current?.metrics ?? null, error: current?.error ?? '', settled: current !== null, isCurrent }
}

/* Whether the panel has anything to show: a measured run, or a failed read (a failure is never hidden as
   "nothing to show"). A never-started task and the moment before the first response have nothing. */
export function hasRunToShow({ metrics, error }: Pick<RunMetricsState, 'metrics' | 'error'>): boolean {
  return Boolean(error) || (metrics !== null && metrics.clock_status !== 'not_started')
}

// The self-fetching form, for a caller that needs no gate (today: its own test).
export default function TaskRunMetrics({ taskId }: { taskId: number }) {
  return <RunMetricsView {...useTaskRunMetrics(taskId)} />
}

export function RunMetricsView({ metrics, error }: Pick<RunMetricsState, 'metrics' | 'error'>) {
  if (error) return <Banner severity="error">{error}</Banner>
  if (!metrics) return null

  /* NEVER STARTED: one plain line (not `ui/EmptyState`'s tall block). Gated on `clock_status`, never a null
     `started_at` (a real run may predate the transition trail). Worded to claim only what an empty trail
     proves: a Reset soft-deletes the attempt's trail (`app/task_artifacts.py::activity_row_is_visible`)
     and `_RESET_FRESH_COLUMNS` clears `metrics`, indistinguishable from never dispatched. */
  if (metrics.clock_status === 'not_started') {
    return (
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        No run to measure yet.
      </Typography>
    )
  }

  const { tokens_in, tokens_out, token_status, cost_usd, cost_status, unpriced_models } = metrics
  // The run happened, only its timeline is missing: the clock tiles say "no timeline"; capture tiles are
  // untouched.
  const clockAbsent = metrics.clock_status === 'unavailable'
  const clockAbsence = clockAbsent ? 'no timeline' : 'not captured'
  const tokensMissing = token_status === 'missing'
  const cost = cost_usd === null ? null : Number(cost_usd)
  // An unparseable `cost_usd` takes the absence branch (should be unreachable).
  const costAbsent = cost_status === 'unavailable' || cost === null || Number.isNaN(cost)
  /* What ran: the tier dispatched → the model the session recorded, else the `--model` given, else the CLI's
     own default. Omitted entirely when never dispatched (no absence tile: nothing was chosen yet). */
  const model = metrics.model ?? null
  /* The sessions' own resource peaks: tiles only when some session reported — an older server or a
     run before the daemon measured sessions says nothing, rather than three "not reported" tiles. */
  const peakRss = metrics.peak_rss_kb ?? null
  const peakCpu = metrics.peak_cpu_pct ?? null
  const cpuSeconds = metrics.cpu_seconds ?? null
  const hasUsage = peakRss !== null || peakCpu !== null || cpuSeconds !== null
  const modelValue = model
    ? [model.tier ? capitalize(model.tier) : null, model.ran ?? model.dispatched ?? 'CLI default']
        .filter(Boolean)
        .join(' → ')
    : null

  return (
    <Box data-testid="task-run-metrics" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {/* One in-progress line for the panel: every figure is a running total. */}
      {metrics.in_progress && (
        <Typography variant="caption" data-testid="run-metrics-in-progress" sx={{ color: 'info.main' }}>
          Still running — running totals.
        </Typography>
      )}

      {/* One no-timeline line for the panel (a fact about the run); `text.secondary`, nothing to fix. */}
      {clockAbsent && (
        <Typography variant="caption" data-testid="run-metrics-no-timeline" sx={{ color: 'text.secondary' }}>
          No transition timeline — times unavailable. Token figures unaffected.
        </Typography>
      )}

      {/* Four tiles, ONE ROW from 900px, 2×2 below (the optional Model tile takes its own full row) — a fixed count, since `auto-fit` orphans a 3+1 around
          490-600px. Tracks capped at 192px so the facts cluster instead of spreading across the Metrics
          tab's full width. `minmax(0, …)` fits `42.8M / 613K` at 390px. */}
      <FactsGrid fit
        sx={{
          gridTemplateColumns: `repeat(2, minmax(0, ${grid(32)}))`,
          [FEED_META]: { gridTemplateColumns: `repeat(4, minmax(0, ${grid(32)}))` },
        }}
      >
        <Stat label="Total run time" {...figure(metrics.total_seconds === null ? null : humanDuration(metrics.total_seconds, true), clockAbsence)} />
        <Stat label="Agentic time" {...figure(metrics.agentic_seconds === null ? null : humanDuration(metrics.agentic_seconds, true), clockAbsence)} />
        <Stat
          label="Tokens in / out"
          /* 'missing': the counts are zero for want of a capture, so no numbers at all. */
          {...figure(tokensMissing ? null : `${compactCount(tokens_in)} / ${compactCount(tokens_out)}`, 'not captured')}
          exact={tokensMissing ? undefined : `${tokens_in.toLocaleString()} in / ${tokens_out.toLocaleString()} out`}
          qualifier={token_status === 'partial' ? 'partial — some captures are missing' : undefined}
        />
        <Stat
          label="Token cost estimate"
          hint="List rates when it ran"
          {...figure(costAbsent ? null : formatCostUsd(cost as number), 'not captured')}
          /* "excluded": pre-per-class-capture sessions — unrecoverable, nothing to fix (unlike the line below). */
          qualifier={!costAbsent && cost_status === 'partial' ? 'partial — some usage is excluded' : undefined}
        />
        {hasUsage && (
          <>
            <Stat label="Peak RAM" {...figure(peakRss === null ? null : formatBytes(peakRss * 1024), 'not reported')} />
            <Stat label="Peak CPU" {...figure(peakCpu === null ? null : formatCorePct(peakCpu), 'not reported')} />
            <Stat label="CPU time" {...figure(cpuSeconds === null ? null : humanDuration(cpuSeconds), 'not reported')} />
          </>
        )}
        {modelValue !== null && <Stat mono wide label="Model" value={modelValue} exact={modelValue} />}
      </FactsGrid>

      {/* The one operator-FIXABLE gap: these models have no rate, so their spend is missing from the cost.
          Named individually (one price row each); the cost status is partial when any are present. A row
          added now prices later captures; a capture already written keeps the figure it was priced at. */}
      {unpriced_models.length > 0 && (
        <Tip title={`Price row names: ${unpriced_models.join(', ')}`}><Typography
          variant="caption"
          data-testid="run-metrics-unpriced"
          sx={{ color: 'warning.main', overflowWrap: 'anywhere' }}
        >
          Cost excludes {unpriced_models.length === 1 ? 'an unpriced model' : 'unpriced models'}:{' '}
          {unpriced_models.map(fmtPriceKey).join(', ')} — price {unpriced_models.length === 1 ? 'it' : 'them'} in Settings for later runs.
        </Typography></Tip>
      )}
    </Box>
  )
}
