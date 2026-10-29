import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { FilterRow, FilterSelect, vocabularyOptions } from '../../ui/FilterRow'
import { formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import CellLabel from '../../ui/CellLabel'
import DataTable from '../../ui/DataTable'
import EmptyState from '../../ui/EmptyState'
import LoadFailedState from '../../ui/LoadFailedState'
import NoMatchesState from '../../ui/NoMatchesState'
import LoadingState from '../../ui/LoadingState'
import StatusChip, { type Tone } from '../../ui/StatusChip'
import { DateTimeBoundFields } from '../../ui/DateTimeBoundFields'
import { FULL_BLEED_SX, monoSx, PHONE, PHONE_CARD_SX } from '../../ui/listStyles'
import { ClearFiltersButton, LogPagerFooter, StaleResultBanner } from '../../ui/LogPagerFooter'
import { logPages, PER_PAGE, setRangeParams, usePagedLogFilters } from '../../hooks/usePagedLogFilters'
import { humanize } from '../../lib/humanize'

/* Audit > Exclusions — the tenancy read seam's trail: what an ORM read's org predicate held back (the
 * seam ships enforcing), shown before a first external tenant is onboarded. The seam mode is the loudest
 * fact (header badge).
 * An empty log is ambiguous — "excluded nothing" vs "nothing writing the log"; the writer swallows
 * failures by design, so `writer_ok` is a precondition: it raises a banner.
 * Rows are per-minute aggregates (class, scope_kind, org, route, minute) with a count; `When` prints the
 * zoned minute, since it is the bucket's identity.
 * A table, not `ui/feed/FeedList`: seven cross-read facts and no name; feed slots would drop columns.
 * `ui/DataTable` + `PHONE_CARD_SX` + `ui/CellLabel` restack as cards at ≤480px.
 * No free-text search: filters are selects over the payload's closed vocabularies, so an option may
 * match nothing (an honest answer), and options never narrow mid-use.
 * Operator-only at the route (`App.tsx`). Rendered as an Audit tab (`pages/audit/AuditPage.tsx`), whose
 * shell owns the page header: the seam badge sits at the top of the panel. */

type TenancyExclusion = {
  id: number
  /** ISO, truncated to the minute — the aggregation bucket. */
  occurred_at: string
  mapped_class: string
  scope_kind: string
  bound_org: string | null
  /** The org's name, or `unbound` — resolved server-side. */
  bound_org_label: string
  route_or_job: string
  mode: string
  excluded_count: number
}

type TenancyExclusionsPage = {
  items: TenancyExclusion[]
  total: number
  total_unfiltered: number
  /** Earliest instant a row could still be retained: `max(now - retention_days, log_started_at)`. */
  covering_since: string
  retention_days: number
  /** When the log began covering anything; null on a database migrated before the marker. */
  log_started_at: string | null
  /** False when this API process's flusher is failing (3+ misses): empty or short results are not
   *  evidence that the seam excluded nothing. */
  writer_ok: boolean
  /** Only when `writer_ok` is false: the server's sentence, rendered as given. */
  writer_detail: string | null
  latest_occurred_at: string | null
  classes: string[]
  orgs: { id: string; name: string }[]
  scope_kinds: string[]
  modes: string[]
  seam_mode: string
}

/** Skeleton rows for the first load, so the layout does not jump. */
const SKELETON_ROWS = 8

/** The `bound_org` literal for "nothing bound"; not a uuid, so it shares the org select safely. */
const UNBOUND = 'unbound'

/* Tolerance for reading `covering_since` and `log_started_at` as one instant. `covering_since` is
 * truncated to the minute, so when log age binds it is up to 60s earlier (e.g. 08:03:00Z vs
 * 08:03:25.9Z); 90s adds skew headroom. Under a retention-bound window the two differ by days, so it
 * cannot misfire except at the crossover, where the clause is borderline true. */
const SAME_INSTANT_MS = 90_000

/* Tone maps over server vocabularies: unknown tokens render as themselves (`?? 'neutral'` / `?? '·'`,
   per `ui/statusTone.ts`). `identity_denied` is `error`; `unbound` and `operator` (only on
   `unbound_write`, expected until the operator plane gets an org-bearing write path) are `warning`;
   `derived_unstamped` is `info` (a bound org with an unstamped parent — clears as the backlog drains);
   `org` is `info`, never `success`. */
const SCOPE_TONE: Record<string, Tone> = {
  unbound: 'warning',
  identity_denied: 'error',
  derived_unstamped: 'info',
  operator: 'warning',
  org: 'info',
}

const SCOPE_GLYPH: Record<string, string> = {
  unbound: '∅',
  identity_denied: '⊘',
  derived_unstamped: '↑',
  operator: '◇',
  org: '◈',
}

const MODE_TONE: Record<string, Tone> = {
  read: 'neutral',
  unbound_write: 'warning',
}

const MODE_GLYPH: Record<string, string> = {
  read: '▤',
  unbound_write: '✎',
}

/* The phone card's line layout (`listStyles.PHONE_CARD_SX` leaves it to the caller): `When` leads with
 * the count beside it, the short tokens flow, and `Route or job` takes its own last line so it wraps.
 * `order`, not DOM moves, keeps reading order. Selected by `data-col`, not `nth-of-type`. */
const EXCLUSION_PHONE_CARD_SX = {
  [PHONE]: {
    '& tbody td[data-col="when"]': { order: 0, flex: '1 1 auto', fontWeight: 600 },
    /* `flexDirection: 'row'` undoes `TableCell`'s `align="right"` `row-reverse`, which would put the
       `CellLabel` after the value on the card. */
    '& tbody td[data-col="count"]': { order: 1, flex: '0 0 auto', flexDirection: 'row' },
    '& tbody td[data-col="class"]': { order: 2, flex: '0 0 auto' },
    '& tbody td[data-col="scope"]': { order: 3, flex: '0 0 auto' },
    '& tbody td[data-col="org"]': { order: 4, flex: '0 1 auto' },
    '& tbody td[data-col="mode"]': { order: 5, flex: '0 0 auto' },
    /* Heading on its own line above the route, so the route gets the full card width. `> span` is the
       `CellLabel`; the value is a bare text node. */
    '& tbody td[data-col="route"]': { order: 6, flex: '1 1 100%', flexWrap: 'wrap' },
    '& tbody td[data-col="route"] > span': { flex: '1 1 100%' },
  },
} as const

/** Head cells in column order; only the count right-aligns (compared down the column). */
const COLUMNS: readonly { key: string; label: string; align?: 'right' }[] = [
  { key: 'when', label: 'When' },
  { key: 'class', label: 'Class' },
  { key: 'scope', label: 'Scope' },
  { key: 'org', label: 'Org' },
  { key: 'route', label: 'Route or job' },
  { key: 'mode', label: 'Mode' },
  { key: 'count', label: 'Count', align: 'right' },
]

export default function TenancyExclusionsPage(): JSX.Element {
  /* The viewer's zone, named on the page: inputs take a zoneless wall clock, so display and filter
     must agree. Every filter change goes through `applyFilter`, resetting the page index, so a narrowed
     result is not shown past its end. */
  const {
    zone, from, to, page, setPage, narrow: applyFilter, clearShared, sharedFiltersSet, rangeFields,
  } = usePagedLogFilters()

  const [mappedClass, setMappedClass] = useState('')
  const [org, setOrg] = useState('')
  const [scopeKind, setScopeKind] = useState('')
  const [mode, setMode] = useState('')

  /* Cleared means omitted: a blank param has no API value. */
  const url = useMemo(() => {
    const params = new URLSearchParams()
    params.set('limit', String(PER_PAGE))
    params.set('offset', String(page * PER_PAGE))
    if (mappedClass) params.set('mapped_class', mappedClass)
    if (org) params.set('bound_org', org)
    if (scopeKind) params.set('scope_kind', scopeKind)
    if (mode) params.set('mode', mode)
    setRangeParams(params, from, to)
    return `/api/tenancy-exclusions?${params.toString()}`
  }, [mappedClass, org, scopeKind, mode, from, to, page])

  /* On a failed read the hook keeps `data`: stale rows under a banner beat none. Superseded responses
     are fenced by the hook. */
  const { data, loading, error, reload } = useApiRead(
    ['tenancy-exclusions', url],
    () => get<TenancyExclusionsPage>(url),
    'Failed to load the tenancy exclusion log',
  )

  const items = data?.items ?? []
  const total = data?.total ?? 0
  const totalUnfiltered = data?.total_unfiltered ?? 0
  const pages = logPages(total, page)
  const { totalPages, maxPageIndex } = pages

  /* A clamp: rows age out under the operator, shrinking the page count. */
  useEffect(() => {
    if (page > maxPageIndex) setPage(Math.max(0, maxPageIndex))
  }, [page, maxPageIndex])

  const hasFilters = sharedFiltersSet || mappedClass !== '' || org !== '' || scopeKind !== '' || mode !== ''

  const clearFilters = useCallback(() => {
    applyFilter(() => {
      setMappedClass('')
      setOrg('')
      setScopeKind('')
      setMode('')
      clearShared()
    })
  }, [applyFilter, clearShared])

  /* Options are the payload's lists plus the active value, so an applied filter never vanishes from its
     own control. */
  const classOptions = useMemo(
    () => vocabularyOptions(data?.classes ?? [], mappedClass, { sort: true }),
    [data?.classes, mappedClass],
  )

  /* `unbound` is a first-class org choice (a value of `bound_org`), distinct from "any org". */
  const orgOptions = useMemo(() => {
    const known = new Map<string, string>()
    for (const entry of data?.orgs ?? []) known.set(entry.id, entry.name)
    if (org && org !== UNBOUND && !known.has(org)) known.set(org, org)
    const named = Array.from(known, ([id, name]) => ({ value: id, label: name })).sort((a, b) =>
      a.label.localeCompare(b.label),
    )
    return [{ value: UNBOUND, label: 'Unbound — no org' }, ...named]
  }, [data?.orgs, org])

  const scopeOptions = useMemo(
    () => vocabularyOptions(data?.scope_kinds ?? [], scopeKind, { label: humanize }),
    [data?.scope_kinds, scopeKind],
  )
  const modeOptions = useMemo(
    () => vocabularyOptions(data?.modes ?? [], mode, { label: humanize }),
    [data?.modes, mode],
  )

  const retry = useCallback(() => { void reload() }, [reload])

  /* Writer health is a precondition for reading the rest of the page (see header). `=== false`, not
     `!writer_ok`: an older API that omits the field is not a claim of failure. */
  const writerFailing = data !== null && data.writer_ok === false
  /* Fallback for `writer_ok: false` with no detail, so the suppression below is never unexplained. */
  const writerDetail = data?.writer_detail ?? null

  /* Which term bound the window: retention (a full cycle has run) or `log_started_at` (the log is
     younger than retention — not enough evidence yet). Same timestamp, opposite readings. */
  const windowBoundByLogAge = useMemo(() => {
    if (data === null || data.log_started_at === null) return false
    const started = Date.parse(data.log_started_at)
    const covering = Date.parse(data.covering_since)
    if (Number.isNaN(started) || Number.isNaN(covering)) return false
    return Math.abs(covering - started) <= SAME_INSTANT_MS
  }, [data])

  /* The seam mode as the page's loudest fact, in words. An unrecognised mode renders itself. */
  const seamMode = data?.seam_mode ?? ''
  const seamBadge = seamMode
    ? {
        shadow: {
          tone: 'warning' as Tone,
          label: 'seam: shadow — nothing is actually being excluded yet',
          title:
            'The read seam computes the org predicate and records what it WOULD have excluded. Every read still returns everything it always did.',
        },
        enforcing: {
          tone: 'error' as Tone,
          label: 'seam: enforcing — these reads are being excluded for real',
          title: 'The org predicate is applied. Every row below is data a read did not return.',
        },
      }[seamMode] ?? {
        tone: 'neutral' as Tone,
        label: `seam: ${seamMode}`,
        title: 'A seam mode this console does not have copy for. The rows below are unaffected.',
      }
    : null

  const seamChip = seamBadge && (
    <StatusChip
      label={seamBadge.label}
      tone={seamBadge.tone}
      glyph={seamMode === 'enforcing' ? '⊘' : '◐'}
      title={seamBadge.title}
      data-status={`seam-${seamMode}`}
      labelTestId="tenancy-seam-mode"
    />
  )

  return (
    <Box data-testid="tenancy-exclusions-page">
      {/* The badge stays the loudest fact. */}
      {seamChip && <Box sx={{ mb: 1 }}>{seamChip}</Box>}

      {/* Writer-failure banner, above the caption and filters: it qualifies everything below. `warning`,
          not `error` (the request succeeded; rows are incomplete by an unknown amount — as
          `pages/audit/SessionsTab.tsx` uses amber). Shown with rows too: counts are then floors. */}
      {writerFailing && (
        <Box sx={{ mb: 1 }}>
          <Banner severity="warning" testId="tenancy-exclusions-writer-failing">
            {/* Two paragraphs; `overflowWrap: 'anywhere'` because `writer_detail` ends in a spaceless
                exception string that would widen past 390px. */}
            <Box component="span" sx={{ display: 'block', overflowWrap: 'anywhere' }}>
              {writerDetail ??
                'The exclusion writer on the API process serving this page is failing, and it did not report why.'}
            </Box>
            <Box component="span" sx={{ display: 'block', mt: 0.5, fontWeight: 600 }}>
              Every count below is a floor, not a total, and an empty result is not evidence that the
              seam excluded nothing. This page cannot support the flip until the writer recovers.
            </Box>
          </Banner>
        </Box>
      )}

      {/* The window the absence of rows is a claim about: "nothing since `covering_since`", never "ever". */}
      {data !== null && (
        <Typography
          variant="caption"
          data-testid="tenancy-exclusions-window"
          sx={{ display: 'block', mb: 1, color: 'text.secondary' }}
        >
          {`Retained ${data.retention_days} day${data.retention_days === 1 ? '' : 's'}, covering ${formatZoned(data.covering_since, zone)} onward`}
          {/* A clause replacing the full stop — see `windowBoundByLogAge`. */}
          {windowBoundByLogAge ? ' — this log has only existed since then. ' : '. '}
          {data.latest_occurred_at !== null
            ? `Most recent exclusion ${formatZoned(data.latest_occurred_at, zone)}. `
            : 'Nothing recorded in that window. '}
          {`Times shown and filtered in ${zone}.`}
        </Typography>
      )}

      <FilterRow>
        <FilterSelect
          label="Class"
          ariaLabel="Filter by ORM class"
          value={mappedClass}
          onChange={next => applyFilter(() => setMappedClass(next))}
          allLabel="Any class"
          options={classOptions}
          minWidth={170}
        />
        <FilterSelect
          label="Org"
          ariaLabel="Filter by bound org"
          value={org}
          onChange={next => applyFilter(() => setOrg(next))}
          allLabel="Any org"
          options={orgOptions}
          minWidth={190}
        />
        <FilterSelect
          label="Scope"
          ariaLabel="Filter by scope kind"
          value={scopeKind}
          onChange={next => applyFilter(() => setScopeKind(next))}
          allLabel="Any scope"
          options={scopeOptions}
          minWidth={170}
        />
        <FilterSelect
          label="Mode"
          ariaLabel="Filter by seam mode"
          value={mode}
          onChange={next => applyFilter(() => setMode(next))}
          allLabel="Any mode"
          options={modeOptions}
          minWidth={160}
        />

        <DateTimeBoundFields {...rangeFields} />

        <ClearFiltersButton show={hasFilters} onClick={clearFilters} testId="tenancy-exclusions-clear-filters" />
      </FilterRow>

      {error && data !== null && (
        <StaleResultBanner
          error={error}
          onRetry={retry}
          testId="tenancy-exclusions-error"
          retryTestId="tenancy-exclusions-retry"
        />
      )}

      <Box sx={{ mt: 1 }}>
        {loading && data === null ? (
          <LoadingState label="Loading the exclusion log…" rows={SKELETON_ROWS} />
        ) : error && data === null ? (
          /* A load failure is never drawn as an empty log. */
          <LoadFailedState title="Exclusion log unavailable" error={error} onRetry={retry} testId="tenancy-exclusions-retry" />
        ) : total === 0 ? (
          /* `total_unfiltered` decides: "filters match nothing" vs "the seam excluded nothing". */
          totalUnfiltered > 0 ? (
            /* Distinct testid from the filter row's Clear (both on screen at once). */
            <NoMatchesState
              title="No exclusions match these filters."
              onClear={hasFilters ? clearFilters : undefined}
              testId="tenancy-exclusions-clear-filters-empty"
            />
          ) : (
            /* The bounded claim ("since `covering_since`"). The title stays literal (the table is empty); a
             * failing writer is the banner's to say. */
            <EmptyState title={`No exclusions since ${formatZoned(data?.covering_since, zone)}`} />
          )
        ) : (
          <>
            {/* `FULL_BLEED_SX` + `PHONE_CARD_SX` + `EXCLUSION_PHONE_CARD_SX` as an array, never spread:
                each carries a `[PHONE]` key and a spread would drop the earlier ones. */}
            <Box sx={[FULL_BLEED_SX, PHONE_CARD_SX, EXCLUSION_PHONE_CARD_SX]}>
              <DataTable
                ariaLabel="Tenancy exclusions — what the read seam would have excluded"
                testId="tenancy-exclusions-table"
              >
                {/* `nowrap` headings; `ui/DataTable` scrolls inside its own box. */}
                <TableHead sx={{ '& th': { whiteSpace: 'nowrap' } }}>
                  <TableRow>
                    {COLUMNS.map(column => (
                      <TableCell key={column.key} align={column.align}>
                        {column.label}
                      </TableCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {items.map(row => {
                    const scopeWord = humanize(row.scope_kind)
                    const modeWord = humanize(row.mode)
                    const unbound = row.bound_org === null
                    return (
                      <TableRow
                        key={row.id}
                        hover
                        data-testid="tenancy-exclusion-row"
                        data-scope-kind={row.scope_kind}
                        data-mapped-class={row.mapped_class}
                      >
                        {/* The zoned minute (the bucket identity), mono and one line. */}
                        <TableCell data-col="when" sx={[monoSx, { whiteSpace: 'nowrap' }]}>
                          <CellLabel>When</CellLabel>
                          {formatZoned(row.occurred_at, zone)}
                        </TableCell>

                        {/* Never ellipsised: the table scrolls instead. */}
                        <TableCell
                          data-col="class"
                          sx={[monoSx, { whiteSpace: 'nowrap', fontWeight: 600 }]}
                        >
                          <CellLabel>Class</CellLabel>
                          {row.mapped_class}
                        </TableCell>

                        <TableCell data-col="scope">
                          <CellLabel>Scope</CellLabel>
                          <StatusChip
                            label={scopeWord}
                            tone={SCOPE_TONE[row.scope_kind] ?? 'neutral'}
                            glyph={SCOPE_GLYPH[row.scope_kind] ?? '·'}
                            data-status={`scope-${row.scope_kind}`}
                          />
                        </TableCell>

                        {/* Org names wrap; `unbound` styled as an absence (italic, muted). */}
                        <TableCell
                          data-col="org"
                          sx={{
                            minWidth: 130,
                            maxWidth: 220,
                            whiteSpace: 'normal',
                            overflowWrap: 'anywhere',
                            ...(unbound ? { fontStyle: 'italic', color: 'text.secondary' } : null),
                          }}
                        >
                          <CellLabel>Org</CellLabel>
                          {row.bound_org_label}
                        </TableCell>

                        {/* Route (~60 chars) wraps, `anywhere` (no spaces) — its tail is the resource.
                            260px floor measured: the longest route settles into two lines. */}
                        <TableCell
                          data-col="route"
                          sx={[
                            monoSx,
                            {
                              minWidth: 260,
                              maxWidth: 460,
                              whiteSpace: 'normal',
                              overflowWrap: 'anywhere',
                            },
                          ]}
                        >
                          <CellLabel>Route or job</CellLabel>
                          {row.route_or_job}
                        </TableCell>

                        <TableCell data-col="mode">
                          <CellLabel>Mode</CellLabel>
                          <StatusChip
                            label={modeWord}
                            tone={MODE_TONE[row.mode] ?? 'neutral'}
                            glyph={MODE_GLYPH[row.mode] ?? '·'}
                            data-status={`mode-${row.mode}`}
                          />
                        </TableCell>

                        {/* Right-aligned `tabular-nums` so digits line up. */}
                        <TableCell
                          data-col="count"
                          align="right"
                          sx={[
                            monoSx,
                            {
                              whiteSpace: 'nowrap',
                              fontWeight: 600,
                              fontVariantNumeric: 'tabular-nums',
                              // The phone card has no column; lead with the label.
                              [PHONE]: { justifyContent: 'flex-start' },
                            },
                          ]}
                          title={`${row.excluded_count.toLocaleString()} row${row.excluded_count === 1 ? '' : 's'} would have been excluded in this minute`}
                        >
                          <CellLabel>Count</CellLabel>
                          {row.excluded_count.toLocaleString()}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </DataTable>
            </Box>

            <LogPagerFooter
              page={page}
              setPage={setPage}
              pages={pages}
              logName="exclusion log"
              testId="tenancy-exclusions-pagination"
              ceilingTestId="tenancy-exclusions-offset-ceiling"
              label={`Page ${page + 1} of ${totalPages} — ${total.toLocaleString()} row${total === 1 ? '' : 's'}`}
            />
          </>
        )}
      </Box>
    </Box>
  )
}
