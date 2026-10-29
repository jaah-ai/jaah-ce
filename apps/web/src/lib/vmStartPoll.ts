/** How often a new session's VM is re-read while it starts (`components/WorktreePool.tsx`).
 *
 * ITS OWN MODULE so a test can lower it (a component module cannot be mocked by its own test): riding out a
 * dropped read takes two passes, and sleeping real seconds per pass is slow.
 */
export const VM_START_POLL_MS = 3000
