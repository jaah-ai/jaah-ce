/* A REQUEST THE NETWORK SWALLOWED. Some networks (an ISP's DPI box, a corporate TLS-inspecting firewall) silently drop
 * a long-lived HTTP/2 connection to the console: the browser keeps sending on it, hears nothing, and only opens a
 * new connection about 20s later. Nothing errors in that window. This store says so: `requestStalled` is `true`
 * while any console read (`net/send.ts::send`) has gone `STALL_AFTER_MS` unanswered, and `false` again the moment
 * none has. Nothing shows it to the user — the read relay (`net/relay.ts`) recovers in silence. Two silent mechanisms
 * read it: `clientTelemetry.ts` holds its reports while it is up, and `lazyRetry.ts` keeps retrying a route chunk.
 *
 * Only time the page could have heard an answer counts. A hidden tab, a frozen one, or a machine that slept leaves
 * a read open while the wall clock runs on, and that is no stall. So each read keeps its own LIVE time: it stops
 * accruing while the page is hidden and resumes when it is visible again, and while reads are open a check runs every
 * `BEAT_MS` — a gap of `FROZEN_GAP_MS` or more between two checks (the page did not run: frozen, or asleep) is left
 * out. Hiding the page never clears a stall that was already up: it only stops the clocks.
 *
 * A wait that saw the page hidden or frozen at any point (hidden when sent, hidden while open, or a frozen gap) is
 * never reported as a `stall`: a backgrounded tab waking from sleep would otherwise burst them. Such a read is not
 * reported at all, and such a write only as a `slow_write` when the relay carried it or it got no answer.
 *
 * The threshold sits well above a slow answer: the control plane's slowest read is a few seconds, and a body that
 * streams for longer (a download) counts only until its headers arrive.
 *
 * A read that crossed the threshold is reported once it settles (`clientTelemetry.ts`, a no-op unless the session
 * is flagged): what it was, its live wait, and whether it was answered. A read that took `SLOW_READ_MS` or more but
 * never stalled is reported the same way as a `slow_read`. Each report waits for the browser's timing of that very
 * request (`resourceTiming.ts::timingOfRequest`: recorded once its body arrived, matched by when it was sent), which
 * splits the wait into the control plane's own time and the network's — looked for every `REPORT_POLL_MS`, for at
 * most `REPORT_CAP_MS`; a read the relay won leaves no such timing (the direct copy was aborted) and reports
 * `timing: null`. Nothing is scheduled for a session that does not report. `openReadsSince` tells `loadTiming.ts` whether a page
 * or route is still fetching what it shows.
 *
 * WRITES are timed the same way (`send.ts` watches every method) but never raise `requestStalled`. A write is reported as a
 * `slow_write` when it took `SLOW_READ_MS` or more, or when the relay carried it or it got no answer at all (however
 * fast): its detail says how it ended (`outcome`: `direct`, `relayed`, `replayed` — the server's stored answer — or
 * `failed`), its `latency_ms`, and the relay's state, so the next incident shows whether a save was rescued. */
import { clientTelemetryEnabled, reportClientEvent } from '../lib/clientTelemetry'
import { createExternalStore, type ExternalStore } from '../lib/externalStore'
import { timingOfRequest } from '../lib/resourceTiming'
import { cameOverRelay, relayState } from './relay'

export const STALL_AFTER_MS = 8_000
/** A read answered this slowly is reported (`slow_read`) even though it never stalled. */
export const SLOW_READ_MS = 2_000
/** A settled slow read looks for its own resource timing this often, and is reported without it after the cap. */
export const REPORT_POLL_MS = 250
export const REPORT_CAP_MS = 10_000
/** While a read is open the clocks are checked at least this often, so a gap the page did not run shows up. */
export const BEAT_MS = 1_000
/** A gap between two checks this long means the page did not run in between (frozen, or the machine slept). */
export const FROZEN_GAP_MS = 3_000

/** One open read: live time banked so far, when its current live stretch began (`null` while hidden), when it was
 *  sent (`performance.now()`), and whether the page was hidden or frozen at any point since. */
type Wait = { live: number; since: number | null; request: WaitRequest; sentAt: number; unseen: boolean }

const started = new Map<number, Wait>()
let nextId = 0
let timer: ReturnType<typeof setTimeout> | null = null
let lastCheck = 0
const stalled = createExternalStore(false)
/** Read-only: a console read is stalled (`clientTelemetry.ts` holds its reports, `lazyRetry.ts` waits on it). */
export const requestStalled: Pick<ExternalStore<boolean>, 'get' | 'subscribe'> = stalled

/** What a watched read was: its method and path. */
export type WaitRequest = { method: string; path: string }
export type WaitOutcome = 'ok' | 'error'

const isRead = (request: WaitRequest): boolean => request.method.toUpperCase() === 'GET'
const hidden = (): boolean => typeof document !== 'undefined' && document.visibilityState === 'hidden'
const liveOf = (wait: Wait, now: number): number => wait.live + (wait.since === null ? 0 : now - wait.since)

/** Bring every clock to `now`, leaving out a gap the page did not run through. */
function catchUp(now: number): void {
  if (now - lastCheck >= FROZEN_GAP_MS) {
    for (const wait of started.values()) {
      wait.unseen = true
      if (wait.since === null) continue
      wait.live += Math.max(0, lastCheck - wait.since)
      wait.since = now
    }
  }
  lastCheck = now
}

function evaluate(): void {
  if (timer !== null) clearTimeout(timer)
  timer = null
  const now = Date.now()
  catchUp(now)
  let next = Infinity
  let anyStalled = false
  for (const wait of started.values()) {
    // Only a read stalls the tab: a write may legitimately take its time, and is reported on its own.
    if (!isRead(wait.request)) continue
    const live = liveOf(wait, now)
    if (live >= STALL_AFTER_MS) anyStalled = true
    else if (wait.since !== null) next = Math.min(next, STALL_AFTER_MS - live)
  }
  stalled.set(anyStalled)
  // A heartbeat while any clock runs, so a frozen gap is seen even once every open read is already stalled.
  const running = [...started.values()].some(wait => wait.since !== null && isRead(wait.request))
  if (running) timer = setTimeout(evaluate, Math.min(next, BEAT_MS))
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    const now = Date.now()
    catchUp(now)
    for (const wait of started.values()) {
      if (hidden()) wait.unseen = true
      if (hidden() && wait.since !== null) {
        wait.live += now - wait.since
        wait.since = null
      } else if (!hidden() && wait.since === null) {
        wait.since = now
      }
    }
    evaluate()
  })
}

/** Report timers still looking for their read's timing (`resetWaits` clears them). */
const reporting = new Set<ReturnType<typeof setTimeout>>()

/** How a write ended, for its report. */
export type WriteOutcome = 'direct' | 'relayed' | 'replayed' | 'failed'

function writeOutcome(outcome: WaitOutcome, answer: unknown): WriteOutcome {
  if (outcome === 'error') return 'failed'
  if (!(answer instanceof Response)) return 'direct'
  if (answer.headers.get('idempotent-replayed') === 'true') return 'replayed'
  return cameOverRelay(answer) ? 'relayed' : 'direct'
}


function reportWait(
  request: WaitRequest, sentAt: number, waited: number, unseen: boolean, outcome: WaitOutcome, answer?: unknown,
): void {
  if (clientTelemetryEnabled() === false) return
  const path = request.path.split('?')[0]
  // The page it happened on, not the one shown when the report goes out.
  const route = window.location.pathname
  const settledAt = Date.now()
  const schedule = (ms: number) => {
    const handle = setTimeout(() => {
      reporting.delete(handle)
      attempt()
    }, ms)
    reporting.add(handle)
  }
  const attempt = () => {
    const timing = timingOfRequest(request.path, sentAt)
    if (timing === null && Date.now() - settledAt < REPORT_CAP_MS) return schedule(REPORT_POLL_MS)
    if (isRead(request)) {
      reportClientEvent(waited >= STALL_AFTER_MS ? 'stall' : 'slow_read', {
        message: `${request.method} ${path}`,
        route,
        detail: { method: request.method, path, waited_ms: waited, outcome, timing },
      })
      return
    }
    reportClientEvent(waited >= STALL_AFTER_MS && !unseen ? 'stall' : 'slow_write', {
      message: `${request.method} ${path}`,
      route,
      detail: {
        method: request.method, path, waited_ms: waited, latency_ms: waited, outcome: writeOutcome(outcome, answer),
        relay: relayState(), timing,
      },
    })
  }
  schedule(0)
}

/** Start watching one request; call the returned function once it is answered (or abandoned), with its answer. */
export function beginWait(request: WaitRequest): (outcome?: WaitOutcome, answer?: unknown) => void {
  const id = nextId++
  const now = Date.now()
  if (started.size === 0) lastCheck = now
  else catchUp(now)
  const wait: Wait = { live: 0, since: hidden() ? null : now, request, sentAt: performance.now(), unseen: hidden() }
  started.set(id, wait)
  evaluate()
  return (outcome = 'ok', answer?: unknown) => {
    if (!started.has(id)) return
    const at = Date.now()
    catchUp(at)
    started.delete(id)
    // Reported once its timing is in (or at the cap): the next flush carries it.
    const waited = liveOf(wait, at)
    // A write its caller abandoned (an unmount, a Stop) is no network failure.
    const abandoned = !isRead(request) && answer instanceof DOMException && answer.name === 'AbortError'
    const rescuedOrLost = !isRead(request) && !abandoned && writeOutcome(outcome, answer) !== 'direct'
    // A wait the page was hidden or frozen through: only whether a save was rescued is worth a report.
    const slow = waited >= SLOW_READ_MS && !abandoned && !wait.unseen
    if (slow || rescuedOrLost) reportWait(request, wait.sentAt, waited, wait.unseen, outcome, answer)
    evaluate()
  }
}

/** Watch `response` until it settles, either way, and hand it back unchanged. */
export function watchWait<T>(response: Promise<T>, request: WaitRequest): Promise<T> {
  const end = beginWait(request)
  response.then(answer => end('ok', answer), err => end('error', err))
  return response
}

/** How many reads sent at or after `at` (`performance.now()`) are still open. A read sent earlier (a long poll, a
 *  refresh of the page left behind) says nothing about whether what came after has loaded. */
export function openReadsSince(at: number): number {
  let open = 0
  for (const wait of started.values()) if (wait.sentAt >= at && isRead(wait.request)) open++
  return open
}

/** Test seam: nothing watched, nothing stalled. */
export function resetWaits(): void {
  started.clear()
  for (const pending of reporting) clearTimeout(pending)
  reporting.clear()
  if (timer !== null) clearTimeout(timer)
  timer = null
  stalled.reset()
}
