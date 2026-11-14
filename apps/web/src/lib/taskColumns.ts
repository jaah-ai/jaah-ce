/* THE `/tasks` BOARD'S OPTIONAL COLUMNS — the Filter & view menu's Columns page. Each key is a
 * `FeedSlot.key` (or `id`, the row's lead) built by `components/tasks/listViews/feedRow.tsx`; the title and the
 * state disc are not optional. Hidden keys are stored per user, server-side, as `task_hidden_columns`
 * (`apps/api/app/preferences.py`), so the choice follows the person to every device. A column hidden here is
 * gone at every width; one shown still drops at the widths its gate names. React-free. */

export type TaskColumn = { key: string; label: string }

/* Display order: the row's own order, left to right. `org` is offered only to whoever sees it. */
export const TASK_COLUMNS: readonly TaskColumn[] = [
  { key: 'id', label: 'ID' },
  { key: 'persona', label: 'Persona' },
  { key: 'rating', label: 'Rating' },
  { key: 'tokens', label: 'Tokens' },
  { key: 'cost', label: 'Cost' },
  { key: 'people', label: 'Creator → Assignee' },
  { key: 'org', label: 'Organization' },
  { key: 'age', label: 'Age' },
]

/** The preference key, named once so the writer and the readers cannot drift apart. */
export const HIDDEN_COLUMNS_PREF = 'task_hidden_columns'

const KNOWN = new Set(TASK_COLUMNS.map(column => column.key))

/** The stored hidden keys; anything not a known column key is ignored (a newer release's key, a typo). */
export function readHiddenColumns(preferences: Record<string, unknown> | null | undefined): string[] {
  const raw = preferences?.[HIDDEN_COLUMNS_PREF]
  return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === 'string' && KNOWN.has(v)) : []
}

/** `hidden` with `key` shown or hidden, in display order (equal choices store equal documents). */
export function toggledHiddenColumns(hidden: readonly string[], key: string): string[] {
  const next = new Set(hidden)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  return TASK_COLUMNS.map(column => column.key).filter(k => next.has(k))
}
