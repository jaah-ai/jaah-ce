import Box from '@mui/material/Box'
import NativeSelect from '@mui/material/NativeSelect'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { useCallback, useId, type JSX, type SetStateAction } from 'react'
import { PER_OPTIONS } from '../hooks/useListPage'
import { groupedCount } from '../lib/humanize'
import { usePageClamp } from '../hooks/usePageClamp'
import { PHONE } from './listStyles'
import { Pager } from './Pager'

/* THE FOOTER OF A `hooks/useListPage` LIST: where the reader is ("1–50 of 1,234"), the console's one
 * `Pager`, and how many rows a page holds. Each part shows only when it can say something: nothing at all before
 * the first total (no "of 0" posing as a count) or on an empty set (the list's own empty state speaks); no Prev/
 * Next on a single page; no size choice when every size shows the whole set (≤ 25). A footer with only its readout
 * left (one page, no size choice) is not drawn at any width: "1–1 of 1" under one row repeats what it shows.
 * THREE COLUMNS ON A DESKTOP — readout left, pager centred on the list, size right, the outer two `1fr` so the
 * pager stays centred whatever the readout's width. TWO ROWS AT `PHONE`: readout and size share the first line,
 * the pager takes the second whole — one line of all three is ~425px against a 390px phone's 358px. */

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
    rowGap: 0.25,
  },
} as const

/* Tabular digits: "1–50" → "51–100" must not jitter the line as the reader pages. */
const READOUT_SX = {
  gridArea: 'readout',
  color: 'text.secondary',
  fontVariantNumeric: 'tabular-nums',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  minWidth: 0,
} as const

/* `Pager` brings its own `mt: 1`; in the grid the row gap owns spacing. */
const PAGER_SX = { gridArea: 'pager', mt: 0, minWidth: 0 } as const

const PER_SX = {
  gridArea: 'per',
  justifySelf: 'end',
  display: 'flex',
  alignItems: 'center',
  gap: 0.75,
  color: 'text.secondary',
} as const

/* A quiet underline-less select sized to its digits; the 44px floor applies below MUI `sm` (a finger, not a
   width — `ui/listStyles.ts`'s `fieldTapTargetSx` reasoning), on the native control itself. */
const selectSx = (theme: Theme) => ({
  fontSize: '0.8125rem',
  fontVariantNumeric: 'tabular-nums',
  '& .MuiNativeSelect-select': { py: 0.5, pl: 0.75, minHeight: 0 },
  [theme.breakpoints.down('sm')]: {
    '& .MuiNativeSelect-select': { minHeight: theme.custom?.tapTargetPx ?? 44, boxSizing: 'border-box' },
  },
})

export function ListPager({
  page,
  per,
  total,
  onPage,
  onPer,
  testId,
  aside,
}: {
  page: number
  per: number
  /** The filtered set's size; null while the first page is in flight. */
  total: number | null
  onPage: (page: number) => void
  onPer: (per: number) => void
  testId?: string
  /** Rows drawn beside the set but not in its `total` (e.g. "1 preparing"), said after the count. */
  aside?: string
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
  if (totalPages <= 1 && total <= PER_OPTIONS[0] && !aside) return null

  return (
    <Box data-testid={testId} sx={FOOTER_SX}>
      <Typography variant="body2" data-testid={testId && `${testId}-readout`} sx={READOUT_SX}>
        {readout ?? `${groupedCount(first)}–${groupedCount(last)} of ${groupedCount(total)}${aside ? ` · ${aside}` : ''}`}
      </Typography>
      {totalPages > 1 && (
        <Pager
          page={page}
          totalPages={totalPages}
          onPrevious={() => onPage(page - 1)}
          onNext={() => onPage(page + 1)}
          testId={testId && `${testId}-pager`}
          sx={PAGER_SX}
        />
      )}
      {total > PER_OPTIONS[0] && (
        <Box data-testid={testId && `${testId}-per`} sx={PER_SX}>
          <Typography component="label" htmlFor={selectId} variant="body2" sx={{ color: 'inherit', whiteSpace: 'nowrap' }}>
            Per page
          </Typography>
          <NativeSelect
            id={selectId}
            value={per}
            onChange={(event) => onPer(Number(event.target.value))}
            disableUnderline
            sx={selectSx}
          >
            {PER_OPTIONS.map((n) => <option key={n} value={n}>{n}</option>)}
          </NativeSelect>
        </Box>
      )}
    </Box>
  )
}

export default ListPager
