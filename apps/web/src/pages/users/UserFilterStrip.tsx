import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { listOrgs, ORG_LOOKUP_PER } from '../../lib/orgs'
import { USER_STATUSES } from '../../lib/users'
import { AsyncLookup, type LookupOption } from '../../ui/AsyncLookup'
import FilterBar from '../../ui/FilterBar'
import { orgOption, useOrgsByIds } from '../../ui/OrgSelect'
import { FilterRow, FilterSelect } from '../../ui/FilterRow'
import { PEOPLE_FOLD_FILTERS } from '../../ui/listStyles'
import SearchBox from '../../ui/SearchBox'
import ToolbarMenu, { filterRadioPage, PAGE_BODY_SX } from '../../ui/ToolbarMenu'
import type { UserFilters } from './userFilters'

/* THE PEOPLE ROSTER'S FILTER STRIP: search, Status, and — for an operator alone — Org, every one a
 * query parameter the server applies to the page AND `total`. The layout contract is the Nodes strip's
 * (`pages/nodes/roster/RosterFilterStrips.tsx`): ONE `FilterRow` in `ui/FilterBar`, so the controls pack two
 * per line on a phone. Search is `ui/SearchBox` (debounced: a word is one request).
 * ORG IS A SERVER SEARCH (`ui/AsyncLookup` over `GET /api/orgs?q=`), not a select over the roster: an operator's
 * fleet has more orgs than a native list reads, and the lookup clears to "every org" where `OrgSelect` (a
 * create form's required field) has no unset state. It narrows WITHIN the global org scope (`lib/orgScope.ts`),
 * which the server applies first.
 * A NARROW ROSTER (a phone, or beside an open user) folds the operator's Status and Org behind one Filters button
 * (`ui/ToolbarMenu`), as the Nodes strip does (`pages/nodes/roster/RosterFilterStrips.tsx`): both are drawn and a
 * container query shows one, so opening a user never remounts the strip. A tenant's Search and Status share one
 * line at 390px already, so its strip never folds. */

/** Top clearance for a Directory panel's filter strip: the panel sits flush under the tab strip, so a
 *  Search box's floated label would be clipped against it (seen at 390px). One home: People and Groups both use it. */
export const FILTER_STRIP_SX = { pt: 1 } as const

const SEARCH_SX = { flex: '1 1 190px', minWidth: 190, m: 0 } as const
const ORG_SX = { flex: '1 1 180px', minWidth: 160, maxWidth: { sm: 260 } } as const

const STATUS_OPTIONS = USER_STATUSES.map(value => ({ value, label: value[0].toUpperCase() + value.slice(1) }))

async function searchOrgs(q: string, signal: AbortSignal): Promise<LookupOption[]> {
  return (await listOrgs({ q, per: ORG_LOOKUP_PER }, signal)).items.map(orgOption)
}

/** The picked org's row, for its label: a link or a reload carries only the id. One read, by `?ids=`. */
function usePickedOrg(id: string): LookupOption | null {
  const query = useOrgsByIds(id ? [id] : [])
  if (!id) return null
  const org = query.data?.find(o => o.id === id)
  /* Until it answers (or if it cannot), the id stands in: the filter is live either way, and the box must
     not read empty over a narrowed roster. */
  return org ? orgOption(org) : { key: id, label: query.isError ? id : '…' }
}

export function UserFilterStrip({ filters, onChange, operator }: {
  filters: UserFilters
  /** One field at a time, so a caller cannot reset three axes by forgetting to spread. */
  onChange: (patch: Partial<UserFilters>) => void
  /** The Org control is the operator's alone: a tenant's roster is its own org already. */
  operator: boolean
}): JSX.Element {
  return (
    <Box data-testid="user-filters" sx={FILTER_STRIP_SX}>
      <FilterBar>
        <FilterRow>
          <SearchBox
            value={filters.q}
            onChange={q => onChange({ q })}
            label="Search"
            placeholder="Name or email"
            ariaLabel="Search people"
            testId="user-search"
            sx={SEARCH_SX}
          />
          <Box data-testid="user-filter-inline" sx={{ display: 'contents', ...(operator && { [PEOPLE_FOLD_FILTERS]: { display: 'none' } }) }}>
            <FilterSelect
              label="Status"
              ariaLabel="Filter by status"
              value={filters.status}
              onChange={status => onChange({ status: status as UserFilters['status'] })}
              allLabel="All"
              options={STATUS_OPTIONS}
              minWidth={120}
            />
            {operator && <OrgFilter value={filters.org} onChange={org => onChange({ org })} />}
          </Box>
          {/* `alignSelf: stretch` lets the Filters trigger take the strip's field height (`ui/ToolbarMenu`). */}
          {operator && (
            <Box data-testid="user-filter-menu" sx={{ display: 'none', alignSelf: 'stretch', [PEOPLE_FOLD_FILTERS]: { display: 'flex' } }}>
              <UserFilterMenu filters={filters} onChange={onChange} />
            </Box>
          )}
        </FilterRow>
      </FilterBar>
    </Box>
  )
}

/** The folded Status and Org, one page each; the badge counts the two on (Search shows itself), Clear resets them. */
function UserFilterMenu({ filters, onChange }: Pick<Parameters<typeof UserFilterStrip>[0], 'filters' | 'onChange'>): JSX.Element {
  const org = usePickedOrg(filters.org)
  return (
    <ToolbarMenu
      name="Filter people"
      heading="Filters"
      hint="Filters"
      badgeCount={[filters.status, filters.org].filter(Boolean).length}
      items={[
        filterRadioPage('user-filter-status', 'status', 'Status', 'All', STATUS_OPTIONS, filters.status,
          status => onChange({ status: status as UserFilters['status'] })),
        {
          kind: 'page',
          id: 'org',
          label: 'Org',
          value: org?.label ?? 'All',
          render: () => (
            <Box sx={{ ...PAGE_BODY_SX, px: 1.5, py: 1.5, minWidth: 240 }}>
              <OrgFilter value={filters.org} onChange={org => onChange({ org })} idSuffix="-menu" />
            </Box>
          ),
        },
      ]}
      onClear={() => onChange({ status: '', org: '' })}
    />
  )
}

function OrgFilter({ value, onChange, idSuffix = '' }: {
  value: string
  onChange: (org: string) => void
  /** Tells the folded menu's copy from the inline one: both can be mounted at once. */
  idSuffix?: string
}): JSX.Element {
  const picked = usePickedOrg(value)
  return (
    <Box sx={ORG_SX}>
      <AsyncLookup
        id={`user-filter-org${idSuffix}`}
        testId={`user-filter-org${idSuffix}`}
        label="Org"
        placeholder="All"
        ariaLabel="Filter by org"
        value={picked}
        onChange={next => onChange(next?.key ?? '')}
        fetchOptions={searchOrgs}
        fetchOnFocus
        margin="none"
        noOptionsText="No org matches"
      />
    </Box>
  )
}
