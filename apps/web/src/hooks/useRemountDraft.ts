import { useCallback, useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'

/* The one draft in flight: the mounted editor's latest input, and which instance owns it. Module memory only
   (never storage): it holds what the editor's own state holds, for no longer than the editor is mounted. */
let held: { key: string; owner: symbol; draft: unknown } | null = null
/* The instances that committed. An adopting render React throws away never commits, so its draft is dropped. */
const mounted = new Set<symbol>()

/**
 * An editor's unsaved input, carried across the remount `components/ListDetail.tsx` causes when the viewport
 * crosses the docking width (the editor moves between the pane and the full page).
 *
 * That move unmounts the old instance and mounts the new one in ONE commit, at the same history entry. So the
 * new instance adopts the draft while it renders (before the old one's cleanup runs), and an instance that
 * unmounts with nobody adopting its draft drops it: leaving the page, or coming Back to it, starts clean.
 *
 * `restored` is the adopted draft (seed the editor's state from it), `null` on an ordinary mount. Call `keep`
 * from an effect with the current input.
 */
export function useRemountDraft<T>(): { restored: T | null; keep: (draft: T) => void } {
  const { key } = useLocation()
  const [owner] = useState(() => Symbol('draft owner'))
  // Idempotent under StrictMode's second initializer call: adopting twice changes nothing.
  const [restored] = useState<T | null>(() => {
    if (held === null || held.key !== key) return null
    held.owner = owner
    return held.draft as T
  })
  useEffect(() => {
    mounted.add(owner)
    return () => {
      mounted.delete(owner)
      if (held?.owner === owner) held = null
      // Adopted by a render that never committed: by the next task it would have.
      else setTimeout(() => {
        if (held !== null && !mounted.has(held.owner)) held = null
      })
    }
  }, [owner])
  const keep = useCallback((draft: T) => {
    held = { key, owner, draft }
  }, [key, owner])
  return { restored, keep }
}
