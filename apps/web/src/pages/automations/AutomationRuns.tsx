/* AN AUTOMATION'S RUNS (`GET /api/automations/{id}/runs`), newest first (AC4.6, AC7.4). One row per firing:
 * when, why (cause), the outcome, the message an email run ran on (subject · sender), and one line of what
 * happened to its targets ("480 filed · 15 skipped: previous still open"). A run from before the fire engine has no targets and reads its stored detail instead (AC5.6): its
 * headline, then the first journal lines inline (expandable), so a pass's decisions read without opening it.
 * The cause shows only where it differs from the automation's own kind (a "Run now" on a schedule); the outcome
 * chip takes Audit > Schedule's tones. Consecutive skips with one reason fold into one row ("×12", its span on hover) —
 * a wedged schedule's `already_running` skips as one "Skipped 12 runs since …" line — and
 * so do consecutive missed runs with one detail (not ten bare "Missed" rows in a row); the strip filters server-side (Status, Search),
 * so the ticks that did something are findable.
 * A row opens the run's own page (`AutomationRunPage.tsx`) through `onOpenRun`, the editor's: it owns the unsaved-
 * edit gate and the address, and hands the row along so the page paints at once. */
import { useCallback, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Typography from '@mui/material/Typography'
import { usePagedList } from '../../hooks/usePagedList'
import { clockTime, NO_VALUE } from '../../lib/relativeTime'
import { formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import { FilterBar } from '../../ui/FilterBar'
import { FilterRow, FilterSelect } from '../../ui/FilterRow'
import { fieldTapTargetSx } from '../../ui/listStyles'
import { LoadMoreFooter } from '../../ui/PagedList'
import RelativeTime from '../../ui/RelativeTime'
import SearchBox from '../../ui/SearchBox'
import StatusChip from '../../ui/StatusChip'
import { TaskLink } from '../../ui/DetailFields'
import { detailLines } from '../cron/vocabulary'
import { RUNS_PAGE_SIZE } from '../../hooks/usePagedList'
import { listAutomationRuns, type Automation, type AutomationRun } from './api'
import { CAUSE_LABELS, runLine, runOutcomeLabel, runOutcomeTone, targetSummary } from './vocabulary'

/** The server's `status` tokens (`routes/automations.py::RUNS_STATUSES`). */
const STATUS_OPTIONS = [
  { value: 'no_skips', label: 'Hide skips' },
  { value: 'acted', label: 'Did work' },
]

/** A received message with an empty Subject header. */
const NO_SUBJECT = '(no subject)'

/** Journal lines shown under the headline before "Show N more". */
export const JOURNAL_PREVIEW_LINES = 3

/** Consecutive runs folded into one row: skips with the same reason, missed runs with the same detail, else a run
 *  alone. `oldest` is the stretch's first LOADED run (the list is newest first, so `run` is its last); `open` when the
 *  stretch reaches the end of the loaded rows with more to load, so its count is a floor ("at least 50"). */
type RunGroup = { run: AutomationRun; count: number; oldest: AutomationRun; open?: boolean }

/** The outcomes that fold, each with its hover's plural noun ("12 skips", "10 missed"). */
const FOLDED_NOUN: Record<string, string> = { skipped: 'skips', missed: 'missed' }

/** The skip a held run lock writes on every tick (`app/crons.py::SKIP_ALREADY_RUNNING`): a stuck schedule's ledger is
 *  nothing else, so its stretch folds whatever the detail says and reads as one sentence (`skippedSince`). */
const ALREADY_RUNNING = 'already_running'

/** Folds each stretch of consecutive `skipped` (or `missed`) runs with one detail into its newest run, counted.
 *  `hasMore`: older runs are still unloaded, so the last stretch may continue past them (`open`). */
export function groupRuns(runs: AutomationRun[], hasMore = false): RunGroup[] {
  const groups: RunGroup[] = []
  for (const run of runs) {
    const last = groups[groups.length - 1]
    if (
      last && Object.hasOwn(FOLDED_NOUN, run.outcome) && last.run.outcome === run.outcome
      && last.run.skip_reason === run.skip_reason
      && (last.run.detail === run.detail || run.skip_reason === ALREADY_RUNNING)
    ) {
      last.count += 1
      last.oldest = run
    } else {
      groups.push({ run, count: 1, oldest: run })
    }
  }
  const last = groups[groups.length - 1]
  if (hasMore && last && Object.hasOwn(FOLDED_NOUN, last.run.outcome)) last.open = true
  return groups
}

/** A fold's count, as a floor while its stretch may run past the loaded rows: "12", or "at least 50". */
const foldCount = ({ count, open }: RunGroup) => (open ? `at least ${count}` : String(count))

/** A folded row's hover: "12 skips, 10:01–10:03" ("10 missed, …"); to the second when the stretch sits inside one minute. */
export function foldedSpan(group: RunGroup): string {
  const { run, oldest } = group
  const stamp = (iso: string | null, seconds: boolean) => (iso ? clockTime(iso, { seconds }) : NO_VALUE)
  const sameMinute = stamp(oldest.fired_at, false) === stamp(run.fired_at, false)
  const from = stamp(oldest.fired_at, sameMinute)
  const to = stamp(run.fired_at, sameMinute)
  return `${foldCount(group)} ${Object.hasOwn(FOLDED_NOUN, run.outcome) ? FOLDED_NOUN[run.outcome] : 'runs'}, ${from === to ? from : `${from}–${to}`}`
}

/** A folded stretch of `already_running` skips as one line ("Skipped 12 runs since Oct 8, 2026, 10:01 EDT"), else
 *  null: each said the same thing, and only how many and since when is news. */
export function skippedSince(group: RunGroup): string | null {
  const { run, count, oldest, open } = group
  if (count < 2 || run.outcome !== 'skipped' || run.skip_reason !== ALREADY_RUNNING) return null
  // An open stretch started before its oldest loaded run, so "since" is a bound, not the start.
  return `Skipped ${foldCount(group)} runs since ${open ? 'before ' : ''}${formatZoned(oldest.fired_at)}`
}

function RunJournal({ lines }: { lines: string[] }): JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (lines.length === 0) return null
  const shown = open ? lines : lines.slice(0, JOURNAL_PREVIEW_LINES)
  const hidden = lines.length - shown.length
  return (
    <Box data-testid="automation-run-journal" sx={{ px: 1, pb: 1, minWidth: 0 }}>
      <Box
        component="ul"
        sx={{ listStyle: 'none', m: 0, p: 0, pl: 1, borderLeft: 2, borderColor: 'divider', minWidth: 0 }}
      >
        {shown.map((line, index) => (
          <Typography
            key={index}
            component="li"
            variant="mono"
            sx={{ display: 'block', fontSize: 12, lineHeight: 1.6, color: 'text.secondary', overflowWrap: 'anywhere' }}
          >
            {line}
          </Typography>
        ))}
      </Box>
      {(hidden > 0 || open) && lines.length > JOURNAL_PREVIEW_LINES && (
        <Button size="small" onClick={() => setOpen(!open)} sx={{ mt: 0.5 }} aria-expanded={open}>
          {open ? 'Show fewer' : `Show ${hidden} more line${hidden === 1 ? '' : 's'}`}
        </Button>
      )}
    </Box>
  )
}

function RunRow({ group, ownCause, onOpen }: {
  group: RunGroup
  /** The automation's own kind: a run with this cause says nothing new, so its cause is not drawn. */
  ownCause: string
  onOpen: () => void
}): JSX.Element {
  const { run, count } = group
  const hasTargets = run.targets_total > 0
  const lines = detailLines(run.detail)
  const since = skippedSince(group)
  const line = since ?? (hasTargets ? targetSummary(run) : runLine(run, lines[0].trim()))
  /* A failed run leads with its short cause; the raw text (an exception `repr`) stays under it, as the
     automation's header does. Only when it says something the raw line does not — a difference of case
     alone is the same words. */
  const cause = run.failure_cause && run.failure_cause.trim().toLowerCase() !== line.trim().toLowerCase()
    ? run.failure_cause
    : null
  const journal = hasTargets || since ? [] : lines.slice(1).filter(text => text.trim() !== '')
  return (
    <Box component="li" sx={{ borderBottom: 1, borderColor: 'divider', minWidth: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }}>
        <ButtonBase
          data-testid="automation-run-row"
          data-id={run.id}
          onClick={onOpen}
          title={count > 1 ? foldedSpan(group) : undefined}
          sx={{
            flex: '1 1 auto',
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'stretch',
            textAlign: 'left',
            gap: 0.5,
            py: 1,
            px: 1,
            borderRadius: 1,
            '&:hover': { bgcolor: 'action.hover' },
            '&.Mui-focusVisible': { outline: 2, outlineColor: 'primary.main' },
          }}
        >
          <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1, minWidth: 0 }}>
            <RelativeTime iso={run.fired_at} variant="body2" sx={{ fontWeight: 500 }} />
            {run.cause !== ownCause && (
              <Typography variant="body2" component="span" data-testid="automation-run-trigger" sx={{ color: 'text.secondary' }}>
                {CAUSE_LABELS[run.cause] ?? run.cause}
              </Typography>
            )}
            <StatusChip label={runOutcomeLabel(run)} tone={runOutcomeTone(run)} data-status={run.outcome} />
            {count > 1 && !since && (
              <Typography
                variant="body2"
                component="span"
                data-testid="automation-run-repeat"
                sx={{ color: 'text.secondary', fontWeight: 600 }}
              >
                ×{count}{group.open ? '+' : ''}
              </Typography>
            )}
          </Box>
          {run.email && (
            /* Subject and sender share the line, each ellipsised: a long subject never pushes the sender out. */
            <Box
              data-testid="automation-run-email"
              title={run.email.from ? `${run.email.subject || NO_SUBJECT} — from ${run.email.from}` : undefined}
              sx={{ display: 'flex', gap: 0.75, minWidth: 0, typography: 'body2', whiteSpace: 'nowrap' }}
            >
              <Box component="span" sx={{ flex: '0 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {run.email.subject || NO_SUBJECT}
              </Box>
              {run.email.from && (
                <Box component="span" sx={{ flex: '0 1 auto', minWidth: '6em', maxWidth: '50%', overflow: 'hidden', textOverflow: 'ellipsis', color: 'text.secondary' }}>
                  · {run.email.from}
                </Box>
              )}
            </Box>
          )}
          {cause && (
            <Typography
              variant="body2"
              data-testid="automation-run-cause"
              sx={{ color: 'error.main', fontWeight: 600, overflowWrap: 'anywhere' }}
            >
              {cause}
            </Typography>
          )}
          {line && (
            <Typography
              variant="body2"
              data-testid="automation-run-summary"
              sx={{ color: hasTargets ? 'text.primary' : 'text.secondary', overflowWrap: 'anywhere' }}
            >
              {line}
            </Typography>
          )}
        </ButtonBase>
        {/* Beside the button, not in it: a link inside a button is two controls in one. */}
        {!hasTargets && run.task_id !== null && (
          <TaskLink taskId={run.task_id} variant="body2" sx={{ flex: '0 0 auto', pr: 1 }} />
        )}
      </Box>
      {/* Below the button for the same reason: the expander is its own control. */}
      <RunJournal lines={journal} />
    </Box>
  )
}

/** The empty ledger says what comes next for a schedule — its next run, or why there is none; an event waits. */
export function noRunsTitle(automation: Automation): string {
  if (automation.when.kind !== 'schedule') return 'No runs yet'
  if (automation.paused) return 'No runs yet — paused'
  if (!automation.next_run_at) return 'No runs yet — not scheduled'
  return `No runs yet — next run ${formatZoned(automation.next_run_at)}`
}

export default function AutomationRuns({ automation, reloadKey, onOpenRun }: {
  automation: Automation
  reloadKey: number
  onOpenRun: (run: AutomationRun) => void
}): JSX.Element {
  const [status, setStatus] = useState('')
  const [q, setQ] = useState('')
  const filtered = status !== '' || q !== ''
  /* A new `reloadKey` or filter is a new `fetchPage`, so the ledger restarts at page one. */
  const fetchPage = useCallback(
    (offset: number | null) => listAutomationRuns(automation.id, offset ?? 0, RUNS_PAGE_SIZE, 50, { status, q }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [automation.id, reloadKey, status, q],
  )
  const paging = usePagedList({
    fetchPage,
    rowsOf: page => page.items,
    nextOf: page => (page.items.length >= RUNS_PAGE_SIZE ? page.offset + page.items.length : null),
    errorFallback: 'Could not load runs',
  })
  const { rows, loaded, error } = paging

  return (
    <Box data-testid="automation-runs" sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
      <FilterBar>
        <FilterRow>
          <FilterSelect
            label="Status"
            ariaLabel="Filter runs by status"
            value={status}
            onChange={setStatus}
            allLabel="All runs"
            options={STATUS_OPTIONS}
            minWidth={130}
          />
          <SearchBox
            label="Search"
            ariaLabel="Search run details"
            value={q}
            onChange={setQ}
            placeholder="e.g. issued"
            testId="automation-runs-search"
            sx={[{ m: 0, minWidth: 160, flex: '1 1 200px' }, fieldTapTargetSx]}
          />
        </FilterRow>
      </FilterBar>
      {error && (
        <Banner severity="error">
          {error}{' '}
          <Button size="small" onClick={() => void paging.reload()}>Retry</Button>
        </Banner>
      )}
      {loaded && rows.length === 0 && (
        <EmptyState title={filtered ? 'No runs match these filters' : noRunsTitle(automation)} />
      )}
      {rows.length > 0 && (
        <Box component="ul" ref={paging.bodyRef} sx={{ listStyle: 'none', m: 0, p: 0, minWidth: 0 }}>
          {groupRuns(rows, paging.hasMore).map(group => (
            <RunRow
              key={group.run.id}
              group={group}
              ownCause={automation.when.kind}
              onOpen={() => onOpenRun(group.run)}
            />
          ))}
        </Box>
      )}
      {loaded && <LoadMoreFooter hasMore={paging.hasMore} loading={paging.loading} onMore={() => void paging.loadMore()} />}
    </Box>
  )
}
