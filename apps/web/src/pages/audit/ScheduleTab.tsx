import { useCallback, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { FilterRow, FilterSelect, vocabularyOptions, withUnmatched } from '../../ui/FilterRow'
import { usePagedList } from '../../hooks/usePagedList'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { useOfficialZone } from '../../lib/officialZone'
import { toggled } from '../../lib/sets'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import NoMatchesState from '../../ui/NoMatchesState'
import FeedList from '../../ui/feed/FeedList'
import FilterBar from '../../ui/FilterBar'
import PagedList from '../../ui/PagedList'
import StatusChip from '../../ui/StatusChip'
import { cronRunRow } from '../cron/cronRunRow'
import { formatZoned } from '../../lib/zonedTime'
import { OUTCOME_EXPLANATION } from '../cron/vocabulary'
import { listFleetCronRuns, type FleetCronRun } from './scheduleApi'
import { foldMissed, missedSentence, type ScheduleLine } from './scheduleFold'
import type { FeedRowSpec } from '../../ui/feed/types'

/* AUDIT > WORKFLOWS > SCHEDULE — every firing of every cron on this control plane, newest first.
 *
 * Not the "Runs" tab (the DURABLE ENGINE's ledger): one row per time a cadence came due, whether or not
 * anything ran — a SKIPPED or MISSED firing has no workflow, and is exactly what "why has this not
 * happened" looks for (`routeGroups.ts` carries the same argument). On Audit rather than per cron because
 * the question is population-wide; `FleetCronRun` carries `cron_id`/`cron_name` (see `./scheduleApi.ts`).
 *
 * THE ROW: cron + sentence as title, outcome as state mark, duration / filed task / age as the cluster. A stretch
 * of consecutive missed firings is one expandable row (`./scheduleFold.ts`).
 * NOT ONE WORD OF THE VOCABULARY IS DECIDED HERE: `pages/cron/vocabulary.ts::readRun`,
 * `OUTCOME_TONE` and `readSpans` are the single home.
 *
 * ORG-ADMIN-ONLY at the route (`App.tsx`) and strip (`AuditPage.tsx`); the boundary is
 * `GET /api/crons/runs`' own gate (`apps/api/app/routes/crons.py` says why).
 */

/* THE ROW IS `pages/cron/cronRunRow`'s; this ledger adds the cron's link (`cron`). NO SKIP CHIP: `readRun`'s sentence
   already says why a firing was skipped ("Skipped — the work the last firing created is still open."), so a raw
   `previous_task_open` beside it said the same thing in machine words. The token stays on the state
   mark's `data-skip` for oracles. */

export default function ScheduleTab({ enabled = true }: {
  /* WHETHER THE LEDGER IS WANTED YET (`AuditPage`'s gate: `TabPanel` keeps panels mounted). LATCHED BY
     THE CALLER, false→true only, so returning does not re-fetch. */
  enabled?: boolean
}) {
  /* THE FILTERS LIVE IN THE URL (`hooks/useUrlFilters`); the mounted Runs tab owns `?workflow=`/`?status=`/`?q=`
     on the same location. */
  const [searchParams, setFilters] = useUrlFilters()
  const cronFilter = searchParams.get('cron') ?? ''
  const outcomeFilter = searchParams.get('outcome') ?? ''

  /* THE ZONE THE SCHEDULE WAS EVALUATED IN — the CP's, not the viewer's (`lib/officialZone.ts`). */
  const zone = useOfficialZone()

  /* A FRESH FIRST PAGE ON EVERY FILTER CHANGE (a new `fetchPage`): a cursor minted under old filters selects
     the wrong rows. ROWS ON SCREEN STAY WHILE A NEW FILTER IS IN FLIGHT; the pager is disabled instead, so a
     press cannot append the old filter's page to the new one. */
  const fetchPage = useCallback(
    (before: string | null) => listFleetCronRuns({ cron: cronFilter, outcome: outcomeFilter, before }),
    [cronFilter, outcomeFilter],
  )
  const paging = usePagedList({
    fetchPage,
    rowsOf: page => page.runs,
    nextOf: page => page.next_before,
    errorFallback: 'Failed to load the schedule ledger.',
    enabled,
  })
  const { rows: runs, error } = paging

  /* THE PICKERS' OPTIONS COME FROM THE SERVER'S SCAN, never the loaded rows — which would hide exactly the
     QUIET schedule an operator is looking for. */
  const { lastPage } = paging
  const cronSelectOptions = useMemo(
    () => withUnmatched((lastPage?.crons ?? []).map(c => ({ value: c.id, label: c.name })), cronFilter),
    [lastPage, cronFilter],
  )
  const outcomeSelectOptions = useMemo(
    () => vocabularyOptions(lastPage?.outcomes ?? [], outcomeFilter),
    [lastPage, outcomeFilter],
  )

  /* WHICH MISSED FOLDS ARE OPEN (`./scheduleFold.ts`), by line key. */
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())
  const runRow = (run: FleetCronRun) => cronRunRow(run, {
    zone,
    testIdPrefix: 'schedule-run',
    cron: { id: run.cron_id, name: run.cron_name },
  })
  /* A FOLD IS ITS NEWEST FIRING'S ROW (state mark, age), retitled and counted; it opens to every firing it folds. */
  const lineRow = (line: ScheduleLine): FeedRowSpec => {
    if (line.kind === 'run') return runRow(line.run)
    const isOpen = expanded.has(line.key)
    const sentence = missedSentence(line.runs)
    const [newest, oldest] = [line.runs[0], line.runs[line.runs.length - 1]]
    const lead = runRow(newest)
    return {
      ...lead,
      key: line.key,
      title: sentence,
      titleText: sentence,
      ariaLabel: sentence,
      disclosure: { expanded: isOpen, onToggle: () => setExpanded(prev => toggled(prev, line.key)), label: 'missed firings' },
      data: { 'row-kind': 'missed-fold', count: line.runs.length },
      notes: undefined,
      children: isOpen ? line.runs.map(runRow) : undefined,
      slots: lead.slots?.map(slot => slot.key === 'fired'
        ? { ...slot, title: `${line.runs.length} missed, fired ${formatZoned(oldest.fired_at, zone)} – ${formatZoned(newest.fired_at, zone)}` }
        : slot),
    }
  }

  const filtered = Boolean(cronFilter || outcomeFilter)
  /* THE ALARM: crons erroring every firing since their last success (`routes/crons.py::FAILING_STREAK_THRESHOLD`).
     Ignores the filters, so narrowing the ledger never hides it; a chip narrows to that cron. */
  const failing = lastPage?.failing ?? []
  /* THE LEGEND: an outcome whose every row would say the same sentence says it here, once, for the outcomes on
     screen; each row carries the word alone (`pages/cron/vocabulary.ts::OUTCOME_EXPLANATION`). */
  const explained = Object.entries(OUTCOME_EXPLANATION).filter(([outcome]) => runs.some(r => r.outcome === outcome && !r.skip_reason))

  return (
    <Stack spacing={1} sx={{ pt: 1 }}>
      {/* THE ERROR DOES NOT CLEAR THE ROWS: the last good reading under a stale banner. */}
      {error && <Banner severity="error">{`${error} — showing the last reading.`}</Banner>}

      {/* Warning, not error: `error-banner` is e2e's "this page failed to load" probe. */}
      {failing.length > 0 && (
        <Banner severity="warning" testId="schedule-failing">
          <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap', alignItems: 'center' }}>
            <span>Failing:</span>
            {failing.map(c => (
              <StatusChip
                key={c.id}
                label={`${c.name} ×${c.consecutive_errors}`}
                tone="error"
                data-status={c.id}
                title={`${c.consecutive_errors} errors in a row`}
                ariaLabel={`Show ${c.name} firings`}
                onClick={() => setFilters({ cron: c.id })}
                press={{ glyph: '⌕', label: 'Filter' }}
              />
            ))}
          </Stack>
        </Banner>
      )}

      <FilterBar>
        <FilterRow>
          <FilterSelect
            label="Cron"
            ariaLabel="Filter firings by cron"
            value={cronFilter}
            onChange={next => setFilters({ cron: next })}
            allLabel="All crons"
            options={cronSelectOptions}
            minWidth={200}
          />
          <FilterSelect
            label="Outcome"
            ariaLabel="Filter firings by outcome"
            value={outcomeFilter}
            onChange={next => setFilters({ outcome: next })}
            allLabel="Any outcome"
            options={outcomeSelectOptions}
            minWidth={160}
          />
        </FilterRow>
      </FilterBar>

      {explained.map(([outcome, text]) => (
        <Typography key={outcome} variant="caption" sx={{ color: 'text.secondary' }} data-testid={`schedule-legend-${outcome}`}>
          <Box component="span" sx={{ fontWeight: 600, textTransform: 'capitalize' }}>{outcome}</Box>
          {`: ${text}`}
        </Typography>
      ))}

      <PagedList
        paging={paging}
        retryTestId="schedule-runs-retry"
        /* TWO ANSWERS: "no firings kept" vs "nothing matches your filter". "Recorded", not "ever fired":
           every retention mode but "All firings" discards rows (`pages/cron/vocabulary.ts::RETENTION_MODE_OPTIONS`). */
        empty={filtered
          ? <NoMatchesState title="No firings match these filters" />
          : <EmptyState title="No firings recorded" scoped />}
        loadMoreLabel="Load older firings"
        loadMoreTestId="schedule-runs-load-more"
        caption={paging.hasMore
          ? `${runs.length} most recent firings.`
          : `${runs.length} firing${runs.length === 1 ? '' : 's'}.`}
      >
        {/* `schedule-runs-table` is what `e2e/audit-tabs.spec.ts` reads. */}
        <FeedList
          ariaLabel="Cron firings"
          testId="schedule-runs-table"
          sections={[{
            key: 'firings',
            /* Filtered to missed, every row is one: folding them all would hide what was asked for. */
            rows: (outcomeFilter === 'missed' ? runs.map(run => ({ kind: 'run', run }) as const) : foldMissed(runs)).map(lineRow),
          }]}
        />
      </PagedList>
    </Stack>
  )
}
