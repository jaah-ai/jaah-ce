/* The console's one zone-aware absolute timestamp: an instant printed with its zone named.
 * Not `lib/relativeTime.ts::absoluteTime` (the deliberately zone-silent locale timestamp).
 * Instant → string only; day boundaries live in `lib/zonedDay.ts`.
 * A zone parameter because a cron schedule runs in one control-plane-wide zone: the cron
 * list renders in the viewer's zone, the detail page passes the control plane's.
 */

// Imported, not re-exported: callers read `NO_VALUE` from its one home.
import { NO_VALUE } from './relativeTime'

/* Components spelled out, not `dateStyle`/`timeStyle`: `Intl` throws a `TypeError` when those are
 * combined with `timeZoneName`. `hour12: false` pins the 24-hour clock in every locale. */
const PARTS = {
  year: 'numeric',
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZoneName: 'short',
} as const

/** What a caller may vary: precision only. */
export type ZonedTimeOptions = {
  /* Second precision for sub-minute subjects: a cron's next five firings at a 10s cadence share a minute. */
  seconds?: boolean
  /* Millisecond precision: a firing's fired/started/finished instants land within one second. Implies
  `seconds`. */
  millis?: boolean
}

/**
 * `iso` as an absolute date-time in `zone`, always with the zone's short name: `Mar 08, 2026, 03:00 EDT`.
 * `zone` is IANA; omitted, the viewer's. An unrecognised zone falls back to the viewer's rather than
 * throwing (it comes off the wire). A missing or unparseable `iso` yields `NO_VALUE`, never a throw.
 */
export function formatZoned(
  iso: string | null | undefined,
  zone?: string,
  options: ZonedTimeOptions = {},
): string {
  if (!iso) return NO_VALUE
  const ms = Date.parse(iso)
  if (Number.isNaN(ms)) return NO_VALUE
  const millis = options.millis === true
  return formatter(zone, millis || options.seconds === true, millis).format(new Date(ms))
}

/**
 * A start/end pair for a narrow line, in `zone` (as `formatZoned`), each instant unbreakable (NBSP) so a wrap
 * falls between the two facts, never inside one: `Oct 18, 14:03` / `14:20 EDT`. Shorter than two
 * `formatZoned` calls by what the pair makes redundant: the year when it is `now`'s, the end's date when
 * it is the start's day, and the start's zone when the end names the same one. A missing or unparseable
 * instant yields `NO_VALUE` for that half; `end` is `null` when `endIso` is.
 */
export function formatZonedSpan(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
  zone?: string,
  now: Date = new Date(),
): { start: string; end: string | null } {
  const start = zonedParts(startIso, zone)
  const end = endIso == null ? null : zonedParts(endIso, zone)
  const year = zonedParts(now.toISOString(), zone)?.year
  const date = (p: Record<string, string>) => `${p.month} ${p.day}${p.year === year ? '' : `, ${p.year}`}`
  const clock = (p: Record<string, string>) => `${p.hour}:${p.minute}`
  const nbsp = (text: string) => text.replace(/ /g, ' ')
  if (!start) return { start: NO_VALUE, end: endIso == null ? null : end ? nbsp(formatZoned(endIso, zone)) : NO_VALUE }
  if (!end) {
    return {
      start: nbsp(`${date(start)}, ${clock(start)} ${start.timeZoneName}`),
      end: endIso == null ? null : NO_VALUE,
    }
  }
  const oneZone = start.timeZoneName === end.timeZoneName
  const sameDay = oneZone && date(start) === date(end)
  return {
    start: nbsp(`${date(start)}, ${clock(start)}${oneZone ? '' : ` ${start.timeZoneName}`}`),
    end: nbsp(`${sameDay ? '' : `${date(end)}, `}${clock(end)} ${end.timeZoneName}`),
  }
}

/* `formatZoned`'s components by name; `null` for a missing or unparseable instant. */
function zonedParts(iso: string | null | undefined, zone?: string): Record<string, string> | null {
  if (!iso) return null
  const ms = Date.parse(iso)
  if (Number.isNaN(ms)) return null
  return Object.fromEntries(formatter(zone, false).formatToParts(new Date(ms)).map(p => [p.type, p.value]))
}

/* The locale is pinned to `en-US`, here only: the locale decides how the zone is spelled (`EST` vs
 * `GMT-5`), which is this module's whole point, so tests and readers need one stable spelling.
 * `month: 'short'` removes the day/month ambiguity. */
function formatter(
  zone: string | undefined,
  seconds: boolean,
  millis = false,
): Intl.DateTimeFormat {
  const withSeconds = seconds ? { ...PARTS, second: '2-digit' as const } : PARTS
  // `fractionalSecondDigits` is a component option, under the same `Intl` rule as above.
  const parts = millis ? { ...withSeconds, fractionalSecondDigits: 3 as const } : withSeconds
  try {
    return new Intl.DateTimeFormat('en-US', { ...parts, timeZone: zone })
  } catch {
    // `RangeError: Invalid time zone specified`; see `formatZoned`'s fallback.
    return new Intl.DateTimeFormat('en-US', parts)
  }
}

/* ── A filter bound on its way out ──
 * These parse the operator's `datetime-local` input into an API value, beside the display half so
 * both are decided in one place.
 */

/** An instant as ISO-8601 with an explicit offset; the audit and command-log APIs 422 a zoneless value.
 *  The local offset (not UTC) keeps the wire reading as the typed clock time. Milliseconds are carried
 *  because bounds are inclusive: truncation would withhold up to a second of the newest rows (matters
 *  for `isoWithOffset(new Date())`, not `datetime-local`'s whole minutes). */
export function isoWithOffset(date: Date): string {
  const pad = (n: number, width = 2) => String(Math.floor(Math.abs(n))).padStart(width, '0')
  const offsetMinutes = -date.getTimezoneOffset()
  const sign = offsetMinutes < 0 ? '-' : '+'
  const abs = Math.abs(offsetMinutes)
  return (
    `${pad(date.getFullYear(), 4)}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `.${pad(date.getMilliseconds(), 3)}${sign}${pad(abs / 60)}:${pad(abs % 60)}`
  )
}

/** A `datetime-local` value (zoneless wall clock) as an offset-carrying instant, in the viewer's local
 *  zone, which the page names beside the inputs.
 *  `null` = cleared (drop the key); `false` = unparseable (where `datetime-local` degrades to text), and
 *  the page must say so rather than show a range that was never applied. */
export function localInputToIso(value: string): string | null | false {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return false
  return isoWithOffset(parsed)
}

/** The viewer's own IANA zone; named on the page, never implied. */
export function viewerZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}
