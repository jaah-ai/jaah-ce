/* Calendar-day arithmetic in a named IANA zone — the operator's day (`operator_settings.cron_timezone`,
 * via `lib/officialZone.ts`), used by `components/tasks/listViews/rows.ts` and `lib/taskDateRange.ts`.
 * Separate from `lib/zonedTime.ts` (formatting only): this produces instants, so DST bugs live here.
 * Wall clock → instant has no browser primitive, so it iterates: read the wall clock as UTC, subtract the
 * offset at that instant, re-read the offset at the corrected instant, subtract again. Two passes are
 * exact for every IANA zone. `zoneOffsetMs` is the only `formatToParts` caller.
 * Days are never added in milliseconds (DST); `addZonedDays` moves the day number and re-solves.
 * Some zones spring forward at 00:00 (Santiago, Havana, Azores, Beirut, Cairo): the two-step lands on
 * 23:00 of the previous day west of Greenwich — a wrong day. `instantOfWallClock` round-trips and snaps
 * forward to the first real instant; every caller solves for a day start, and `endOfZonedDay` derives
 * from the next day's start. The configured choices are unaffected, but the fallback is the viewer's
 * zone, which may be Santiago. */

/* Memoised per zone. Unbounded is safe: keys are the configured zone (closed server-side choices), the
   viewer's, and 'UTC'. */
const FORMATTERS = new Map<string, Intl.DateTimeFormat>()

/* `hourCycle: 'h23'`, not `hour12: false`: the latter can yield hour "24" for midnight under some ICU
   builds, shifting day starts. `en-US` is defensive (only numbers are read). */
function partsFormatter(zone: string): Intl.DateTimeFormat {
  const cached = FORMATTERS.get(zone)
  if (cached !== undefined) return cached
  const options: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }
  let formatter: Intl.DateTimeFormat
  try {
    formatter = new Intl.DateTimeFormat('en-US', { ...options, timeZone: zone })
  } catch {
    // Unknown zone in this ICU build: fall back to the viewer's day boundary, never throw in render.
    formatter = new Intl.DateTimeFormat('en-US', options)
  }
  FORMATTERS.set(zone, formatter)
  return formatter
}

/** The wall clock `instant` reads as in `zone`. */
type WallClock = { year: number; month: number; day: number; hour: number; minute: number; second: number }

function wallClock(instant: Date, zone: string): WallClock {
  const parts = partsFormatter(zone).formatToParts(instant)
  const read = (type: Intl.DateTimeFormatPartTypes): number => {
    const part = parts.find(candidate => candidate.type === type)
    return part === undefined ? 0 : Number(part.value)
  }
  return {
    year: read('year'),
    month: read('month'),
    day: read('day'),
    hour: read('hour'),
    minute: read('minute'),
    second: read('second'),
  }
}

/** `zone`'s offset from UTC in ms at `instant` (positive east). Floored to the second: the wall clock
 *  has no sub-second part. */
function zoneOffsetMs(instant: Date, zone: string): number {
  const clock = wallClock(instant, zone)
  const asUtc = Date.UTC(clock.year, clock.month - 1, clock.day, clock.hour, clock.minute, clock.second)
  return asUtc - Math.floor(instant.getTime() / 1000) * 1000
}

/** Wall time → instant in `zone`, by the two-step plus the gap probe (file header). */
function instantOfWallClock(clock: WallClock, ms: number, zone: string): Date {
  const target = Date.UTC(clock.year, clock.month - 1, clock.day, clock.hour, clock.minute, clock.second, ms)
  const firstOffset = zoneOffsetMs(new Date(target), zone)
  const secondOffset = zoneOffsetMs(new Date(target - firstOffset), zone)
  const solved = new Date(target - secondOffset)
  /* Round trip = gap test. A mismatch means the clock is in a spring-forward gap; return the first
     instant after it (`Math.min` of the offsets gives the later instant). */
  const readBack = wallClock(solved, zone)
  const exists =
    readBack.year === clock.year &&
    readBack.month === clock.month &&
    readBack.day === clock.day &&
    readBack.hour === clock.hour &&
    readBack.minute === clock.minute &&
    readBack.second === clock.second
  if (exists) return solved
  return new Date(target - Math.min(firstOffset, secondOffset))
}

/** Midnight of the day `instant` falls in, in `zone`. */
export function startOfZonedDay(instant: Date, zone: string): Date {
  const clock = wallClock(instant, zone)
  return instantOfWallClock({ ...clock, hour: 0, minute: 0, second: 0 }, 0, zone)
}

/** The last instant of `instant`'s day in `zone`: one ms before the next day starts. Asking for
 *  23:59:59.999 fails when a day springs forward at 23:00 (no such time) or falls back at midnight
 *  (it occurs twice). */
export function endOfZonedDay(instant: Date, zone: string): Date {
  return new Date(addZonedDays(instant, 1, zone).getTime() - 1)
}

/** Midnight `days` calendar days from `instant`'s day, in `zone`. `Date.UTC` normalises month/year
 *  rollover. */
export function addZonedDays(instant: Date, days: number, zone: string): Date {
  const clock = wallClock(instant, zone)
  const rolled = new Date(Date.UTC(clock.year, clock.month - 1, clock.day + days))
  return instantOfWallClock(
    {
      year: rolled.getUTCFullYear(),
      month: rolled.getUTCMonth() + 1,
      day: rolled.getUTCDate(),
      hour: 0,
      minute: 0,
      second: 0,
    },
    0,
    zone,
  )
}

/** Midnight of the Monday opening `instant`'s week in `zone`. Monday by choice (a working week); not
 *  `Intl.Locale.weekInfo`, so all operators read one board the same. */
export function startOfZonedWeek(instant: Date, zone: string): Date {
  const clock = wallClock(instant, zone)
  // Weekday of the date itself (UTC-constructed); `(dow + 6) % 7` is days since Monday.
  const dayOfWeek = new Date(Date.UTC(clock.year, clock.month - 1, clock.day)).getUTCDay()
  return addZonedDays(instant, -((dayOfWeek + 6) % 7), zone)
}

/** `YYYY-MM-DD` as midnight in `zone`, or `null` if not a real date (validity from `calendarDate`). */
export function zonedDayFromInput(value: string, zone: string): Date | null {
  const parsed = calendarDate(value)
  if (parsed === null) return null
  return instantOfWallClock({ ...parsed, hour: 0, minute: 0, second: 0 }, 0, zone)
}

/** `YYYY-MM-DD` as the day it names, or `null` — no zone involved. Used by
 *  `taskDateRange.ts::isDateInput`; `zonedDayFromInput` wraps it so the two cannot disagree.
 *  The round trip rejects impossible days (`Date.UTC(2026, 1, 31)` is 3 March). */
export function calendarDate(value: string): { year: number; month: number; day: number } | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const normalised = new Date(Date.UTC(year, month - 1, day))
  if (
    normalised.getUTCFullYear() !== year ||
    normalised.getUTCMonth() !== month - 1 ||
    normalised.getUTCDate() !== day
  ) {
    return null
  }
  return { year, month, day }
}

/** `instant` as `YYYY-MM-DDTHH:mm:ss.sss±HH:MM` in `zone`'s wall clock with its offset — not UTC, so
 *  the query param reads as the operator's midnight. Milliseconds carried: the upper bound is inclusive
 *  (`endOfZonedDay`). */
export function zonedIso(instant: Date, zone: string): string {
  const clock = wallClock(instant, zone)
  /* Offset rounded to whole minutes (ISO-8601 cannot spell finer). A half-typed year like `0202` hits
     pre-1900 LMT offsets (-04:56:02), which unrounded the server's `fromisoformat` rejects. Sign is read
     from the rounded value, so no `-00:00`. */
  const offsetMinutes = Math.round(zoneOffsetMs(instant, zone) / 60_000)
  const sign = offsetMinutes < 0 ? '-' : '+'
  const abs = Math.abs(offsetMinutes)
  const date = `${pad(clock.year, 4)}-${pad(clock.month)}-${pad(clock.day)}`
  // `getTime() % 1000`, not `getMilliseconds()` (a host-clock read). `+ 1000) % 1000` handles pre-1970.
  const millis = ((instant.getTime() % 1000) + 1000) % 1000
  const time = `${pad(clock.hour)}:${pad(clock.minute)}:${pad(clock.second)}.${pad(millis, 3)}`
  return `${date}T${time}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0')
}
