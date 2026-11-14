import { get, withQuery } from '../api'
import type { Page } from '../hooks/useListPage'
import type { ManagedUser } from '../types'
import type { LookupOption } from '../ui/AsyncLookup'
import { memberName } from './memberLabel'

/* THE CONSOLE'S ONE USER SEARCH: every person picker (clone owner, waiver owner, ticket
 * requester, the Chats tab) asks `GET /api/users?q=<typed>&per=20` — the People roster's own route, server-
 * filtered on the shown label and the address (`routes/users.py::list_users`) and gated as it is
 * (`users:view`). A capped page, never the roster: an org of thousands must not ship to every drawer.
 * The People tab itself pages the same route through `hooks/useListPage`. */

/** One picker page: enough to find a person by a few letters, small enough to answer at once. */
export const USER_SEARCH_PER = 20

/** The People Status filter's vocabulary (`routes/users.py::UserStatus`). */
export const USER_STATUSES = ['active', 'disabled', 'invited'] as const
export type UserStatus = (typeof USER_STATUSES)[number]

/** What a picker may offer. `enabled`: only accounts that can sign in (no admin disable, not held by an
 *  invitation) — applied by the SERVER (`?enabled=true`), so a prefix whose first matches are disabled still
 *  fills the page with working ones. `org`: one org's members only (`?org=`), for the operator picking inside a
 *  customer's org; a tenant's read is its own org already. */
export type UserSearchOpts = { enabled?: boolean; org?: string }

/** Up to `USER_SEARCH_PER` people matching `q` (empty = the first page by address). `signal` is
 *  `ui/AsyncLookup`'s, so a superseded keystroke is aborted. */
export async function searchUsers(q: string, signal?: AbortSignal, opts: UserSearchOpts = {}): Promise<ManagedUser[]> {
  const path = withQuery('/api/users', { per: USER_SEARCH_PER, q: q.trim(), enabled: opts.enabled, org: opts.org })
  const page = await get<Page<ManagedUser>>(path, signal ? { signal } : undefined)
  return page.items
}

/** A person as a lookup row: the name (or the address for a nameless account) as the label, the address as
 *  the hint when the label does not already show it — names repeat, the address does not. A name typed with the
 *  address inside it ("Org B orgb-admin@…") would print the address twice. */
export function userOption(user: Pick<ManagedUser, 'id' | 'email' | 'first_name' | 'last_name'>): LookupOption {
  const label = memberName(user)
  const shown = label.toLowerCase().split(/[\s()<>,]+/).includes(user.email.toLowerCase())
  return { key: user.id, label, hint: shown ? undefined : user.email }
}

/** `searchUsers` as `ui/AsyncLookup` rows — every picker's `fetchOptions`. */
export async function searchUserOptions(
  q: string,
  signal?: AbortSignal,
  opts: UserSearchOpts = {},
): Promise<LookupOption[]> {
  return (await searchUsers(q, signal, opts)).map(userOption)
}
