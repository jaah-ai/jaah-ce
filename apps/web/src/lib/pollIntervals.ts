/* The console's poll intervals, ONE MODULE AWAY from the pages that poll on them so a test can mock it (a page or
 * component module cannot be mocked by its own test): asserting what a poll loop does and does NOT do takes
 * several passes, and sleeping real seconds per pass is slow and silently checks fewer passes whenever the
 * interval grows. */

/** How often the account-provisioning flow re-asks the node for its progress (`pages/credentials/AccountEditPage.tsx`). */
export const PROVISION_POLL_MS = 2000

/** How often a new session's VM is re-read while it starts (`components/WorktreePool.tsx`). */
export const VM_START_POLL_MS = 3000

/** How often a customer's Products feed re-reads itself while a product is still on its way (`ProductsTab.tsx::settling`). */
export const SETTLING_POLL_MS = 3000

/** How often every NodeLink list and editor (`pages/nodes/nodeLinkStore`) re-reads while a server pass is out: the
 *  `nodelink-provision` cron's provision, a cloud delete, a node's unmount. */
export const NODELINK_POLL_MS = 15000

/** How often a server pass still running is re-read: a link code's status (`hooks/useLinkCodeFlow.ts`). */
export const RUNNING_POLL_MS = 3000

/** How often a sessions read re-reads: Audit › Sessions while the stream is down (`pages/audit/SessionsTab.tsx`), a
 *  node's sessions usage (`components/NodeSessionsUsage.tsx`). */
export const SESSIONS_POLL_MS = 15_000

/** How often a Jaah AI wallet or usage read re-reads while its stream is down, or a task's run is live
 *  (`components/billing/JaahBalanceCard.tsx`, `components/tasks/TaskSessionUsage.tsx`, `pages/orgs/OrgJaahAiTab.tsx`,
 *  `pages/customers/JaahTenantsUsageSection.tsx`). */
export const USAGE_POLL_MS = 60_000
