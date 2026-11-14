/* THE CHOKE POINT. Every request the console makes to its own API starts here, from whichever page, list, dialog or tab
 * asks for it (`api.ts`'s JSON helpers call `send`): it applies the session headers, then the live host's rewriter if
 * one is installed, then the one race (`relay.ts::raceRequest`), timed by `stall.ts`. A network that kills the
 * browser's connection is therefore met by the same rules everywhere, and fixed in one place.
 *
 * A tab an org switch left behind (`lib/activeOrg.ts`) sends nothing to `/api/`: each request waits here until the
 * session acts as this tab's org again, and one the API refuses `409 org_switched` waits the same way and is then
 * sent again — never handed to the page, whose own error line would only repeat what the reload dialog says.
 *
 * A write (`POST|PUT|PATCH|DELETE` under `/api/`) carries an `Idempotency-Key` minted here, once per call: the direct
 * copy and any relayed copy share it, so the server applies it at most once (`apps/api/app/idempotency.py`). It is
 * never stored on a form: a user's retry after a failure is a new call and a new key, so a 5xx is never replayed. */

import { withHeldRowOrg } from '../lib/actAs'
import {
  orgSwitchRefusal, raiseOrgSwitched, sessionHeaders, switchSignalCount, tabLeftBehind, tabOrg, whenTabCurrent,
} from '../lib/activeOrg'
import { createExternalStore, useExternalStore } from '../lib/externalStore'
import { noteEdgeAnswered, noteRateLimited } from '../lib/pollGate'
import { NoAnswerError } from './errors'
import { observe } from './reachability'
import { raceRequest } from './relay'
import { watchWait } from './stall'

export type SendInit = RequestInit & {
  /** 'session' (default): the console's relay socket. 'anon': the cookieless live socket — the same socket and race,
   *  anonymous only because the live host holds no session cookie (the server decides from the cookie), so it names
   *  the caller's intent and enforces nothing on the console host. 'none': never relayed. */
  relay?: 'session' | 'anon' | 'none'
  /** Bound in ms until the answer's headers. Default: writes `relay.ts::WRITE_BOUND_MS`; reads unbounded, as today.
   *  A caller that must bound the BODY too passes `abortAfter`'s signal instead. A raw write ignores it. */
  boundMs?: number
  /** true: a read sent to `path` as given — no session headers, no rewriter, no stall report — yet still raced
   *  (an `/api/` path only) and bounded by `boundMs`: a static file such as /version.json, or the live host's
   *  rewritten URL (`live/apiMap.ts`). A raw write is the bare `fetch` it always was. */
  raw?: boolean
}

export const IDEMPOTENCY_HEADER = 'Idempotency-Key'

/** A fresh key: 24 base64url characters (144 random bits), the shape `idempotency.py::KEY_PATTERN` accepts. */
export function mintIdempotencyKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(18))
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_')
}

/* ── THE REWRITER SEAM (Live Cast viewer) ─────────────────────────────────────────────────────────────
   The console's own task list and task detail are mounted again on the public live host (`src/live/`), where
   every `/api/...` path they ask for has to land on `/api/public/live/{org}/{slug}/...`, be answered locally
   with an empty value, or not be sent at all — and carry no cookie. ONE seam does
   that for every request `send` carries and for every URL the browser fetches by itself (`apiHref`: an
   `<img src>`, an `<iframe src>`, a download link, the event stream). The console installs none, so `fetch` is
   called exactly as before and every href is unchanged. */
export type ApiRewriter = {
  /** Send what `path` names, as this host must: the console's `fetch(path, init)` call, rewritten. */
  fetch(path: string, init: RequestInit): Promise<Response>
  /** A URL the browser fetches by itself (no headers), rewritten; '' when it must not be fetched at all. */
  href(path: string): string
}

let rewriter: ApiRewriter | null = null
const rewriterVersion = createExternalStore(0)

/** Install the live host's rewriter, or `null` for the console. Installing re-renders every `useApiRewriterVersion`
 *  reader, so an href or a stream path rendered before it is rewritten under it. */
export function setApiRewriter(next: ApiRewriter | null): void {
  rewriter = next
  rewriterVersion.set(rewriterVersion.get() + 1)
}

/** A URL the browser fetches by itself: unchanged in the console. */
export const apiHref = (path: string): string => (rewriter ? rewriter.href(path) : path)

/** Re-render when the rewriter changes (a component holding an `apiHref` or an open stream path). */
export function useApiRewriterVersion(): number {
  return useExternalStore(rewriterVersion)
}

/** A signal aborted after `ms`: bounds a request AND the reading of its body (`signal` aborts both), which `boundMs`
 *  cannot, its timer ending with the headers. `clear` once the body is read. */
export function abortAfter(ms: number): { signal: AbortSignal; clear(): void } {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(new NoAnswerError()), ms)
  return { signal: ctl.signal, clear: () => clearTimeout(timer) }
}

/** A read given its own `boundMs`: aborted and failed with `NoAnswerError` once it passes it. */
function bounded(init: RequestInit, boundMs: number, run: (init: RequestInit) => Promise<Response>): Promise<Response> {
  const ctl = new AbortController()
  const outer = init.signal
  if (outer) outer.addEventListener('abort', () => ctl.abort(outer.reason), { once: true })
  let timer: ReturnType<typeof setTimeout> | undefined
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { ctl.abort(); reject(new NoAnswerError()) }, boundMs)
  })
  return Promise.race([run({ ...init, signal: ctl.signal }), expired]).finally(() => clearTimeout(timer))
}

/** A `raw` send (`SendInit.raw`): the read race with the caller's own headers and credentials, nothing added. */
function sendRaw(path: string, request: RequestInit, relay: SendInit['relay'], boundMs?: number): Promise<Response> {
  if ((request.method ?? 'GET').toUpperCase() !== 'GET') return fetch(path, request)
  const race = (read: RequestInit) => raceRequest(path, { ...read, relay }, read.headers, raced => fetch(path, raced))
  return boundMs === undefined ? race(request) : bounded(request, boundMs, race)
}

/** Send one request to `path` (module header). A write below another org's row a page holds goes as that org
 *  (`lib/actAs.ts::useActAsRows`). */
export function send(path: string, init: SendInit = {}): Promise<Response> {
  const read = (init.method ?? 'GET').toUpperCase() === 'GET'
  return sendUnwatched(path, init).then(resp => (read ? noteReadAnswer(resp) : resp))
}

/* A REFUSED READ PAUSES POLLING. A 429 on a read is the edge's per-address rate limit (the WAF answers reads with an
   HTML page, the live host's with JSON); polling on at the normal interval only kept it tripped for every tab behind
   the same address, so it pauses the console's polling for a back-off (`lib/pollGate.ts::noteRateLimited`). A 429 on
   a write is the API refusing that one action (a resend cooldown, an invitation limit) and pauses nothing. */
function noteReadAnswer(resp: Response): Response {
  if (!resp) return resp
  if (resp.status === 429) noteRateLimited(resp.headers?.get('retry-after'))
  else noteEdgeAnswered()
  return resp
}

function sendUnwatched(path: string, init: SendInit): Promise<Response> {
  const { relay = 'session', boundMs, raw, ...request } = init
  if (raw) return sendRaw(path, request, relay, boundMs)
  if (rewriter) return rewriter.fetch(path, request)
  if (!path.startsWith('/api/')) return sendConsole(path, request, relay, boundMs)
  // Every console request says whether the API answered at all (`reachability.ts`).
  return heldWhileLeftBehind(request.signal, () => observe(sendConsole(path, request, relay, boundMs)))
}

/** `run`, sent only while this tab is current, and sent again once it is current again after an org-switch refusal
 *  (module header). A refusal whose org is already stale — another signal (a switch back) landed while it was on its
 *  way — is not raised: it is simply sent again once the tab is current. The stale-tab guard refuses before any
 *  handler and before the idempotency layer (`apps/api/app/main.py`, `stale_tab_guard.py`), so a write sent again
 *  is applied at most once. A current tab sends at once, in the caller's own tick. */
function heldWhileLeftBehind(signal: AbortSignal | null | undefined, run: () => Promise<Response>): Promise<Response> {
  let ownOrgRefusals = 0
  const attempt = (): Promise<Response> => {
    const signals = switchSignalCount()
    return run().then(async resp => {
      if (resp.status !== 409 || tabOrg() === null) return resp
      const active = orgSwitchRefusal(resp.status, await resp.clone().json().catch(() => null))
      if (!active) return resp
      // Naming this tab's own org means its pin moved while the request was out: sent again with the new pin, once.
      if (active.id === tabOrg()) {
        if (++ownOrgRefusals > 1) return resp
      } else if (signals === switchSignalCount()) {
        raiseOrgSwitched(active)
      }
      return whenTabCurrent(signal).then(attempt)
    })
  }
  return tabLeftBehind() ? whenTabCurrent(signal).then(attempt) : attempt()
}

function sendConsole(path: string, request: RequestInit, relay: SendInit['relay'], boundMs?: number): Promise<Response> {
  const method = (request.method ?? 'GET').toUpperCase()
  const headers = sessionHeaders(withHeldRowOrg(path, request), request.method)
  if (method === 'GET') {
    /* A read unanswered past a second is raced on a fresh connection (`relay.ts`); one still unanswered past
       `STALL_AFTER_MS` counts as stalled (`stall.ts`), silently. */
    const race = (read: RequestInit) =>
      raceRequest(path, { ...read, relay }, headers, raced => fetch(path, { ...raced, headers }))
    return watchWait(boundMs === undefined ? race(request) : bounded(request, boundMs, race), { method, path })
  }
  // Not the console's API: no key, no race, no bound — the bare `fetch` it always was.
  if (!path.startsWith('/api/')) return watchWait(fetch(path, { ...request, headers }), { method, path })
  const keyed = { ...Object.fromEntries(new Headers(headers).entries()), [IDEMPOTENCY_HEADER]: mintIdempotencyKey() }
  return watchWait(raceRequest(path, { ...request, relay, boundMs }, keyed, raced => fetch(path, raced)), { method, path })
}
