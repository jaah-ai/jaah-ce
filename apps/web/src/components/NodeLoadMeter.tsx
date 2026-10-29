import Box from '@mui/material/Box'
import type { JSX } from 'react'
import type { NodeHostMetrics, NodeHostMetricsLevel, NodeRecord } from '../types'
import type { Tone } from '../ui/StatusChip'
import { TONE_COLOR, toneFor } from '../ui/statusTone'
import { humanAge, humanDuration } from '../lib/relativeTime'
import { scaleBitRate } from '../lib/bitRate'
import { PHONE } from '../ui/listStyles'
import { NONE, NONE_SX } from '../ui/NoneMark'

/* The compact CPU/RAM/disk meter, used on the `/nodes` list and the node's Metrics tab header.
 * It makes no judgement: levels come from the server (`app/node_host_metrics.py::classify`) and map to
 * tones via `ui/statusTone.ts`; see `types.ts`'s NODE HOST METRICS block.
 * Colour is never the only carrier (`Nodes.tsx`'s `STATUS_GLYPH_SX`): (1) the letter `C`/`M`/`D`; (2) fill
 * length against a bordered track (a border survives forced-colours); (3) a `?` after the letter when
 * stale, in a fixed-width slot; (4) the accessible name and `title` carry the figures.
 * Absence is not zero: nullish `metrics` renders a muted em-dash, never a 0% bar (a missing value is
 * shown as missing, never as a zero); a node that never reported disk gets
 * no `D` row at all rather than an empty one, and no `C`/`M` row where CPU/RAM are withheld. Network has
 * no bar (no percentage, no level): it rides the accessible name and tooltip, and the Metrics tab.
 * Bars are `flex: 1` in a `width: 100%` block; `compact` drops the numeric readout, which would crowd
 * the 76px list column.
 * ON A PHONE `compact` drops the letters too: in the 24px rows they stacked into one unbroken column
 * down the list ("CMDCMD"). Each bar keeps a fixed slot instead (C top, M middle, D bottom; a withheld or
 * unreported one is an empty slot, never a bar), so position names the bar under the column's Load header.
 * /tmp INODES are a flag, not a bar: only a `warn` level (the server's) paints a badge. A full inode table
 * breaks every `mktemp` with disk to spare. Compact puts it BESIDE the bars (the 24px row has no fourth slot,
 * and laid over them it hid the C and M bars it shares the cell with); the figure is in its tooltip.
 */

// ── Levels and tones ──────────────────────────────────────────────────────────────────────────────

/** The tone a level paints in; the mapping is `statusTone.ts`'s, and the bar's fill is its `TONE_COLOR`
 *  palette path (a CSS-variable path, so it repaints on `[data-theme]`). */
export function levelTone(level: NodeHostMetricsLevel): Tone {
  return toneFor(`badge-node-host-${level}`)
}

/** The meter's one-word level: `unknown` when stale, else the server's `level` raised to `warn` while it flags
 *  /tmp's inode table (`tmp_inode_level`, kept out of `level` because it is a flag, not a load figure). */
export function headlineLevel(metrics: NodeHostMetrics): NodeHostMetricsLevel {
  if (metrics.stale) return 'unknown'
  if (metrics.tmp_inode_level === 'warn' && (metrics.level === 'ok' || metrics.level === 'unknown')) return 'warn'
  return metrics.level
}

// ── The figures, in words ─────────────────────────────────────────────────────────────────────────

/* An integer percentage, except a real reading in (0, 1) reads `<1%`: a bare `0%` would look like the
never-reported case. */
export function formatPct(pct: number): string {
  const clamped = Math.min(100, Math.max(0, pct))
  if (clamped > 0 && clamped < 1) return '<1%'
  return `${Math.round(clamped)}%`
}

/* A process's or session's CPU as percent of ONE core (top-style: 200% = two cores busy), so never clamped
to 100; a real reading in (0, 1) reads `<1%`, as `formatPct`. */
export function formatCorePct(pct: number): string {
  if (pct > 0 && pct < 1) return '<1%'
  return `${Math.round(Math.max(0, pct))}%`
}

/** A load average: two decimals under 10, one above (`0.42`, `12.5`). */
export function formatLoad(value: number): string {
  return value.toFixed(value >= 10 ? 1 : 2)
}

/* A network rate, from the wire's bytes per second to bits per second — the unit network speed is quoted
in — with decimal prefixes and two to three significant figures. */
export function formatRate(bytesPerSec: number): string {
  const { value, prefix } = scaleBitRate(bytesPerSec, 999.5)
  // Thresholds at the rounding edges, so 99.96 reads "100", never "100.0".
  const shown = !prefix || value >= 99.95 ? Math.round(value).toString() : value.toFixed(value >= 9.995 ? 1 : 2)
  return `${shown} ${prefix}bit/s`
}

/** The figures as a sentence — accessible name and tooltip. It names the window the daemon actually
 *  observed, since "12% CPU" over 40s and over 5min are different claims. `diskHost` names the host whose
 *  disk the figure is, for a co-located node that shares it ("Host disk (aws1-host) 52%"). */
export function hostMetricsSummary(metrics: NodeHostMetrics | null | undefined, diskHost?: string | null): string {
  if (metrics == null) return 'No usage reported'
  // Each figure only when present: CPU/RAM are null where withheld (`types.ts`), disk/network when unreported.
  const parts = [
    metrics.cpu_pct == null ? null : `CPU ${formatPct(metrics.cpu_pct)}`,
    metrics.mem_pct == null ? null : `RAM ${formatPct(metrics.mem_pct)}`,
    metrics.disk_pct == null ? null
      : diskHost ? `Host disk (${diskHost}) ${formatPct(metrics.disk_pct)}`
      : `Disk ${formatPct(metrics.disk_pct)}`,
    tmpInodesWarn(metrics) ? `/tmp inodes ${formatPct(metrics.tmp_inode_pct as number)} used` : null,
    metrics.net_rx_bps == null || metrics.net_tx_bps == null
      ? null
      : `Network in ${formatRate(metrics.net_rx_bps)}, out ${formatRate(metrics.net_tx_bps)}`,
  ].filter((part): part is string => part !== null)
  if (parts.length === 0) return 'No usage figures'
  const head = `${parts.join(' · ')} · ${humanDuration(metrics.window_secs)} average`
  // The age only when the server calls the reading stale.
  return metrics.stale ? `${head} — stale, last reported ${humanAge(metrics.age_seconds)}` : head
}

/** Whether the server flags /tmp's inode table (`warn`), with a figure to print. Stale is no flag. */
function tmpInodesWarn(metrics: NodeHostMetrics): boolean {
  return !metrics.stale && metrics.tmp_inode_level === 'warn' && metrics.tmp_inode_pct != null
}

/** The host whose disk a co-located node's D bar shows. A tenant on another org's host is told its own disk share
 *  instead, with CPU and RAM withheld (`routes/nodes/node_wire.py::_own_disk_only`) — that bar is the node's, so unnamed. */
export function sharedDiskHost(n: NodeRecord): string | null {
  if (n.hosting !== 'co_located' || !n.hosted_on) return null
  const m = n.host_metrics
  return m && (m.cpu_pct != null || m.mem_pct != null) ? n.hosted_on : null
}

// ── Geometry ──────────────────────────────────────────────────────────────────────────────────────

/* `compact` fits the list's 76px `col-node-load` cell (no padding): 15px letter + 3px gap + 4px padding
 * leaves a 54px bar. Three rows (C/M/D) must fit the phone band's 24px row (`ui/feed/geometry.ts`'s
 * `ROW_H_PHONE`) without growing it (an owner decision): 6px bars + two 2px gaps = 22px. Each row is
 * the bar's height (`rowPx`), and the 9px letter (below the 11px type floor, theme.ts:268, by that decision)
 * is centred in it rather than stretching it. `full` prints the numbers. */
const SCALE = {
  compact: { letterPx: 15, fontSize: '0.5625rem', barPx: 6, rowPx: 6, phoneRowPx: 5, rowGapPx: 2, readoutPx: 0 },
  full: { letterPx: 20, fontSize: '0.8125rem', barPx: 12, rowPx: null, phoneRowPx: null, rowGapPx: 5, readoutPx: 44 },
} as const
/* A phone's compact bars are 5px (19px a trio), so each row's three sit apart from the next row's in the 24px band
   rather than running on as one even stack of bars down the list. */
const phoneRow = (scale: (typeof SCALE)[keyof typeof SCALE]) =>
  scale.phoneRowPx ? { [PHONE]: { height: scale.phoneRowPx } } : {}

// ── One bar ───────────────────────────────────────────────────────────────────────────────────────

function Bar({
  metric,
  letter,
  pct,
  level,
  stale,
  scale,
}: {
  /** `cpu` / `mem` / `disk` — the test hook and `data-` discriminant, never a display string. */
  metric: 'cpu' | 'mem' | 'disk'
  letter: string
  pct: number
  level: NodeHostMetricsLevel
  stale: boolean
  scale: (typeof SCALE)[keyof typeof SCALE]
}): JSX.Element {
  const tone = levelTone(level)
  const width = Math.min(100, Math.max(0, pct))
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: '3px', minWidth: 0, height: scale.rowPx ?? undefined, ...phoneRow(scale) }}>
      {/* `aria-hidden`: the wrapper's name already says "CPU"/"RAM". Fixed width so `?` cannot shift the bar. */}
      <Box
        component="span"
        aria-hidden="true"
        sx={theme => ({
          width: scale.letterPx,
          flex: '0 0 auto',
          fontFamily: theme.typography.mono.fontFamily,
          fontSize: scale.fontSize,
          lineHeight: 1,
          // The 9px capital is taller than the 6px row: centred on its bar, it paints into the gaps.
          ...(scale.rowPx ? { height: scale.rowPx, display: 'flex', alignItems: 'center' } : {}),
          color: 'text.secondary',
          // A phone's compact meter names its bars by slot (the header above it): a stale one keeps only its `?`.
          ...(scale.rowPx ? { [PHONE]: stale ? { width: 'auto' } : { display: 'none' } } : {}),
        })}
      >
        <Box component="span" sx={scale.rowPx ? { [PHONE]: { display: 'none' } } : undefined}>{letter}</Box>
        {stale ? '?' : ''}
      </Box>
      {/* The track: tint makes the empty scale legible, the border survives forced-colours mode. */}
      <Box
        data-testid={`node-load-track-${metric}`}
        sx={{
          flex: '1 1 auto',
          minWidth: 0,
          height: scale.barPx,
          // Compact: the border inside `barPx`; outside it, a 6px bar was 8px and overflowed its 6px row.
          boxSizing: scale.rowPx ? 'border-box' : 'content-box',
          border: 1,
          borderColor: 'divider',
          backgroundColor: 'action.hover',
          borderRadius: '999px',
          overflow: 'hidden',
          ...phoneRow(scale),
        }}
      >
        {/* `style`, not `sx`, for `width` alone: a per-row value would mint an Emotion class per percentage.
        Inline also lets the unit test read the fill length. */}
        <Box
          data-testid={`node-load-fill-${metric}`}
          data-tone={tone}
          data-level={level}
          style={{ width: `${width}%` }}
          sx={{ height: '100%', bgcolor: TONE_COLOR[tone] }}
        />
      </Box>
      {scale.readoutPx > 0 && (
        <Box
          component="span"
          data-testid={`node-load-readout-${metric}`}
          sx={theme => ({
            width: scale.readoutPx,
            flex: '0 0 auto',
            textAlign: 'right',
            fontFamily: theme.typography.mono.fontFamily,
            fontSize: scale.fontSize,
            fontWeight: 600,
            lineHeight: 1,
            color: 'text.primary',
          })}
        >
          {formatPct(pct)}
        </Box>
      )}
    </Box>
  )
}

/** Whether a reading has any figure to bar: one without (never reported, CPU/RAM withheld, disk not yet
 *  measured) draws as absent. */
export function hasLoadBar(metrics: NodeHostMetrics | null): boolean {
  return metrics != null && (metrics.cpu_pct != null || metrics.mem_pct != null || metrics.disk_pct != null)
}

// ── The meter ─────────────────────────────────────────────────────────────────────────────────────

export function NodeLoadMeter({
  metrics,
  variant = 'compact',
  diskHost = null,
}: {
  metrics: NodeHostMetrics | null
  variant?: 'compact' | 'full'
  /** The host whose disk the D bar shows, for a co-located node; null when the disk is the node's own. */
  diskHost?: string | null
}): JSX.Element {
  const scale = SCALE[variant]
  const summary = hostMetricsSummary(metrics, diskHost)

  /* Never reported: the one absent mark (`ui/NoneMark`), never a 0% bar. `== null` deliberately catches `undefined` too: an older response shape must not throw in every
  row's render and take `/nodes` down. */
  /* A reading with no figure to bar (CPU/RAM withheld, disk not yet measured) reads as absent too: an
  empty box would look like a rendering fault. */
  if (metrics == null || !hasLoadBar(metrics)) {
    return (
      <Box
        data-testid="node-load-meter"
        data-state="absent"
        role="img"
        aria-label={summary}
        title={summary}
        // Centred in the list's cell; `full` sits under its field's label like any other value.
        sx={[NONE_SX, { fontSize: scale.fontSize, lineHeight: 1, textAlign: variant === 'full' ? 'left' : 'center' }]}
      >
        {NONE}
      </Box>
    )
  }

  /* A stale reading is `unknown` at both bars, so the paint cannot contradict `stale` even against an
  older server. */
  const cpuLevel = metrics.stale ? 'unknown' : metrics.cpu_level
  const memLevel = metrics.stale ? 'unknown' : metrics.mem_level
  // `?? 'unknown'`: an older server sends no disk fields at all.
  const diskLevel = metrics.stale ? 'unknown' : (metrics.disk_level ?? 'unknown')
  // A missing bar's slot: held open on a phone's compact meter only, where position is the bar's name.
  const slot = scale.rowPx
    ? <Box aria-hidden="true" data-testid="node-load-slot" sx={{ display: 'none', [PHONE]: { display: 'block', height: scale.phoneRowPx } }} />
    : null

  const tmpPct = tmpInodesWarn(metrics) ? formatPct(metrics.tmp_inode_pct as number) : null
  const tmpBadge = tmpPct == null ? null : (
    <Box
      component="span"
      data-testid="node-load-tmp-inodes"
      data-tone={levelTone('warn')}
      title={`/tmp inodes ${tmpPct} used`}
      sx={theme => ({
        px: '3px',
        border: 1,
        borderColor: TONE_COLOR[levelTone('warn')],
        borderRadius: '4px',
        bgcolor: 'background.paper',
        color: TONE_COLOR[levelTone('warn')],
        fontFamily: theme.typography.mono.fontFamily,
        fontSize: scale.fontSize,
        fontWeight: 600,
        lineHeight: 1.2,
        whiteSpace: 'nowrap',
        alignSelf: scale.rowPx ? 'center' : 'flex-start',
        flexShrink: 0,
        pointerEvents: 'auto',
      })}
    >
      {scale.rowPx ? '/tmp' : `/tmp inodes ${tmpPct}`}
    </Box>
  )

  const meter = (
    <Box
      data-testid="node-load-meter"
      data-state={metrics.stale ? 'stale' : 'fresh'}
      data-level={metrics.stale ? 'unknown' : metrics.level}
      role="img"
      aria-label={summary}
      title={summary}
      sx={{
        width: '100%',
        minWidth: 0,
        px: '2px',
        // Room for the last capital's overhang, so the cell never reports a wrapped value: 22px + 2 = the 24px row.
        py: scale.rowPx ? '1px' : 0,
        display: 'flex',
        flexDirection: 'column',
        gap: `${scale.rowGapPx}px`,
        // Data, not a control: clicks belong to the row underneath.
        pointerEvents: 'none',
        ...(scale.rowPx && tmpBadge ? { flex: 1 } : {}),
      }}
    >
      {metrics.cpu_pct != null ? (
        <Bar metric="cpu" letter="C" pct={metrics.cpu_pct} level={cpuLevel} stale={metrics.stale} scale={scale} />
      ) : slot}
      {metrics.mem_pct != null ? (
        <Bar metric="mem" letter="M" pct={metrics.mem_pct} level={memLevel} stale={metrics.stale} scale={scale} />
      ) : slot}
      {metrics.disk_pct != null ? (
        <Bar metric="disk" letter="D" pct={metrics.disk_pct} level={diskLevel} stale={metrics.stale} scale={scale} />
      ) : slot}
      {scale.rowPx ? null : tmpBadge}
    </Box>
  )
  return scale.rowPx && tmpBadge
    ? <Box sx={{ display: 'flex', alignItems: 'center', gap: '3px', width: '100%', minWidth: 0 }}>{meter}{tmpBadge}</Box>
    : meter
}

export default NodeLoadMeter
