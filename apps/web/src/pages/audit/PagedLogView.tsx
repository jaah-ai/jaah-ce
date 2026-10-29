import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import type { SxProps, Theme } from '@mui/material/styles'
import type { usePagedLogFilters } from '../../hooks/usePagedLogFilters'
import { DateTimeBoundFields } from '../../ui/DateTimeBoundFields'
import { FilterRow } from '../../ui/FilterRow'
import { ClearFiltersButton } from '../../ui/LogPagerFooter'
import { fieldTapTargetSx, PHONE_QUERY } from '../../ui/listStyles'
import SearchBox from '../../ui/SearchBox'
import ToolbarMenu, { PAGE_BODY_SX, type ToolbarMenuItem } from '../../ui/ToolbarMenu'
import { Q_MAX_LEN } from './queryLimits'

type PagedLogFilters = ReturnType<typeof usePagedLogFilters>

type RangeFields = PagedLogFilters['rangeFields']

/** The range row's value: which bounds are set, the dates themselves being on its page. */
function rangeValue({ from, to }: RangeFields): string {
  if (from && to) return 'Between'
  if (from) return 'After'
  if (to) return 'Before'
  return 'Any'
}

/** A tab's own filters as pages of the phone's Filters menu: how many are on, and what clears them AND the range
 *  (one write: two in a row would each start from the same stale view, the second undoing the first). */
export type PhoneFilterPages = { items: ToolbarMenuItem[]; count: number; onClear: () => void }

/* On a phone From and To took a full row each under the search: they move behind the Filters button, as the
   Nodes strip's do (`pages/nodes/roster/RosterFilterStrips.tsx::NodeFilterMenu`), after the tab's own pages. The
   badge counts the filters set; Clear clears exactly those. */
function PhoneFilterMenu({ range, pages }: { range: RangeFields; pages?: PhoneFilterPages }): JSX.Element {
  return (
    <ToolbarMenu
      name="Filter log"
      heading="Filters"
      hint="Filters"
      badgeCount={[range.from, range.to].filter(Boolean).length + (pages?.count ?? 0)}
      items={[...(pages?.items ?? []), {
        kind: 'page',
        id: 'range',
        label: 'Time range',
        value: rangeValue(range),
        render: () => (
          <Box sx={{ ...PAGE_BODY_SX, gap: 1.5, px: 1.5, py: 1.5 }}>
            <DateTimeBoundFields {...range} />
          </Box>
        ),
      }]}
      onClear={pages ? pages.onClear : () => {
        range.onFrom('')
        range.onTo('')
      }}
    />
  )
}

/* The paged audit-log frame: search, the tab's `filters`, date range, Clear; the zone note; the tab's body.
   `search` is the shared debounced box's fields, or a tab's own control (Activity's chip box). A tab with
   `phoneFilters` draws them as Filters pages on a phone instead of `filters`, so the list starts a row higher.
   `foldQuery` (a container query on an ancestor of the strip) folds the same way above the phone wherever it
   matches: beside an open record the list is a phone's width in a desktop window (Activity). */
export default function PagedLogView({
  testIdPrefix, search, filters, phoneFilters, foldQuery, range, zone, hasFilters, onClear, sx, children,
}: {
  /** `<prefix>-clear-filters` and `<prefix>-zone-note`. */
  testIdPrefix: string
  search: { fields: PagedLogFilters['searchFields']; ariaLabel: string; placeholder: string } | { control: ReactNode }
  filters: ReactNode
  phoneFilters?: PhoneFilterPages
  foldQuery?: string
  range: RangeFields
  zone: string
  hasFilters: boolean
  onClear: () => void
  sx?: SxProps<Theme>
  children: ReactNode
}): JSX.Element {
  const phone = useMediaQuery(PHONE_QUERY)
  return (
    <Box sx={sx}>
      <FilterRow>
        {'control' in search ? search.control : (
          <SearchBox
            {...search.fields}
            label="Search"
            ariaLabel={search.ariaLabel}
            maxLength={Q_MAX_LEN}
            placeholder={search.placeholder}
            sx={[{ m: 0, minWidth: 170, flex: '1 1 170px' }, fieldTapTargetSx]}
          />
        )}
        {phone ? (
          <>
            {!phoneFilters && filters}
            <PhoneFilterMenu range={range} pages={phoneFilters} />
          </>
        ) : foldQuery ? (
          <>
            {/* Both drawn, one shown: the swap is CSS on the container's width, so a pane opening never remounts a
                half-typed date. `contents` keeps the unfolded fields items of the strip itself. */}
            <Box sx={{ display: 'contents', [foldQuery]: { display: 'none' } }}>
              {filters}
              <DateTimeBoundFields {...range} />
            </Box>
            {/* `alignSelf: stretch` lets the Filters trigger take the strip's field height (`ui/ToolbarMenu`). */}
            <Box sx={{ display: 'none', alignSelf: 'stretch', [foldQuery]: { display: 'flex' } }}>
              <PhoneFilterMenu range={range} pages={phoneFilters} />
            </Box>
          </>
        ) : (
          <>
            {filters}
            <DateTimeBoundFields {...range} />
          </>
        )}
        <ClearFiltersButton show={hasFilters} onClick={onClear} testId={`${testIdPrefix}-clear-filters`} />
      </FilterRow>
      {/* The zone is named: From/To take a zoneless wall clock and the rows print an instant. */}
      <Typography
        variant="caption"
        data-testid={`${testIdPrefix}-zone-note`}
        sx={{ display: 'block', mt: 0.75, mb: 1, color: 'text.secondary' }}
      >
        Times shown and filtered in {zone}.
      </Typography>
      {children}
    </Box>
  )
}
