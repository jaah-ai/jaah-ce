import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react'

/** The console's ONE commit-on-blur inline editor (the slot Title/Description row in `components/WorktreePool.tsx`
 *  and the Console header's Title in `components/ConsoleTerminal.tsx`): Enter commits, Escape cancels, blur
 *  commits. Rendering is the caller's; this holds the state machine only.
 *
 *  `exitHandled` stops the blur that follows Enter/Escape from writing again. Untouched in, untouched out: a value
 *  unchanged from `open(seed)` is not sent (trimmed on both sides — blanks-only is a no-op, an intentional clear
 *  reaches `onSave` as `''`), so a mis-click cannot freeze a CP-composed Title as an operator title. An open
 *  editor does not outlive the capability that opened it (`canEdit` dropping closes it, and a blur that lands
 *  before that effect runs is refused the same way); presentation only — the route is the boundary. */
export function useCommitOnBlurEdit({ canEdit, onSave }: { canEdit: boolean; onSave: (value: string) => void }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const exitHandled = useRef(false)
  const openedWith = useRef('')

  useEffect(() => {
    if (!canEdit) setEditing(false)
  }, [canEdit])

  function open(seed: string) {
    openedWith.current = seed
    setDraft(seed)
    exitHandled.current = false
    setEditing(true)
  }

  // Stable: a caller resets it from an effect keyed on something else (the Console header's slot change).
  const close = useCallback(() => setEditing(false), [])

  function commit() {
    setEditing(false)
    // The awaited half: a blur can land before the `canEdit` effect runs.
    if (!canEdit) return
    const next = draft.trim()
    if (next !== openedWith.current.trim()) onSave(next)
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter') { exitHandled.current = true; commit() }
    /* Escape stopped here: a surrounding MUI `Modal` (the pool drawer) would otherwise close on a field cancel;
       with no field open it keeps Escape. */
    else if (e.key === 'Escape') { e.stopPropagation(); exitHandled.current = true; setEditing(false) }
  }

  function onBlur() {
    if (exitHandled.current) { exitHandled.current = false; return }
    commit()
  }

  return { editing, draft, setDraft, open, close, onKeyDown, onBlur }
}
