// THE USER'S FOCUSED PROJECT — the one they have told the console they work in. Stored server-side as
// `focused_project_id` (`apps/api/app/preferences.py`), so it follows them across browsers and the phone app.
// Set on `/profile`, a user's `/users/:id` Settings tab, and the `/tasks` rail's project row menu.
// Two readers:
//   - `/tasks` with no `?scope=` (what `/` redirects to) opens on it (`pages/Tasks.tsx`);
//   - a task created on All projects is filed into it (`components/tasks/shared.tsx::resolveCreateProject`).
// A project the user cannot see (deleted, or a grant revoked) is ignored by both: All projects, as if unset.
// Pure of React, so the read rule is unit-testable; `hooks/useFocusedProjectId.ts` is its React seam.

/** The preference key, named once so the writers and the readers cannot drift apart. */
export const FOCUSED_PROJECT_PREF = 'focused_project_id'

/** The stored focused project id, or `null` for none or anything that is not a positive integer id. */
export function readFocusedProjectId(preferences: Record<string, unknown> | null | undefined): number | null {
  const raw = preferences?.[FOCUSED_PROJECT_PREF]
  return typeof raw === 'number' && Number.isSafeInteger(raw) && raw > 0 ? raw : null
}

/**
 * The `?scope=` a `/tasks` visit is rewritten to for the focused project, or `null` to leave the URL alone.
 *   - no `?scope=` (opening Jaah) → the focused project, unless the loaded rail proves it is not visible;
 *   - the focused project's own scope that the loaded rail does not list → `all` (a stale focus, so opening
 *     Jaah does not land on an empty board). An explicit link to it would show nothing either.
 * `visibleProjectIds` is the rail's project list, `undefined` while it loads: the landing does not wait for
 * it, so the board never fetches All projects first.
 */
export function focusedLandingScope(
  rawScope: string | null,
  focusedProjectId: number | null,
  visibleProjectIds: readonly number[] | undefined,
): string | null {
  if (focusedProjectId === null) return null
  const visible = visibleProjectIds === undefined || visibleProjectIds.includes(focusedProjectId)
  const focusedKey = `project:${focusedProjectId}`
  if (rawScope === null) return visible ? focusedKey : null
  return rawScope === focusedKey && !visible ? 'all' : null
}
