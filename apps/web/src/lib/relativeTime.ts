// The age formatters, one home: `relativeAge` is prose ("4m ago"), `compactAge` the fixed-slot cell
// form ("4m"); `absoluteTime` is the `title` both owe the reader. Do not hand-roll another.

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** Rendered when there is no timestamp at all — the app's established "no value" glyph. */
export const NO_VALUE = '—'

/**
 * `iso` as an age relative to `now`: `now`, `42s ago`, `4m ago`, `3h ago`, `9d ago`. For prose or a slot
 * sized for words; use `compactAge` in a fixed `FeedSlot` (the day band is unbounded).
 * A future timestamp reads `now` (clock drift); unparseable yields `NO_VALUE`, never `NaN ago`.
 */
export function relativeAge(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return NO_VALUE
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return NO_VALUE
  const seconds = Math.floor((now.getTime() - then) / 1000)
  if (seconds < 10) return 'now'
  if (seconds < MINUTE) return `${seconds}s ago`
  if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)}m ago`
  if (seconds < DAY) return `${Math.floor(seconds / HOUR)}h ago`
  return `${Math.floor(seconds / DAY)}d ago`
}

/**
 * Units only — `now`, `20m`, `23h`, `9d`, `364d`, `2y` — for `ui/feed/geometry.ts::AGE_SLOT`. At 11px
 * mono (~6.6px/char) ` ago` costs 26px and clips a 44px slot; this form is bounded at four characters
 * because days roll to years.
 * One unit, truncated (119 minutes → `1h`), so a row never reads staler than it is. Under a minute is
 * `now`, not `0m`. Absent/malformed → `NO_VALUE`. Pair it with `absoluteTime` on `title`.
 */
export function compactAge(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE
  const ms = Date.parse(iso)
  if (Number.isNaN(ms)) return NO_VALUE
  const age = Date.now() - ms
  return age < MINUTE * 1000 ? 'now' : compactSpan(age)
}

/** `compactAge`'s units for a span already in ms, either direction of `now` (a quota's age, a login's
 *  time left); a zero reads `0m`, not `now`, because the caller's words carry the tense. */
export function compactSpan(ms: number): string {
  const minutes = Math.floor(Math.max(0, ms) / 60_000)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  return days < 365 ? `${days}d` : `${Math.floor(days / 365)}y`
}

/** Full locale timestamp, zone-silent: a `relativeAge`/`compactAge` cell's `title`, or a stamp of its own. */
export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? NO_VALUE : at.toLocaleString()
}

/** Absolute date and minute (`Oct 13, 2026, 02:05 PM`), for a stamp shown as-is. Em-dash for null; the raw
 *  string for one that will not parse. */
export function fmtTimestamp(iso: string | null): string {
  if (!iso) return NO_VALUE
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

/** The short stamp for a narrow strip: the time today (`10:02 AM`), the day this year (`Jun 19`), else the day
 *  and year (`Jun 19, 2025`). Pair it with `fmtTimestamp` on `title`. Em-dash for null; raw for unparseable. */
export function briefTimestamp(iso: string | null, now: Date = new Date()): string {
  if (!iso) return NO_VALUE
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric',
  })
}

/** The ONE clock-only stamp: `HH:MM` (`HH:MM:SS` with `seconds`), viewer's locale and zone, 24-hour so a tick,
 *  a bubble and a notice agree; `h23: false` takes the locale's own cycle instead, to sit beside a `fmtTimestamp`.
 *  Date-bearing stamps are `fmtTimestamp`/`formatZoned`; do not hand-roll another. */
export function clockTime(iso: string, { seconds = false, h23 = true } = {}): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return NO_VALUE
  return d.toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit', second: seconds ? '2-digit' : undefined, hourCycle: h23 ? 'h23' : undefined,
  })
}

/** Date only, in the viewer's locale; an unparseable value is echoed back as given. */
export function shortDay(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleDateString()
}

/** A date-only reading ("Oct 13, 2026") of a UTC calendar day or an instant, read in UTC; a bare
 *  `YYYY-MM-DD` is that day. Absent → `NO_VALUE`; unparseable is echoed back as given. */
export function dayLabel(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
}

/** Seconds as at most two units, a zero second unit dropped: `42s`, `4m 12s`, `1h`, `2h 5m`, `3d 4h`.
 *  `wholeMinutes` floors to minutes and keeps the zero (`<1m`, `4m`, `1h 0m`). Negative clamps to zero. */
export function humanDuration(seconds: number, wholeMinutes = false): string {
  if (wholeMinutes && seconds > 0 && seconds < 60) return '<1m'
  const s = Math.max(0, Math.floor(seconds))
  const m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24)
  if (d || h) {
    const [big, small] = d ? [`${d}d`, `${h % 24}h`] : [`${h}h`, `${m % 60}m`]
    return wholeMinutes || small[0] !== '0' ? `${big} ${small}` : big
  }
  if (m || wholeMinutes) return s % 60 && !wholeMinutes ? `${m}m ${s % 60}s` : `${m}m`
  return `${s}s`
}

/** `humanDuration` as an age the server counted (`4m 12s ago`). `null` is `never`, NOT `—`: a never-written
 *  clock is a fact the server asserts (worse than late), whereas `—` means "no value". */
export function humanAge(seconds: number | null | undefined): string {
  return seconds === null || seconds === undefined ? 'never' : `${humanDuration(seconds)} ago`
}
