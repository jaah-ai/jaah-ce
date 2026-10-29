// Persistence for the /tasks board's Focus toggle. Pure of React: the page owns WHEN to read/write, this
// module owns what a well-formed stored value is.
//
// Everything else the board shows is the URL's — the selection (`?scope=`), the grouping and the
// row-removing filters (`lib/taskUrlFilters.ts`, `lib/taskScope.ts`, `lib/taskGrouping.ts`) — so a link
// reproduces it. Focus stays here: it is remembered per tab for the
// session, off on a fresh tab (Task 90: "within the session").

import { readJsonObject, writeJson } from './safeStorage'

/** The board's persisted per-tab state. */
export type TaskFilters = {
  /**
   * Focus toggle: hides finished/archived rows. The kept state sets are single-homed
   * in `apps/api/app/task_lifecycle.py`.
   */
  focus: boolean
}

/** First-visit default and fallback for anything unreadable: unfocused. */
export const DEFAULT_TASK_FILTERS: TaskFilters = {
  focus: false,
}

/**
 * Storage key, scoped to the admin id: one browser profile can be shared (see `taskExpandStorage.ts`).
 * `null` gets its own `anon` bucket. The string must stay unchanged so stored preferences survive releases.
 */
export function taskFiltersStorageKey(adminId: string | null): string {
  return `cockpit.tasks.filters:${adminId ?? 'anon'}`
}

/**
 * The stored filter set, or the default for anything missing or unusable.
 *
 * `getItem` and `JSON.parse` both sit inside the try: private-mode Safari and blocked storage throw on
 * ACCESS. Validation is per field (each falls back to its own default); extra keys from older payloads
 * (the retired `projectFilter`/`groupBy`) are ignored, which is the whole migration.
 */
export function loadTaskFilters(key: string): TaskFilters {
  const stored = readJsonObject('session', key)
  if (!stored) return DEFAULT_TASK_FILTERS
  return {
    // Strict boolean: a stored `"false"` is truthy. Falls back to OFF, the widest board.
    focus: typeof stored.focus === 'boolean' ? stored.focus : false,
  }
}

/**
 * Persist the view state; returns whether the write landed. Failing to remember must never cost the
 * operator the view itself.
 */
export function saveTaskFilters(key: string, filters: TaskFilters): boolean {
  return writeJson('session', key, filters)
}
