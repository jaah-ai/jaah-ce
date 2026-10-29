import { useEffect, useState } from 'react'

// A once-a-minute re-render for output that is a function of the current time (`compactAge`). The board
// is otherwise event-driven, so an untouched board would freeze a relative age at a wrong value.
// One minute because `compactAge` only changes on minute boundaries; a caller whose output moves faster
// (a live runner-phase duration) passes `intervalMs`. The returned counter is an opaque render token.
export function useMinuteTick(intervalMs = 60_000): number {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setTick(current => current + 1), intervalMs)
    return () => clearInterval(timer)
  }, [intervalMs])
  return tick
}

/* `Date.now()`, re-read every `intervalMs`; frozen at its last reading while `intervalMs` is null. Starting (or
 * restarting) the clock re-reads it first, so a reading frozen through a pause never shows past the switch-on. */
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
