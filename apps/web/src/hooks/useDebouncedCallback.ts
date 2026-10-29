import { useEffect, useRef } from 'react'

// Trailing-edge debounce: a burst of triggers runs the callback once after `delayMs` of quiet, so the
// last trigger is never dropped. `maxWaitMs` forces a flush under a never-quiet stream. The callback is
// read from a ref at fire time (latest closure). Timers cancel on unmount.
// For a settled VALUE rather than a callback, `useSettled` (`./useSettled.ts`) is the sibling.
export function useDebouncedCallback(
  callback: () => void,
  delayMs: number,
  maxWaitMs?: number,
): { trigger: () => void; cancel: () => void } {
  const callbackRef = useRef(callback)
  callbackRef.current = callback

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const maxTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  function cancel() {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (maxTimerRef.current) {
      clearTimeout(maxTimerRef.current)
      maxTimerRef.current = null
    }
  }

  function fire() {
    cancel()
    callbackRef.current()
  }

  function trigger() {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(fire, delayMs)
    // Max-wait arms once per burst and survives debounce resets.
    if (maxWaitMs !== undefined && maxTimerRef.current === null) {
      maxTimerRef.current = setTimeout(fire, maxWaitMs)
    }
  }

  useEffect(() => cancel, [])

  return { trigger, cancel }
}
