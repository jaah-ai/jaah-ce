/** How often the account-provisioning flow re-asks the node for its progress.
 *
 * ITS OWN MODULE so a test can lower it (a page module cannot be mocked by its own test): asserting what
 * the poll loop does NOT do needs several passes, and sleeping real seconds is slow and silently checks
 * fewer passes whenever the interval grows.
 */
export const PROVISION_POLL_MS = 2000
