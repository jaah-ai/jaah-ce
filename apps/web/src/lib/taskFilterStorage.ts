// Persistence for the /tasks board's filters. Pure of React: the page owns WHEN to read/write, this module owns what
// a well-formed stored value is.
//
// The board's filters live in the URL — the selection (`?scope=`), the row-removing filters (`lib/taskUrlFilters.ts`)
// and the stage strip's pick (`?stage=`/`?only=`, `lib/pipelineBoard.ts`) — so a link reproduces them. This module
// REMEMBERS that URL state per selection, in this browser (`rememberedView`): opening a selection with none of
// those params in its URL puts back the ones it last had, search included. Focus is not a URL param:
// it is remembered per tab for the session, off on a fresh tab (Task 90: "within the session").

import { adminScopedKey, readJsonObject, writeJson } from './safeStorage'
import { OWNED_PARAMS } from './taskUrlFilters'
import { ONLY_PARAM, STAGE_PARAM } from './pipelineBoard'

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

/** Storage key, per admin (`adminScopedKey`). */
export function taskFiltersStorageKey(adminId: string | null): string {
  return adminScopedKey('cockpit.tasks.filters', adminId)
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

/** The URL params a selection remembers: the row filters and the stage strip's pick. */
const REMEMBERED_PARAMS: readonly string[] = [...OWNED_PARAMS, STAGE_PARAM, ONLY_PARAM]

/** Storage key for one selection's remembered filters (`?scope=` as written; `all` for All projects), per admin. */
export function rememberedViewKey(adminId: string | null, scope: string | null): string {
  return adminScopedKey(`cockpit.tasks.view.${scope ?? 'all'}`, adminId)
}

function rememberedOf(params: URLSearchParams): Record<string, string[]> {
  const kept: Record<string, string[]> = {}
  for (const name of REMEMBERED_PARAMS) {
    const values = params.getAll(name)
    if (values.length > 0) kept[name] = values
  }
  return kept
}

/** The remembered part of `params` as one string: equal strings, nothing new to remember. */
export function rememberedFingerprint(params: URLSearchParams): string {
  return JSON.stringify(rememberedOf(params))
}

/** Remember the filters `params` carries (an empty set too: cleared filters stay cleared). */
export function saveRememberedView(key: string, params: URLSearchParams): boolean {
  return writeJson('local', key, rememberedOf(params))
}

/**
 * `params` with the selection's remembered filters put back, or `null` when nothing changes. Landing (`switching`
 * false): a URL that already names a filter is left as it is (a shared link wins). Switching selection: the filters
 * carried from the last selection are replaced by this one's remembered ones — none, for a selection never filtered
 * here (each selection keeps its own; a stage id is one project's anyway).
 */
export function restoreRememberedView(
  key: string, params: URLSearchParams, { switching = false }: { switching?: boolean } = {},
): URLSearchParams | null {
  if (!switching && REMEMBERED_PARAMS.some(name => params.has(name))) return null
  const stored = readJsonObject('local', key)
  if (!stored && !switching) return null
  const next = new URLSearchParams(params)
  for (const name of REMEMBERED_PARAMS) next.delete(name)
  if (stored) {
    for (const name of REMEMBERED_PARAMS) {
      const values = stored[name]
      if (!Array.isArray(values)) continue
      for (const value of values) if (typeof value === 'string') next.append(name, value)
    }
  }
  return next.toString() === params.toString() ? null : next
}

/** `restoreRememberedView` for a landing whose link pins a stage (a return from the task editor's Save,
 *  `lib/taskReturn.ts`): the rest of the remembered filters come back, the pinned stage stays (and the remembered
 *  step pick, which named another stage, goes). */
export function restoreKeepingStage(
  key: string, params: URLSearchParams, stage: string, switching: boolean,
): URLSearchParams | null {
  const unpinned = new URLSearchParams(params)
  unpinned.delete(STAGE_PARAM)
  const next = new URLSearchParams(restoreRememberedView(key, unpinned, { switching }) ?? unpinned)
  next.set(STAGE_PARAM, stage)
  next.delete(ONLY_PARAM)
  return next.toString() === params.toString() ? null : next
}
