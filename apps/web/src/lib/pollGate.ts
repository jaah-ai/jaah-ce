/* THE ONE GATE EVERY BACKGROUND READ ASKS: may this page poll now?
 *
 * Three reasons say no, and every console poll honours all three (the timers of `hooks/usePoll.ts`, the stream-down
 * poll and SSE-driven refetches of `hooks/useLiveRefetch.ts`, event-driven invalidations, and React Query's
 * `refetchInterval`, through its `focusManager`):
 * - HIDDEN: the tab is in the background — nobody is looking.
 * - IDLE: nobody has touched the page for `IDLE_AFTER_MS`. A headless browser (an agent's visual check left open on
 *   /tasks) is never "hidden", so the hidden rule alone never paused it, and such pages were most of one office's
 *   traffic to the console. An idle page is SLOWED, not frozen: every `IDLE_PULSE_MS` it re-reads what live events
 *   changed meanwhile (each deferred refetch and skipped `usePoll` pass once, each invalidated query once), so a wall
 *   display still moves; React Query's own `refetchInterval` waits for the first touch, which catches everything up.
 * - RATE-LIMITED: a read was refused 429. Polling on at the normal interval only keeps the limit tripped for every
 *   tab behind the same address, so polling stops for a back-off that doubles per episode (`noteRateLimited`).
 *
 * When the gate opens again (shown, touched, back-off over, or an idle pulse) every `onPollingResumed` subscriber
 * runs once: each re-reads what it skipped meanwhile, never once per skipped tick. */
import { focusManager } from '@tanstack/react-query'
import { queryClient } from '../queryClient'

/** No pointer, key, wheel or touch, and no return to the tab, for this long: the page is idle. */
export const IDLE_AFTER_MS = 3 * 60_000
/** An idle (but visible, not rate-limited) page catches up this often. */
export const IDLE_PULSE_MS = 2 * 60_000
/** The first 429 pauses polling this long, doubling per episode up to `RATE_LIMIT_MAX_MS`, unless `Retry-After` asks
 *  for longer. Each wait is spread ±`RATE_LIMIT_JITTER` so the tabs behind one address do not all resume at once. */
export const RATE_LIMIT_MIN_MS = 60_000
export const RATE_LIMIT_MAX_MS = 10 * 60_000
const RATE_LIMIT_JITTER = 0.2

/** How often the idle clock is read: idleness starts within this of its bound. */
const IDLE_CHECK_MS = 15_000
/* A person's input only: a `scroll` or `focus` is also what the page's own code does (a log following its output, a
   composer focusing itself), and would wake an untouched page on every pulse. A person scrolling sends `wheel`/`touchstart`/`keydown`. */
const ACTIVITY_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart'] as const

let started = false
let idleDetection = true
let lastActivityAt = Date.now()
let idle = false
let lastPulseAt = 0
let rateLimitedUntil = 0
let rateLimitBackoffMs = 0
let rateLimitTimer: ReturnType<typeof setTimeout> | null = null
let idleTimer: ReturnType<typeof setInterval> | null = null
let pausedBefore = false
const resumeListeners = new Set<() => void>()

const hasDocument = () => typeof document !== 'undefined'

/** The tab is in the background. */
export function isPageHidden(): boolean {
  return hasDocument() && document.visibilityState === 'hidden'
}

/** A read was refused 429 and its back-off has not run out. */
export function isRateLimited(): boolean {
  return Date.now() < rateLimitedUntil
}

/** True while no background read may go out: hidden, idle or rate-limited (module header). */
export function isPollingPaused(): boolean {
  return isPageHidden() || idle || isRateLimited()
}

/** Run `listener` each time the gate opens again (module header). Returns the unsubscribe. */
export function onPollingResumed(listener: () => void): () => void {
  resumeListeners.add(listener)
  return () => { resumeListeners.delete(listener) }
}

/** A read refused 429: pause polling for the back-off (module header). `retryAfter` is the header's value, in seconds;
 *  an HTTP date or nothing falls back to the back-off. The reads a refusal catches in flight answer 429 together: only
 *  the first 429 after a back-off ran out doubles the next one. */
export function noteRateLimited(retryAfter?: string | null): void {
  const fresh = !isRateLimited()
  if (fresh) rateLimitBackoffMs = Math.min(Math.max(rateLimitBackoffMs * 2, RATE_LIMIT_MIN_MS), RATE_LIMIT_MAX_MS)
  const asked = Number(retryAfter) * 1000
  const askedMs = retryAfter && Number.isFinite(asked) ? Math.min(asked, RATE_LIMIT_MAX_MS) : 0
  if (!fresh && askedMs === 0) return
  const spread = 1 + (Math.random() * 2 - 1) * RATE_LIMIT_JITTER
  const waitMs = Math.max(fresh ? rateLimitBackoffMs * spread : 0, askedMs)
  rateLimitedUntil = Math.max(rateLimitedUntil, Date.now() + waitMs)
  armRateLimitTimer()
  sync()
}

/** A read answered with anything but 429 once the back-off ran out: the next refusal starts short again. */
export function noteEdgeAnswered(): void {
  if (rateLimitBackoffMs !== 0 && !isRateLimited()) rateLimitBackoffMs = 0
}

/** Back to a fresh page: active, not rate-limited, no subscribers. `detectIdle: false` turns the idle rule off (the
 *  unit suites' default, `test/setup.ts`, whose fake clocks run minutes ahead with nobody touching the page). */
export function resetPollGate({ detectIdle = true }: { detectIdle?: boolean } = {}): void {
  if (rateLimitTimer !== null) clearTimeout(rateLimitTimer)
  rateLimitTimer = null
  if (idleTimer !== null) clearInterval(idleTimer)
  idleTimer = null
  idleDetection = detectIdle
  lastActivityAt = Date.now()
  idle = false
  lastPulseAt = 0
  rateLimitedUntil = 0
  rateLimitBackoffMs = 0
  resumeListeners.clear()
  pausedBefore = isPageHidden()
  if (started) {
    focusManager.setFocused(undefined)
    if (idleDetection) idleTimer = setInterval(checkIdle, IDLE_CHECK_MS)
  }
}

/* ── internals ───────────────────────────────────────────────────────────────────────────────────────── */

/** Opens the gate when the back-off runs out; a timer that fires early (a clock stepped back) waits again. */
function armRateLimitTimer(): void {
  if (rateLimitTimer !== null) clearTimeout(rateLimitTimer)
  rateLimitTimer = setTimeout(() => {
    rateLimitTimer = null
    if (isRateLimited()) armRateLimitTimer()
    else sync()
  }, Math.max(rateLimitedUntil - Date.now(), 0))
}

/** Tell React Query and the subscribers what the gate says now; resume subscribers on a closed → open edge, and on
 *  every return to the tab (a subscriber acts only on what it skipped, so an extra call costs nothing). */
function sync(shown = false): void {
  const paused = isPollingPaused()
  // `undefined` hands focus back to React Query's own visibility rule (and refetches what went stale); `false` stops
  // its `refetchInterval`.
  focusManager.setFocused(paused ? false : undefined)
  const opened = (pausedBefore || shown) && !paused
  pausedBefore = paused
  if (opened) emitResumed()
}

function emitResumed(): void {
  for (const listener of [...resumeListeners]) listener()
}

function onActivity(): void {
  lastActivityAt = Date.now()
  if (!idle) return
  idle = false
  sync()
}

/** Shown again is a touch: a person came back to the tab. */
function onVisibility(): void {
  const shown = !isPageHidden()
  if (shown) {
    lastActivityAt = Date.now()
    idle = false
  }
  sync(shown)
}

function checkIdle(): void {
  if (!idleDetection) return
  const now = Date.now()
  if (!idle) {
    if (now - lastActivityAt < IDLE_AFTER_MS) return
    idle = true
    lastPulseAt = now
    sync()
    return
  }
  if (now - lastPulseAt < IDLE_PULSE_MS || isPageHidden() || isRateLimited()) return
  lastPulseAt = now
  pulse()
}

/* SLOWED, NOT FROZEN: one catch-up of what live events changed — the deferred refetches, and each query an event
   invalidated while paused (`refetchType: 'none'`). Not a focus refetch: that would re-read every stale catalogue
   on the page, which nothing changed. */
function pulse(): void {
  emitResumed()
  void queryClient.refetchQueries({ type: 'active', predicate: query => query.state.isInvalidated }, { cancelRefetch: false })
}

function start(): void {
  if (started || typeof window === 'undefined') return
  started = true
  lastActivityAt = Date.now()
  pausedBefore = isPageHidden()
  for (const type of ACTIVITY_EVENTS) window.addEventListener(type, onActivity, { capture: true, passive: true })
  if (hasDocument()) document.addEventListener('visibilitychange', onVisibility)
  if (idleDetection) idleTimer = setInterval(checkIdle, IDLE_CHECK_MS)
}

// From the first import on (`net/send.ts`, at boot), so a touch on any page counts.
start()
