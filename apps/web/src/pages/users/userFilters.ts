import { useTypedUrlFilters } from '../../hooks/useUrlFilters'
import { USER_STATUSES, type UserStatus } from '../../lib/users'
import { oneOf } from '../../lib/strings'
import { isUuidShaped } from '../../lib/uuidShape'

/* THE PEOPLE ROSTER'S FILTERS, in the URL (`hooks/useUrlFilters.ts`): a pasteable "the disabled
 * people" link, written with `replace`. Every value is a query parameter of `GET /api/users`, which applies
 * it to the page AND `total` — nothing here filters. A value outside its vocabulary reads as none: a stale
 * bookmark must not send a 422 the strip cannot show. Keys are the console's words: `q`, `status`, `org`. */

export type UserFilters = { q: string; status: UserStatus | ''; org: string }

export const EMPTY_USER_FILTERS: UserFilters = { q: '', status: '', org: '' }

/* The server's `max_length` on `q` (`list_page.py::Q_MAX`). */
const Q_MAX = 200

export function readUserFilters(params: URLSearchParams): UserFilters {
  const org = params.get('org') ?? ''
  return {
    q: (params.get('q') ?? '').slice(0, Q_MAX),
    status: oneOf(USER_STATUSES, params.get('status')) ?? '',
    // `?org=` is a uuid server-side; anything else would 422.
    org: isUuidShaped(org) ? org : '',
  }
}

export const hasUserFilters = (filters: UserFilters): boolean =>
  Boolean(filters.q.trim() || filters.status || filters.org)

/** The roster read's own filter params (the page params are `useListPage`'s). `org` is sent only for an
 *  operator, the one viewer offered the control. */
export function userFilterParams(filters: UserFilters, { operator }: { operator: boolean }): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.q.trim()) params.set('q', filters.q.trim())
  if (filters.status) params.set('status', filters.status)
  if (operator && filters.org) params.set('org', filters.org)
  return params
}

/** Read through from the URL, patched one field at a time, cleared as one. */
export function useUserFilters(): [UserFilters, (patch: Partial<UserFilters>) => void, () => void] {
  return useTypedUrlFilters(readUserFilters, EMPTY_USER_FILTERS)
}
