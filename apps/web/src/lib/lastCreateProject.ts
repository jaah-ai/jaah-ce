// The project of the last task this viewer filed by hand (the full editor or the board's create row) — the
// create ladder's rung under All projects (`components/tasks/shared.tsx::resolveCreateProject`).
//
// `localStorage` only, per browser and per signed-in admin (`adminScopedKey`): a convenience, never state that must
// survive. A project the viewer can no longer create in is skipped by the ladder, which falls through to its default.

import { adminScopedKey, readJson, writeJson } from './safeStorage'

export function lastCreateProjectKey(adminId: string | null): string {
  return adminScopedKey('cockpit.tasks.lastCreateProject', adminId)
}

/** The remembered project id, or null when none (or unreadable). */
export function loadLastCreateProject(adminId: string | null): number | null {
  const id = readJson('local', lastCreateProjectKey(adminId))
  return typeof id === 'number' && Number.isInteger(id) ? id : null
}

/** Remember `projectId` as the last hand-filed task's project. Best effort: a failed write costs only the default. */
export function saveLastCreateProject(adminId: string | null, projectId: number | null | undefined): void {
  if (typeof projectId === 'number') writeJson('local', lastCreateProjectKey(adminId), projectId)
}
