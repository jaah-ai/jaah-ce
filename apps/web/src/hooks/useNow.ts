import { useEffect, useState } from 'react'

/** The clock behind output that is a function of the current time. One minute because `compactAge` only
 *  changes on minute boundaries; a caller whose output moves faster (a live runner-phase duration) passes
 *  its own interval. */
export const MINUTE_MS = 60_000

/* `Date.now()`, re-read every `intervalMs`; frozen at its last reading while `intervalMs` is null. Starting (or
 * restarting) the clock re-reads it first, so a reading frozen through a pause never shows past the switch-on.
 * The console is otherwise event-driven, so a quiet page would freeze a relative age at a wrong value; a
 * caller that only needs the re-render ignores the value. */
export function useNow(intervalMs: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (intervalMs === null) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(timer)
  }, [intervalMs])
  return now
}
