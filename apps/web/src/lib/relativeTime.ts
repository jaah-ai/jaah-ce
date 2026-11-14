// The age formatters, one home: `relativeAge` is prose ("4m ago"), `compactAge` the fixed-slot cell
// form ("4m"); `absoluteTime` is the `title` both owe the reader. Do not hand-roll another.

import { countOf } from './humanize'

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** The same spans in ms, for `Date` arithmetic; the minute is `hooks/useNow::MINUTE_MS`, the clock's tick. */
export const HOUR_MS = HOUR * 1000
export const DAY_MS = DAY * 1000

/** The UTC calendar day (`YYYY-MM-DD`) of an instant, never the local one: UTC+13 would say tomorrow. */
export const utcDay = (at: string | number | Date): string => new Date(at).toISOString().slice(0, 10)

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
 *  time left); a zero reads `0m`, not `now`, because the caller's words carry the tense. `seconds` spells
 *  the sub-minute band as seconds (`45s`) where `0m` would say nothing — a run's time in `active`. */
export function compactSpan(ms: number, { seconds = false }: { seconds?: boolean } = {}): string {
  const clamped = Math.max(0, ms)
  if (seconds && clamped < 60_000) return `${Math.floor(clamped / 1000)}s`
  const minutes = Math.floor(clamped / 60_000)
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

/** The ONE short date: "Nov 3", or "Nov 3, 2027" outside the current year; `withTime` adds the clock
 *  ("Nov 3, 2:05 PM"). Local time, en-US. An unparseable date reads as itself. */
export function shortDate(iso: string, now: Date = new Date(), withTime = false): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const sameYear = date.getFullYear() === now.getFullYear()
  return date.toLocaleString('en-US', {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }), ...(withTime ? { hour: 'numeric', minute: '2-digit' } : {}),
  })
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

/** A span in ms at the precision it has, the scale stepping down with the value: `240ms`, `4.2s`, `42s`, `3m 5s`,
 *  `2h 10m`. Not `humanDuration` (whole seconds): cron firings settle in 120–400ms. Negative, NaN or infinite is
 *  not a measurable span: `NO_VALUE`. */
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

/** `to − from` as a `formatSpan`, or `NO_VALUE` when either end is missing/unparseable. A negative span (clock
 *  skew) is refused, not clamped to `0ms`. */
export function formatSpanBetween(from: string | null | undefined, to: string | null | undefined): string {
  if (!from || !to) return NO_VALUE
  const a = Date.parse(from)
  const b = Date.parse(to)
  return Number.isNaN(a) || Number.isNaN(b) ? NO_VALUE : formatSpan(b - a)
}

/** Seconds, split into the count and unit an operator reads it as. */
export type IntervalUnits = {
  /** Null when there is no interval yet (renders empty, not `0`). */
  count: number | null
  /** The unit's size in seconds: 1, 60, 3600 or 86400. */
  unit: number
}

/* Largest first: the BIGGEST unit dividing exactly (3600 -> "1 hour"). Anything else shows as seconds,
 * never rounded. */
const UNIT_SIZES = [DAY, HOUR, MINUTE, 1] as const

export function intervalUnits(seconds: number | null): IntervalUnits {
  if (seconds === null) return { count: null, unit: 1 }
  const unit = UNIT_SIZES.find(size => seconds % size === 0 && seconds >= size) ?? 1
  return { count: seconds / unit, unit }
}

const UNIT_WORDS: Record<number, string> = { [DAY]: 'day', [HOUR]: 'hour', [MINUTE]: 'minute', 1: 'second' }

/** A period as words, in its largest whole unit (`intervalUnits`): `10 seconds`, `1 hour`, `90 seconds`. */
export function periodWords(seconds: number): string {
  const { unit } = intervalUnits(seconds)
  return countOf(seconds / unit, UNIT_WORDS[unit])
}

/** A countdown as a clock, `3:05` for 185 seconds: ceilinged, never negative. */
export function countdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
