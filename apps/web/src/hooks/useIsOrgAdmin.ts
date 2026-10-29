import { useAuth } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'

/** `lib/orgAdmin.ts::isOrgAdminSeat` for the signed-in user. */
export function useIsOrgAdmin(): boolean {
  return isOrgAdminSeat(useAuth().admin)
}
