import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import useMediaQuery from '@mui/material/useMediaQuery'
import FilterBar from '../../../ui/FilterBar'
import { FilterRow, FilterSelect, withUnmatched } from '../../../ui/FilterRow'
import { PHONE_QUERY } from '../../../ui/listStyles'
import SearchBox from '../../../ui/SearchBox'
import ToolbarMenu, { filterRadioPage, PAGE_CAPTION_SX } from '../../../ui/ToolbarMenu'
import { HOST_STATE } from '../hosts/hostFacts'
import { HOST_STATES, type HostFilters } from '../hosts/api'
import { NODE_STATUS_LABEL } from '../../../lib/nodeStatus'
import { NODEVAULT_CAPABILITY_LABEL } from '../../../lib/nodeVaultCapability'
import { NODE_ENABLED_STATES, NODE_NODEVAULT_STATES, NODE_STATUSES, type NodeFilters } from './api'

/* THE TWO ROSTERS' FILTER STRIPS: search plus narrowings, all SERVER-side — each control is a
 * query parameter the server applies to the page AND `total`; nothing here filters. The layout contract is
 * the Credentials strip's (`pages/credentials/CredentialFilterStrip.tsx`): `ui/FilterBar` gets ONE child, a
 * `FilterRow`, so the controls pack two per line on a phone rather than one per line. Search is
 * `ui/SearchBox` (debounced: a word is one request), never `FilterSearch` (the client-side one).
 * No Org picker (one decision, one control): the operator's org narrowing is the global org scope
 * (`lib/orgScope.ts`), which the server applies to both reads.
 * ON A PHONE each strip is Search and ONE sliders button (`ui/ToolbarMenu`, the console's list-settings menu)
 * holding its narrowings (Nodes: Status, Enabled, NodeVault, Pool; Hosts: State): selects beside Search took two lines, or
 * a 56px one, above the first row. The page's verb (`action`, Add node) ends that same line, so it costs no line
 * of its own above the strip; from `sm` up it stays in the tab strip, or ends the filter line where there is none. */

const SEARCH_SX = { flex: '1 1 190px', minWidth: 190, m: 0 } as const
// A phone's line holds Search, Filters and Add node: Search gives way first, so the line stays one at 360px.
const PHONE_SEARCH_SX = { flex: '1 1 120px', minWidth: 120, m: 0 } as const

const STATUS_OPTIONS = NODE_STATUSES.map(value => ({ value, label: NODE_STATUS_LABEL[value] }))
const ENABLED_OPTIONS = NODE_ENABLED_STATES.map(value => ({ value, label: value[0].toUpperCase() + value.slice(1) }))
const NODEVAULT_OPTIONS = NODE_NODEVAULT_STATES.map(value => ({ value, label: NODEVAULT_CAPABILITY_LABEL[value] }))
const HOST_STATE_OPTIONS = HOST_STATES.map(value => ({ value, label: HOST_STATE[value].label }))

/** A pool the strip may offer: the node group's id (what `?pool=` carries) and its name. */
export type PoolOption = { id: string; name: string }

export function NodeFilterStrip({ filters, onChange, pools, poolsError, action }: {
  filters: NodeFilters
  /** One field at a time, so a caller cannot reset three axes by forgetting to spread. */
  onChange: (patch: Partial<NodeFilters>) => void
  /** The pools this viewer may narrow by; `null` withholds the picker (a reader the groups route refuses, or
   *  an org with no pool — a picker over nothing would only narrow to what is already shown). */
  pools: PoolOption[] | null
  /** That read's failure, in the server's words, rendered on the Pool control. */
  poolsError?: string
  /** The page's verb, drawn at the end of the line (a phone's, or one with no tab strip above it). */
  action?: ReactNode
}): JSX.Element {
  const phone = useMediaQuery(PHONE_QUERY)
  const search = (
    <SearchBox
      value={filters.q}
      onChange={q => onChange({ q })}
      label="Search"
      // The phone's field is ~170px of text: the long form was cut mid-word ("identity k").
      placeholder={phone ? 'Name, tag or key' : 'Name, tag or identity key'}
      ariaLabel="Search nodes"
      testId="node-search"
      sx={phone ? PHONE_SEARCH_SX : SEARCH_SX}
    />
  )
  if (phone) {
    return (
      <Box data-testid="node-filters">
        <FilterBar>
          <FilterRow>
            {search}
            <NodeFilterMenu filters={filters} onChange={onChange} pools={pools} poolsError={poolsError} />
            {action}
          </FilterRow>
        </FilterBar>
      </Box>
    )
  }
  return (
    <Box data-testid="node-filters">
      <FilterBar>
        <FilterRow>
          {search}
          <FilterSelect
            label="Status"
            ariaLabel="Filter by status"
            value={filters.status}
            onChange={status => onChange({ status })}
            allLabel="Any status"
            options={STATUS_OPTIONS}
            minWidth={130}
          />
          <FilterSelect
            label="Enabled"
            ariaLabel="Filter by enabled state"
            value={filters.enabled}
            onChange={enabled => onChange({ enabled })}
            allLabel="Any"
            options={ENABLED_OPTIONS}
            minWidth={130}
          />
          <FilterSelect
            label="NodeVault"
            ariaLabel="Filter by NodeVault capability"
            value={filters.nodevault}
            onChange={nodevault => onChange({ nodevault })}
            allLabel="Any"
            options={NODEVAULT_OPTIONS}
            minWidth={140}
          />
          {(pools !== null || poolsError) && (
            <FilterSelect
              label="Pool"
              ariaLabel="Filter by pool"
              value={filters.pool}
              onChange={pool => onChange({ pool })}
              allLabel="All pools"
              /* A `?pool=` the roster no longer holds stays visible as itself (`withUnmatched`'s rule). */
              options={withUnmatched((pools ?? []).map(p => ({ value: p.id, label: p.name })), filters.pool)}
              maxWidth={220}
              disabled={Boolean(poolsError)}
              errorText={poolsError || undefined}
            />
          )}
          {action}
        </FilterRow>
      </FilterBar>
    </Box>
  )
}

type FilterOption = { value: string; label: string }

/** The phone's Status / Enabled / NodeVault / Pool, one radio page each; the badge counts the narrowings on (Search shows
 *  itself), and Clear resets exactly those. */
function NodeFilterMenu({ filters, onChange, pools, poolsError }: Parameters<typeof NodeFilterStrip>[0]): JSX.Element {
  const page = (id: keyof NodeFilters, label: string, allLabel: string, options: FilterOption[]) =>
    filterRadioPage(`node-filter-${id}`, id, label, allLabel, options, filters[id], value => onChange({ [id]: value }))
  const items = [
    page('status', 'Status', 'Any status', STATUS_OPTIONS),
    page('enabled', 'Enabled', 'Any', ENABLED_OPTIONS),
    page('nodevault', 'NodeVault', 'Any', NODEVAULT_OPTIONS),
    // A failed pools read keeps its row, its page saying why there is nothing to pick.
    ...(poolsError
      ? [{
          kind: 'page' as const,
          id: 'pool',
          label: 'Pool',
          value: 'Unavailable',
          render: () => <Box data-testid="node-filter-pool-error" sx={{ ...PAGE_CAPTION_SX, pt: 1.5, color: 'error.main' }}>{poolsError}</Box>,
        }]
      : pools !== null
        ? [page('pool', 'Pool', 'All pools', pools.map(p => ({ value: p.id, label: p.name })))]
        : []),
  ]
  return (
    <ToolbarMenu
      name="Filter nodes"
      heading="Filters"
      hint="Filters"
      badgeCount={[filters.status, filters.enabled, filters.nodevault, filters.pool].filter(Boolean).length}
      items={items}
      onClear={() => onChange({ status: '', enabled: '', nodevault: '', pool: '' })}
    />
  )
}

export function HostFilterStrip({ filters, onChange, action }: {
  filters: HostFilters
  onChange: (patch: Partial<HostFilters>) => void
  /** The page's verb, drawn at the end of the phone's line. */
  action?: ReactNode
}): JSX.Element {
  const phone = useMediaQuery(PHONE_QUERY)
  return (
    <Box data-testid="host-filters">
      <FilterBar>
        <FilterRow>
          <SearchBox
            value={filters.q}
            onChange={q => onChange({ q })}
            label="Search"
            placeholder="Host name"
            ariaLabel="Search hosts"
            testId="host-search"
            sx={phone ? PHONE_SEARCH_SX : SEARCH_SX}
          />
          {phone ? (
            <>
              <ToolbarMenu
                name="Filter hosts"
                heading="Filters"
                hint="Filters"
                badgeCount={filters.state ? 1 : 0}
                items={[filterRadioPage('host-filter-state', 'state', 'State', 'Any state', HOST_STATE_OPTIONS, filters.state, state => onChange({ state }))]}
                onClear={() => onChange({ state: '' })}
              />
              {action}
            </>
          ) : (
            <FilterSelect
              label="State"
              ariaLabel="Filter by state"
              value={filters.state}
              onChange={state => onChange({ state })}
              allLabel="Any state"
              options={HOST_STATE_OPTIONS}
              minWidth={140}
            />
          )}
        </FilterRow>
      </FilterBar>
    </Box>
  )
}
