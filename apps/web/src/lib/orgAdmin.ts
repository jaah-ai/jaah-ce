import type { Admin } from '../auth'
import { USERS_TAB_PATHS } from '../routeGroups'

/* ── The tenant-administration verdict ──
`routes/auth.py`'s mirror of `app/authz/principal.py::Principal.is_org_admin` (G4): the operator, or an Org
Owner/Admin of a TENANT org. On the operator plane an org role administers nothing (the fleet's users,
nodes and crons stay the operator's), so it answers false there without `is_operator`. What every
tenant-admin control (`@org_admin` / `org_admin_writer` routes) is gated on, and what `auth.tsx`'s
capability seam is allow-all for. Presentation only; the server re-derives it. */
export function isOrgAdminSeat(admin: Admin | null | undefined): boolean {
  if (!admin) return false
  if (admin.is_operator === true) return true
  return admin.tenant !== false && (admin.org_role === 'owner' || admin.org_role === 'admin')
}

/* An Org Owner/Admin's own org: Settings > Organizations, one card per subject (`pages/settings/ownOrg/`), and who
   has access in Directory > People > Access. The operator manages every org at `/customers/:id`
   (`pages/customers/CustomerPage.tsx`). */
export const ORG_SETTINGS_URL = '/settings?area=organizations'
export const ORG_ACCESS_URL = USERS_TAB_PATHS.access
/** The retired org page's tab key (`?orgtab=`): `pages/Settings.tsx` sends an old link to the tab's card. */
export const ORG_SETTINGS_TAB_PARAM = 'orgtab'
/* A retired tab id whose card's section slug differs from it. */
const RENAMED_ORG_TABS: Readonly<Record<string, string>> = { domains: 'join-domains' }
/** The Organizations card an old `?orgtab=` named, by its section slug (a retired tab names no card: the area opens). */
export function orgSectionOfTab(tab: string): string {
  return RENAMED_ORG_TABS[tab] ?? tab
}
