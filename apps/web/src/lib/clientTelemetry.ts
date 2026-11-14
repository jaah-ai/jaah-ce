/* WHAT A FLAGGED SESSION TELLS THE CONTROL PLANE ABOUT ITS OWN FAILURES. A console that white-screens or spins on
 * an operator's network leaves nothing on the server: the render error, the chunk that never arrived, the read the
 * network swallowed all happen in the browser. A superadmin flags an org or a user (`client_telemetry` on the /me
 * identity, set by `auth.tsx`), and only then does this tab report them to `POST /api/client-events`, read back on
 * Audit > Client errors. Unflagged, `reportClientEvent` returns at once: nothing is queued, nothing is sent. Before the
 * identity has answered (`enabled` still `null`) events are HELD, not sent: on the networks this exists for, the
 * first identity read is often the stalled one, and its stall is the event that matters. The answer then sends them
 * or drops them.
 *
 * Reporting must never make things worse. It never throws; it sends with the plain `fetch`, so its own POST is never
 * watched by `net/stall.ts` nor raced by `net/relay.ts` (a report about a stall must not cause one); it holds its
 * batch while the connection is stalled and sends the moment the stall clears; and it is bounded everywhere — the same
 * kind+message+route within `DEDUPE_MS` is counted on the held event (`detail.repeats`) rather than queued again
 * (a repeat after that event was sent is dropped), at most `MAX_PER_MINUTE` new failures a minute and, on a
 * budget of their own so slowness never crowds a failure out, `MAX_TIMING_PER_MINUTE` slow loads and reads
 * (`TIMING_KINDS`, `loadTiming.ts`), at most `MAX_QUEUE` held. A batch the network lost or the server failed on (5xx) goes back on the queue and a retry is scheduled,
 * `FLUSH_AFTER_MS` doubling each time up to `MAX_RETRY_MS` and back to `FLUSH_AFTER_MS` once the server answers; one
 * the server refused (4xx) would be refused again, so it is dropped.
 *
 * Nothing secret leaves the tab: before an event is queued, its message and its `stack`, `component_stack`, `source`
 * `url` and `path` details lose bearer tokens, JWT-shaped strings and URL query strings (`scrub`). */
import { sessionHeaders, tabLeftBehind, whenTabCurrent } from './activeOrg'
import { relayState } from '../net/relay'
import { send } from '../net/send'
import { requestStalled } from '../net/stall'
import type { ClientEventKind } from '../types'
import { APP_VERSION } from '../version'

type ClientEvent = {
  kind: ClientEventKind
  occurred_at: string
  route: string
  message: string
  detail: Record<string, unknown>
  build_version: string
}

const ENDPOINT = '/api/client-events'
export const DEDUPE_MS = 60_000
export const MAX_PER_MINUTE = 20
export const MAX_TIMING_PER_MINUTE = 10
const TIMING_KINDS: ReadonlySet<ClientEventKind> = new Set(['page_load', 'navigation', 'slow_read', 'slow_write'])
export const FLUSH_AFTER_MS = 2_000
export const MAX_QUEUE = 200
export const MAX_RETRY_MS = 60_000
const MAX_BATCH = 50
/** A `keepalive` body is capped by the browser (64 KB across a page's in-flight keepalive requests). */
const KEEPALIVE_MAX_BYTES = 60_000

let enabled: boolean | null = null
let queue: ClientEvent[] = []
const recent = new Map<string, { at: number; event: ClientEvent }>()
/** When each event of the last minute was accepted, per budget: failures, and slow loads and reads. */
const accepted: Record<'failure' | 'timing', number[]> = { failure: [], timing: [] }
let timer: ReturnType<typeof setTimeout> | null = null
let retryMs = FLUSH_AFTER_MS
let stopWatchingStall: (() => void) | null = null

/** Driven from the signed-in identity's `client_telemetry`; `false` drops whatever is held, `true` sends it, `null`
 *  (identity not answered yet) holds. */
export function setClientTelemetryEnabled(on: boolean | null): void {
  enabled = on
  if (on === false) resetClientTelemetry()
  else if (queue.length > 0 && timer === null) timer = setTimeout(() => flushClientEvents(), FLUSH_AFTER_MS)
}

/** Whether this session reports: `true`/`false` once the identity answered, `null` before. */
export function clientTelemetryEnabled(): boolean | null {
  return enabled
}

/** Cut `text` to `max` characters (a stack, a message). */
export function clip(text: unknown, max: number): string {
  return String(text ?? '').slice(0, max)
}

const SCRUBBED_DETAILS = ['stack', 'component_stack', 'source', 'url', 'path'] as const

/** `text` without bearer tokens, JWT-shaped strings, or the query string of any URL in it. */
export function scrub(text: string): string {
  return text
    .replace(/\b(Bearer)\s+[A-Za-z0-9._~+/-]+=*/gi, '$1 [redacted]')
    .replace(/\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/g, '[redacted]')
    // A stack frame's `:line:column` after the query is kept.
    .replace(/(\b[a-z][a-z0-9+.-]*:\/\/[^\s?#"'<>()]*)\?[^\s#"'<>()]*?(?=(?::\d+){1,2}(?![^\s#"'<>()])|[\s#"'<>()]|$)/gi, '$1?[redacted]')
}

function context(): Record<string, unknown> {
  const conn = (navigator as Navigator & { connection?: { effectiveType?: string; rtt?: number; downlink?: number } })
    .connection
  return {
    connection: conn ? { effective_type: conn.effectiveType, rtt: conn.rtt, downlink: conn.downlink } : null,
    online: navigator.onLine,
    visibility: document.visibilityState,
    since_load_ms: Math.round(performance.now()),
    relay: relayState(),
  }
}

/** Report one client-side failure. A no-op unless this session is flagged; never throws. `route` is the page it
 *  happened on, when that is not the page shown now (a report sent after its moment). */
export function reportClientEvent(
  kind: ClientEventKind,
  { message, detail = {}, route: at }: { message: string; detail?: Record<string, unknown>; route?: string },
): void {
  if (enabled === false) return
  try {
    const now = Date.now()
    const route = clip(at ?? window.location.pathname, 500)
    const text = clip(scrub(String(message ?? '')), 1000)
    const scrubbed: Record<string, unknown> = { ...detail }
    for (const key of SCRUBBED_DETAILS) {
      const value = scrubbed[key]
      if (typeof value === 'string') scrubbed[key] = scrub(value)
    }
    const key = `${kind}\u0000${text}\u0000${route}`
    const held = recent.get(key)
    if (held && now - held.at < DEDUPE_MS) {
      held.event.detail.repeats = Number(held.event.detail.repeats ?? 0) + 1
      return
    }
    const budget = TIMING_KINDS.has(kind) ? 'timing' : 'failure'
    accepted[budget] = accepted[budget].filter(at => now - at < 60_000)
    if (accepted[budget].length >= (budget === 'timing' ? MAX_TIMING_PER_MINUTE : MAX_PER_MINUTE)) return
    accepted[budget].push(now)
    const event: ClientEvent = {
      kind,
      occurred_at: new Date(now).toISOString(),
      route,
      message: text,
      detail: { ...scrubbed, ...context() },
      build_version: clip(APP_VERSION, 64),
    }
    recent.set(key, { at: now, event })
    for (const [k, v] of recent) if (now - v.at >= DEDUPE_MS) recent.delete(k)
    queue.push(event)
    if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE)
    if (enabled && timer === null) timer = setTimeout(() => flushClientEvents(), FLUSH_AFTER_MS)
  } catch {
    /* Reporting never breaks the page it reports on. */
  }
}

/** A flush is waiting for this tab to be current again (`flushClientEvents`). */
let waitingForTab = false

function requeue(batch: ClientEvent[]): void {
  if (enabled === false) return
  queue = [...batch, ...queue].slice(-MAX_QUEUE)
  if (timer === null) {
    timer = setTimeout(() => flushClientEvents(), retryMs)
    retryMs = Math.min(retryMs * 2, MAX_RETRY_MS)
  }
}

/** Send what is held. Held back while the connection is stalled (sent the moment it clears) unless `force` (the page
 *  is going away: a keepalive request is its last chance), and while an org switch left this tab behind (sent once it
 *  is current again; the API would refuse it). */
export function flushClientEvents(force = false): void {
  try {
    if (timer !== null) clearTimeout(timer)
    timer = null
    if (!enabled || queue.length === 0) return
    if (tabLeftBehind()) {
      if (!waitingForTab) {
        waitingForTab = true
        void whenTabCurrent().then(() => { waitingForTab = false; flushClientEvents() })
      }
      return
    }
    if (!force && requestStalled.get()) {
      stopWatchingStall ??= requestStalled.subscribe(() => {
        if (requestStalled.get()) return
        stopWatchingStall?.()
        stopWatchingStall = null
        flushClientEvents()
      })
      return
    }
    const batch = queue.splice(0, MAX_BATCH)
    const body = JSON.stringify({ events: batch })
    /* `raw`: the bare POST through the choke point — never raced or stall-timed (the stall report itself would recurse),
       and never sent while an org switch left this tab behind (the flush above waits for it instead). */
    send(ENDPOINT, {
      raw: true,
      method: 'POST',
      credentials: 'same-origin',
      keepalive: body.length < KEEPALIVE_MAX_BYTES,
      headers: sessionHeaders({ 'Content-Type': 'application/json' }, 'POST'),
      body,
    }).then(
      resp => {
        if (resp.status >= 500) requeue(batch)
        else retryMs = FLUSH_AFTER_MS
      },
      () => requeue(batch),
    )
    if (queue.length > 0) timer = setTimeout(() => flushClientEvents(), FLUSH_AFTER_MS)
  } catch {
    /* Never throws: a fetch that cannot even be built leaves the queue for the next flush. */
  }
}

function errorDetail(error: unknown): { message: string; detail: Record<string, unknown> } {
  if (error instanceof Error) return { message: error.message || error.name, detail: { name: error.name, stack: clip(error.stack, 4096) } }
  return { message: clip(error, 1000), detail: {} }
}

/** Once, at boot (`main.tsx`): uncaught errors and rejections, and a last flush when the page goes away. Each listener
 *  returns at once while unflagged. */
export function installClientTelemetry(win: Window): void {
  win.addEventListener('error', event => {
    if (enabled === false) return
    const { message, detail } = errorDetail(event.error ?? event.message)
    reportClientEvent('uncaught_error', {
      message: event.message || message,
      detail: { ...detail, source: clip(event.filename, 500), line: event.lineno, column: event.colno },
    })
  })
  win.addEventListener('unhandledrejection', event => {
    if (enabled === false) return
    const { message, detail } = errorDetail(event.reason)
    reportClientEvent('uncaught_error', { message, detail: { ...detail, rejection: true } })
  })
  win.addEventListener('pagehide', () => flushClientEvents(true))
}

/** What disabling does: nothing held, nothing scheduled, no rate history. */
export function resetClientTelemetry(): void {
  queue = []
  recent.clear()
  accepted.failure = []
  accepted.timing = []
  retryMs = FLUSH_AFTER_MS
  waitingForTab = false
  if (timer !== null) clearTimeout(timer)
  timer = null
  stopWatchingStall?.()
  stopWatchingStall = null
}
