import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'

/**
 * `useState` seeded from storage and written back on change — the panes' layout preferences
 * (components/ConsoleDock.tsx, components/ListDetail.tsx). PERSISTENCE IS AN EFFECT, never inside an updater
 * (StrictMode double-invokes updaters). The seed is skipped by comparing the VALUE, not a first-run flag (a
 * flag would not survive StrictMode's re-invoke), so a setter returning the same value writes nothing.
 */
export function usePersistedState<T>(load: () => T, save: (value: T) => void): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState(load)
  const lastSaved = useRef(value)
  useEffect(() => {
    if (Object.is(lastSaved.current, value)) return
    lastSaved.current = value
    save(value)
  }, [value, save])
  return [value, setValue]
}
