import Button from '@mui/material/Button'
import type { JSX } from 'react'
import { tapTargetSx } from './listStyles'
import { EmptyState } from './EmptyState'

/* A filtered list that filtered everything out: says so, and offers the one way back. The ONE home for
 * that block, so every list's Clear is a real `type="button"` at tap-target height. No `onClear` = no
 * button (a list whose filters cannot be cleared from here, or have nothing to clear). */
export function NoMatchesState({
  onClear,
  title = 'Nothing matches these filters',
  clearLabel = 'Clear filters',
  testId,
}: {
  onClear?: () => void
  title?: string
  clearLabel?: string
  /** The Clear button's `data-testid`. */
  testId?: string
}): JSX.Element {
  return (
    <EmptyState
      title={title}
      action={
        onClear && (
          <Button type="button" variant="outlined" onClick={onClear} sx={tapTargetSx} data-testid={testId}>
            {clearLabel}
          </Button>
        )
      }
    />
  )
}

export default NoMatchesState
