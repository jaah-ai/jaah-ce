import Alert from '@mui/material/Alert'
import type { JSX, ReactNode } from 'react'

/* The ONE page-level message block (MUI `Alert`). `severity` is the ONLY colour knob — no variant/color/
 * icon override, so there is one way to spell "this is bad". `role="status"` is the one politeness knob:
 * MUI's Alert is an assertive `role="alert"`, and a wrapper cannot demote a live region, so a banner that
 * states a fact rather than interrupting (a blocked-by note, a saved confirmation) asks for it here.
 *
 * The message wraps anywhere: a server sentence quoting a URL or a 300-char reason never scrolls inside the
 * banner (MUI's message is `overflow: auto`), so no caller wraps its text in a wrapping span of its own.
 *
 * THE ROOT `data-testid` IS UNCONDITIONAL AND UNSUPPRESSABLE: e2e reads
 * `'.error-banner, [data-testid="error-banner"]'` to prove a page has NO error, and a negative assertion
 * silently passes if a page omits it. `testId` lands on the inner MESSAGE node, never the root.
 */
export function Banner({
  severity,
  children,
  onDismiss,
  dismissLabel,
  testId,
  action,
  role,
}: {
  severity: 'error' | 'success' | 'warning' | 'info'
  children: ReactNode
  /** Demotes the live region from MUI's assertive default to a polite `status`. */
  role?: 'status'
  /** When given, the Alert shows MUI's close affordance and calls this on activation. */
  onDismiss?: () => void
  /** The close affordance's accessible name and title (MUI's default: Close). */
  dismissLabel?: string
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
      closeText={dismissLabel}
      action={action}
      role={role}
      slotProps={{
        /* The message wraps anywhere (header): `minWidth: 0` lets the flex item shrink below its content. */
        message: { ...(testId ? { 'data-testid': testId } : {}), sx: { minWidth: 0, overflowWrap: 'anywhere' } } as object,
        /* MUI's 4px top padding would sit the control below the centred message. */
        action: { sx: { pt: 0 } },
      }}
      /* Last, so no spread can shadow it. */
      data-testid={severity === 'error' ? 'error-banner' : `${severity}-banner`}
      /* Both numbers are REDUCTIONS of MUI's 6px/16px padding. */
      sx={{ py: 0, px: 1, alignItems: 'center', '& .MuiAlert-message': { py: 0.5 } }}
    >
      {children}
    </Alert>
  )
}

export default Banner
