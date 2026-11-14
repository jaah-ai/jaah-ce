import type { JSX, ReactNode } from 'react'
import Button, { type ButtonProps } from '@mui/material/Button'
import { tapTargetSx } from './listStyles'
import Tip from './Tip'

/* The console's ONE form submit button: contained, primary, at the tap-target floor, and disabled while its
 * form is `busy` — when it reads `busyLabel` (default `Saving…`). Every other `Button` prop passes through;
 * `testId` lands as `data-testid`; a `title` is its `ui/Tip`. */
export function SubmitButton({
  busy = false,
  busyLabel = 'Saving…',
  disabled,
  testId,
  title,
  children,
  sx,
  ...rest
}: Omit<ButtonProps, 'type'> & {
  busy?: boolean
  busyLabel?: ReactNode
  testId?: string
}): JSX.Element {
  return (
    <Tip title={title}><Button
      variant="contained"
      color="primary"
      {...rest}
      type="submit"
      disabled={busy || disabled}
      data-testid={testId}
      sx={[tapTargetSx, ...(Array.isArray(sx) ? sx : sx ? [sx] : [])]}
    >
      {busy ? busyLabel : children}
    </Button></Tip>
  )
}

export default SubmitButton
