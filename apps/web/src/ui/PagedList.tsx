import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { SxProps, Theme } from '@mui/material/styles'
import type { JSX, ReactNode } from 'react'
import LoadingState from './LoadingState'
import { tapTargetSx } from './listStyles'
import type { PagedListState } from '../hooks/usePagedList'

/* The console's ONE "Load more" footer: a centred outlined button that says `Loading…` while its page is
 * in flight, then a caption under it. Hidden at the end, never disabled. A keyset page carries no total
 * (counting a fast-growing table is a scan), so the caption says how many are loaded and whether that is
 * all. The caller owns the reads (`hooks/usePagedList`, or `useInfiniteQuery`). */
export function LoadMoreFooter({
  hasMore,
  loading,
  onMore,
  label = 'Load more',
  testId,
  caption,
  sx,
}: {
  hasMore: boolean
  loading: boolean
  onMore: () => void
  label?: string
  testId?: string
  caption?: ReactNode
  sx?: SxProps<Theme>
}): JSX.Element | null {
  if (!hasMore && !caption) return null
  return (
    <Stack spacing={1} sx={sx}>
      {hasMore && (
        <Box sx={{ display: 'flex', justifyContent: 'center' }}>
          <Button variant="outlined" disabled={loading} onClick={onMore} sx={tapTargetSx} data-testid={testId}>
            {loading ? 'Loading…' : label}
          </Button>
        </Box>
      )}
      {caption && (
        <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary', textAlign: 'center' }}>
          {caption}
        </Typography>
      )}
    </Stack>
  )
}

/* A `hooks/usePagedList` ledger's body, in one branch chain:
 *   1. a failed read with no rows → Try again (EXCLUSIVE: never "No firings recorded" under a load error,
 *      and the only way out — the latch is sticky and "Load older" needs a cursor never obtained);
 *   2. no page yet → a skeleton;
 *   3. no rows → `empty` (the caller's two answers: nothing kept vs nothing matches);
 *   4. the rows (`children`) and the footer.
 * The error banner is the caller's: it also speaks over rows from an earlier read. */
export function PagedList({
  paging,
  children,
  empty,
  retryTestId,
  retryExtra,
  skeletonRows = 5,
  loadMoreLabel,
  loadMoreTestId,
  caption,
}: {
  paging: PagedListState<unknown, unknown>
  /** The loaded rows, as a list. */
  children: ReactNode
  empty: ReactNode
  retryTestId: string
  /** Beside Try again (a Clear filters for a refused filter, which fails every retry). */
  retryExtra?: ReactNode
  /** A handful: a skeleton taller than the settled list makes the panel shrink on settle. */
  skeletonRows?: number
  loadMoreLabel: string
  loadMoreTestId: string
  caption: ReactNode
}): JSX.Element {
  const { rows, loading, loaded, error, hasMore } = paging
  if (error && rows.length === 0) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', gap: 1, py: 1 }}>
        <Button
          variant="outlined"
          disabled={loading}
          onClick={() => void paging.reload()}
          sx={tapTargetSx}
          data-testid={retryTestId}
        >
          {loading ? 'Loading…' : 'Try again'}
        </Button>
        {retryExtra}
      </Box>
    )
  }
  if (!loaded || (loading && rows.length === 0)) return <LoadingState rows={skeletonRows} />
  if (rows.length === 0) return <>{empty}</>
  return (
    <>
      <Box ref={paging.bodyRef}>{children}</Box>
      <LoadMoreFooter
        hasMore={hasMore}
        loading={loading}
        onMore={() => void paging.loadMore()}
        label={loadMoreLabel}
        testId={loadMoreTestId}
        caption={caption}
      />
    </>
  )
}

export default PagedList
