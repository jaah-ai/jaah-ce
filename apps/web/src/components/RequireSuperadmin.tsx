import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { DenyListBackground, useListBackground } from './ListDetail'
import UnknownRoute from './UnknownRoute'

// Nav/route guard is UX / defense-in-depth only — the authoritative gate is the server's.
//
// Admits the operator (`is_operator`, `routes/auth.py::AdminOut.is_operator`: superadmin AND operator
// plane), as the `Tenancy.OPERATOR` routes behind it do; `orgAdmin` widens it to a tenant's Org
// Owner/Admin (`auth.tsx::isOrgAdminSeat`) for surfaces the API gates `@org_admin` and scopes per org.
//
// An `orgAdmin` refusal redirects to `/no-access`: a refused arrival is a shared or stale link, and a silent hop to
// `/tasks` left it unexplained. An operator page refused is "Page not found" in place, so this guard sits
// OUTSIDE any `RequireFeature` on an operator page. It takes `RequireFeature`'s `resolving` hold: a
// persisted snapshot predating `is_operator` would otherwise bounce the real operator off a deep link.
export default function RequireSuperadmin({ orgAdmin = false }: { orgAdmin?: boolean }) {
  const { admin, loading, resolving } = useAuth()
  // Behind an open editor, a refused list withdraws the split instead of redirecting (ListDetail.tsx).
  const background = useListBackground()
  if (loading || resolving) return null
  const admitted = orgAdmin ? isOrgAdminSeat(admin) : admin?.is_operator === true
  if (!admitted && background) return <DenyListBackground deny={background} />
  // An operator page refused reads as a path that does not exist, Operator members acting as a tenant included: a
  // refusal, or an offer to switch, would advertise which addresses are the operator's.
  if (!admitted && !orgAdmin) return <UnknownRoute />
  if (!admitted) return <Navigate to="/no-access" replace />
  return <Outlet />
}
