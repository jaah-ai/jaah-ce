/* AN AUTOMATION'S RUNS (`GET /api/automations/{id}/runs`), newest first (AC4.6, AC7.4), as the console's one list
 * (`ui/feed/FeedList`), the twin of Audit > Schedule's `pages/cron/cronRunRow.tsx`. One row per firing: the outcome
 * is the state disc (its word on hover), the title is what happened — the message an email run ran on (subject ·
 * sender), then one line for its targets ("480 filed · 15 skipped: previous still open") or, for a run from before
 * the fire engine, its stored headline (AC5.6); a failed run's title is its short cause, the raw text (an exception
 * `repr`) on hover. The journal under a headline is hover prose: its first three lines, so a pass's decisions read
 * without opening it (AC5.6), then "+N more lines" for the rest, read in full on the run's page. Trailing: the
 * cause where it differs from the automation's own kind (a "Run now" on a schedule; its slot gates off a phone,
 * so the hover says it too), the filed task, the age. Consecutive skips with one reason fold into one row (a ×12 marker, its span on hover) — a wedged
 * schedule's `already_running` skips as one "Skipped 12 runs since …" title — and so do consecutive missed runs
 * with one detail (not ten bare "Missed" rows in a row); the strip filters server-side (Status, Search), so the
 * ticks that did something are findable.
 * A row opens the run's own page (`AutomationRunPage.tsx`) through `onOpenRun`, the editor's: it owns the unsaved-
 * edit gate and the address, and hands the row along so the page paints at once. */
import { useCallback, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import { RUNS_PAGE_SIZE, usePagedList } from '../../hooks/usePagedList'
import { clockTime, compactAge, NO_VALUE } from '../../lib/relativeTime'
import { formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import { TaskLink } from '../../ui/DetailFields'
import EmptyState from '../../ui/EmptyState'
import FeedList from '../../ui/feed/FeedList'
import { AGE_SLOT } from '../../ui/feed/geometry'
import type { FeedNote, FeedRowSpec, OverflowItem } from '../../ui/feed/types'
import { FilterBar } from '../../ui/FilterBar'
import { FilterRow, FilterSelect } from '../../ui/FilterRow'
import { fieldTapTargetSx } from '../../ui/listStyles'
import NoMatchesState from '../../ui/NoMatchesState'
import PagedList from '../../ui/PagedList'
import SearchBox from '../../ui/SearchBox'
import { detailLines, OUTCOME } from '../cron/vocabulary'
import { listAutomationRuns, type Automation, type AutomationRun } from './api'
import { CAUSE_LABELS, runLine, runOutcomeLabel, runOutcomeTone, targetSummary } from './vocabulary'
import { FONT_WEIGHT, SIZE, grid } from '../../theme'

/** The server's `status` tokens (`routes/automations.py::RUNS_STATUSES`). */
const STATUS_OPTIONS = [
  { value: 'no_skips', label: 'Hide skips' },
  { value: 'acted', label: 'Did work' },
]

/** A received message with an empty Subject header. */
const NO_SUBJECT = '(no subject)'

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

/** The journal lines a row's hover shows before "+N more lines". */
const JOURNAL_LINES = 3

/** One firing (or fold) as a feed row. `ownCause` is the automation's own kind: a run with that cause says nothing
 *  new, so its cause slot stays blank. */
function runRow(group: RunGroup, ownCause: string, onOpen: () => void): FeedRowSpec {
  const { run, count } = group
  const hasTargets = run.targets_total > 0
  const lines = detailLines(run.detail)
  const since = skippedSince(group)
  const line = since ?? (hasTargets ? targetSummary(run) : runLine(run, lines[0].trim()))
  /* A failed run leads with its short cause; the raw text (an exception `repr`) is its hover, as the automation's
     header does. Only when it says something the raw line does not — a difference of case alone is the same words. */
  const cause = run.failure_cause && run.failure_cause.trim().toLowerCase() !== line.trim().toLowerCase()
    ? run.failure_cause
    : null
  const journal = hasTargets || since ? [] : lines.slice(1).filter(text => text.trim() !== '')
  const label = runOutcomeLabel(run)
  const firedAt = formatZoned(run.fired_at)
  const trigger = run.cause !== ownCause ? (CAUSE_LABELS[run.cause] ?? run.cause) : null
  const email = run.email ? `${run.email.subject || NO_SUBJECT}${run.email.from ? ` · ${run.email.from}` : ''}` : null
  const openLabel = `Open the run of ${firedAt}`

  const notes: FeedNote[] = []
  if (cause && line) notes.push({ key: 'raw', text: line, tone: 'error' })
  if (trigger) notes.push({ key: 'cause', text: trigger, tone: 'muted' })
  if (journal.length > 0) notes.push({ key: 'journal', text: journal.slice(0, JOURNAL_LINES).join('\n'), tone: 'muted' })
  const more = journal.length - JOURNAL_LINES
  if (more > 0) {
    notes.push({
      key: 'more',
      tone: 'muted',
      text: more === 1 ? '+1 more line — open the run to read it' : `+${more} more lines — open the run to read them`,
    })
  }

  /* Same verb in the ⋯: `onRowClick` is pointer-only, so this is the keyboard path. The task's ⋯ item is what lets its
     slot gate off a phone (`ui/feed/types.ts`). */
  const actions: OverflowItem[] = [{ key: 'open', icon: '▤', label: 'Open', ariaLabel: openLabel, onClick: onOpen }]
  const taskId = !hasTargets && run.task_id !== null ? run.task_id : null
  if (taskId !== null) actions.push({ key: 'task', icon: '↗', label: `Open task #${taskId}`, ariaLabel: `Open task #${taskId}`, to: `/tasks/${taskId}` })

  /* The headline: the cause (error ink), else the line, else the outcome's word alone — never a line that only
     repeats the disc. */
  const headline = cause
    ? <Box component="span" data-testid="automation-run-cause" sx={{ color: 'error.main', fontWeight: FONT_WEIGHT.semibold }}>{cause}</Box>
    : line
      ? <Box component="span" data-testid="automation-run-summary" sx={{ color: hasTargets ? 'text.primary' : 'text.secondary' }}>{line}</Box>
      : email ? null : label

  return {
    key: run.id,
    indicator: {
      tone: runOutcomeTone(run),
      glyph: OUTCOME[run.outcome]?.glyph ?? '?',
      label,
      title: label,
      /* The token an oracle reads. */
      data: { status: run.outcome },
    },
    title: (
      <>
        {email && <Box component="span" data-testid="automation-run-email">{email}</Box>}
        {email && headline && ' — '}
        {headline}
      </>
    ),
    titleText: [email, cause ?? line].filter(Boolean).join(' — ') || label,
    href: null,
    /* Two lines at every width: a subject beside a target summary is cut mid-word on one line at phone width. */
    titleLines: 2,
    markers: count > 1 && !since
      ? [{ key: 'repeat', glyph: `×${count}${group.open ? '+' : ''}`, label: `${foldCount(group)} runs folded`, title: foldedSpan(group), testId: 'automation-run-repeat' }]
      : undefined,
    slots: [
      {
        key: 'cause',
        width: grid(13),
        gate: 'meta',
        align: 'left',
        dim: true,
        testId: trigger ? 'automation-run-trigger' : undefined,
        node: trigger,
      },
      {
        /* A real anchor (middle-click, copy link) to the task's own page; `FeedList` ignores row presses inside an `<a>`. */
        key: 'task',
        width: grid(9.5),
        gate: 'id',
        align: 'right',
        node: taskId === null ? null : <TaskLink taskId={taskId} testId="automation-run-task-link" />,
      },
      {
        /* `compactAge` (≤4 chars fits `AGE_SLOT`) with the zoned instant on hover. */
        key: 'fired',
        width: AGE_SLOT,
        align: 'right',
        mono: true,
        dim: true,
        testId: 'automation-run-fired',
        title: `Fired ${firedAt}`,
        node: compactAge(run.fired_at),
      },
    ],
    notes: notes.length > 0 ? notes : undefined,
    onRowClick: onOpen,
    rowClickLabel: openLabel,
    actions,
    data: { id: run.id },
  }
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
  const { rows, error } = paging

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
            minWidth={SIZE.fieldSm}
          />
          <SearchBox
            label="Search"
            ariaLabel="Search run details"
            value={q}
            onChange={setQ}
            placeholder="e.g. issued"
            testId="automation-runs-search"
            sx={[{ m: 0, minWidth: SIZE.fieldMd, flex: `1 1 ${grid(33)}` }, fieldTapTargetSx]}
          />
        </FilterRow>
      </FilterBar>
      {/* Over rows from an earlier read; with none, `PagedList`'s Try again is the way out. */}
      {error && rows.length > 0 && <Banner severity="error">{error}</Banner>}
      <PagedList
        paging={paging}
        retryTestId="automation-runs-retry"
        empty={filtered ? <NoMatchesState title="No runs match" /> : <EmptyState title={noRunsTitle(automation)} />}
        loadMoreLabel="Load more"
        loadMoreTestId="automation-runs-load-more"
        caption={null}
      >
        <FeedList
          ariaLabel="Runs"
          testId="automation-runs-list"
          sections={[{
            key: 'runs',
            rows: groupRuns(rows, paging.hasMore).map(group => runRow(group, automation.when.kind, () => onOpenRun(group.run))),
          }]}
        />
      </PagedList>
    </Box>
  )
}
