import Box from '@mui/material/Box'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import type { JSX } from 'react'
import { kbSize } from '../lib/formatBytes'
import type { NodeHostMetrics, NodeTopProcess } from '../types'
import CellLabel from '../ui/CellLabel'
import DataTable from '../ui/DataTable'
import { DetailSection } from '../ui/detail/DetailPage'
import { ELLIPSIS_SX, monoSx, phoneCardSx } from '../ui/listStyles'
import { NONE } from '../ui/NoneMark'
import { Stat, StatGrid } from '../ui/StatGrid'
import { formatCorePct, formatLoad, formatPct } from './NodeLoadMeter'

/* ══ A NODE'S HOST SIGNALS ═══════════════════════════════════════════════════════════════
 * What explains a slow box beyond CPU/RAM percentages: load averages against the cores, swap in use,
 * pressure stall (PSI `some avg60`) per resource, and the busiest processes (name and user only — never
 * argv, which carries secrets). One home for the host page's Status tab and the node page's Health tab.
 * Drawn by `NodeMetrics` from its payload's `latest` — the one read carrying `top_processes` — under the
 * card's own latest figures, so the block costs no read of its own and goes stale (or fails) with them.
 * Absence is not zero: a figure the node did not report (an older daemon, or withheld on another org's
 * host) is left out, and a section with nothing reported draws nothing at all.
 */

/** Swap in use: the used size, with "of <total>" as quieter text; "None" where no swap is configured; the
 *  share alone when the total is unknown. Null when unreported. */
function swapValue(m: NodeHostMetrics): { used: string; of?: string } | null {
  const total = m.swap_total_kb ?? null
  const free = m.swap_free_kb ?? null
  if (total === 0) return { used: NONE }
  if (total !== null && free !== null) return { used: kbSize(Math.max(0, total - free)), of: `of ${kbSize(total)}` }
  return m.swap_pct == null ? null : { used: formatPct(m.swap_pct) }
}

/* A figure on one line (a wrapped "3.85 / 6.93 /" over "8.53" read as two numbers); the tiles are wide enough
   for it at 390px (two across). */
const ONE_LINE_SX = { whiteSpace: 'nowrap' } as const
/** The secondary half of a figure ("of 8.0 GB"): quieter and smaller, so the figure itself leads. */
const QUIET_SX = { color: 'text.secondary', fontWeight: 400, fontSize: '0.8125rem' } as const
/** Each tile ≥ 168px: "3.85 / 6.93 / 8.53" and "473.5 MB of 8.0 GB" fit; two across at 390px. */
const SIGNAL_TILE_PX = 168

/** Busiest first: CPU, then RAM (the list is the union of the top five by each). */
function byLoad(a: NodeTopProcess, b: NodeTopProcess): number {
  return b.cpu_pct - a.cpu_pct || b.rss_kb - a.rss_kb
}

const PROCESS_COLUMNS = ['Name', 'User', 'CPU', 'RAM'] as const

/* On a phone the name takes the card's first line; user, CPU and RAM wrap beneath it. */
const PROCESS_PHONE_SX = phoneCardSx({ '& tbody td[data-col="name"]': { flex: '1 1 100%' } }, { py: 0.5 })

/** The signals as tiles plus the process table, from one reading; null when it carries none of them. */
export function NodeSignalsView({ metrics }: { metrics: NodeHostMetrics | null | undefined }): JSX.Element | null {
  if (!metrics) return null
  const loads = [metrics.load_1, metrics.load_5, metrics.load_15]
  const hasLoad = loads.some(v => v != null)
  const swap = swapValue(metrics)
  const pressure = [
    { key: 'cpu', label: 'CPU pressure', value: metrics.psi_cpu_pct },
    { key: 'mem', label: 'Memory pressure', value: metrics.psi_mem_pct },
    { key: 'io', label: 'IO pressure', value: metrics.psi_io_pct },
  ].filter((p): p is { key: string; label: string; value: number } => p.value != null)
  const processes = [...(metrics.top_processes ?? [])].sort(byLoad)
  if (!hasLoad && swap === null && pressure.length === 0 && processes.length === 0) return null

  return (
    <DetailSection title="Signals" headingLevel="h3" testId="node-signals">
      {(hasLoad || swap !== null || pressure.length > 0) && (
        <StatGrid minTile={SIGNAL_TILE_PX}>
          {hasLoad && (
            <Stat
              mono
              label={metrics.cpu_count ? `Load · ${metrics.cpu_count} cores` : 'Load'}
              hint="1, 5, 15 min"
              value={<Box component="span" sx={ONE_LINE_SX}>{loads.map(v => (v == null ? '—' : formatLoad(v))).join(' / ')}</Box>}
              testId="signals-load"
            />
          )}
          {swap !== null && (
            <Stat
              mono
              absent={swap.used === NONE}
              label="Swap used"
              value={
                <Box component="span" sx={ONE_LINE_SX}>
                  {swap.used}
                  {swap.of && <Box component="span" sx={QUIET_SX}>{` ${swap.of}`}</Box>}
                </Box>
              }
              testId="signals-swap"
            />
          )}
          {pressure.map(p => (
            <Stat key={p.key} mono label={p.label} value={formatPct(p.value)} testId={`signals-psi-${p.key}`} />
          ))}
        </StatGrid>
      )}
      {processes.length > 0 && (
        <Box sx={PROCESS_PHONE_SX}>
          <DataTable ariaLabel="Top processes" testId="signals-processes">
            <TableHead>
              <TableRow>
                {PROCESS_COLUMNS.map(h => (
                  <TableCell key={h} align={h === 'CPU' || h === 'RAM' ? 'right' : undefined}>{h}</TableCell>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {processes.map((p, i) => (
                <TableRow key={`${p.name}-${p.user}-${i}`} data-testid="signals-process-row">
                  <TableCell data-col="name" sx={[monoSx, { maxWidth: '14rem' }]}>
                    <CellLabel>Name</CellLabel>
                    <Box component="span" title={p.name} sx={ELLIPSIS_SX}>{p.name}</Box>
                  </TableCell>
                  <TableCell data-col="user" sx={{ maxWidth: '10rem' }}>
                    <CellLabel>User</CellLabel>
                    <Box component="span" title={p.user} sx={ELLIPSIS_SX}>{p.user}</Box>
                  </TableCell>
                  <TableCell data-col="cpu" align="right" sx={[monoSx, { whiteSpace: 'nowrap' }]}>
                    <CellLabel>CPU</CellLabel>
                    {formatCorePct(p.cpu_pct)}
                  </TableCell>
                  <TableCell data-col="ram" align="right" sx={[monoSx, { whiteSpace: 'nowrap' }]}>
                    <CellLabel>RAM</CellLabel>
                    {kbSize(p.rss_kb)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </DataTable>
        </Box>
      )}
    </DetailSection>
  )
}

export default NodeSignalsView
