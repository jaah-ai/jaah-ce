import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { Dispatch, JSX, ReactNode, SetStateAction } from 'react'
import Banner from './Banner'
import { PageNav } from './Pager'
import { tapTargetSx } from './listStyles'
import type { logPages } from '../hooks/usePagedLogFilters'

/** The filter row's Clear, rendered only while a filter is set. */
export function ClearFiltersButton(props: { show: boolean; onClick: () => void; testId: string }) {
  if (!props.show) return null
  return <Button variant="text" onClick={props.onClick} sx={tapTargetSx} data-testid={props.testId}>Clear filters</Button>
}

/** A failure over a good load keeps its rows and carries its own Retry. */
export function StaleResultBanner(props: {
  error: string; onRetry: () => void; testId?: string; retryTestId: string
}): JSX.Element {
  return (
    <Box sx={{ mt: 1 }}>
      <Banner severity="error" testId={props.testId}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
          <Box component="span">{props.error} Showing the last result that loaded.</Box>
          <Button size="small" variant="outlined" data-testid={props.retryTestId} onClick={props.onRetry}>
            Retry
          </Button>
        </Stack>
      </Banner>
    </Box>
  )
}

/** The pager over a 0-based `page`, plus why Next is off when the endpoint's reach, not the log, ends.
 *  `logName` names the log in that sentence ("activity log"). */
export function LogPagerFooter({ page, setPage, pages, logName, testId, ceilingTestId, label }: {
  page: number
  setPage: Dispatch<SetStateAction<number>>
  pages: ReturnType<typeof logPages>
  logName: string
  testId: string
  ceilingTestId: string
  label?: ReactNode
}): JSX.Element {
  const { totalPages, maxPageIndex, atLastReachablePage } = pages
  return (
    <>
      <PageNav
        testId={testId}
        page={page + 1}
        totalPages={totalPages}
        nextDisabled={atLastReachablePage}
        label={label}
        setPage={setPage}
      />
      {atLastReachablePage && totalPages - 1 > maxPageIndex && (
        <Typography
          variant="caption"
          data-testid={ceilingTestId}
          sx={{ display: 'block', textAlign: 'center', mt: 0.5, color: 'text.secondary' }}
        >
          {`This is as deep as the ${logName} can be paged. Narrow the filters to reach older rows.`}
        </Typography>
      )}
    </>
  )
}
