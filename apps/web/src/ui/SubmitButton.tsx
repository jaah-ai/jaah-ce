import type { JSX, ReactNode } from 'react'
import Button, { type ButtonProps } from '@mui/material/Button'
import { tapTargetSx } from './listStyles'

/* The console's ONE form submit button: contained, primary, at the tap-target floor, and disabled while its
 * form is `busy` — when it reads `busyLabel` (default `Saving…`). Every other `Button` prop passes through;
 * `testId` lands as `data-testid`. */
export function SubmitButton({
  busy = false,
  busyLabel = 'Saving…',
  disabled,
  testId,
  children,
  sx,
  ...rest
}: Omit<ButtonProps, 'type'> & {
  busy?: boolean
  busyLabel?: ReactNode
  testId?: string
}): JSX.Element {
  return (
    <Button
      variant="contained"
      color="primary"
      {...rest}
      type="submit"
      disabled={busy || disabled}
      data-testid={testId}
      sx={[tapTargetSx, ...(Array.isArray(sx) ? sx : sx ? [sx] : [])]}
    >
      {busy ? busyLabel : children}
    </Button>
  )
}

export default SubmitButton
