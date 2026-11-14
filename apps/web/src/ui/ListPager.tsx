import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useCallback, useId, type JSX, type SetStateAction } from 'react'
import { PER_OPTIONS } from '../hooks/useListPage'
import { groupedCount } from '../lib/humanize'
import { usePageClamp } from '../hooks/usePageClamp'
import { PHONE, ELLIPSIS_SX, tapTargetSx } from './listStyles'
import { FONT_SIZE } from '../theme'
import SelectField from './SelectField'

/* THE FOOTER OF A `hooks/useListPage` LIST: where the reader is ("1–50 of 1,234"), the console's one
 * "Previous / Page X of N / Next", and how many rows a page holds. Each part shows only when it can say something: nothing at all before
 * the first total (no "of 0" posing as a count) or on an empty set (the list's own empty state speaks); no Prev/
 * Next on a single page; no size choice when every size shows the whole set (≤ 25) or the page size is fixed (no
 * `onPer`). A footer with only its readout
 * left (one page, no size choice) is not drawn at any width: "1–1 of 1" under one row repeats what it shows.
 * THREE COLUMNS ON A DESKTOP — readout left, pager centred on the list, size right, the outer two `1fr` so the
 * pager stays centred whatever the readout's width. TWO ROWS AT `PHONE`: readout and size share the first line,
 * the pager takes the second whole — one line of all three is ~425px against a 390px phone's 358px.
 * A KEYSET list (a cursor has no "page 4 of N") is `ui/PagedList`'s "Load more" instead. */

const FOOTER_SX = {
  display: 'grid',
  gridTemplateColumns: '1fr auto 1fr',
  gridTemplateAreas: '"readout pager per"',
  alignItems: 'center',
  columnGap: 1,
  mt: 1,
  minWidth: 0,
  [PHONE]: {
    gridTemplateColumns: '1fr auto',
    gridTemplateAreas: '"readout per" "pager pager"',
    rowGap: 0,
  },
} as const

/* Tabular digits: "1–50" → "51–100" must not jitter the line as the reader pages. */
const READOUT_SX = {
  gridArea: 'readout',
  color: 'text.secondary',
  fontVariantNumeric: 'tabular-nums',
  ...ELLIPSIS_SX,
} as const

const PAGER_SX = {
  gridArea: 'pager', minWidth: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1, flexWrap: 'wrap',
} as const

const PER_SX = {
  gridArea: 'per',
  justifySelf: 'end',
  display: 'flex',
  alignItems: 'center',
  gap: 0.5,
  color: 'text.secondary',
} as const

/* The console's dense select sized to its digits, in the footer's small type; `SelectField` keeps the 44px floor on
   the phone. */
const SELECT_SX = { '& .MuiInputBase-root': { fontSize: FONT_SIZE.md, fontVariantNumeric: 'tabular-nums' } } as const

export function ListPager({
  page,
  per,
  total,
  onPage,
  onPer,
  testId,
  aside,
  noun,
  nextDisabled = false,
}: {
  page: number
  per: number
  /** The filtered set's size; null while the first page is in flight. */
  total: number | null
  onPage: (page: number) => void
  /** Absent: a fixed page size, no size choice. */
  onPer?: (per: number) => void
  testId?: string
  /** Rows drawn beside the set but not in its `total` (e.g. "1 preparing"), said after the count. */
  aside?: string
  /** What is paged ("sessions"): names the buttons "Next page of sessions", unambiguous out of context. */
  noun?: string
  /** Next off before the last page: the endpoint's reach ends (an offset ceiling) where the set does not. */
  nextDisabled?: boolean
}): JSX.Element | null {
  const selectId = useId()
  /* `usePageClamp`'s snap-back, fed a page setter; before the first total it is fed a set that always reaches
     `page`, so nothing clamps on a guess. */
  const setPage = useCallback(
    (next: SetStateAction<number>) => onPage(typeof next === 'function' ? next(page) : next),
    [onPage, page],
  )
  const totalPages = usePageClamp(page, setPage, total ?? page * per, per)

  if (total === null || (total === 0 && !aside)) return null
  // Nothing in the set but rows beside it (an org's first node still being added): the aside alone.
  const readout = total === 0 ? aside : null
  const first = Math.min((page - 1) * per + 1, total)
  const last = Math.min(page * per, total)
  // Dropped only when it says nothing a row does not: an aside is counted nowhere else.
  const perChoice = !!onPer && total > PER_OPTIONS[0]
  if (totalPages <= 1 && !perChoice && !aside) return null

  return (
    <Box data-testid={testId} sx={FOOTER_SX}>
      <Typography variant="body2" data-testid={testId && `${testId}-readout`} sx={READOUT_SX}>
        {readout ?? `${groupedCount(first)}–${groupedCount(last)} of ${groupedCount(total)}${aside ? ` · ${aside}` : ''}`}
      </Typography>
      {totalPages > 1 && (
        /* Both buttons carry `tapTargetSx`: a plain text `Button` (~31px) is not covered by `theme.ts`'s 44px
           phone floor, and the accessor keeps the number single-homed in `theme.custom.tapTargetPx`. */
        <Box data-testid={testId && `${testId}-pager`} sx={PAGER_SX}>
          <Button type="button" variant="text" disabled={page <= 1} onClick={() => onPage(page - 1)} sx={tapTargetSx}
            aria-label={noun ? `Previous page of ${noun}` : undefined}>
            Previous
          </Button>
          <Typography variant="body2" sx={{ color: 'text.secondary', textAlign: 'center' }}>
            Page {page} of {totalPages}
          </Typography>
          <Button type="button" variant="text" disabled={nextDisabled || page >= totalPages} onClick={() => onPage(page + 1)}
            sx={tapTargetSx} aria-label={noun ? `Next page of ${noun}` : undefined}>
            Next
          </Button>
        </Box>
      )}
      {perChoice && (
        <Box data-testid={testId && `${testId}-per`} sx={PER_SX}>
          <Typography component="label" htmlFor={selectId} variant="body2" sx={{ color: 'inherit', whiteSpace: 'nowrap' }}>
            Per page
          </Typography>
          <SelectField
            id={selectId}
            dense
            value={String(per)}
            onChange={(next) => onPer?.(Number(next))}
            options={PER_OPTIONS.map((n) => ({ value: String(n), label: String(n) }))}
            sx={SELECT_SX}
          />
        </Box>
      )}
    </Box>
  )
}

export default ListPager
