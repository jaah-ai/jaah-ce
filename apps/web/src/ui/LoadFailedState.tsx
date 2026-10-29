import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import type { JSX } from 'react'
import Banner from './Banner'
import { EmptyState } from './EmptyState'
import { tapTargetSx } from './listStyles'

/* A read that failed with nothing to show: the server's detail verbatim, "<X> unavailable", and Retry — the ONE
 * home for that block, so a failed read is never drawn as an empty list. `testId` is the Retry button's. */
export function LoadFailedState({ title, error, onRetry, testId }: {
  title: string
  error?: string | null
  onRetry: () => void
  testId?: string
}): JSX.Element {
  const retry = <Button type="button" variant="outlined" onClick={onRetry} sx={tapTargetSx} data-testid={testId}>Retry</Button>
  return (
    <Box>
      {error && <Banner severity="error">{error}</Banner>}
      <EmptyState title={title} action={retry} />
    </Box>
  )
}

export default LoadFailedState
