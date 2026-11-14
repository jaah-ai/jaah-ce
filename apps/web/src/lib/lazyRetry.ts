import { lazy, type ComponentType, type LazyExoticComponent } from 'react'
import { reportClientEvent } from './clientTelemetry'
import { chunkLoadStarted } from './loadTiming'
import { requestStalled } from '../net/stall'
import { refreshStuckChunks, STUCK_AFTER_MS } from './chunkImport'

/* `React.lazy` for a ROUTE chunk that survives a failed or stuck fetch. `lazy` latches its first rejection for the
 * life of the page: a chunk request that hiccups (a dev server under load, a dropped connection) would leave that
 * route on the error boundary, whose Retry re-renders the same rejected promise, until a full reload. So a failed
 * load is retried after each of the growing pauses in `delays` (a dev server re-optimising its dependencies under CI
 * load answers failures for seconds). A chunk that stays unreachable past the last pause (a 404 on a stale tab after
 * a deploy) rejects into the boundary a few seconds later. `delays` is injectable so the unit test does not sit out
 * the real pauses.
 *
 * A load that has not settled after `ATTEMPT_TIMEOUT_MS` counts as failed too: after a laptop wakes, the first
 * import can sit 27-94 s on a dead connection, and a second `import()` of the same address waits on that same
 * request. So before retrying a timed-out load, `refreshStuckChunks` (`chunkImport.ts`) moves each chunk whose
 * address has itself been stuck long enough to a one-off address, and the retry starts a new download (else it
 * joins the request already on its way). The slow load is not abandoned: every attempt
 * still pending keeps racing, so one that is merely slow (a dev server transforming under load) still wins.
 *
 * While a console read is stalled (`net/stall.ts`), the browser says it is offline, or the last attempt timed out,
 * the chunk keeps being tried past the pauses: every `STALLED_POLL_MS`, and at once when the read settles or
 * `online` fires, until `STALLED_CAP_MS` after the first try. The rejection that finally reaches the boundary is
 * marked a chunk-load error (`isChunkLoadError`), so its Retry reloads the page instead of re-rendering the latched
 * promise. A load that failed or timed out at all is reported ONCE (`clientTelemetry.ts`), when it settles:
 * `recovered: true` if a retry saved it, `false` if the boundary gets it. */
export const RETRY_DELAYS_MS: readonly number[] = [400, 1200, 3000]
export const STALLED_CAP_MS = 60_000
export const STALLED_POLL_MS = 5_000
export const ATTEMPT_TIMEOUT_MS = STUCK_AFTER_MS

const marked = new WeakSet<Error>()
const CHUNK_MESSAGE =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i

/** Mark `error` as a lazy chunk that never arrived (a non-Error is wrapped first) and hand it back. */
export function markChunkLoadError(error: unknown): Error {
  const err = error instanceof Error ? error : new Error(String(error))
  marked.add(err)
  return err
}

/** A lazy chunk that never arrived: marked by `lazyRetry`, or a browser's own words for a failed dynamic import. */
export function isChunkLoadError(error: unknown): boolean {
  return error instanceof Error && (marked.has(error) || CHUNK_MESSAGE.test(error.message))
}

export const CHUNK_RELOAD_KEY = 'console-chunk-reload-at'
export const CHUNK_RELOAD_WINDOW_MS = 30_000

/** Reload the console to fetch a chunk `React.lazy` latched as failed, at most once per window so a file that is
 * really gone cannot loop the page; storage that is absent or throws means no reload. The live viewer has the same
 * guard (`live/chunkRecovery.ts::reloadOnce`) but cannot share it: its copy is built as a self-contained classic
 * script, and a module both pages import becomes a shared chunk that `liveChunkRecoveryPlugin` (vite.config.ts)
 * refuses. True when it reloaded. */
export function reloadForChunk(win: Window, now: number = Date.now()): boolean {
  try {
    const last = Number(win.sessionStorage.getItem(CHUNK_RELOAD_KEY))
    if (last && now - last < CHUNK_RELOAD_WINDOW_MS) return false
    win.sessionStorage.setItem(CHUNK_RELOAD_KEY, String(now))
  } catch {
    return false
  }
  win.location.reload()
  return true
}

function connectionDown(): boolean {
  return requestStalled.get() || !navigator.onLine
}

/** Resolve after `ms`, or sooner the moment the stalled read settles or the browser comes back online. */
function untilRecovered(ms: number): Promise<void> {
  return new Promise(resolve => {
    const done = () => {
      clearTimeout(timer)
      unsubscribe()
      window.removeEventListener('online', done)
      resolve()
    }
    const timer = setTimeout(done, ms)
    const unsubscribe = requestStalled.subscribe(() => {
      if (!requestStalled.get()) done()
    })
    window.addEventListener('online', done)
  })
}

function reportChunkError(error: unknown, attempts: number, timedOut: number, elapsed: number, recovered: boolean): void {
  const message = error instanceof Error ? error.message : String(error)
  const url = /https?:\/\/\S+/.exec(message)?.[0] ?? null
  const entries = url ? performance.getEntriesByName(url) : []
  const timing = entries[entries.length - 1] as PerformanceResourceTiming | undefined
  reportClientEvent('chunk_error', {
    message,
    detail: {
      attempts,
      timed_out: timedOut,
      elapsed_ms: elapsed,
      recovered,
      url,
      timing: timing
        ? { next_hop_protocol: timing.nextHopProtocol, duration: Math.round(timing.duration), transfer_size: timing.transferSize }
        : null,
    },
  })
}

type Outcome<T> = { ok: true; value: T } | { ok: false; error: unknown }
type Attempt<T> = { outcome: Promise<Outcome<T>> }

function start<T>(load: () => Promise<T>): Attempt<T> {
  const outcome = Promise.resolve()
    .then(load)
    .then(
      (value): Outcome<T> => ({ ok: true, value }),
      (error: unknown): Outcome<T> => ({ ok: false, error }),
    )
  return { outcome }
}

function sleep(ms: number): { done: Promise<void>; cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | undefined
  const done = new Promise<void>(resolve => {
    timer = setTimeout(resolve, ms)
  })
  return { done, cancel: () => clearTimeout(timer) }
}

type Round<T> = { kind: 'loaded'; value: T } | { kind: 'failed' } | { kind: 'waited' }

/** Race every try in `pending` against `wait`: the first value loaded, `current` failing, or `wait` running out. A
 * failed try leaves `pending`; an older one failing (its error handed to `onError`) leaves the rest racing. */
async function race<T>(
  pending: Attempt<T>[],
  wait: Promise<void>,
  current: Attempt<T> | null,
  onError: (error: unknown) => void,
): Promise<Round<T>> {
  for (;;) {
    const settled = await Promise.race([
      ...pending.map(attempt => attempt.outcome.then(outcome => ({ attempt, outcome }))),
      wait.then(() => null),
    ])
    if (settled === null) return { kind: 'waited' }
    pending.splice(pending.indexOf(settled.attempt), 1)
    if (settled.outcome.ok) return { kind: 'loaded', value: settled.outcome.value }
    onError(settled.outcome.error)
    if (settled.attempt === current) return { kind: 'failed' }
  }
}

/** `load`, retried after each of `delays` (and after each try that outlasts `attemptTimeoutMs`), then while the
 * connection is down or the last try timed out, until `stalledCapMs` from the start. */
export async function loadWithRetry<T>(
  load: () => Promise<T>,
  delays: readonly number[] = RETRY_DELAYS_MS,
  stalledCapMs: number = STALLED_CAP_MS,
  attemptTimeoutMs: number = ATTEMPT_TIMEOUT_MS,
): Promise<T> {
  const startedAt = Date.now()
  const pending: Attempt<T>[] = []
  let lastError: unknown = null
  let timedOut = 0
  const recordError = (error: unknown) => {
    lastError = error
  }
  const failure = () =>
    lastError ?? new Error(`Timed out loading a route chunk (${timedOut} tries of ${attemptTimeoutMs} ms)`)
  const loaded = (value: T, attempts: number): T => {
    if (lastError !== null || timedOut > 0) reportChunkError(failure(), attempts, timedOut, Date.now() - startedAt, true)
    return value
  }

  for (let attempts = 1; ; attempts++) {
    const current = start(load)
    pending.push(current)
    // Past the pauses, a try gets one poll interval (never beyond the cap) before the next fresh address.
    const pastPauses = attempts > delays.length + 1
    const roundMs = pastPauses
      ? Math.max(0, Math.min(attemptTimeoutMs, STALLED_POLL_MS, stalledCapMs - (Date.now() - startedAt)))
      : attemptTimeoutMs
    const timer = sleep(roundMs)
    const round = await race(pending, timer.done, current, recordError)
    timer.cancel()
    if (round.kind === 'loaded') return loaded(round.value, attempts)
    const lastTimedOut = round.kind === 'waited'
    if (lastTimedOut) timedOut++

    const elapsed = Date.now() - startedAt
    const pause = delays[attempts - 1]
    const keepTrying = pause === undefined && (lastTimedOut || connectionDown()) && elapsed < stalledCapMs
    if (pause === undefined && !keepTrying) {
      const error = failure()
      reportChunkError(error, attempts, timedOut, elapsed, false)
      throw markChunkLoadError(error)
    }
    if (lastTimedOut) refreshStuckChunks()
    // A timed-out try past the pauses already waited its poll interval: the next one starts at once.
    if (pause === undefined && lastTimedOut) continue
    const wait = pause !== undefined ? sleep(pause) : null
    const recovered = wait?.done ?? untilRecovered(Math.min(STALLED_POLL_MS, stalledCapMs - elapsed))
    const paused = await race(pending, recovered, null, recordError)
    wait?.cancel()
    if (paused.kind === 'loaded') return loaded(paused.value, attempts)
  }
}

/* WARM EVERY CHUNK ONCE THE CONSOLE IS IDLE. Retries cannot save a chunk on a network that silently kills the
 * browser's connection (`net/relay.ts`): reads race over the relay's WebSocket, but a dynamic `import()` can only wait
 * for the browser to open a new connection, and on one customer's network each try hung 40-200 s until the route gave
 * up on the boundary. So after the first paint every chunk `lazyRetry` knows is imported once, one at a time, at idle:
 * a later navigation finds its module already loaded and fetches nothing. A chunk that loads registers its own lazy
 * children, so the walk continues until nothing new is known. A failure is ignored (the navigation that needs it
 * retries as before). Production builds only: on the dev server every import is a transform, and warming them all
 * would slow every e2e page. Skipped when the browser asks to save data. */
const loaders = new Set<() => Promise<unknown>>()
const warmed = new Set<() => Promise<unknown>>()
let warming = false

function whenIdle(): Promise<void> {
  return new Promise(resolve => {
    if (typeof window.requestIdleCallback === 'function') window.requestIdleCallback(() => resolve(), { timeout: 5_000 })
    else setTimeout(resolve, 200)
  })
}

/** Import every known lazy chunk once, sequentially at idle; resolves when none is left. Idempotent. */
export async function prefetchChunks(): Promise<void> {
  if (warming) return
  const save = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection?.saveData
  if (save) return
  warming = true
  try {
    for (let next = pending(); next; next = pending()) {
      warmed.add(next)
      await whenIdle()
      await Promise.resolve().then(next).catch(() => undefined)
    }
  } finally {
    warming = false
  }
}

function pending(): (() => Promise<unknown>) | undefined {
  for (const load of loaders) if (!warmed.has(load)) return load
  return undefined
}

/** Start `prefetchChunks` a few seconds after the page has loaded, in a production build only. */
export function prefetchChunksAfterLoad(win: Window = window, delayMs = 3_000): void {
  if (!import.meta.env.PROD) return
  const start = () => setTimeout(() => void prefetchChunks(), delayMs)
  if (win.document.readyState === 'complete') start()
  else win.addEventListener('load', start, { once: true })
}

export function lazyRetry<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
  delays: readonly number[] = RETRY_DELAYS_MS,
): LazyExoticComponent<T> {
  loaders.add(load)
  return lazy(() => {
    // The page or route is still loading while its chunk is in flight (`loadTiming.ts`).
    const settled = chunkLoadStarted()
    return loadWithRetry(load, delays).finally(settled)
  })
}
