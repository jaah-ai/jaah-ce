import type { JSX } from 'react'
import Button, { type ButtonProps } from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'

/* The console's ONE in-flight action button that is not a form's `SubmitButton` (any variant, `type`, size or
 * label): while `busy` it is disabled and its start icon is a spinner sized to it (14px small, else 16) in the
 * label's colour. The spinner is `aria-hidden`, so the accessible name stays the label. Every other `Button`
 * prop passes through; the label (e.g. "Sending…" while busy) is the caller's. */
export function BusyButton({ busy = false, disabled, startIcon, ...rest }: ButtonProps & { busy?: boolean }): JSX.Element {
  return (
    <Button
      {...rest}
      disabled={busy || disabled}
      startIcon={busy ? <CircularProgress size={rest.size === 'small' ? 14 : 16} color="inherit" aria-hidden /> : startIcon}
    />
  )
}

export default BusyButton
