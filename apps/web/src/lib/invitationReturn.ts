/* WHERE A SIGN-IN RETURNS AFTER AN INVITATION LINK. The emailed button opens `/invitations/<id>`; signed out,
 * that page remembers itself here and sends the visitor to `/login`. Every sign-in door (password, Google,
 * the redirect providers, the second factor) lands on `/`, and `/` (`App.tsx::HomeRoute`) sends a remembered
 * invitation back to its page, which forgets it once it renders signed in. Per tab (`sessionStorage`, which
 * survives the providers' same-tab redirects); only an invitation path is ever stored or returned, so this is
 * never an open redirect. A storage failure simply returns nowhere. */

import { readString, removeKey, writeString } from './safeStorage'

const KEY = 'jaah.invitationReturn'
const INVITATION_PATH = /^\/invitations\/[A-Za-z0-9-]+$/

export function rememberInvitationReturn(path: string): void {
  // Not remembered (storage unavailable): the invitation still waits in the account menu.
  if (INVITATION_PATH.test(path)) writeString('session', KEY, path)
}

/** The remembered invitation page, or null. Reads only; the page itself forgets it. */
export function invitationReturn(): string | null {
  const path = readString('session', KEY)
  return path !== null && INVITATION_PATH.test(path) ? path : null
}

export function forgetInvitationReturn(): void {
  removeKey('session', KEY)
}
