import { Navigate, Outlet } from 'react-router-dom'
import { useAuth, useCan } from '../auth'
import { DenyListBackground, useListBackground } from './ListDetail'
import type { PermissionKey } from '../types'

/* The route-level half of the capability layer, twin of the rail's `requires`/`requiresAny`: a destination
 * hidden from the rail is unreachable by URL, bookmark or mobile saved route too — and a rail row this guard
 * refuses is a defect in the pair.
 *
 * Redirects to `/no-access`, not `/` (as `RequireSuperadmin orgAdmin`): `/` goes to `/tasks`, which would bounce a
 * user lacking `tasks:view` into a second guard; `/no-access` is the chain's unguarded terminal.
 *
 * IT NEVER DENIES ON INFORMATION IT DOES NOT HAVE:
 *   • `loading` — no admin yet: render nothing (as `RequireAuth`), since `useCan` says `false` until then.
 *   • `resolving` — a persisted snapshot is in hand but this load's first `/api/auth/me` has not answered:
 *     a "no" may predate a new grant, so hold rather than redirect (nothing would navigate back).
 * Fail-closed: an unresolved "yes" renders (the API rejects anything wrongly offered); an unresolved "no"
 * waits. Holding is never granting.
 *
 * PRESENTATION ONLY, NEVER THE SECURITY BOUNDARY — and THIS COMMENT IS THE SINGLE HOME for what that means
 * for the whole web capability layer (rail, `useCan`, this guard, the route table).
 * The authoritative gate is `authz_guard` (`apps/api/app/authz/`), registered app-wide as
 * `dependencies=[Depends(authz_guard)]` in `apps/api/app/main.py`, deciding on the resolved route before any
 * handler. FAIL-CLOSED (an undeclared route is denied), and shaped not to confirm what the caller may not
 * see (object denial 404, feature denial 403). So:
 *   • This layer must NEVER be cited as protection for any endpoint.
 *   • It takes nothing away: it only narrows what the console offers, in front of the real gate. */
/* TWO SPELLINGS OF ONE GATE: `permission` (one key) or `anyOf` (an OR), needed today only by the
 * People shell, whose two tabs have different keys. Guarded on either key alone, the route refused an
 * operator the rail offers the row to. Widening takes nothing away: the shell renders only readable tabs
 * (`pages/users/UsersPage.tsx`), panels gate their own writes, and the server decides. The two
 * props are mutually exclusive at the TYPE level (`never`). */
type RequireFeatureProps =
  | { permission: PermissionKey; anyOf?: never }
  | { anyOf: readonly PermissionKey[]; permission?: never }

export function RequireFeature(props: RequireFeatureProps) {
  const { loading, resolving } = useAuth()
  const can = useCan()
  /* Behind an open editor (components/ListDetail.tsx) a refused LIST withdraws the split instead of
     redirecting: the redirect would replace the editor the viewer is allowed to see. */
  const background = useListBackground()
  /* One list either way; an empty `anyOf` DENIES (fail-closed, like `authz_guard` for an undeclared route). */
  const keys: readonly PermissionKey[] = props.anyOf ?? [props.permission]
  if (loading) return null
  if (!keys.some(key => can(key))) {
    if (resolving) return null
    return background ? <DenyListBackground deny={background} /> : <Navigate to="/no-access" replace />
  }
  return <Outlet />
}

export default RequireFeature
