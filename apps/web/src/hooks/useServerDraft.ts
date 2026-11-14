import { type Dispatch, type SetStateAction, useCallback, useEffect, useRef, useState } from 'react'

/* A settings editor's draft of what the server holds (retention, residency, data regions, SIEM export, IP
 * allowlist): seeded from `data` through `toDraft`, and re-seeded by a refetch (window focus) only while nothing is
 * being edited, so a background read never overwrites typing. `edit` is the draft's setter that also marks it
 * edited and runs `onEdit` (the form's `clearSaved`). `commit(saved?)` ends the edit once a save landed: the draft
 * re-seeds from `saved` at once when given, else from the data the save wrote to the query. `reset()` is Cancel —
 * back to the last read. `editing` is state, for a control that follows it. `data` is `undefined` until read;
 * `null` is a read that found nothing. */
export function useServerDraft<S, D>(data: S | undefined, toDraft: (saved: S) => D, initial: D, onEdit?: () => void): {
  draft: D
  edit: Dispatch<SetStateAction<D>>
  editing: boolean
  commit: (saved?: S) => void
  reset: () => void
} {
  const [draft, setDraft] = useState<D>(initial)
  const [editing, setEditing] = useState(false)
  // `editing` is mirrored on the ref so a new read decides on the flag as it is, not as the effect's render saw it.
  const live = useRef({ toDraft, onEdit, editing: false })
  live.current.toDraft = toDraft
  live.current.onEdit = onEdit

  useEffect(() => {
    if (data !== undefined && !live.current.editing) setDraft(live.current.toDraft(data))
  }, [data])

  const edit = useCallback((next: SetStateAction<D>) => {
    live.current.editing = true
    setEditing(true)
    setDraft(next)
    live.current.onEdit?.()
  }, [])

  const commit = useCallback((saved?: S) => {
    live.current.editing = false
    setEditing(false)
    if (saved !== undefined) setDraft(live.current.toDraft(saved))
  }, [])

  const reset = useCallback(() => commit(data), [commit, data])

  return { draft, edit, editing, commit, reset }
}

/* The synchronous sibling, for a row the component already holds (a prop, a query's data) and seeds from DURING
 * RENDER: `seed` is what the server holds now, `draft` follows it while untouched — a draft still equal to the seed
 * it was taken from takes the new one; an edited draft keeps its edits, unless `force` (a published Live Cast's
 * settings are frozen: show what was published, never a stale local edit). `seed` returned is the one the draft was
 * taken from; `reseed(next)` hands over a save's answer as draft and seed alike. */
export function useSeededDraft<D>(seed: D, same: (a: D, b: D) => boolean, force = false): {
  draft: D
  setDraft: Dispatch<SetStateAction<D>>
  seed: D
  dirty: boolean
  reseed: (next: D) => void
} {
  const [draft, setDraft] = useState(seed)
  const [seen, setSeen] = useState(seed)
  if (!same(seed, seen)) {
    setSeen(seed)
    if (force || same(draft, seen)) setDraft(seed)
  }
  const reseed = (next: D) => {
    setDraft(next)
    setSeen(next)
  }
  return { draft, setDraft, seed: seen, dirty: !same(draft, seen), reseed }
}

/** `same` for a plain-data draft: equal by its JSON. */
export const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
