import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import type { SxProps, Theme } from '@mui/material/styles'
import type { Dispatch, JSX, ReactNode, SetStateAction } from 'react'
import { tapTargetSx } from './listStyles'

/* The console's ONE "Previous / Page X of N / Next" footer. The caller owns the render condition
 * (usually `totalPages > 1`; `PageNav` below owns it for a relative page setter) and the page state; `page` is
 * the 1-based number shown.
 * A KEYSET list (a cursor has no "page 4 of N") uses the same footer with its own words — `previousLabel`
 * "Newer" / `nextLabel` "Older", a `label` count line, both `disabled` flags — and publishes `depth` (how many
 * pages back from the newest) as `data-depth`, so a spec can wait for a turn to land.
 * Both buttons carry `tapTargetSx`: a plain text `Button` (~31px) is not covered by `theme.ts`'s 44px
 * phone floor, and the accessor keeps the number single-homed in `theme.custom.tapTargetPx`. */
export function Pager({
  page = 1,
  totalPages = 1,
  onPrevious,
  onNext,
  previousDisabled = page <= 1,
  nextDisabled = page >= totalPages,
  previousLabel = 'Previous',
  nextLabel = 'Next',
  label,
  labelTestId,
  noun,
  depth,
  testId,
  sx,
}: {
  page?: number
  totalPages?: number
  onPrevious: () => void
  onNext: () => void
  previousDisabled?: boolean
  nextDisabled?: boolean
  previousLabel?: string
  nextLabel?: string
  /** Replaces the default "Page X of N" readout. */
  label?: ReactNode
  labelTestId?: string
  /** What is paged ("sessions"): names the buttons "Next page of sessions", unambiguous out of context. */
  noun?: string
  depth?: number
  testId?: string
  sx?: SxProps<Theme>
}): JSX.Element {
  return (
    <Box
      data-testid={testId}
      data-depth={depth}
      sx={[
        { display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1, mt: 1, flexWrap: 'wrap' },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      <Button type="button" variant="text" disabled={previousDisabled} onClick={onPrevious} sx={tapTargetSx}
        aria-label={noun ? `Previous page of ${noun}` : undefined}>
        {previousLabel}
      </Button>
      <Typography variant="body2" data-testid={labelTestId} sx={{ color: 'text.secondary', textAlign: 'center' }}>
        {label ?? `Page ${page} of ${totalPages}`}
      </Typography>
      <Button type="button" variant="text" disabled={nextDisabled} onClick={onNext} sx={tapTargetSx}
        aria-label={noun ? `Next page of ${noun}` : undefined}>
        {nextLabel}
      </Button>
    </Box>
  )
}

/* `Pager` wired to a relative page setter (±1, in whatever base the caller counts); renders nothing on a single page. */
export function PageNav({ setPage, ...rest }: Omit<Parameters<typeof Pager>[0], 'onPrevious' | 'onNext'> & {
  page: number
  totalPages: number
  setPage: Dispatch<SetStateAction<number>>
}): JSX.Element | null {
  if (rest.totalPages <= 1) return null
  return <Pager onPrevious={() => setPage(p => p - 1)} onNext={() => setPage(p => p + 1)} {...rest} />
}
