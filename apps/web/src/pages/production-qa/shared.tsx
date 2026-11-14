import { useRef, type JSX, type ReactNode } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { retryUnlessNotFound } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { usePhone } from '../../hooks/usePhone'
import { useProjects } from '../../hooks/useRosters'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { FilterRow } from '../../ui/FilterRow'
import FilterBar from '../../ui/FilterBar'
import LoadFailedState from '../../ui/LoadFailedState'
import NoMatchesState from '../../ui/NoMatchesState'
import { SelectFilters, type SelectNarrowing } from '../../ui/SelectFilters'
import StatusChip, { type Tone } from '../../ui/StatusChip'
import { TONE_COLOR } from '../../ui/statusTone'
import ListStates from '../../ui/feed/ListStates'
import type { FeedColumn, FeedRowSpec } from '../../ui/feed/types'
import { monoSx } from '../../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, SIZE } from '../../theme'
import { NOT_SENT, PRODUCTION_QA_PAGE, PRODUCTION_QA_POLL_MS, type LedgerEdge, type Page } from '../../lib/productionQa'

/* What the Production QA pages share (`IncidentsPage.tsx`, and the incident and deploy run detail pages): a ledger fact's standing, a coloured phrase, a value a vendor may not send, the project filter, the list's
 * loading/empty/failed states, the list page itself (`ProductionQaList`), and the detail pages' polled read
 * (`useDetailRead`; the status before its record is `ui/detail/DetailStatus`'s `detailStatus`). */

/** The age column under a header: wide enough for its header word ("Done", "Seen"), which `AGE_SLOT`'s 28px clips. */
export const AGE_COLUMN = 40

/** A sha, release or URL: the theme's mono face, breaking anywhere rather than overflowing. */
export const MONO_SX = [monoSx, { overflowWrap: 'anywhere' as const }]

/** One record (`GET /api/<kind>s/{id}`), polled at `PRODUCTION_QA_POLL_MS` while visible; a 404 is not retried. */
export function useDetailRead<T>(kind: 'incident' | 'deployment', id: string, load: (id: string) => Promise<T>): UseQueryResult<T, unknown> {
  return useQuery({
    queryKey: [kind, id],
    queryFn: () => load(id),
    refetchInterval: PRODUCTION_QA_POLL_MS,
    retry: retryUnlessNotFound,
    meta: { persist: false },
  })
}

/** A fact as its word, coloured by its tone, for a phrase inside a sentence (a failing job's path, a breached delta):
 *  never colour alone (AC-28). A status token in a row or field is the console's pill, `ui/StatusChip`. */
export function ToneWord({ label, tone, status }: { label: string; tone: Tone; status?: string }): JSX.Element {
  return (
    <Box component="span" data-status={status} sx={{ color: TONE_COLOR[tone], fontWeight: FONT_WEIGHT.medium }}>
      {label}
    </Box>
  )
}

/** A ledger fact's standing: Revoked, Applied (in the outcome ledger), or Pending (the applier has not run on it). */
export function EdgeStatus({ edge, testId }: { edge: Pick<LedgerEdge, 'revoked_at' | 'applied_at'>; testId?: string }): JSX.Element {
  const [label, tone, status]: [string, Tone, string] = edge.revoked_at
    ? ['Revoked', 'neutral', 'revoked']
    : edge.applied_at ? ['Applied', 'success', 'applied'] : ['Pending', 'info', 'pending']
  return <StatusChip label={label} tone={tone} data-status={status} testId={testId} />
}

/** A field a vendor may leave out: its value, or "Not sent" (muted) — never blank, never a made-up value. */
export function Sent({ value, children }: { value: unknown; children?: ReactNode }): JSX.Element {
  if (value === null || value === undefined || value === '') {
    return <Box component="span" data-sent="false" sx={{ color: 'text.secondary', fontStyle: 'italic' }}>{NOT_SENT}</Box>
  }
  return <Box component="span" data-sent="true">{children ?? String(value)}</Box>
}

/** Values seen across reads (a free-text column such as environment), so a filter keeps offering what it narrowed
 *  away. */
function useSeen(values: readonly string[]): string[] {
  const seen = useRef(new Set<string>())
  values.forEach(v => seen.current.add(v))
  return [...seen.current].sort()
}

/** One URL filter after the project's: its param (also its key in the read's filters) and its dropdown; no
 *  `options` = the values `seen` has read, plus an unknown selected one. */
export type ListFilter<F> = {
  param: Exclude<keyof F & string, 'project_id'>
  label: string
  allLabel: string
  minWidth: number
  options?: { value: string; label: string }[]
}

/** The list page (ui-style-guide § Lists): URL filters (project first), the polled read, its states, the feed. */
export function ProductionQaList<F, T>({
  noun, fetch, filters: spec, seen, row, title, slots, above,
}: {
  noun: string
  fetch: (filters: F) => Promise<Page<T>>
  filters: ListFilter<F>[]
  seen?: (item: T) => string
  row: (item: T) => FeedRowSpec
  title: string
  slots: FeedColumn[]
  /** Drawn above the filters. */
  above?: ReactNode
}): JSX.Element {
  const name = noun.toLowerCase()
  const [params, setFilters] = useUrlFilters()
  const values: Record<string, string> = { project_id: params.get('project') ?? '' }
  spec.forEach(f => { values[f.param] = params.get(f.param) ?? '' })
  const read = useApiRead([name, values], () => fetch(values as F), `Could not load ${name}`, { refetchInterval: PRODUCTION_QA_POLL_MS })
  const items = read.data?.items ?? null
  const seenValues = useSeen(seen ? (items ?? []).map(seen) : [])
  const projects = useProjects({ staleTime: 60_000 })
  const phone = usePhone()
  // Project first, over the org's projects the viewer may see; an unknown selected value stays offered.
  const narrowings: SelectNarrowing<string>[] = [
    { key: 'project', label: 'Project', ariaLabel: 'Filter by project', allLabel: 'All projects', minWidth: SIZE.fieldMd, testId: `${name}-filter-project`,
      options: (projects.data ?? []).map(p => ({ value: String(p.id), label: p.name })).sort((a, b) => a.label.localeCompare(b.label)) },
    ...spec.map(({ param, options, ...f }) => ({ ...f, key: param, ariaLabel: `Filter by ${f.label.toLowerCase()}`, testId: `${name}-filter-${param}`,
      options: options ?? seenValues.map(v => ({ value: v, label: v })) })),
  ]
  const clear = () => setFilters(Object.fromEntries(['project', ...spec.map(f => f.param)].map(p => [p, ''])))
  return (
    <Box data-testid={`${name}-page`} sx={{ minWidth: 0 }}>
      {above}
      <FilterBar>
        <FilterRow>
          <SelectFilters narrowings={narrowings} values={{ ...values, project: values.project_id }} onChange={(key, next) => setFilters({ [key]: next })}
            onClear={clear} phone={phone} menuName={`Filter ${name}`} />
        </FilterRow>
      </FilterBar>
      <ListStates
        loading={items === null && !read.error}
        skeleton={{ rows: 4 }}
        failed={items === null && <LoadFailedState title={`${noun} unavailable`} error={read.error} onRetry={() => void read.reload()} />}
        empty={items?.length === 0 && !read.loading}
        noMatch={Object.values(values).some(Boolean) && <NoMatchesState onClear={clear} testId={`${name}-clear-filters`} />}
        emptyTitle={`No ${name} yet`}
        feed={{
          ariaLabel: noun,
          testId: `${name}-list`,
          sections: [{ key: name, rows: (items ?? []).map(row) }],
          columns: { title: { key: 'title', label: title }, slots },
        }}
      >
        {(items?.length ?? 0) >= PRODUCTION_QA_PAGE && (
          <Box sx={{ color: 'text.secondary', fontSize: FONT_SIZE.md, pt: 1 }}>
            {`Newest ${PRODUCTION_QA_PAGE} — narrow the filters to see older ones`}
          </Box>
        )}
      </ListStates>
    </Box>
  )
}
