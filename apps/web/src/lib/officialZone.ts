/* The control plane's own zone (`operator_settings.cron_timezone`), the one answer to "which zone?" for
 * the `/tasks` board's recency buckets and date range, plus "has the day turned?" (`useOfficialDayStart`).
 * It arrives as `admin.timezone` on `GET /api/auth/me`, resolved server-side (an unresolvable name is
 * already UTC), not via `GET /api/settings`, which needs `system:view` that ordinary readers lack
 * (`components/tasks/taskDefaults.ts`).
 * Fallback is the viewer's own zone, not UTC (old snapshot, first paint, missing field): UTC would
 * silently re-cut the board mid-evening. `zone` is a dependency of the recency memo and both fetch
 * effects, so the board re-cuts when `me` lands.
 */
import { useEffect, useState } from 'react'
import { useAuth } from '../auth'
import { endOfZonedDay, startOfZonedDay } from './zonedDay'
import { viewerZone } from './zonedTime'

/** The payload's value, fenced — or the viewer's zone. A type and non-empty check, not a coercion:
 *  `String(undefined)` is `'undefined'`. Not validated against IANA; `zonedDay.ts`'s formatter degrades
 *  an unconstructable zone itself. */
export function resolveOfficialZone(raw: unknown): string {
  return typeof raw === 'string' && raw !== '' ? raw : viewerZone()
}

/** The zone every day boundary on `/tasks` is cut on. */
export function useOfficialZone(): string {
  const { admin } = useAuth()
  return resolveOfficialZone(admin?.timezone)
}

/** When the current day started in `zone`, as a value that changes when midnight passes. A hook
 *  because consumers (the feed's `sections` memo, the board's fetch effects) only re-run on input
 *  change, so an open board would keep yesterday's "Today". It is a dependency only: `bucketByRecency`
 *  and `resolveDateRangeBounds` read the real clock themselves.
 *  One timer armed for the next boundary via `endOfZonedDay` (DST-correct); each firing re-reads the
 *  clock, and `prev === start` makes a duplicate firing free. The 1s floor stops a zero/negative delay
 *  from re-arming forever. */
export function useOfficialDayStart(zone: string): number {
  const [dayStart, setDayStart] = useState(() => startOfZonedDay(new Date(), zone).getTime())
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>
    const arm = (): void => {
      const now = new Date()
      const start = startOfZonedDay(now, zone).getTime()
      setDayStart(prev => (prev === start ? prev : start))
      // One millisecond past the day's last instant is the next day's first.
      const delay = Math.max(1000, endOfZonedDay(now, zone).getTime() + 1 - now.getTime())
      timer = setTimeout(arm, delay)
    }
    arm()
    return () => clearTimeout(timer)
  }, [zone])
  return dayStart
}
