import Button from '@mui/material/Button'
import type { JSX, ReactNode } from 'react'
import Banner from './Banner'
import { tapTargetSx } from './listStyles'

/* The ONE "this failed, try again" block: an error Banner whose end control is Retry — for a read that failed
 * where a list's `LoadFailedState` does not fit (a tile band, a panel body, a dialog's pick list, a public page,
 * a failure over a list's last good load). `testId` lands on the message node, as `Banner`'s does;
 * `retryTestId` on the Retry. */
export function RetryBanner({ onRetry, testId, retryTestId, children }: {
  onRetry: () => void
  testId?: string
  retryTestId?: string
  children: ReactNode
}): JSX.Element {
  return (
    <Banner severity="error" testId={testId} action={<Button size="small" onClick={onRetry} data-testid={retryTestId} sx={tapTargetSx}>Retry</Button>}>
      {children}
    </Banner>
  )
}

export default RetryBanner
