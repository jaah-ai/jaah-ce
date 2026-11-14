/* A USER'S PINNED TASKS — "Pin" / "Unpin" on a task's ⋯ (the board row and the task page), stored server-side as
 * `task_pinned` (`apps/api/app/preferences.py`), a list of top-level task ids. This user's alone.
 *
 * THE RULE: a pinned task sits at the top of whatever section the board draws it in (a State section, a project, a
 * tag, a recency bucket, or the one flat list), whatever the order; the rest keep theirs. The server orders the
 * board page the same way (`routes/tasks/listing.py::board_page`), so a pinned row is in the first window; this
 * module lifts it within its section once the rows are drawn. A pin outlives Done and archive. Pure of React. */

/** The preference key, named once so the writers and the readers cannot drift apart. */
export const PINNED_PREF = 'task_pinned'

/** Fired on `window` once a pin change is SAVED, so the board re-reads its rows in the server's new order (a row
 *  just unpinned would otherwise keep the top it was served at). From either surface: the task page sits beside the
 *  board. */
export const TASK_PINS_SAVED = 'jaah:task-pins-saved'

/** The stored pins; anything that is not a list of positive integers reads as none. */
export function readPinned(preferences: Record<string, unknown> | null | undefined): number[] {
  const raw = preferences?.[PINNED_PREF]
  return Array.isArray(raw) ? raw.filter((v): v is number => Number.isInteger(v) && v > 0) : []
}

/** `pinned` with `id` added (last: the newest pin survives the server's cap) or removed. */
export function toggledPinned(pinned: readonly number[], id: number): number[] {
  return pinned.includes(id) ? pinned.filter(v => v !== id) : [...pinned, id]
}

/** `items` with the pinned ones first. STABLE: each half keeps its incoming order. */
export function pinnedFirst<T>(items: readonly T[], isPinned: (item: T) => boolean): T[] {
  const pinned: T[] = []
  const rest: T[] = []
  for (const item of items) (isPinned(item) ? pinned : rest).push(item)
  return pinned.length === 0 ? [...items] : [...pinned, ...rest]
}
