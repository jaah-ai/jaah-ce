import type { JSX } from 'react'
import { ConnectReturnBanner, useConnectReturn } from '../../../hooks/useConnectReturn'

/* Connect with Google's return flags (`routes/org_email.py::_settings_redirect`): a success lands on the new
 * connection's page with `?gmail_connect=connected`, a failure on the Email card with `?gmail_connect=error&reason=`.
 * Read once and stripped (`hooks/useConnectReturn.tsx`), as Slack's (`components/SlackWorkspaceCard.tsx`). */

const RETURN_PARAM = 'gmail_connect'

/* The callback's closed slug vocabulary. `Object.create(null)`: the key comes off the URL (`?reason=__proto__`). */
const RETURN_ERRORS: Record<string, string> = Object.assign(Object.create(null), {
  not_configured: "Gmail sign-in isn't set up. Add a service account, or ask an operator.",
  expired: 'The connect attempt timed out. Connect with Google again.',
  state_mismatch: "That response didn't match this browser's attempt. Connect with Google again.",
  wrong_user: 'That attempt belongs to another signed-in user. Connect with Google again.',
  denied: 'Declined at Google — nothing was connected.',
  no_code: 'Google returned no authorization code. Connect with Google again.',
  exchange_failed: 'Google refused to complete the connection. Connect with Google again.',
  send_not_granted: 'Sending email was not allowed. Connect with Google again and allow it.',
  no_address: "Google didn't say which address signed in. Connect with Google again.",
  off_plan: "Your plan doesn't include Tenant email.",
  other_kind: 'That mailbox is already connected with a service account. Disconnect it first.',
  raced: 'Someone else connected that mailbox just now. Reload to see it.',
})
const RETURN_FALLBACK = "Gmail didn't connect. Connect with Google again."

/** Consume the return flags once: what came back (`null` when none), a failure's sentence, and a way to dismiss it. */
export const useGmailConnectReturn = () => useConnectReturn(RETURN_PARAM, RETURN_ERRORS, RETURN_FALLBACK)

/** The return's error where the Email card itself is not offered — an org whose plan leaves Tenant email out, which
 * is exactly when the callback answers `off_plan` (`pages/settings/ownOrg/OwnOrgPanels.tsx`). */
export function GmailConnectReturnBanner(): JSX.Element | null {
  return <ConnectReturnBanner param={RETURN_PARAM} errors={RETURN_ERRORS} fallback={RETURN_FALLBACK} />
}
