import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useQuery } from '@tanstack/react-query'
import {
  gateDuration, gateResultWords, gateRunsKey, getGateRuns, type GateResultRow, type GateRunOut, type GateRunsQuery,
} from '../../../lib/pipelineGates'
import { FONT_SIZE, FONT_WEIGHT, grid } from '../../../theme'
import ExternalLink from '../../../ui/ExternalLink'
import { StatusChip, type Tone } from '../../../ui/StatusChip'
import { PHONE } from '../../../ui/listStyles'

/* GATE RESULTS (PRD D15): one row per gate of one run — its result in words (AC1.4–AC1.6), how long it took, and its
 * report as plain text (never HTML). Shown on the task page for a merge, and in the Release view for a release or a
 * stage's after-arrival checks. A gate's one-line summary is the server's (AC1.8): no kind slug reaches the screen. */

const TONE: Record<GateResultRow['state'], Tone> = {
  passed: 'success', failed: 'error', timed_out: 'error', running: 'info', waiting: 'neutral', off: 'neutral',
  skipped: 'neutral',
}

function tone(r: GateResultRow): Tone {
  return (r.state === 'failed' || r.state === 'timed_out') && r.required === false ? 'warning' : TONE[r.state]
}

/** What the report cell says when the gate left no report. */
function quietReport(r: GateResultRow): string | null {
  if (r.state === 'off') return 'Switched off'
  if (r.state === 'skipped') return 'An earlier required check failed'
  return r.summary
}

/** The longest report a passed gate's chip carries in place of "Passed" (one fact, said once). */
const CHIP_REPORT_MAX = 80

/** A first-try pass whose report is one short line that itself says it passed ("All 8 checks passed."): that line
 * IS the result, so the chip says it and no report repeats it below. Anything else keeps "Passed" on the chip. */
function passedLine(r: GateResultRow): string | null {
  const line = r.report?.trim()
  if (r.state !== 'passed' || r.attempts > 1 || !line || line.includes('\n') || line.length > CHIP_REPORT_MAX
    || !/\bpass/i.test(line)) return null
  return line.replace(/\.$/, '')
}

const GRID_SX = {
  display: 'grid', gridTemplateColumns: `minmax(${grid(16)}, 1fr) ${grid(26)} minmax(0, 2.5fr)`, columnGap: 2,
  [PHONE]: { gridTemplateColumns: '1fr auto' },
} as const

/** What one row shows: its chip's words, and the report or quiet line under it (none when the chip already says it). */
function said(r: GateResultRow): { words: string; report: string | null; quiet: string | null } {
  const line = passedLine(r)
  if (line) return { words: line, report: null, quiet: null }
  const words = gateResultWords(r)
  // "Failed: lint." under a "Failed" chip says Failed twice: the chip keeps it, the report keeps the names.
  const report = r.report && words.startsWith('Failed') ? r.report.replace(/^Failed:\s*/, '') : r.report
  return { words, report, quiet: quietReport(r) }
}

/** Whether a one-gate run fits on one line: no report to show under its chip (a quiet line goes on its hover). */
const fitsOneLine = (run: GateRunOut): boolean => run.results.length === 1 && !said(run.results[0]).report

/** A one-line chip's hover and accessible name: the gate's name and its quiet line, which the line does not show;
 * the accessible name still leads with the result. */
function chipHover(r: GateResultRow, label: string | null): { title?: string; ariaLabel?: string } {
  const { words, quiet } = said(r)
  const hover = [r.name && r.name !== label ? r.name : null, quiet].filter(Boolean).join(' \u2014 ')
  return hover ? { title: hover, ariaLabel: `${words} \u2014 ${hover}` } : {}
}

function Body({ report, quiet }: { report: string | null; quiet: string | null }): JSX.Element | null {
  if (report) {
    return (
      <Typography variant="body2" component="pre" data-testid="gate-result-report"
        sx={{ m: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontFamily: 'monospace', fontSize: FONT_SIZE.sm,
          maxHeight: grid(21), overflowY: 'auto' }}>{report}</Typography>
    )
  }
  return quiet ? <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>{quiet}</Typography> : null
}

function Took({ r, block = false }: { r: GateResultRow; block?: boolean }): JSX.Element | null {
  const took = gateDuration(r)
  if (!took && !r.link) return null
  return (
    <Typography variant="body2" component={block ? 'div' : 'span'}
      sx={{ color: 'text.secondary', display: block ? 'flex' : 'inline-flex', columnGap: 1.5, flexWrap: 'wrap',
        ...(block ? {} : { fontSize: 'inherit' }) }}>
      {took && <span data-testid="gate-result-took">{took}</span>}
      {r.link && <ExternalLink href={r.link} underline="hover" data-testid="gate-result-link">Open</ExternalLink>}
    </Typography>
  )
}

function Row({ r }: { r: GateResultRow }): JSX.Element {
  const { words, report, quiet } = said(r)
  return (
    <Box role="row" data-testid="gate-result" data-state={r.state}
      sx={{ ...GRID_SX, py: 1, borderTop: 1, borderColor: 'divider', alignItems: 'start' }}>
      <Typography role="cell" variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium, overflowWrap: 'anywhere' }}>
        {r.name || 'Check'}
      </Typography>
      <Box role="cell" sx={{ justifySelf: 'start', [PHONE]: { justifySelf: 'end' } }}>
        <StatusChip label={words} tone={tone(r)} wrap testId="gate-result-state" />
      </Box>
      <Box role="cell" sx={{ minWidth: 0, [PHONE]: { gridColumn: '1 / -1', pt: 0.5 } }}>
        <Body report={report} quiet={quiet} />
        <Took r={r} block />
      </Box>
    </Box>
  )
}

/** The run's own word, unless its one gate's chip already says the same (a stopped run whose gate still reads Running
 * keeps "Stopped"). */
function RunWord({ run }: { run: GateRunOut }): JSX.Element | null {
  if (run.results.length === 1 && run.results[0].state === run.run.state) return null
  return (
    <Typography component="span" variant="body2" sx={{ color: 'text.secondary' }} data-testid="gate-run-state">
      {RUN_WORDS[run.run.state]}
    </Typography>
  )
}

/** A one-gate run on one line — its label, chip, run word, duration and link — with any report under it. The label
 * is the heading, so the gate's own name moves to the chip's hover instead of repeating it. */
function OneLine({ run, label, heading, testId }: {
  run: GateRunOut; label: string; heading: boolean; testId: string
}): JSX.Element {
  const r = run.results[0]
  const { words, report } = said(r)
  return (
    <Box data-testid={testId} data-run-state={run.run.state} role="table" aria-label={label}>
      <Box role="row" data-testid="gate-result" data-state={r.state}
        sx={{ display: 'flex', alignItems: 'center', columnGap: 1, rowGap: 0.5, flexWrap: 'wrap' }}>
        <Typography role="cell" variant="subtitle2" component={heading ? 'h3' : 'span'}
          sx={{ fontWeight: heading ? FONT_WEIGHT.semibold : FONT_WEIGHT.medium }}>{label}</Typography>
        <Box role="cell" sx={{ display: 'inline-flex', alignItems: 'center', columnGap: 1, flexWrap: 'wrap' }}>
          <StatusChip label={words} tone={tone(r)} wrap {...chipHover(r, label)} testId="gate-result-state" />
          <RunWord run={run} />
          <Typography component="span" variant="body2"><Took r={r} /></Typography>
        </Box>
      </Box>
      {report && <Box sx={{ pt: 0.5 }}><Body report={report} quiet={null} /></Box>}
    </Box>
  )
}

/** One run: a one-gate run on one line under `title` (or, with `hideTitle` inside a section that already names it,
 * under the gate's name); several gates as a row each under a small heading carrying the run's word. */
export function GateRunTable({ run, title, hideTitle = false, testId = 'gate-results' }: {
  run: GateRunOut; title: string; hideTitle?: boolean; testId?: string
}): JSX.Element {
  if (run.results.length === 1) {
    return <OneLine run={run} label={hideTitle ? run.results[0].name || title : title} heading={!hideTitle} testId={testId} />
  }
  return (
    <Box data-testid={testId} data-run-state={run.run.state} role="table" aria-label={title}>
      <Box sx={{ display: 'flex', alignItems: 'baseline', columnGap: 1, flexWrap: 'wrap', mb: 0.5, '&:empty': { display: 'none' } }}>
        {!hideTitle && <Typography variant="subtitle2" component="h3" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{title}</Typography>}
        <RunWord run={run} />
      </Box>
      <Box role="rowgroup">
        {run.results.map(r => <Row key={r.position} r={r} />)}
      </Box>
    </Box>
  )
}

const RUN_WORDS: Record<GateRunOut['run']['state'], string> = {
  running: 'Running', passed: 'Passed', failed: 'Failed', canceled: 'Stopped',
}

/** A one-line run inside a status bar: its chip, run word, duration and link, the gate's name on the chip's hover.
 * "Checks" leads it unless the chip already says so ("22/27 checks passed"). */
function InBar({ run, testId }: { run: GateRunOut; testId: string }): JSX.Element {
  const r = run.results[0]
  const { words } = said(r)
  return (
    <Box component="span" data-testid={testId} data-run-state={run.run.state}
      sx={{ display: 'inline-flex', alignItems: 'center', columnGap: 1, minWidth: 0, flexWrap: 'wrap' }}>
      {!/check/i.test(words) && <Box component="span" sx={{ color: 'text.secondary' }}>Checks</Box>}
      <Box component="span" data-testid="gate-result" data-state={r.state} sx={{ display: 'inline-flex', minWidth: 0 }}>
        <StatusChip label={words} tone={tone(r)} wrap {...chipHover(r, null)} testId="gate-result-state" />
      </Box>
      <RunWord run={run} />
      <Took r={r} />
    </Box>
  )
}

/** The newest gate run of `query` (filtered by `pick` when given), polled while it runs; nothing when there is none.
 * `placement` splits one run between a status bar and the space under it, so exactly one of a pair draws it:
 * `bar` draws a run that fits on one line, `below` draws every other run (and, alone, draws any run). */
export function GateResults({ projectId, query, title, pick, testId, placement }: {
  projectId: number
  query: GateRunsQuery
  title: string
  pick?: (run: GateRunOut) => boolean
  testId?: string
  placement?: 'bar' | 'below'
}): JSX.Element | null {
  const runs = useQuery({
    queryKey: gateRunsKey(projectId, query),
    queryFn: () => getGateRuns(projectId, query),
    refetchInterval: q => ((q.state.data?.runs ?? []).some(r => r.run.state === 'running') ? 10_000 : false),
  })
  const run = (runs.data?.runs ?? []).find(r => r.results.length > 0 && (!pick || pick(r)))
  if (!run) return null
  if (placement === 'bar') return fitsOneLine(run) ? <InBar run={run} testId={testId ?? 'gate-results'} /> : null
  if (placement === 'below' && fitsOneLine(run)) return null
  return <GateRunTable run={run} title={title} testId={testId} />
}
