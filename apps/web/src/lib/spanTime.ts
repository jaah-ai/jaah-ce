/* A span between two instants at the precision it has. Separate from `relativeTime.ts::humanDuration`
 * (`wholeMinutes` floors to minutes) and `lib/poolSlot.ts::durationLabel` (hours/days): cron firings settle in
 * 120–400ms. Scale steps down with the value: `240ms`, `4.2s`, `42s`, `3m 5s`, `2h 10m`. */
import { NO_VALUE } from './relativeTime'

/** `to − from` as a span, or `NO_VALUE` when either end is missing/unparseable. A negative span (clock
 * skew) is refused, not clamped to `0ms`. */
export function formatSpanBetween(
  from: string | null | undefined,
  to: string | null | undefined,
): string {
  if (!from || !to) return NO_VALUE
  const a = Date.parse(from)
  const b = Date.parse(to)
  if (Number.isNaN(a) || Number.isNaN(b)) return NO_VALUE
  const ms = b - a
  return ms < 0 ? NO_VALUE : formatSpan(ms)
}

/** A span already in milliseconds. */
export function formatSpan(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return NO_VALUE
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = ms / 1000
  // Round before banding: `toFixed(1)` on 9.96 yields `10.0` in the one-decimal band.
  if (Math.round(seconds * 10) / 10 < 10) return `${(Math.round(seconds * 10) / 10).toFixed(1)}s`
  const wholeSeconds = Math.round(seconds)
  if (wholeSeconds < 60) return `${wholeSeconds}s`
  const minutes = Math.floor(wholeSeconds / 60)
  if (minutes < 60) return `${minutes}m ${wholeSeconds % 60}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}
