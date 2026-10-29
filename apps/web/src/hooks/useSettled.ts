import { useEffect, useState } from 'react'

/** The app's ONE settled-value hook — `value` held back until it has stopped changing for `ms` (the first
 * render settles at once). The sibling of `useDebouncedCallback` (`./useDebouncedCallback.ts`), for a value
 * rather than a callback. */
export function useSettled<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return settled
}
