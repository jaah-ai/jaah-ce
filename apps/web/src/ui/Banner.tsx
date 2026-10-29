import Alert from '@mui/material/Alert'
import type { JSX, ReactNode } from 'react'

/* The ONE page-level message block (MUI `Alert`). `severity` is the ONLY colour knob — no variant/color/
 * icon override, so there is one way to spell "this is bad".
 *
 * THE ROOT `data-testid` IS UNCONDITIONAL AND UNSUPPRESSABLE: e2e reads
 * `'.error-banner, [data-testid="error-banner"]'` to prove a page has NO error, and a negative assertion
 * silently passes if a page omits it. `testId` lands on the inner MESSAGE node, never the root.
 */
export function Banner({
  severity,
  children,
  onDismiss,
  testId,
  action,
}: {
  severity: 'error' | 'success' | 'warning' | 'info'
  children: ReactNode
  /** When given, the Alert shows MUI's close affordance and calls this on activation. */
  onDismiss?: () => void
  /** An EXTRA handle on the message node. It can never replace the root token above. */
  testId?: string
  /** One control at the banner's end (the message's own verb, e.g. Change org…). Not with `onDismiss`: MUI
   *  draws one action slot, and the control wins it. */
  action?: ReactNode
}): JSX.Element {
  return (
    <Alert
      severity={severity}
      variant="outlined"
      onClose={onDismiss}
      action={action}
      slotProps={{
        ...(testId ? { message: { 'data-testid': testId } as object } : {}),
        /* MUI's 4px top padding would sit the control below the centred message. */
        action: { sx: { pt: 0 } },
      }}
      /* Last, so no spread can shadow it. */
      data-testid={severity === 'error' ? 'error-banner' : `${severity}-banner`}
      /* Both numbers are REDUCTIONS of MUI's 6px/16px padding. */
      sx={{ py: 0.25, px: 1, alignItems: 'center', '& .MuiAlert-message': { py: 0.5 } }}
    >
      {children}
    </Alert>
  )
}

export default Banner
