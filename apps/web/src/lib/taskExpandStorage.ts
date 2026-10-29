// Persistence for the /tasks outline — which top-level rows are expanded.
// Pure of React: the page owns WHEN to read/write, this module what a well-formed stored value is.
//
// `localStorage` only — the expand state is remembered locally, never on the server:
// per browser, and tabs see each other's changes only on their next mount — the same trade
// `consoleLayoutStorage.ts` and `components/WorktreePool.tsx`'s `cockpit.pool.collapsedGroups` make.

import { readJson, validEntries, writeJson } from './safeStorage'

/**
 * The key for the expanded-row ID SET, scoped to the admin id: one browser profile can be shared
 * (`auth.tsx` clears the query cache on an admin switch), and an inherited outline would leak view state.
 * `null` gets its own `anon` bucket. `adminId` is a UUID string; the stored TASK ids stay numeric (hence
 * `loadExpandedTaskIds`' `typeof === 'number'`).
 */
export function expandedTasksStorageKey(adminId: string | null): string {
  return `cockpit.tasks.expanded:${adminId ?? 'anon'}`
}

/**
 * The stored expanded-row ids, or `[]`. Only IDS persist, never fetched details (a stale snapshot would
 * pose as current; `Tasks.tsx` refetches each id). Non-arrays are discarded; non-finite entries dropped
 * (never `GET /api/tasks/NaN`).
 */
export function loadExpandedTaskIds(key: string): number[] {
  return validEntries(readJson('local', key), (id): id is number => typeof id === 'number' && Number.isFinite(id))
}

/**
 * Persist the expanded-row ids; returns whether the write landed. Failing to remember must never cost the
 * operator the expansion itself.
 */
export function saveExpandedTaskIds(key: string, ids: number[]): boolean {
  return writeJson('local', key, ids)
}
