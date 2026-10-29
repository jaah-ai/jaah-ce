import { createExternalStore, useExternalStore } from './externalStore'
import { readJson, writeJson } from './safeStorage'

/* THE RAIL'S HIGHLIGHT: the `scopeKey` (`lib/taskScope.ts`) of the scope the Tasks board is showing, published
 * by the board (`pages/Tasks.tsx`) so the rail can light that node. `null` = no board mounted.
 *
 * The rail STEERS the board through the URL (`?scope=`, `taskScope.ts::scopeHref`), which is the board's
 * source of truth — so this store is read-only for everyone but the board.
 *
 * THE LAST SCOPE: every non-null publish is also remembered for the tab (`sessionStorage`, so a reload
 * of the task page keeps it), and the task editor returns its Save/Go to that scope rather than the task's own
 * project (`pages/taskDetail/useTaskDetailActions.tsx::backToList`). A fresh tab has none. */

const LAST_SCOPE_KEY = 'cockpit.tasks.lastScope'

const current = createExternalStore<string | null>(null)

export function publishBoardScope(key: string | null): void {
  if (key === current.get()) return
  if (key !== null) writeJson('session', LAST_SCOPE_KEY, key)
  current.set(key)
}

export function useBoardScope(): string | null {
  return useExternalStore(current)
}

/** The scope this tab's board last showed, or `null` when it has shown none (or storage is unreadable). */
export function lastBoardScope(): string | null {
  const stored = readJson('session', LAST_SCOPE_KEY)
  return typeof stored === 'string' && stored !== '' ? stored : null
}
