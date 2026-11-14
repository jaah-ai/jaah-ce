import { useEffect, useState } from 'react'

/* THE EDITOR AFTER A SAVE — for a detail page that mounts its editor under a `key` and remounts it on every write:
 * - A save remounts the editor at the saved row (`editorKey` carries a count bumped by every save), so its
 *   baseline is what the server now holds.
 * - `saved` says "Saved" on the row the last save landed on, and on that row only — WHICH row, not a flag: the pane
 *   keeps the page mounted as another row opens beside the list.
 * - A note for THIS visit: opening another row and coming back shows no stale "Saved" (`id`, the route's, resets
 *   it). A save the router then lands on in place (a create replacing New with the new row's address) keeps its note.
 * The page passes the route's id in, and the row's own id (its key of record) to each method. */
export function useSavedRow(id: string | null | undefined): {
  /** The editor's `key`: the row with its save count, or `fallback` for a create form (no save remounts one). */
  editorKey: (rowId: string | null | undefined, fallback?: string) => string
  /** Whether the last save landed on this row — the editor's "Saved" note. */
  saved: (rowId: string | null | undefined) => boolean
  /** A save landed on `rowId`: the editor remounts there and says so. */
  onSaved: (rowId: string | null | undefined) => void
  /** The editor remounts on the row as the server holds it, nothing said (a Discard, a retire). */
  reopen: () => void
} {
  const [version, setVersion] = useState(0)
  const [savedId, setSavedId] = useState<string | null>(null)
  useEffect(() => setSavedId(prev => (prev === id ? prev : null)), [id])

  const onSaved = (rowId: string | null | undefined) => {
    setSavedId(rowId ?? null)
    setVersion(v => v + 1)
  }
  return {
    editorKey: (rowId, fallback = 'new') => (rowId == null ? fallback : `${rowId}-${version}`),
    saved: rowId => rowId != null && savedId === rowId,
    onSaved,
    reopen: () => onSaved(null),
  }
}
