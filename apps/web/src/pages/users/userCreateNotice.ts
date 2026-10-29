import type { InvitationOutcome } from '../../types'

/* What a create (or a resend) did, as the banner over the roster says it. One home for the wording, read by
 * `UserNewPage` (which writes it) and `UsersTab` (which shows it after the hand-off). */

/** What an invitation write did, as the banner over the roster says it. */
export type Notice = { severity: 'success' | 'warning' | 'error'; text: string }

/** The router-state key `UserNewPage` hands its notice to `/users` under. */
export const NEW_USER_NOTICE = 'newUserNotice'

/* The banner for each create outcome; `skipped` (the box unticked) says nothing — the row is the news.
   Not-configured is a WARNING, not an error: the user exists and can still sign in. */
export function createNotice(outcome: InvitationOutcome | undefined, email: string): Notice | null {
  switch (outcome) {
    case 'sent': return { severity: 'success', text: `Invitation sent to ${email}` }
    case 'not_configured': return { severity: 'warning', text: 'User created; email isn’t configured' }
    case 'failed': return { severity: 'warning', text: 'User created; invitation failed to send' }
    case 'rate_limited': return { severity: 'warning', text: 'User created; invitation limit reached — try Resend later.' }
    default: return null
  }
}
