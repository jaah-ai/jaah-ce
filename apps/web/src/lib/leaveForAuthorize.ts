import { navigateExternal } from './externalNavigate'
import { safeHref } from './safeHref'

/* An OAuth start's last step: leave for the provider's authorize page, or throw `unusableMessage` when the link the
 * server returned is not one `safeHref` passes. Its own module, not `externalNavigate`'s: tests `vi.mock` that one,
 * and a helper beside it would be mocked away with it. */
export function leaveForAuthorize(url: string, unusableMessage: string): void {
  const href = safeHref(url)
  if (!href) throw new Error(unusableMessage)
  navigateExternal(href)
}
