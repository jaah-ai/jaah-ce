// Persistence for the /tasks outline — which top-level rows are expanded.
// Pure of React: the page owns WHEN to read/write, this module what a well-formed stored value is.
//
// `localStorage` only — the expand state is remembered locally, never on the server:
// per browser, and tabs see each other's changes only on their next mount — the same trade
// `consoleLayoutStorage.ts` and `components/WorktreePool.tsx`'s `cockpit.pool.collapsedGroups` make.

import { adminScopedKey, readJson, validEntries, writeJson } from './safeStorage'

/** The most ids the outline remembers — the LAST ones in stored order (the page writes not-yet-restored ids first,
 *  then live expansions): an unpruned set grew with every expansion ever made. */
export const MAX_STORED_EXPANDED_IDS = 50

/** The key for the expanded-row ID SET (`adminScopedKey`). `adminId` is a UUID string; the stored TASK ids stay
 *  numeric (hence `loadExpandedTaskIds`' `typeof === 'number'`). */
export function expandedTasksStorageKey(adminId: string | null): string {
  return adminScopedKey('cockpit.tasks.expanded', adminId)
}

/**
 * The stored expanded-row ids, or `[]`. Only IDS persist, never fetched details (a stale snapshot would
 * pose as current; `Tasks.tsx` refetches each id). Non-arrays are discarded; non-finite entries dropped
 * (never `GET /api/tasks/NaN`).
 */
export function loadExpandedTaskIds(key: string): number[] {
  const ids = validEntries(readJson('local', key), (id): id is number => typeof id === 'number' && Number.isFinite(id))
  return ids.slice(-MAX_STORED_EXPANDED_IDS)
}

/**
 * Persist the expanded-row ids, keeping the last `MAX_STORED_EXPANDED_IDS` in the given order; returns whether the
 * write landed. Failing to remember must never cost the operator the expansion itself.
 */
export function saveExpandedTaskIds(key: string, ids: number[]): boolean {
  return writeJson('local', key, ids.slice(-MAX_STORED_EXPANDED_IDS))
}
