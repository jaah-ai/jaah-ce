import Alert, { type AlertColor } from '@mui/material/Alert'
import Snackbar from '@mui/material/Snackbar'
import type { JSX, ReactNode } from 'react'
import { BOTTOM_NAV_HEIGHT, PHONE } from './listStyles'

/* The ONE self-hiding report of a finished action (MUI `Snackbar` + filled `Alert`), anchored bottom/centre.
 * THE RULES, so no caller re-decides them: a bad outcome (`error`/`warning`) stays twice as long as a good one
 * (8s over 4s); a clickaway NEVER dismisses, so a tap elsewhere on a phone cannot eat the report (the Alert's
 * own ✕ and the timer are the two ways out); and it sits above the phone's bottom nav, else the nav covers it.
 * `seq` keys the Snackbar: a second report inside the first's window restarts the hide timer. The Alert
 * carries `data-testid={testId}` and `data-severity` for the specs. */
const TOAST_SX = { [PHONE]: { bottom: `calc(${BOTTOM_NAV_HEIGHT}px + 0.75rem)` } } as const
const ANCHOR = { vertical: 'bottom', horizontal: 'center' } as const

export default function Toast({ toast, onClose, testId }: {
  toast: { text: ReactNode; severity?: AlertColor; seq?: number } | null
  onClose: () => void
  testId: string
}): JSX.Element {
  const severity = toast?.severity ?? 'success'
  const bad = severity === 'error' || severity === 'warning'
  return (
    <Snackbar
      key={toast?.seq}
      open={toast !== null}
      autoHideDuration={bad ? 8000 : 4000}
      onClose={(_, reason) => { if (reason !== 'clickaway') onClose() }}
      anchorOrigin={ANCHOR}
      sx={TOAST_SX}
    >
      {toast ? (
        <Alert severity={severity} variant="filled" onClose={onClose} data-testid={testId} data-severity={severity}>
          {toast.text}
        </Alert>
      ) : undefined}
    </Snackbar>
  )
}
