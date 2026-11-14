import { safeReturnPath } from './listDetail'

/* THE NESTED-CREATE SEAM's address contract: a form that needs a record which does not exist yet opens that
   record's New page beside it (`/x/new`), and comes back with its own input restored and the new record picked.
   The URL carries only where to go back and which field to fill; the parent's draft stays in module memory
   (`hooks/useNestedCreate.ts`). */

/** Where the New page goes back to: an in-app path (the name `CredentialEditPage` has always read). */
export const RETURN_PARAM = 'return'
/** The parent's field that receives the new record's id. */
export const PICK_PARAM = 'pick'
/** The new record's id, set on the way back after a create; absent on Cancel. */
export const CREATED_PARAM = 'created'
/** Which stashed parent draft to restore on the way back. */
export const TOKEN_PARAM = 'nc'

const joiner = (path: string) => (path.includes('?') ? '&' : '?')

/** The New page's address for a nested create: `newPath` (it may carry its own query, `?type=aws`), told to
 *  come back to `returnTo` carrying the draft token, and which parent field the new id fills. */
export function nestedCreateUrl(newPath: string, returnTo: string, pick: string, token: string): string {
  const back = `${returnTo}${joiner(returnTo)}${TOKEN_PARAM}=${encodeURIComponent(token)}`
  return `${newPath}${joiner(newPath)}${RETURN_PARAM}=${encodeURIComponent(back)}&${PICK_PARAM}=${encodeURIComponent(pick)}`
}

/** `path` told to come back to `returnTo` on Cancel: a page opening a New page without a pick (the Command Center's
 *  New project), which lands exactly where it said (`hooks/useNestedCreate.ts::useNestedReturn`). */
export function withReturn(path: string, returnTo: string): string {
  return `${path}${joiner(path)}${RETURN_PARAM}=${encodeURIComponent(returnTo)}`
}

/** Where a New page goes back to: its `?return=` when that is a safe in-app path (`safeReturnPath`), with
 *  `created=<id>` added after a create and left off on Cancel (`createdId` null); `null` when absent or unsafe. */
export function returnUrl(search: string, origin: string, createdId: string | null): string | null {
  const path = safeReturnPath(search, origin, RETURN_PARAM)
  if (path === null || createdId === null) return path
  const url = new URL(path, origin)
  url.searchParams.set(CREATED_PARAM, createdId)
  return `${url.pathname}${url.search}${url.hash}`
}
