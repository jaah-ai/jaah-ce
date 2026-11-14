import { useCallback, useMemo, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { get, withQuery } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { usePagedList } from '../../hooks/usePagedList'
import { usePhone } from '../../hooks/usePhone'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { absoluteTime, compactAge, NO_VALUE } from '../../lib/relativeTime'
import { toggled } from '../../lib/sets'
import { countOf } from '../../lib/humanize'
import Banner from '../../ui/Banner'
import { CodeBlock } from '../../ui/CodeBlock'
import EmptyState from '../../ui/EmptyState'
import { FilterRow, withUnmatched } from '../../ui/FilterRow'
import FilterBar from '../../ui/FilterBar'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import NoMatchesState from '../../ui/NoMatchesState'
import PagedList from '../../ui/PagedList'
import { SelectFilters } from '../../ui/SelectFilters'
import FeedList from '../../ui/feed/FeedList'
import { AGE_SLOT } from '../../ui/feed/geometry'
import { grid, SIZE } from '../../theme'
import type { FeedRowSpec } from '../../ui/feed/types'
import type { Presentation } from '../../ui/statusTone'
import type { ClientEvent, ClientEventKind, ClientEventsPage, FlaggedClients } from '../../types'

/* AUDIT > CLIENT ERRORS — what flagged console sessions reported: render errors, chunk-load failures, stalls,
 * uncaught errors, and slow page loads, navigations and reads (`lib/loadTiming.ts`), newest first (`GET /api/client-events`). Only flagged orgs (Customers > org > Overrides) and
 * users (People > user > System) report; the "Reporting" strip names them, each linked to its switch.
 * THE ROW: the message as title, the kind as state mark, org/user, route and age as the cluster. It opens in place
 * (`FeedRowSpec.detail`) to the event's facts and its `detail` JSON: there is no per-event address to open.
 * Shaped as `./ScheduleTab.tsx` (URL filters, keyset "Load more"). OPERATOR-ONLY at the route (`App.tsx`) and the
 * strip (`AuditPage.tsx`); the boundary is the API's `superadmin_only`. */

const KIND: Record<ClientEventKind, Presentation> = {
  render_error: { label: 'Render error', tone: 'error', glyph: '✕' },
  chunk_error: { label: 'Chunk load', tone: 'error', glyph: '↓' },
  stall: { label: 'Stall', tone: 'warning', glyph: '⋯' },
  uncaught_error: { label: 'Uncaught error', tone: 'error', glyph: '!' },
  page_load: { label: 'Slow page load', tone: 'warning', glyph: '◔' },
  navigation: { label: 'Slow navigation', tone: 'warning', glyph: '→' },
  slow_read: { label: 'Slow read', tone: 'warning', glyph: '~' },
  slow_write: { label: 'Slow write', tone: 'warning', glyph: '≈' },
}
const KIND_OPTIONS = (Object.keys(KIND) as ClientEventKind[]).map(value => ({ value, label: KIND[value].label }))

const PAGE_SIZE = 100

type Filters = { org: string; user: string; kind: string }

function listClientEvents(filters: Filters, before: number | null) {
  return get<ClientEventsPage>(withQuery('/api/client-events', {
    limit: PAGE_SIZE, org_id: filters.org, user_id: filters.user, kind: filters.kind, before_id: before,
  }))
}

const getFlagged = () => get<FlaggedClients>('/api/client-events/flagged')

const orgSwitchPath = (orgId: string) => `/customers/${encodeURIComponent(orgId)}?tab=overrides`
const userSwitchPath = (userId: string) => `/users/${encodeURIComponent(userId)}?tab=system`

function EventDetail({ event }: { event: ClientEvent }) {
  return (
    <Stack spacing={1} sx={{ py: 1 }} data-testid="client-event-detail">
      <KeyValueList>
        <KeyValue label="Message">{event.message || NO_VALUE}</KeyValue>
        <KeyValue label="Route" mono>{event.route || NO_VALUE}</KeyValue>
        <KeyValue label="Build">{event.build_version || NO_VALUE}</KeyValue>
        <KeyValue label="Browser">{event.user_agent || NO_VALUE}</KeyValue>
        <KeyValue label="IP" mono>{event.client_ip || NO_VALUE}</KeyValue>
        <KeyValue label="Occurred">{event.occurred_at ? absoluteTime(event.occurred_at) : NO_VALUE}</KeyValue>
        <KeyValue label="Received">{absoluteTime(event.created_at)}</KeyValue>
      </KeyValueList>
      {event.detail != null && (
        <CodeBlock maxHeight={grid(60)} testId="client-event-json">{JSON.stringify(event.detail, null, 2)}</CodeBlock>
      )}
    </Stack>
  )
}

export default function ClientErrorsTab({ enabled = true }: {
  /* Latched by `AuditPage`, false→true only, so returning does not re-read. */
  enabled?: boolean
}) {
  const phone = usePhone()
  const [searchParams, setFilters] = useUrlFilters()
  const org = searchParams.get('org') ?? ''
  const user = searchParams.get('user') ?? ''
  const kind = searchParams.get('kind') ?? ''

  const flagged = useApiRead(['client-events-flagged'], getFlagged, 'Failed to load who reports.', { enabled })

  const fetchPage = useCallback(
    (before: number | null) => listClientEvents({ org, user, kind }, before),
    [org, user, kind],
  )
  const paging = usePagedList({
    fetchPage,
    rowsOf: page => page.items,
    nextOf: page => page.next_before_id,
    errorFallback: 'Failed to load client errors.',
    enabled,
  })
  const { rows: events, error } = paging

  /* Options: who reports now, plus anyone on the loaded rows (a flag turned off keeps its history). */
  const orgOptions = useMemo(() => {
    const seen = new Map<string, string>()
    for (const o of flagged.data?.orgs ?? []) seen.set(o.id, o.name)
    for (const e of events) if (e.org_id && !seen.has(e.org_id)) seen.set(e.org_id, e.org_name ?? e.org_id)
    return withUnmatched([...seen].map(([value, label]) => ({ value, label })), org)
  }, [flagged.data, events, org])
  const userOptions = useMemo(() => {
    const seen = new Map<string, string>()
    for (const u of flagged.data?.users ?? []) seen.set(u.id, u.email)
    for (const e of events) if (e.user_id && !seen.has(e.user_id)) seen.set(e.user_id, e.user_email ?? e.user_id)
    return withUnmatched([...seen].map(([value, label]) => ({ value, label })), user)
  }, [flagged.data, events, user])

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())

  const rowOf = (event: ClientEvent): FeedRowSpec => {
    const key = String(event.id)
    const isOpen = expanded.has(key)
    const kind = KIND[event.kind]
    const label = kind?.label ?? event.kind
    const title = event.message || label
    const who = [event.org_name, event.user_email].filter(Boolean).join(' · ') || NO_VALUE
    return {
      key,
      indicator: {
        tone: kind?.tone ?? 'neutral',
        glyph: kind?.glyph ?? '?',
        label,
        title: label,
        data: { kind: event.kind },
      },
      title,
      titleText: title,
      href: null,
      disclosure: { expanded: isOpen, onToggle: () => setExpanded(prev => toggled(prev, key)), label: 'details' },
      detail: isOpen ? <EventDetail event={event} /> : undefined,
      data: { 'event-id': key },
      slots: [
        { key: 'who', width: grid(33), gate: 'meta', node: who, title: who, testId: 'client-event-who' },
        { key: 'route', width: grid(23), gate: 'id', mono: true, node: event.route || NO_VALUE, title: event.route ?? undefined },
        {
          key: 'age',
          width: AGE_SLOT,
          align: 'right',
          dim: true,
          node: compactAge(event.created_at),
          title: absoluteTime(event.created_at),
        },
      ],
    }
  }

  /* An unopened panel renders NOTHING: it stays in the DOM, and a never-resolving skeleton fails
   * `e2e/app-shell-scroll.spec.ts`'s no-surviving-`loading-state` sweep (as `PersonasTab` does). */
  if (!enabled) return null

  const filtered = Boolean(org || user || kind)
  const reporting = flagged.data

  return (
    <Stack spacing={1} sx={{ pt: 1 }}>
      {error && <Banner severity="error">{`${error} — showing the last reading.`}</Banner>}
      {flagged.error && <Banner severity="warning">{flagged.error}</Banner>}

      {/* WHO REPORTS: every flagged org and user, each linked to where its switch is. */}
      {reporting && (
        <Typography variant="body2" component="div" sx={{ color: 'text.secondary' }} data-testid="client-errors-reporting">
          {'Reporting: '}
          {reporting.orgs.length === 0 && reporting.users.length === 0 && 'nobody.'}
          {[
            ...reporting.orgs.map(o => (
              <Link key={`o-${o.id}`} component={RouterLink} to={orgSwitchPath(o.id)} underline="hover" data-testid="client-errors-flagged-org">
                {o.name}
              </Link>
            )),
            ...reporting.users.map(u => (
              <Link key={`u-${u.id}`} component={RouterLink} to={userSwitchPath(u.id)} underline="hover" data-testid="client-errors-flagged-user">
                {u.org_name ? `${u.email} (${u.org_name})` : u.email}
              </Link>
            )),
          ].flatMap((link, i) => (i === 0 ? [link] : [', ', link]))}
        </Typography>
      )}

      <FilterBar>
        <FilterRow>
          <SelectFilters
            narrowings={[
              { key: 'org', label: 'Org', ariaLabel: 'Filter client errors by org', allLabel: 'All orgs', options: orgOptions, testId: 'client-errors-filter-org', minWidth: SIZE.fieldMd },
              { key: 'user', label: 'User', ariaLabel: 'Filter client errors by user', allLabel: 'All users', options: userOptions, testId: 'client-errors-filter-user', minWidth: SIZE.fieldMd },
              { key: 'kind', label: 'Kind', ariaLabel: 'Filter client errors by kind', allLabel: 'Any kind', options: KIND_OPTIONS, testId: 'client-errors-filter-kind', minWidth: SIZE.fieldMd },
            ]}
            values={{ org, user, kind }}
            onChange={(key, value) => setFilters({ [key]: value })}
            onClear={keys => setFilters(Object.fromEntries(keys.map(k => [k, ''])))}
            phone={phone}
            menuName="Filter client errors"
          />
        </FilterRow>
      </FilterBar>

      <PagedList
        paging={paging}
        retryTestId="client-errors-retry"
        empty={filtered
          ? <NoMatchesState title="No client errors match these filters" />
          : <EmptyState title="No client errors reported" scoped />}
        loadMoreLabel="Load older"
        loadMoreTestId="client-errors-load-more"
        caption={paging.hasMore
          ? `${events.length} most recent.`
          : `${countOf(events.length, 'event')}.`}
      >
        <FeedList
          ariaLabel="Client errors"
          testId="client-errors-table"
          sections={[{ key: 'events', rows: events.map(rowOf) }]}
        />
      </PagedList>
    </Stack>
  )
}
