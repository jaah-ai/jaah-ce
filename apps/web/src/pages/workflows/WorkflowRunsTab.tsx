import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import { useTheme } from '@mui/material/styles'
import { FilterRow, FilterSelect, withUnmatched } from '../../ui/FilterRow'
import { usePagedList } from '../../hooks/usePagedList'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { countOf } from '../../lib/humanize'
import { DBOS_STATUSES, DBOS_STATUS_GLYPH, DBOS_STATUS_HELP, DBOS_STATUS_TONE, isInFlight } from '../../lib/dbosStatus'
import { compactAge, NO_VALUE } from '../../lib/relativeTime'
import { formatSpanBetween } from '../../lib/spanTime'
import { formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import NoMatchesState from '../../ui/NoMatchesState'
import FeedList from '../../ui/feed/FeedList'
import { AGE_SLOT, contentSlotWidth, monoFamily } from '../../ui/feed/geometry'
import type { FeedIndicator, FeedRowSpec } from '../../ui/feed/types'
import FilterBar from '../../ui/FilterBar'
import { FEED_NO_META, fieldTapTargetSx, RUNS_NO_WORKFLOW, tapTargetSx, WORKFLOW_RUNS_CONTAINER_SX } from '../../ui/listStyles'
import PagedList from '../../ui/PagedList'
import SearchBox from '../../ui/SearchBox'
import { listWorkflowRuns, type WorkflowRun, type WorkflowType } from './api'
import {
  ENGINE_DOWN_RUNS,
  NO_RUNS_AT_ALL_TITLE,
  NO_RUNS_MATCHED_TITLE,
  SEARCH_LABEL,
  SEARCH_PLACEHOLDER,
  readRun,
  taskIdOf,
} from './vocabulary'
import { TaskLink } from '../../ui/DetailFields'

/* The workflow run ledger, newest first. Two callers: `pages/audit/WorkflowRunsPanel.tsx` (unpinned,
 * global) and `/workflows/:name` (pinned). Pinning changes three things only: no type filter, no
 * Workflow slot, and the empty state names the workflow.
 * Filters live in the URL, read-through: shareable, back-button undoes them, no second copy.
 * A run's reading is the row title (a node, coloured by `kind`); `recovered ×N` is a marker, not a gated
 * slot, so narrow screens still show it. The pager caps a read at 50 rows. */

/* Row mark from the run's status (`lib/dbosStatus.ts`); » in flight, `?` for an unknown status shown as
 * itself. */
function statusMark(run: WorkflowRun): FeedIndicator {
  return {
    tone: DBOS_STATUS_TONE[run.status] ?? 'neutral',
    glyph: DBOS_STATUS_GLYPH[run.status] ?? (isInFlight(run.status) ? '»' : '?'),
    label: run.status,
    title: DBOS_STATUS_HELP[run.status],
    // State oracle for tests.
    data: { status: run.status },
  }
}

/* The Workflow column's floor (its priced width) and its cap, a share of the ledger's width: sized to the widest
   label on the page (`ui/feed/geometry.ts::contentSlotWidth`), so "Task notification delivery" is not cut while
   the run id keeps its 160px. */
const WORKFLOW_SLOT = 116
const WORKFLOW_MAX_CQW = 18

/* The run id the same way, measured in its mono face: `task-notify:44:system_failed:…` was cut beside a reading
   with hundreds of spare pixels. 160px stays the floor (its priced width); the cap leaves the reading its room. */
const RUN_ID_SLOT = 160
const RUN_ID_MAX_CQW = 40

/* Where the Workflow slot is gated off (`runsWorkflow`), its label leads the reading instead, muted: rows of one
   sentence ("Finished, returning nothing.") must still say which workflow ran. Hidden wherever the slot shows. */
const LABEL_PREFIX_SX = {
  display: 'none',
  color: 'text.secondary',
  [FEED_NO_META]: { display: 'inline' },
  [RUNS_NO_WORKFLOW]: { display: 'inline' },
} as const

/** DBOS's seven statuses as their raw tokens (what logs and the engine call them). */
const STATUS_OPTIONS = DBOS_STATUSES.map(status => ({ value: status, label: status }))

export default function WorkflowRunsTab({
  enabled = true,
  pinnedName,
  types = [],
  onRead,
}: {
  /* Whether the ledger is wanted yet: a mounted-but-hidden `TabPanel` should not fetch. Latched
     false→true by such a caller (`WorkflowDetailPage`'s Runs tab); the gate is about the ledger's cost. */
  enabled?: boolean
  /** Restrict every read to one workflow and drop the controls that change it. */
  pinnedName?: string
  /* Type filter options, handed down to avoid a duplicate `/api/workflows` read (a 500-run aggregate
     scan) beside a sibling panel — `pages/audit/WorkflowRunsPanel.tsx` supplies them. Empty is legitimate
     (not loaded or failed): the filter degrades to "All workflows". */
  types?: WorkflowType[]
  /** Called after each ledger read lands, good or failed, so a sibling reading beside it refreshes with it
   *  (`pages/audit/WorkflowRunsPanel.tsx`'s pending cron ticks). */
  onRead?: () => void
}) {
  const theme = useTheme()
  const [searchParams, setFilters] = useUrlFilters()

  /* Filters derived from the URL. A pinned name beats `?workflow=`, so a stray key cannot widen the page. */
  const nameFilter = pinnedName ?? searchParams.get('workflow') ?? ''
  const statusFilter = searchParams.get('status') ?? ''
  const searchFilter = searchParams.get('q') ?? ''
  const [searchReset, setSearchReset] = useState(0)

  /* A run opens in its own pane (`WorkflowRunPage.tsx`), carrying the row so it paints at once. Pinned, the
     ledger is a workflow's Runs tab, itself a pane: `?from=` that tab is where the run's Close returns
     (`lib/listDetail.ts::safeReturnPath`). Unpinned it is the Audit list behind the pane, where Close goes anyway. */
  const navigate = useNavigate()
  const location = useLocation()
  const from = pinnedName === undefined ? '' : `?from=${encodeURIComponent(`${location.pathname}${location.search}`)}`
  /* The title's link carries `from` too: it is a control of its own and never reaches `openRun`. */
  const runPath = (run: WorkflowRun) => `/workflows/runs/${encodeURIComponent(run.workflow_id)}${from}`
  const openRun = (run: WorkflowRun) => navigate(runPath(run), { state: { row: run } })

  /* The cursor is the server's echoed `next_offset`, never `runs.length`. A filter change mints a new
     `fetchPage`, so paging restarts from offset 0 by construction. Nothing is read until enabled. */
  const fetchPage = useCallback(
    (offset: number | null) => listWorkflowRuns({
      name: nameFilter,
      status: statusFilter,
      search: searchFilter,
      offset: offset ?? undefined,
    }),
    [nameFilter, statusFilter, searchFilter],
  )
  const paging = usePagedList({
    fetchPage,
    rowsOf: page => page.runs,
    nextOf: page => page.next_offset,
    errorFallback: 'Failed to load workflow runs',
    enabled,
  })
  const { rows: runs, error, loaded, lastPage } = paging
  useEffect(() => {
    if (lastPage !== null || error) onRead?.()
  }, [lastPage, error, onRead])
  const engineAvailable = lastPage?.engine_available ?? true
  /* A null `next_offset` means end of rows OR the next page would pass `OFFSET_MAX` (`routes/workflows.py`,
     5,000). Only a full last page can be the cap case. The server's echoed `limit`, not `RUNS_PAGE_SIZE`. */
  const lastPageFull = lastPage !== null && lastPage.runs.length >= lastPage.limit

  const typeOptions = useMemo(
    () => withUnmatched(types.map(type => ({ value: type.name, label: type.label })), nameFilter),
    [types, nameFilter],
  )
  /** An unmatched status is always hand-typed or stale — the server refuses it. An unmatched workflow is
   *  e.g. `?workflow=cron_tick` (registry-excluded; the server 422s), or a valid one before the catalogue lands. */
  const statusOptions = useMemo(() => withUnmatched(STATUS_OPTIONS, statusFilter), [statusFilter])

  /* What narrowed the view, for the empty state. The pinned name is the page's subject, not a filter. */
  const describedFilters: string[] = []
  if (!pinnedName && nameFilter) {
    describedFilters.push(`workflow “${types.find(t => t.name === nameFilter)?.label ?? nameFilter}”`)
  }
  if (statusFilter) describedFilters.push(`status ${statusFilter}`)
  if (searchFilter) describedFilters.push(`run ids starting “${searchFilter}”`)

  const clearFilters = useCallback(() => {
    setFilters({ workflow: '', status: '', q: '' })
    // `?q=` may already be empty with text still typed: the bump drops it too (`ui/SearchBox`'s `resetKey`).
    setSearchReset(n => n + 1)
  }, [setFilters])

  /* Pinning drops the Workflow slot (every row would repeat it). */
  const showWorkflow = pinnedName === undefined
  const workflowWidth = contentSlotWidth(showWorkflow ? runs.map(run => run.label) : [], WORKFLOW_SLOT, WORKFLOW_MAX_CQW)
  const runIdWidth = contentSlotWidth(runs.map(run => run.workflow_id), RUN_ID_SLOT, RUN_ID_MAX_CQW, monoFamily(theme))

  /* Rows, slots ordered by gate strength (`ui/feed/geometry.ts::SlotGate`). Duration is wall clock
     `created_at → completed_at`, including queue wait; the run's page splits it. */
  const runRows: FeedRowSpec[] = runs.map((run): FeedRowSpec => {
    const reading = readRun(run)
    const taskId = taskIdOf(run)
    /* A completed run with a bad `created_at` gets `NO_VALUE` (truthy); treat it as unmeasured. */
    const span = run.completed_at ? formatSpanBetween(run.created_at, run.completed_at) : null
    const measured = span !== null && span !== NO_VALUE
    const openLabel = `Open the ${run.label} run of ${formatZoned(run.created_at)}`

    return {
      key: run.workflow_id,
      indicator: statusMark(run),
      /* Failure text in error colour; `pending` muted. */
      title: (
        <>
          {showWorkflow && (
            <Box component="span" data-testid="workflow-run-label-prefix" sx={LABEL_PREFIX_SX}>{`${run.label} · `}</Box>
          )}
          <Box
            component="span"
            data-testid="workflow-run-reading"
            sx={{
              color: reading.kind === 'failure'
                ? 'error.main'
                : reading.kind === 'pending' ? 'text.secondary' : 'text.primary',
            }}
          >
            {reading.sentence}
          </Box>
        </>
      ),
      titleText: reading.sentence,
      /* Two lines at every width (a long failure reading wraps on desktop too, rather than ellipsising), so the
         label prefix never pushes the reading off a phone row. */
      titleLines: 2,
      /* Full accessible name (which run); also silences mark and marker (`FeedRowSpec.ariaLabel`). */
      ariaLabel: `${run.label} run at ${formatZoned(run.created_at)} — ${run.status}`,
      data: { status: run.status },
      /* A run re-run after an executor died may have done its work twice. Shown only when reported. */
      markers: run.recovery_attempts != null && run.recovery_attempts > 1
        ? [{ key: 'recovered', glyph: '↻', label: `recovered ×${run.recovery_attempts}` }]
        : undefined,
      slots: [
        ...(showWorkflow
          ? [{
              key: 'workflow',
              width: workflowWidth,
              gate: 'runsWorkflow' as const,
              align: 'left' as const,
              testId: 'workflow-run-workflow',
              title: run.label,
              node: run.label,
            }]
          : []),
        {
          /* The whole id, ellipsised at the tail. Workflow ids are not uuids (e.g.
             `task-notify:105:done:2026-08-27T21:45:00+00:00`, `task-done-146-0`), so a last-eight cut
             is ambiguous. */
          key: 'runid',
          width: runIdWidth,
          gate: 'runsId',
          align: 'right',
          mono: true,
          dim: true,
          testId: 'workflow-run-id',
          title: run.workflow_id,
          node: run.workflow_id,
        },
        {
          key: 'duration',
          width: 60,
          gate: 'id',
          align: 'right',
          mono: true,
          node: (
            <Box
              component="span"
              sx={{
                color: measured ? 'text.primary' : 'text.disabled',
                fontStyle: span === null ? 'italic' : 'normal',
              }}
              data-testid="workflow-run-duration"
              /* The register as state, so tests do not grade the stylesheet. Always written. */
              data-measured={measured ? 'true' : 'false'}
            >
              {span ?? 'running'}
            </Box>
          ),
        },
        {
          /* A real anchor (middle-click, copy link). `vocabulary.ts::taskIdOf` gates when it is offered. */
          key: 'task',
          width: 48,
          align: 'right',
          node: taskId === null ? null : (
            /* Verb as the accessible name; `#id` as ink (48px slot). */
            <TaskLink taskId={taskId} aria-label={`Open task #${taskId}`} testId="workflow-run-task-link" />
          ),
        },
        {
          /* `compactAge` (bounded at four chars, fits `AGE_SLOT`) with the zoned instant on hover. The
             viewer's zone: a run has no control-plane zone. */
          key: 'started',
          width: AGE_SLOT,
          align: 'right',
          mono: true,
          dim: true,
          testId: 'workflow-run-started',
          title: formatZoned(run.created_at),
          node: compactAge(run.created_at),
        },
      ],
      /* The title is the run's link (middle-click, copy link, and the open row marked `current`); the row body
         opens it with the row in hand. `FeedList` ignores presses from inner controls, the ⋯ popup, selection. */
      href: runPath(run),
      onRowClick: () => openRun(run),
      rowClickLabel: openLabel,
      /* Same verb in the ⋯: `onRowClick` is pointer-only, so this is the keyboard path. */
      actions: [
        { key: 'open', icon: '▤', label: 'Open', ariaLabel: openLabel, onClick: () => openRun(run) },
      ],
    }
  })

  return (
    /* The ledger's own width gates its slots (`ui/listStyles.ts`'s `RUNS_NO_*`): beside an open run it narrows. */
    <Box sx={WORKFLOW_RUNS_CONTAINER_SX}>
      <FilterBar>
        <FilterRow>
          {/* `FilterRow` hands `ui/FilterBar` one wrapping child so controls pack at phone width. */}
          {showWorkflow && (
            <FilterSelect
              label="Workflow"
              ariaLabel="Filter by workflow"
              value={nameFilter}
              onChange={next => setFilters({ workflow: next })}
              allLabel="All workflows"
              options={typeOptions}
              minWidth={190}
            />
          )}
          <FilterSelect
            label="Status"
            ariaLabel="Filter by status"
            value={statusFilter}
            onChange={next => setFilters({ status: next })}
            allLabel="Any status"
            options={statusOptions}
            minWidth={190}
          />
          {/* "Starts with": the server match is a prefix (strings single-homed in `vocabulary.ts`). */}
          <SearchBox
            resetKey={searchReset}
            label={SEARCH_LABEL}
            ariaLabel={SEARCH_LABEL}
            value={searchFilter}
            onChange={next => setFilters({ q: next.trim() })}
            placeholder={SEARCH_PLACEHOLDER}
            /* `m: 0` cancels the dense margin; `fieldTapTargetSx` is the 44px floor on the native input. */
            sx={[{ m: 0, minWidth: 240, flex: '1 1 260px' }, fieldTapTargetSx]}
          />
        </FilterRow>
      </FilterBar>

      <Stack spacing={1}>
        {error && <Banner severity="error">{error}</Banner>}
        {/* Engine down: the list is empty for a reason other than "nothing happened". Exclusive with the
            empty states (branch chain below). */}
        {loaded && !engineAvailable && (
          <Banner severity="warning" testId="workflow-runs-engine-unavailable">
            {ENGINE_DOWN_RUNS}
          </Banner>
        )}

        {/* Nothing while unopened: a skeleton in a hidden `TabPanel` would claim a read in flight
            (`e2e/app-shell-scroll.spec.ts` asserts no `loading-state` survives). */}
        {enabled && (
          <PagedList
            paging={paging}
            retryTestId="workflow-runs-retry"
            /* Also offer Clear filters: a refused filter (e.g. `?workflow=cron_tick`) 422s on every retry. */
            retryExtra={describedFilters.length > 0 && (
              <Button variant="outlined" onClick={clearFilters} sx={tapTargetSx} data-testid="workflow-runs-clear-filters">
                Clear filters
              </Button>
            )}
            /* Engine down with no rows: the banner above is the whole story (no empty state claims nothing
               ever ran). Else a filtered miss vs genuinely empty — different answers. */
            empty={!engineAvailable ? null : describedFilters.length > 0 ? (
              <NoMatchesState
                title={NO_RUNS_MATCHED_TITLE}
                onClear={clearFilters}
                testId="workflow-runs-clear-filters"
              />
            ) : (
              <EmptyState title={NO_RUNS_AT_ALL_TITLE} />
            )}
            loadMoreLabel="Load older runs"
            loadMoreTestId="workflow-runs-load-more"
            /* Three readings: more exist; pager cap reached (`lastPageFull`) — narrow filters; or all of them. */
            caption={paging.hasMore
              ? `${runs.length} most recent runs`
              : lastPageFull
                ? `${countOf(runs.length, 'run')} — narrow the filters to see older ones`
                : countOf(runs.length, 'run')}
          >
            {/* `workflow-runs-table`: `e2e/audit-tabs.spec.ts` waits on it. */}
            <FeedList
              ariaLabel="Workflow runs"
              testId="workflow-runs-table"
              sections={[{ key: 'runs', rows: runRows }]}
            />
          </PagedList>
        )}
      </Stack>
    </Box>
  )
}
