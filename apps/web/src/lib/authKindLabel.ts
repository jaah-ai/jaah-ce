import type { AccountAuthKind } from '../types'

/* How an account's `auth_kind` reads on screen (an account's form, a provider row, a task's outcome attribution).
 * One home; do not re-add a local copy. */
export const AUTH_KIND_LABEL: Record<AccountAuthKind, string> = { oauth: 'Subscription', api_key: 'API key' }
