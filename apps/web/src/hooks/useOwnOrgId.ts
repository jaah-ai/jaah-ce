import { useAuth } from '../auth'

/** The one home of "the signed-in viewer's own org id" (`admin.org`), or null with no org bound. Not
 * `auth.tsx::currentOrgId`, which prefers the membership marked current over `admin.org`. */
export function useOwnOrgId(): string | null {
  return useAuth().admin?.org?.id ?? null
}
