/* HOW LONG A FLAGGED SESSION WAITED FOR A PAGE. `clientTelemetry.ts` reports what failed; a console that is merely
 * slow fails nothing, so without this an operator's "the page takes forever" leaves no trace. Two kinds, each
 * reported only when slow and only for a flagged session (`reportClientEvent` is a no-op otherwise):
 *
 * - `page_load` — a full load of the console: from the browser's navigation start until the app has gone
 *   `QUIET_MS` with no chunk loading and no read open, reported at `SLOW_PAGE_LOAD_MS` or more. Its detail carries the
 *   browser's navigation timing (TTFB, DOM ready, load, first and largest contentful paint), when the identity read
 *   answered, and the slowest resources.
 * - `navigation` — a route change inside the app (the router's `pathname`): from the change until the same quiet,
 *   reported at `SLOW_NAVIGATION_MS` or more, with the slowest resources it fetched. A route change before the page
 *   load is timed out (a boot redirect, an early click) is part of the page load, not a navigation of its own.
 *
 * "Loading" is a lazy chunk or a console read (`net/stall.ts::openReadsSince`) begun since the page or route began:
 * one begun before (a long poll, the previous page's refresh, a chunk still retrying) says nothing about the new one.
 * A watch still loading `SETTLE_CAP_MS` in is reported as it stands (`outcome: 'cap'`); one a newer navigation
 * interrupts while still loading is reported if it was already slow (`'superseded'`: the user gave up waiting, which is
 * itself the signal). A page hidden at any point while it loaded is not reported: a background tab's clocks measure
 * nothing the user waited for. Slow single reads are `net/stall.ts`'s (`slow_read`).
 *
 * Costs an unflagged session nothing past its identity read: no navigation is watched once the flag is known off, and
 * the page-load watch already running stops at its next poll. Like every reporter it never throws into the page. */
import { clientTelemetryEnabled, reportClientEvent } from './clientTelemetry'
import { openReadsSince } from '../net/stall'
import { resourceName, timingOf } from './resourceTiming'

export const SLOW_PAGE_LOAD_MS = 3_000
export const SLOW_NAVIGATION_MS = 1_000
/** Nothing loading for this long means the page has what it shows. */
export const QUIET_MS = 500
export const SETTLE_CAP_MS = 30_000
export const POLL_MS = 100
/** The browser keeps 250 resource timings by default; a session that outlives them would report empty details. */
const RESOURCE_BUFFER = 1_000
const SLOWEST = 5
const IDENTITY_PATH = '/api/auth/me'

type Kind = 'page_load' | 'navigation'
type Outcome = 'settled' | 'superseded' | 'cap'
type Watch = { kind: Kind; startedAt: number; lastBusyAt: number; route: string; from: string | null; hidden: boolean }

/** Each lazy chunk in flight, by when it began (`performance.now()`). */
const chunks = new Map<number, number>()
let nextChunk = 0
let current: Watch | null = null
let timer: ReturnType<typeof setTimeout> | null = null
let lcp: number | null = null
/** The page load has been timed (reported or not); route changes are navigations from here on. */
let booted = false

/** A lazy chunk began loading; call the returned function once it settled, either way. */
export function chunkLoadStarted(): () => void {
  const id = nextChunk++
  chunks.set(id, performance.now())
  return () => {
    chunks.delete(id)
  }
}

const isHidden = (): boolean => document.visibilityState === 'hidden'

function loading(watch: Watch): boolean {
  for (const begun of chunks.values()) if (begun >= watch.startedAt) return true
  return openReadsSince(watch.startedAt) > 0
}

function resources(from: number, to: number): Record<string, unknown> {
  const entries = (performance.getEntriesByType('resource') as PerformanceResourceTiming[]).filter(
    entry => entry.startTime >= from && entry.startTime <= to,
  )
  const slowest = [...entries].sort((a, b) => b.duration - a.duration).slice(0, SLOWEST)
  return {
    resource_count: entries.length,
    transfer_bytes: entries.reduce((sum, entry) => sum + (entry.transferSize || 0), 0),
    slowest: slowest.map(entry => ({
      name: resourceName(entry.name),
      type: entry.initiatorType,
      start_ms: Math.round(entry.startTime - from),
      ms: Math.round(entry.duration),
      ...timingOf(entry),
    })),
  }
}

/** A point on the page's clock, `null` when the browser never reached it (0). */
const at = (value: number | undefined) => (value && value > 0 ? Math.round(value) : null)
/** A span, `null` only when the browser did not time it: 0 is real (a cached lookup, a reused connection). */
const span = (end: number, start: number) => (end >= start && end > 0 ? Math.round(end - start) : null)

function navigationTiming(end: number): Record<string, unknown> {
  const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
  const fcp = performance.getEntriesByName('first-contentful-paint')[0]
  const identity = (performance.getEntriesByType('resource') as PerformanceResourceTiming[]).find(
    entry => resourceName(entry.name) === IDENTITY_PATH,
  )
  return {
    navigation: nav
      ? {
          type: nav.type,
          protocol: nav.nextHopProtocol || null,
          redirects: nav.redirectCount,
          dns_ms: span(nav.domainLookupEnd, nav.domainLookupStart),
          connect_ms: span(nav.connectEnd, nav.connectStart),
          ttfb_ms: at(nav.responseStart),
          html_ms: at(nav.responseEnd),
          dom_ready_ms: at(nav.domContentLoadedEventEnd),
          load_ms: at(nav.loadEventEnd),
          bytes: nav.transferSize,
        }
      : null,
    fcp_ms: at(fcp?.startTime),
    lcp_ms: lcp !== null && lcp <= end ? Math.round(lcp) : null,
    identity_ms: at(identity?.responseEnd),
  }
}

function stop(): void {
  if (timer !== null) clearTimeout(timer)
  timer = null
  if (current?.kind === 'page_load') booted = true
  current = null
}

function finish(watch: Watch, end: number, outcome: Outcome): void {
  stop()
  if (watch.hidden || clientTelemetryEnabled() === false) return
  const elapsed = Math.round(end - watch.startedAt)
  if (elapsed < (watch.kind === 'page_load' ? SLOW_PAGE_LOAD_MS : SLOW_NAVIGATION_MS)) return
  const seconds = (elapsed / 1000).toFixed(1)
  reportClientEvent(watch.kind, {
    message: watch.kind === 'page_load' ? `Page ready in ${seconds} s` : `Opened in ${seconds} s`,
    detail: {
      elapsed_ms: elapsed,
      outcome,
      ...(watch.kind === 'page_load' ? { landed: watch.route, ...navigationTiming(end) } : { from: watch.from }),
      ...resources(watch.startedAt, end),
    },
  })
}

function poll(): void {
  timer = null
  const watch = current
  if (watch === null) return
  try {
    if (clientTelemetryEnabled() === false) return stop()
    const now = performance.now()
    if (isHidden()) watch.hidden = true
    if (loading(watch)) watch.lastBusyAt = now
    else if (now - watch.lastBusyAt >= QUIET_MS) return finish(watch, watch.lastBusyAt, 'settled')
    if (now - watch.startedAt >= SETTLE_CAP_MS) return finish(watch, now, 'cap')
    timer = setTimeout(poll, POLL_MS)
  } catch {
    stop()
  }
}

function watch(kind: Kind, startedAt: number, route: string, from: string | null): void {
  const previous = current
  // One that already had what it shows is done as of its last loading, not as of this change.
  if (previous !== null) {
    if (loading(previous)) finish(previous, performance.now(), 'superseded')
    else finish(previous, previous.lastBusyAt, 'settled')
  }
  if (clientTelemetryEnabled() === false) {
    if (kind === 'page_load') booted = true
    return
  }
  current = { kind, startedAt, lastBusyAt: performance.now(), route, from, hidden: isHidden() }
  timer = setTimeout(poll, POLL_MS)
}

/** Once, at boot (`main.tsx`): time this page load from navigation start, then every route change `onPathname`
 *  announces (the router's location). */
export function installLoadTiming(win: Window, onPathname: (listener: (pathname: string) => void) => unknown): void {
  try {
    win.performance.setResourceTimingBufferSize?.(RESOURCE_BUFFER)
    // Full anyway (a very long session): start over rather than report empty details from then on.
    win.performance.addEventListener?.('resourcetimingbufferfull', () => win.performance.clearResourceTimings())
    new PerformanceObserver(list => {
      const entries = list.getEntries()
      lcp = entries[entries.length - 1]?.startTime ?? lcp
    }).observe({ type: 'largest-contentful-paint', buffered: true })
  } catch {
    /* An older browser: no LCP (`lcp_ms` stays null) or the default resource buffer. */
  }
  try {
    win.document.addEventListener('visibilitychange', () => {
      if (current !== null && isHidden()) current.hidden = true
    })
    let pathname = win.location.pathname
    watch('page_load', 0, pathname, null)
    onPathname(next => {
      try {
        if (next === pathname) return
        const from = pathname
        pathname = next
        if (!booted) {
          if (current !== null) current.route = next
          return
        }
        watch('navigation', performance.now(), next, from)
      } catch {
        /* Timing never breaks the navigation it times. */
      }
    })
  } catch {
    /* Nor the boot it starts in. */
  }
}

/** Test seam: nothing watched, nothing pending. */
export function resetLoadTiming(): void {
  if (timer !== null) clearTimeout(timer)
  timer = null
  current = null
  chunks.clear()
  lcp = null
  booted = false
}
