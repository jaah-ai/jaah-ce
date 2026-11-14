/* A REQUEST RACED ON A FRESH CONNECTION. Some networks silently kill the browser's HTTP/2 connection to the console
 * after a handful of requests, and the browser only opens a new one ~20 s later; no script can force it sooner
 * (`net/stall.ts` notes the stall, silently). A WebSocket is always a new TCP connection here (the load balancer
 * offers no WebSocket-over-HTTP/2), so a read unanswered after `RACE_AFTER_MS` is also sent over the API's read
 * relay (`apps/api/app/routes/read_relay.py`), which replays it in-process as this browser, and the first answer
 * wins. A write is raced by the same rules under its idempotency key (WRITES, below), so the server applies it once.
 *
 * A SLOW ROUTE IS NOT A DEAD CONNECTION. Before a read is raced, the direct connection must look dead: no direct answer
 * has landed in the last `RACE_AFTER_MS`, and a cheap probe of the console's static manifest (`PROBE_PATH`, never an
 * API worker) goes unanswered for `PROBE_MS`. A connection that answers either way is alive and the read keeps waiting
 * on it alone, checked again every `RACE_AFTER_MS`: relaying a merely slow read sends it to the API twice, and the
 * aborted direct copy keeps running there, so a slow API got slower the more it was raced.
 *
 * Once the relay has won a race, the direct connection is suspect: for `SUSPECT_MS` a read skips the `RACE_AFTER_MS`
 * wait and probes at once, but it still races only when that probe goes unanswered: a suspect connection that answers
 * its probe is alive, and the read waits on it alone (sending every read twice at once doubled a slow server's load).
 * A direct read answered while suspect ends the suspicion: the connection is back. A relay win whose own answer shows
 * the app held it for `RACE_AFTER_MS` or more (`Server-Timing: app;dur=`, `app/server_timing.py`) was mostly a slow
 * server's time, not a dead connection's: it sets the suspicion (a probe at once costs a slow server nothing) but never
 * the dropping-network memory below, which would otherwise outlive the slowness for `DROPPING_NETWORK_MS`.
 *
 * A NETWORK THAT DROPS QUIET CONNECTIONS. The suspicion outlives no quiet spell longer than `SUSPECT_MS`, so on such
 * a network every burst after a few idle minutes paid `RACE_AFTER_MS` + `PROBE_MS` before the relay took over
 * (measured on a customer's: exactly 2.0 s, burst after burst). So the TAB remembers, for `DROPPING_NETWORK_MS`, a
 * relay win that followed a PROVEN stall (the patient path: no direct answer, an unanswered probe); while it does, a
 * direct connection quiet for `IDLE_DROP_MS` is taken for suspect and the next read probes at once, as above. A win in
 * a race started on that fast path renews nothing, and a direct answer that beats such a race clears the memory: that
 * connection survived the quiet. A tab whose relay never won after a real stall keeps the patient path.
 *
 * A relay socket can be killed silently too: the relay acknowledges each read the moment it takes it, so a read
 * unacknowledged after `RELAY_ACK_MS` retires its socket and goes again on a new one; an acknowledged read that
 * still has no answer after `RELAY_ANSWER_MS` (a slow route) is given up on that socket alone. A socket carries at
 * most `MAX_READS_PER_SOCKET` reads, and closes once its last one is answered. A read whose direct copy won (or whose
 * caller aborted) sends `{"cancel": id}`, so the server stops replaying it; a write's relayed copy is never cancelled.
 *
 * The socket holds the session cookie it opened with, so it is dropped whenever the signed-in identity changes
 * (`resetReadRelay`, called by `auth.tsx`), and a relayed 401 never wins: the direct read decides a refusal.
 * On the Live Cast host the same path opens with no session cookie (the host has none), so the server takes it as its
 * ANONYMOUS socket, which carries only `GET /api/public/live/*`: the viewer's reads race by these same rules.
 *
 * The last race's outcome (`relayState`) rides on every client report (`clientTelemetry.ts`), so a stall on the record
 * says whether the relay rescued it, how fast, or why it could not. */

import { ORG_SWITCHED_EVENT, SESSION_CHANGED_EVENT } from '../lib/activeOrg'
import { base64ToBytes, bytesToBase64 } from '../lib/base64'
import { ApiError, NoAnswerError, OUTCOME_UNKNOWN_CODE } from './errors'

export const RACE_AFTER_MS = 1_000
export const SUSPECT_MS = 120_000
export const DROPPING_NETWORK_MS = 30 * 60_000
/** Above the console's 30 s polls, so a polling page is not raced twice per poll; the customer's drops came after ~1 min. */
export const IDLE_DROP_MS = 45_000
export const RELAY_ACK_MS = 1_500
export const RELAY_ANSWER_MS = 10_000
export const MAX_READS_PER_SOCKET = 12
export const PROBE_MS = 1_000
export const PROBE_PATH = '/version.json'
const RELAY_PATH = '/api/read-relay'
const SOCKET_OPEN_MS = 3_000
const MAX_RELAY_ATTEMPTS = 3
/** A write body the relay carries, decoded (`apps/api/app/routes/read_relay.py::MAX_WRITE_BODY_BYTES`). */
export const MAX_RELAY_WRITE_BYTES = 1024 * 1024
/** A write neither copy answered within this long, on a connection that does not answer its probe, fails. */
export const WRITE_BOUND_MS = 30_000
/** The server's answer to a copy of a write whose other copy is still running: not an answer (`idempotency.py`). */
const IN_PROGRESS = 'idempotency_in_progress'
/** Status codes whose `Response` may carry no body (the constructor throws if given one). */
const NULL_BODY = new Set([101, 204, 205, 304])

type Answer = { status: number; headers: Record<string, string>; body: string }
type Waiter = { resolve(answer: Answer): void; reject(err: Error): void; acked(): void }

/** Why a relayed read gave no usable answer. `refused`: final for this read; `silent`: the socket is dead. */
class RelayMiss extends Error {
  constructor(readonly kind: 'refused' | 'silent' | 'slow' | 'closed', detail: string) {
    super(`relay ${kind}: ${detail}`)
  }
}

let enabled = typeof WebSocket === 'function'
let suspectUntil = 0
/** When this tab's relay last won a race after a proven stall (0: never, or cleared): the tab is on a network that
 *  drops quiet connections. */
let relayWonAt = 0
let nextId = 0
let current: RelaySocket | null = null
/** When a direct read (or the liveness probe) last got an answer: proof the direct connection is alive. */
let lastDirectAnswerAt = 0
/** The probe in flight per credentials mode (`probeDirect`). */
const probesInFlight = new Map<RequestCredentials, Promise<boolean>>()

/** How the last relayed read ended: `won` it, or why not (`direct` answered first, or the relay's own miss). A win
 *  also splits its `latency_ms`: `open_ms` the winning attempt's wait for its relay socket to open (0 on one already
 *  open) and `server_ms` the app's own time (`Server-Timing`); the rest is the network, the relay hop and any earlier
 *  attempt on a socket that went silent. */
type RelayOutcome = { won: boolean; latency_ms: number; reason: string | null; open_ms?: number | null; server_ms?: number | null }
let lastOutcome: RelayOutcome | null = null
/** Every `Response` the relay produced: `stall.ts` reports a write as relayed by it. */
const relayedAnswers = new WeakSet<Response>()
/** How long the read behind each relayed `Response` waited for its socket to open. */
const openWaits = new WeakMap<Response, number>()

/** Record how a relayed copy sent at `sentAt` ended; `answer` is the winning relayed response. */
function recordOutcome(sentAt: number, won: boolean, reason: string | null, answer?: Response): void {
  lastOutcome = { won, latency_ms: Date.now() - sentAt, reason }
  if (answer) {
    lastOutcome.open_ms = openWaits.get(answer) ?? null
    lastOutcome.server_ms = serverMs(answer)
  }
}

/** Whether `response` came over the relay rather than the direct connection. */
export function cameOverRelay(response: unknown): boolean {
  return response instanceof Response && relayedAnswers.has(response)
}

/** Whether the direct connection is currently suspect, whether this tab is on a network that drops quiet connections,
 *  and how the last race went (null: none raced yet). */
export function relayState(): { suspect: boolean; dropping: boolean; last: RelayOutcome | null } {
  const now = Date.now()
  return { suspect: now < suspectUntil, dropping: droppingNetwork(now), last: lastOutcome }
}

/** Forget the relay socket and the suspicion: the next race opens a socket under the session the browser holds now. */
export function resetReadRelay(): void {
  suspectUntil = 0
  relayWonAt = 0
  lastDirectAnswerAt = 0
  probesInFlight.clear()
  current?.retire()
  current = null
}

/* The socket replays every request with the cookie it opened with. Once another tab switched org or signed in
   someone else, that cookie is not the one this tab's direct requests now carry, so a relayed write could be judged
   as a session the direct copy is not: the socket goes (the liveness memory stays — the network did not change). */
if (typeof window !== 'undefined') {
  const dropSocket = () => { current?.retire(); current = null }
  window.addEventListener(ORG_SWITCHED_EVENT, dropSocket)
  window.addEventListener(SESSION_CHANGED_EVENT, dropSocket)
}

/** Test seam: turn the relay on or off (and reset it). */
export function setReadRelayEnabled(on: boolean): void {
  enabled = on
  resetReadRelay()
}

/** Whether a read should probe at once (and race only if the probe goes unanswered): the connection is suspect, or this
 *  tab's network drops quiet connections and this one has been quiet for `IDLE_DROP_MS`. */
function raceAtOnce(now: number): boolean {
  return now < suspectUntil || quietOnDroppingNetwork(now)
}

/** A relayed copy won its race (a read's or a write's) with `response`: the direct connection is suspect for
 *  `SUSPECT_MS`, and a win after a proven stall (`proven`) marks this tab's network as one that drops quiet connections
 *  — unless the app itself held the answer past the race window (`slowServer`): that wait was the server's. */
function relayWon(proven: boolean, response: Response): void {
  const now = Date.now()
  if (proven && !slowServer(response)) relayWonAt = now
  suspectUntil = now + SUSPECT_MS
}

/** The direct copy answered, won or lost: the connection is alive. Unless the race is already over (`done`), the
 *  suspicion is dropped too, and a race started at once for a quiet spell (`afterQuiet`) forgets the dropping
 *  network: the connection survived the spell after all. */
function directAnswered(done: boolean, afterQuiet: boolean): void {
  lastDirectAnswerAt = Date.now()
  if (done) return
  suspectUntil = 0
  if (afterQuiet) relayWonAt = 0
}

/** One relay copy's score, from the moment it is sent (`recordOutcome`): `won` with the winning answer (which also
 *  marks the direct connection suspect: `relayWon`), `lost` with why, or `failed` with its error — read as a loss to
 *  the direct copy once the race is over (`done`), else in the error's own words. */
function relayScore(proven: boolean): { won: (answer: Response) => void; lost: (reason: string) => void; failed: (done: boolean, err: unknown) => void } {
  const sentAt = Date.now()
  return {
    won: answer => { recordOutcome(sentAt, true, null, answer); relayWon(proven, answer) },
    lost: reason => recordOutcome(sentAt, false, reason),
    failed: (done, err) => recordOutcome(sentAt, false, done ? 'direct' : err instanceof Error ? err.message : String(err)),
  }
}

function droppingNetwork(now: number): boolean {
  return relayWonAt > 0 && now >= relayWonAt && now - relayWonAt < DROPPING_NETWORK_MS
}

function quietOnDroppingNetwork(now: number): boolean {
  return droppingNetwork(now) && now - lastDirectAnswerAt >= IDLE_DROP_MS
}

/** Whether the direct connection answered within the last `RACE_AFTER_MS`. */
function directRecentlyAnswered(): boolean {
  return Date.now() - lastDirectAnswerAt < RACE_AFTER_MS
}

/** One shared probe of the direct connection: true when anything answers within `PROBE_MS`. Any status counts (a
 *  404 still came back over the connection); the manifest is static, so a busy API cannot slow the answer.
 *  `credentials` is the read's own: a browser pools credential-less (`omit`) requests on ANOTHER connection than
 *  `same-origin` ones, so a probe in the other mode would answer over a healthy socket while the read's own one lies
 *  dead (the live host's reads are `omit`, the console's `same-origin`). */
function probeDirect(credentials: RequestCredentials = 'same-origin'): Promise<boolean> {
  const inFlight = probesInFlight.get(credentials)
  if (inFlight) return inFlight
  const ctl = new AbortController()
  const timer = delay(PROBE_MS)
  const probe = Promise.race([
    fetch(PROBE_PATH, { cache: 'no-store', credentials, signal: ctl.signal }).then(
      () => { lastDirectAnswerAt = Date.now(); return true },
      () => false,
    ),
    timer.done.then(() => false),
  ]).finally(() => {
    timer.clear()
    ctl.abort()
    if (probesInFlight.get(credentials) === probe) probesInFlight.delete(credentials)
  })
  probesInFlight.set(credentials, probe)
  return probe
}

function delay(ms: number): { done: Promise<void>; clear(): void } {
  let timer: ReturnType<typeof setTimeout> | undefined
  const done = new Promise<void>(resolve => { timer = setTimeout(resolve, ms) })
  return { done, clear: () => clearTimeout(timer) }
}

class RelaySocket {
  private readonly ws: WebSocket
  private readonly waiters = new Map<number, Waiter>()
  private reads = 0
  private dead = false
  private draining = false
  private isOpen = false
  readonly opened: Promise<void>

  constructor() {
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    this.ws = new WebSocket(`${scheme}//${window.location.host}${RELAY_PATH}`)
    this.opened = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => { this.retire(); reject(new RelayMiss('silent', 'did not open')) }, SOCKET_OPEN_MS)
      this.ws.onopen = () => { clearTimeout(timer); this.isOpen = true; resolve() }
      this.ws.onclose = () => { clearTimeout(timer); this.retire(); reject(new RelayMiss('closed', 'socket closed')) }
    })
    this.opened.catch(() => undefined)
    this.ws.onmessage = event => this.receive(event.data)
  }

  /** Whether another read may go on this socket. */
  get usable(): boolean {
    return !this.dead && !this.draining && this.reads < MAX_READS_PER_SOCKET
  }

  /** Take no more reads; close once the last one is answered. */
  drain(): void {
    this.draining = true
    this.closeIfDrained()
  }

  /** `cancel()` forgets the read here; `cancel(true)` also tells the server to stop replaying it (`read_relay.py`). */
  send(path: string, headers: Record<string, string>, write?: RelayWrite): { acked: Promise<void>; answer: Promise<Answer>; cancel(notify?: boolean): void } {
    const id = nextId++
    this.reads += 1
    let markAcked = () => {}
    const acked = new Promise<void>(resolve => { markAcked = resolve })
    const answer = new Promise<Answer>((resolve, reject) => {
      this.waiters.set(id, { resolve, reject, acked: markAcked })
      this.opened.then(
        () => {
          if (this.dead || !this.waiters.has(id)) return
          // A read's frame is exactly what it always was; a write names its method and carries its body.
          const frame = write ? { id, method: write.method, path, headers, body: write.body } : { id, path, headers }
          this.ws.send(JSON.stringify(frame))
        },
        (err: Error) => reject(err),
      )
    })
    // Observed by `relayRead` once the socket opens; a retirement before then is not an unhandled error.
    answer.catch(() => undefined)
    const cancel = (notify = false) => {
      const waiter = this.waiters.get(id)
      if (!waiter) return
      this.waiters.delete(id)
      if (notify && this.isOpen && !this.dead) {
        try { this.ws.send(JSON.stringify({ cancel: id })) } catch { /* closing: the server cancels it with the socket */ }
      }
      waiter.reject(new RelayMiss('closed', 'cancelled'))
      this.closeIfDrained()
    }
    return { acked, answer, cancel }
  }

  retire(): void {
    if (this.dead) return
    this.dead = true
    for (const waiter of this.waiters.values()) waiter.reject(new RelayMiss('closed', 'socket retired'))
    this.waiters.clear()
    try { this.ws.close() } catch { /* already closing */ }
  }

  private closeIfDrained(): void {
    if (this.draining && this.waiters.size === 0) this.retire()
  }

  private receive(data: unknown): void {
    let frame: { id?: number; ack?: boolean; error?: string } & Partial<Answer>
    try {
      frame = JSON.parse(String(data))
    } catch {
      return
    }
    const id = frame.id
    const waiter = typeof id === 'number' ? this.waiters.get(id) : undefined
    if (!waiter || typeof id !== 'number') return
    if (frame.ack === true) return waiter.acked()
    this.waiters.delete(id)
    if (frame.error !== undefined || typeof frame.status !== 'number') {
      waiter.reject(new RelayMiss('refused', frame.error ?? 'malformed'))
    } else {
      waiter.resolve({ status: frame.status, headers: frame.headers ?? {}, body: frame.body ?? '' })
    }
    this.closeIfDrained()
  }
}

function socket(): RelaySocket {
  if (current?.usable) return current
  current?.drain()
  current = new RelaySocket()
  return current
}

function toResponse(answer: Answer): Response {
  const bytes = base64ToBytes(answer.body)
  const response = new Response(NULL_BODY.has(answer.status) ? null : bytes, { status: answer.status, headers: answer.headers })
  relayedAnswers.add(response)
  return response
}

/** Resolve with `promise`, or reject with `miss` after `ms`; the timer never outlives the race. */
async function within<T>(promise: Promise<T>, ms: number, miss: RelayMiss): Promise<T> {
  const timer = delay(ms)
  try {
    return await Promise.race([promise, timer.done.then(() => { throw miss })])
  } finally {
    timer.clear()
  }
}

/** What a relayed write adds to a read's frame: its method, its body (base64), and how long its answer may take. */
type RelayWrite = { method: string; body?: string; deadline: number; onAck(): void }

/** One request over the relay, on up to `MAX_RELAY_ATTEMPTS` sockets (a write: every copy under its one key). */
async function relayRead(
  path: string,
  headers: Record<string, string>,
  aborted: () => boolean,
  write?: RelayWrite,
  lost?: AbortSignal,
): Promise<Response> {
  let lastMiss: unknown = new RelayMiss('silent', 'no answer')
  for (let attempt = 0; attempt < MAX_RELAY_ATTEMPTS && !aborted(); attempt++) {
    const relay = socket()
    const sent = relay.send(path, headers, write)
    // A read whose race is over (`lost`): the server stops replaying it.
    const stop = () => sent.cancel(true)
    lost?.addEventListener('abort', stop, { once: true })
    try {
      const waitedFrom = Date.now()
      await relay.opened
      const openMs = Date.now() - waitedFrom
      await within(Promise.race([sent.acked, sent.answer.then(() => undefined)]), RELAY_ACK_MS, new RelayMiss('silent', 'no ack'))
      write?.onAck()
      // A write's answer may take until its own bound, re-read per attempt so no copy outlives it.
      const answerMs = write ? Math.max(RELAY_ACK_MS, write.deadline - Date.now()) : RELAY_ANSWER_MS
      const answer = await within(sent.answer, answerMs, new RelayMiss('slow', 'no answer'))
      // The socket's cookie may predate this tab's sign-in: a refusal is the direct read's to decide.
      if (answer.status === 401) throw new RelayMiss('refused', 'unauthorized')
      if (write && answer.status === 403) throw new RelayMiss('refused', 'forbidden')
      const response = toResponse(answer)
      openWaits.set(response, openMs)
      return response
    } catch (err) {
      // A read given up on here (slow, refused, its socket retired) is a losing copy too: the server stops it.
      sent.cancel(!write)
      lastMiss = err
      if (!(err instanceof RelayMiss) || err.kind === 'refused') break
      // A socket that did not even acknowledge is dead; a slow route is given up on that socket alone.
      if (err.kind === 'silent') relay.retire()
    } finally {
      lost?.removeEventListener('abort', stop)
    }
  }
  throw lastMiss
}

/** How long the app held this answer (`Server-Timing: app;dur=<ms>`, `app/server_timing.py`), or null when unsaid. */
export function serverMs(response: Response): number | null {
  const match = /(?:^|,)\s*app;dur=([\d.]+)/.exec(response.headers.get('server-timing') ?? '')
  return match ? Number(match[1]) : null
}

/** A relayed answer the app itself held for `RACE_AFTER_MS` or more: the server was slow, so its win says nothing
 *  about the direct connection. */
function slowServer(response: Response): boolean {
  return (serverMs(response) ?? 0) >= RACE_AFTER_MS
}

/** Whether `path` is a console read the relay may carry. */
function relayable(path: string): boolean {
  return enabled && path.startsWith('/api/')
}

/** What `raceRequest` takes beyond `fetch`'s own init (`send.ts::SendInit`, the fields the race reads). */
export type RaceInit = RequestInit & { relay?: 'session' | 'anon' | 'none'; boundMs?: number }

/**
 * THE ONE RACE every console request goes through (`send.ts`). A read is raced exactly as it always was (`raceRead`);
 * a write by the same liveness rules, under the idempotency key `send` already put in `headers` (`raceWrite`).
 * `relay: 'none'` sends `direct` alone.
 */
export function raceRequest(
  path: string,
  init: RaceInit,
  headers: HeadersInit | undefined,
  direct: (init: RequestInit) => Promise<Response>,
): Promise<Response> {
  const { relay, boundMs, ...request } = init
  if ((request.method ?? 'GET').toUpperCase() === 'GET') {
    return relay === 'none' ? direct(request) : raceRead(path, request, headers, direct)
  }
  return raceWrite(path, request, headers, direct, { relay: relay !== 'none', boundMs: boundMs ?? WRITE_BOUND_MS })
}

/**
 * Send a read with `direct` (the console's own `fetch`) and, if it is unanswered after `RACE_AFTER_MS` while the
 * connection looks dead (probed at once while it is suspect, or quiet on a dropping network: `raceAtOnce`), over the relay
 * too; resolve with whichever answers first. The loser is aborted, or cancelled on the server. A caller's own abort ends both.
 */
function raceRead(
  path: string,
  init: RequestInit,
  headers: HeadersInit | undefined,
  direct: (init: RequestInit) => Promise<Response>,
): Promise<Response> {
  const outer = init.signal ?? null
  if (!relayable(path) || outer?.aborted) return direct(init)
  const ctl = new AbortController()
  /** Aborted once the race is over: a relayed copy still out is cancelled on the server. */
  const over = new AbortController()

  const startedAt = Date.now()
  /** Raced at once only because the connection had been quiet on a dropping network (not suspect). */
  const afterQuiet = startedAt >= suspectUntil && quietOnDroppingNetwork(startedAt)

  return new Promise<Response>((resolve, reject) => {
    let done = false
    let relaying = false
    let directFailed: unknown = null
    let timer: ReturnType<typeof setTimeout> | undefined
    const finish = (settle: () => void) => {
      if (done) return
      done = true
      clearTimeout(timer)
      outer?.removeEventListener('abort', onAbort)
      settle()
      over.abort()
    }
    function onAbort() {
      ctl.abort(outer?.reason)
      finish(() => reject(outer?.reason ?? new DOMException('aborted', 'AbortError')))
    }
    outer?.addEventListener('abort', onAbort, { once: true })
    /** `proven`: started on the patient path, after the direct connection failed its probe. */
    const startRelay = (proven: boolean) => {
      if (done) return
      relaying = true
      const score = relayScore(proven)
      const sentHeaders = Object.fromEntries(new Headers(headers).entries())
      relayRead(path, sentHeaders, () => done, undefined, over.signal).then(
        response => {
          if (done) return score.lost('direct')
          score.won(response)
          finish(() => resolve(response))
          ctl.abort()
        },
        err => {
          score.failed(done, err)
          relaying = false
          if (directFailed !== null) finish(() => reject(directFailed))
        },
      )
    }
    direct({ ...init, signal: ctl.signal }).then(
      response => {
        directAnswered(done, afterQuiet)
        finish(() => resolve(response))
      },
      err => {
        if (relaying) directFailed = err
        else finish(() => reject(err))
      },
    )
    /* Race only a connection that looks dead (see the header): a recent direct answer or an answered probe means
       this read is merely slow, so it waits on, checked again a `RACE_AFTER_MS` later. `proven`: false on the fast
       path's first check (a suspect connection probed at once), true once the read has waited the window. */
    const check = (proven: boolean) => {
      if (done) return
      if (directRecentlyAnswered()) {
        timer = setTimeout(check, RACE_AFTER_MS, true)
        return
      }
      void probeDirect(init.credentials).then(alive => {
        if (done) return
        if (alive) timer = setTimeout(check, RACE_AFTER_MS, true)
        else startRelay(proven)
      })
    }
    timer = raceAtOnce(startedAt) ? setTimeout(check, 0, false) : setTimeout(check, RACE_AFTER_MS, true)
  })
}

/* ── WRITES ───────────────────────────────────────────────────────────────────────────────────────────────────────
 * A write is raced by the read's rules (`raceAtOnce`'s probe at once, else the patient path: `RACE_AFTER_MS`, the shared
 * probe), and
 * may safely go twice because `send` gave it ONE `Idempotency-Key` and the server applies a key at most once
 * (`apps/api/app/idempotency.py`). What differs from a read:
 *  - its body rides the relay frame (base64, at most `MAX_RELAY_WRITE_BYTES`); a `FormData` body is serialized ONCE, up
 *    front, and that same byte string goes direct too, so both copies carry one fingerprint (a second multipart
 *    boundary would read to the server as a different body under the same key);
 *  - a relayed 401 or 403 never wins, and a `409 idempotency_in_progress` from either copy is not an answer while the
 *    other copy is still open;
 *  - a direct copy that FAILS (no HTTP answer at all) sends the relay copy at once, if it is not out already;
 *  - it is bounded: `PROBE_MS` before `boundMs` the shared probe runs; if the direct connection does not answer it,
 *    the direct copy is aborted at `boundMs` and `NoAnswerError` is thrown. A connection that answers the probe is
 *    alive and the write merely slow, so it keeps waiting, as before: aborting a live write would only invite the user
 *    to retry it under a NEW key and apply it twice. */

/** How a write's body travels: what `direct` sends, and the relay frame's base64 (`undefined`: it cannot ride). */
type PreparedWrite = { init: RequestInit; relayBody: () => Promise<string | null | undefined>; frameHeaders: Headers }

/** `null`: no body. `undefined`: too large or unreadable, so the write goes direct only. */
async function encodeBody(body: BodyInit, headers: Headers): Promise<string | null | undefined> {
  if (typeof ReadableStream !== 'undefined' && body instanceof ReadableStream) return undefined
  try {
    const read = new Response(body)
    const bytes = new Uint8Array(await read.arrayBuffer())
    if (bytes.byteLength > MAX_RELAY_WRITE_BYTES) return undefined
    const type = read.headers.get('content-type')
    if (type && !headers.has('content-type')) headers.set('content-type', type)
    return bytesToBase64(bytes)
  } catch {
    return undefined
  }
}

/** How the write's body travels. Synchronous except for a `FormData` body, serialized before either copy is sent. */
function prepareWrite(
  init: RequestInit,
  headers: HeadersInit | undefined,
): PreparedWrite | Promise<PreparedWrite> {
  const frameHeaders = new Headers(headers)
  const body = init.body
  if (body === undefined || body === null) {
    return { init: { ...init, headers }, relayBody: async () => null, frameHeaders }
  }
  if (typeof FormData !== 'undefined' && body instanceof FormData) {
    // Sized before it is serialized: a large upload goes direct, as the browser streams it, never read into memory.
    let size = 0
    for (const [, value] of body.entries()) size += typeof value === 'string' ? value.length * 3 : value.size
    if (size > MAX_RELAY_WRITE_BYTES) return { init: { ...init, headers }, relayBody: async () => undefined, frameHeaders }
    // The serialized bytes' own boundary, whatever a caller named.
    frameHeaders.delete('content-type')
    return encodeBody(body, frameHeaders).then(encoded => {
      if (encoded === undefined || encoded === null) return { init: { ...init, headers }, relayBody: async () => undefined, frameHeaders }
      // The serialized bytes, with the boundary they were serialized under, go direct too: one body, one fingerprint.
      const bytes = base64ToBytes(encoded)
      const sent = { ...init, body: bytes, headers: Object.fromEntries(frameHeaders.entries()) }
      return { init: sent, relayBody: async () => encoded, frameHeaders }
    })
  }
  // Encoded only if the relay copy is ever sent: a healthy write pays nothing.
  return { init: { ...init, headers }, relayBody: () => encodeBody(body, frameHeaders), frameHeaders }
}

/** Whether `response` is the server's "the other copy is still running" (`idempotency.py::IN_PROGRESS`). */
async function inProgress(response: Response): Promise<boolean> {
  if (response.status !== 409) return false
  const body = await response.clone().json().catch(() => null) as { detail?: unknown } | null
  return body?.detail === IN_PROGRESS
}

function raceWrite(
  path: string,
  init: RequestInit,
  headers: HeadersInit | undefined,
  direct: (init: RequestInit) => Promise<Response>,
  { relay, boundMs }: { relay: boolean; boundMs: number },
): Promise<Response> {
  const outer = init.signal ?? null
  if (outer?.aborted) return direct({ ...init, headers })
  const ctl = new AbortController()
  const startedAt = Date.now()
  const afterQuiet = startedAt >= suspectUntil && quietOnDroppingNetwork(startedAt)

  return new Promise<Response>((resolve, reject) => {
    let done = false
    let directOpen = true
    let relayOpen = false
    let relaySent = false
    /** The server took a relay copy (it acknowledged it): past the bound, the write may have been applied. */
    let relayTaken = false
    /** A `409 idempotency_in_progress`, kept as the answer only if no other copy answers. */
    let held: Response | null = null
    let directFailed: unknown = null
    const timers: ReturnType<typeof setTimeout>[] = []
    const later = (ms: number, run: () => void) => { timers.push(setTimeout(run, ms)) }
    const finish = (settle: () => void) => {
      if (done) return
      done = true
      for (const timer of timers) clearTimeout(timer)
      settle()
    }
    /* Left attached after the answer: a caller that stops reading a streamed body (a support chat's Stop) still ends
       the direct copy, exactly as when its own signal rode a bare `fetch`. */
    function onAbort() {
      ctl.abort(outer?.reason)
      finish(() => reject(outer?.reason ?? new DOMException('aborted', 'AbortError')))
    }
    outer?.addEventListener('abort', onAbort, { once: true })
    /** Both copies are over without a usable answer: the held in-progress answer, else the direct copy's failure. */
    const settleIfIdle = () => {
      if (directOpen || relayOpen) return
      if (held) finish(() => resolve(held as Response))
      else if (directFailed !== null) finish(() => reject(directFailed))
    }

    const begin = (prepared: PreparedWrite) => {
      if (done) return
      const canRelay = relay && relayable(path)

      const startRelay = (proven: boolean) => {
        if (done || relaySent || !canRelay) return
        relaySent = true
        relayOpen = true
        const score = relayScore(proven)
        void prepared.relayBody().then(async body => {
          if (body === undefined) throw new RelayMiss('refused', 'too_large')
          const frameHeaders = Object.fromEntries(prepared.frameHeaders.entries())
          const method = (init.method ?? 'POST').toUpperCase()
          const onAck = () => { relayTaken = true }
          return relayRead(path, frameHeaders, () => done, { method, body: body ?? undefined, deadline: startedAt + boundMs, onAck })
        }).then(
          async response => {
            if (done) return score.lost('direct')
            if (await inProgress(response)) {
              score.lost(IN_PROGRESS)
              relayOpen = false
              held = held ?? response
              return settleIfIdle()
            }
            if (done) return score.lost('direct')
            score.won(response)
            finish(() => resolve(response))
            ctl.abort()
          },
          err => {
            score.failed(done, err)
            relayOpen = false
            settleIfIdle()
          },
        )
      }

      direct({ ...prepared.init, signal: ctl.signal }).then(
        async response => {
          directAnswered(done, afterQuiet)
          if (relayOpen && await inProgress(response)) {
            directOpen = false
            held = held ?? response
            return settleIfIdle()
          }
          finish(() => resolve(response))
        },
        err => {
          directOpen = false
          if (done) return
          directFailed = err
          // No HTTP answer at all: the connection is no use for this write; the key makes a second copy safe.
          startRelay(false)
          settleIfIdle()
        },
      )

      const check = (proven: boolean) => {
        if (done || relaySent) return
        if (directRecentlyAnswered()) return later(RACE_AFTER_MS, () => check(true))
        void probeDirect().then(alive => {
          if (done) return
          if (alive) later(RACE_AFTER_MS, () => check(true))
          else startRelay(proven)
        })
      }
      if (canRelay) {
        if (raceAtOnce(startedAt)) later(0, () => check(false))
        else later(RACE_AFTER_MS, () => check(true))
      }

      /* The bound: probed just before it, so a write on a connection that still answers is slow, not lost. */
      const bound = () => {
        if (done) return
        if (directOpen) {
          ctl.abort()
          directOpen = false
        }
        if (held) return finish(() => resolve(held as Response))
        // "Not saved" only when no copy is known to have reached the server; a retry then mints a new key.
        if (relayTaken) return finish(() => reject(new ApiError(0, OUTCOME_UNKNOWN_CODE)))
        finish(() => reject(new NoAnswerError()))
      }
      /* Re-probed every `RACE_AFTER_MS` past the deadline while the connection still answers: one that dies later
         still ends the write. */
      const deadline = startedAt + boundMs
      const checkBound = () => {
        if (done) return
        const probe = directOpen ? probeDirect() : Promise.resolve(false)
        void probe.then(alive => {
          if (done) return
          if (alive && directOpen) return later(RACE_AFTER_MS, checkBound)
          const left = deadline - Date.now()
          if (left > 0) later(left, bound)
          else bound()
        })
      }
      later(Math.max(0, boundMs - PROBE_MS - (Date.now() - startedAt)), checkBound)
    }
    // A body that needs no serializing goes out at once, in this same tick, as a bare `fetch` would.
    const prepared = prepareWrite(init, headers)
    if (prepared instanceof Promise) void prepared.then(begin)
    else begin(prepared)
  })
}
