// Layout persistence, split from the pure `consoleLayout.ts`. `localStorage` is the whole store (no
// API/migration): a layout is PER BROWSER and not synced across tabs until re-read.
import { parseLayoutModel, type LayoutModel } from './consoleLayout'
import { readJson, writeJson } from './safeStorage'

/** Keyed by kind too: "Web Console" and "Web Term" are separate sessions on the same worktree. */
export function layoutStorageKey(project: string, number: number, kind: 'clauded' | 'term'): string {
  return `console-layout:${project}/${number}/${kind}`
}

/**
 * Stored layout, or null when absent, unreadable, or invalid (`parseLayoutModel`; a `version` other than 1
 * is DISCARDED, not migrated). Checking that stored `@id`s still name live windows is the console
 * runtime's job — this has no window list.
 */
export function loadLayout(key: string): LayoutModel | null {
  try {
    return parseLayoutModel(readJson('local', key))
  } catch {
    // `parseNode` recurses unbounded, so an absurdly deep tree is a RangeError — treat as unsaved.
    return null
  }
}

/** Returns whether it was written; failing to persist must never block applying the layout. */
export function saveLayout(key: string, model: LayoutModel): boolean {
  return writeJson('local', key, model)
}
