// Thin fetch wrapper: same-origin JSON, session cookie, typed errors.

import { AUTH_EXPIRED_EVENT, clearAuthSnapshot } from './authStore'
import { noticeActAsUnavailable, withHeldRowOrg } from './lib/actAs'
import { noticeOrgSwitched, sessionHeaders } from './lib/activeOrg'
import { createExternalStore, useExternalStore } from './lib/externalStore'

export class ApiError extends Error {
  status: number
  detail: unknown
  body: unknown

  constructor(status: number, detail: unknown, body: unknown = null) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
    this.status = status
    this.detail = detail
    this.body = body
  }
}

/** The record is not there to read: a 404, or a 422 for an id the route could not parse (shown as not found). */
export const isNotFound = (err: unknown): boolean =>
  err instanceof ApiError && (err.status === 404 || err.status === 422)

/** A detail read's `retry`: a missing record is an answer, never retried; anything else twice more. */
export const retryUnlessNotFound = (count: number, err: unknown): boolean => !isNotFound(err) && count < 2

/* ── THE REWRITER SEAM (Live Cast viewer) ─────────────────────────────────────────────────────────────
   The console's own task list and task detail are mounted again on the public live host (`src/live/`), where
   every `/api/...` path they ask for has to land on `/api/public/live/{org}/{slug}/...`, be answered locally
   with an empty value, or not be sent at all — and carry no cookie. ONE seam does
   that for every request this module makes (`api`, `getText`, `getPage`, `apiFetch`) and for every URL the
   browser fetches by itself (`apiHref`: an `<img src>`, an `<iframe src>`, a download link, the event
   stream). The console installs none, so `fetch` is called exactly as before and every href is unchanged. */
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

/** `fetch` with the console's session headers — or, on the live host, through its rewriter. Exported for the one
 *  caller that reads a body none of the helpers below can (`components/richtext/attachmentResolver`'s blob). A write
 *  below another org's row a page holds goes as that org (`lib/actAs.ts::useActAsRows`). */
export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (rewriter) return rewriter.fetch(path, init)
  return fetch(path, { ...init, headers: sessionHeaders(withHeldRowOrg(path, init), init.method) })
}

/** A JSON helper's refusal (`api`, `getPage`): the side effects every refusal carries — an expired session, the IP
 *  allowlist, an org switch, Act as gone — then the `ApiError` the caller catches. */
function refuse(resp: Response, body: { detail?: unknown } | null): never {
  signalAuthExpired(resp.status, body?.detail)
  signalIpNotAllowed(resp.status, body)
  signalTermsRequired(resp.status, body)
  noticeOrgSwitched(resp.status, body)
  noticeActAsUnavailable(resp.status, body)
  throw new ApiError(resp.status, body?.detail ?? null, body)
}

export async function api<T>(
  path: string,
  opts: RequestInit = {},
  { jsonHeader = true }: { jsonHeader?: boolean } = {},
): Promise<T> {
  /* `headers` after `...opts`, so a caller's own headers (an act-as one, `lib/actAs.ts::actAsOnce`) join the JSON
     content type instead of replacing it. */
  const init: RequestInit = {
    credentials: 'same-origin',
    ...opts,
    headers: jsonHeader ? { 'Content-Type': 'application/json', ...(opts.headers ?? {}) } : opts.headers,
  }
  const resp = await apiFetch(path, init)
  if (resp.status === 204) return undefined as T
  const body = await resp.json().catch(() => null)
  if (!resp.ok) refuse(resp, body)
  return body as T
}

/* The expired-session side effect, hoisted out of `api` so `getText` below shares it rather than
   spelling a second copy: a 401 that logged one caller out and not the other is a session the app
   disagrees with itself about. */
export function signalAuthExpired(status: number, detail?: unknown): void {
  if (status !== 401) return
  clearAuthSnapshot()
  if (typeof window === 'undefined') return
  /* A HIPAA org's idle logoff (`apps/api/app/auth.py::SESSION_IDLE_TIMEOUT_DETAIL`) says so on the sign-in
     page. A full load, not a route change: nothing the idle tab held stays in memory. Not from `/login`
     itself, whose own `/me` read meets the same cookie. */
  if (detail === SESSION_IDLE_TIMEOUT && window.location.pathname !== '/login') {
    window.location.replace(IDLE_LOGIN_PATH)
    return
  }
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
}

/* The org's IP allowlist refused this address (`{code: 'ip_not_allowed', ip}` — every `/api` request answers
   it once the address is outside the list): the session is no use from here, so `/login` says why and names
   the address. The address rides in session storage, never the URL: a link anyone can craft must not make the
   page name an address an admin should add (`pages/Login.tsx::refusedAddress`). A full load, like the idle
   logoff; not from `/login` itself. */
export function signalIpNotAllowed(status: number, body: unknown): void {
  if (status !== 403) return
  const refusal = body as { code?: unknown; ip?: unknown } | null
  if (refusal?.code !== IP_NOT_ALLOWED) return
  clearAuthSnapshot()
  if (typeof window === 'undefined' || window.location.pathname === '/login') return
  try {
    if (typeof refusal.ip === 'string') window.sessionStorage.setItem(REFUSED_ADDRESS_KEY, refusal.ip)
  } catch { /* no storage: the page words it without the address */ }
  window.location.replace(`/login?login_error=${IP_NOT_ALLOWED}`)
}

const IP_NOT_ALLOWED = 'ip_not_allowed'

/* The session still owes the Terms acceptance (`{code: 'terms_required'}`, `apps/api/app/auth.py::TERMS_REQUIRED`):
   every other `/api` request answers it until accepted, so the console opens the acceptance page. A full load, like
   the IP allowlist's, so no half-loaded console page stays behind; not from the page itself. */
export function signalTermsRequired(status: number, body: unknown): void {
  if (status !== 403 || (body as { code?: unknown } | null)?.code !== TERMS_REQUIRED) return
  if (typeof window === 'undefined' || window.location.pathname === TERMS_ACCEPT_PATH) return
  window.location.replace(TERMS_ACCEPT_PATH)
}

const TERMS_REQUIRED = 'terms_required'
/** The Terms acceptance page (`pages/TermsAcceptPage.tsx`). */
export const TERMS_ACCEPT_PATH = '/terms/accept'
/** Where `signalIpNotAllowed` leaves the refused address for `/login`. */
export const REFUSED_ADDRESS_KEY = 'jaah.refusedAddress'

/** The API's 401 detail for a session its HIPAA org's 15-minute idle rule ended. */
export const SESSION_IDLE_TIMEOUT = 'session_idle_timeout'
/** Where an idled-out tab lands (`pages/Login.tsx` reads `reason`). */
export const IDLE_LOGIN_PATH = '/login?reason=idle'


/* A GET whose response body is TEXT, not JSON — the one shape `api` above cannot serve, because it
   parses every body through `resp.json()`. It exists for the Artifacts tab's markdown kinds
   (`GET /api/tasks/:id/artifacts/:id/content` answers `text/markdown`), whose bytes are handed to
   `RichTextRenderer` as a string; the `report` kind needs nothing here, its bytes being fetched by the
   browser as a sandboxed frame's own document.

   SAME credentials, same 401 handling, same `ApiError` — so a caller's failure branch reads identically
   whichever helper it used. There is no `detail` to lift out of a text body, hence the null.

   NOTHING IS INFERRED FROM `body` BEING NULL — see `statusOnly`, which says the status and declines to
   guess who sent it. An earlier draft of this pair DID infer from it, and this helper's hardcoded null
   would then have made every ordinary 404 from a real API route claim it never reached the API. */
export async function getText(path: string, opts: RequestInit = {}): Promise<string> {
  const resp = await apiFetch(path, { credentials: 'same-origin', ...opts })
  if (!resp.ok) {
    await noticeRefusal(resp)
    throw new ApiError(resp.status, null, null)
  }
  return resp.text()
}

/** A non-JSON read's refusal side effects (expired session, IP allowlist, org switch), shared by `getText` and
 *  the Artifacts tab's report-frame probe so every read of the bytes logs out alike. A refusal's body is the
 *  API's JSON, not the bytes the caller wanted: read for its `detail` and `code` only; a 403 may not be JSON. */
export async function noticeRefusal(resp: Response): Promise<void> {
  const refusal = [401, 403, 409].includes(resp.status) ? await resp.json().catch(() => null) : null
  signalAuthExpired(resp.status, refusal?.detail)
  signalIpNotAllowed(resp.status, refusal)
  signalTermsRequired(resp.status, refusal)
  if (resp.status === 409) {
    noticeOrgSwitched(resp.status, refusal)
    noticeActAsUnavailable(resp.status, refusal)
  }
}

/* ONE FastAPI validation failure, as `RequestValidationError` serializes it. `loc` is the path to the
   offending value from the request root — `["body", "tech_stack", 0, "technology"]` — and it is the
   only thing in the payload that names the FIELD, which is the half an operator needs to act. */
type ValidationItem = { loc?: unknown[]; msg?: string }

const isValidationItem = (v: unknown): v is ValidationItem =>
  typeof v === 'object' && v !== null && 'msg' in v && typeof (v as ValidationItem).msg === 'string'

/* `["body", "tech_stack", 0, "technology"]` → `tech_stack.0.technology`. The leading segment is the
   REQUEST PART the value came from, which is an implementation detail of FastAPI's own plumbing rather
   than anything on the operator's screen — every field this app submits is in the body — so it is
   dropped. A `loc` of just `["body"]` (a whole payload the server could not parse) leaves nothing to
   name, and the message alone is then the honest render. */
function validationField(loc: unknown[] | undefined): string {
  // `Array.isArray`, not `?? []`: `loc` is TYPED `unknown[]` but arrives from `JSON.parse`, so a
  // server answering `loc: "body.name"` would make `.filter` throw — out of a helper whose whole job is
  // to run inside a catch block and whose docstring promises it always returns a string.
  const path = (Array.isArray(loc) ? loc : []).filter(seg => typeof seg === 'string' || typeof seg === 'number')
  const named = path.length > 1 ? path.slice(1) : []
  return named.join('.')
}

/* HOW MANY OF THEM TO SHOW. A body with one bad field is the common case; a form submitted against a
   changed contract can name a dozen, and a banner carrying all twelve is one an operator scrolls past
   rather than reads. Three name the problem; the count carries the rest. */
const MAX_VALIDATION_ITEMS = 3

/* HOW LONG A THROWN MESSAGE MAY BE. `err.message` on a non-`ApiError` is whatever the runtime wrote —
   usually one clause, occasionally a serialized object — and the banner is a single line. */
const MAX_THROWN_MESSAGE = 200

/* `<context>: <reason>`, punctuated so it reads as one sentence rather than as two strings that met.
   The fallbacks at the ~153 call sites were written to stand ALONE, so they end every possible way and
   a bare `: ` between them produces `Nothing was removed.: the control plane is down`.

   THREE CASES, and the dash is why none of them has to lose a character. A fallback ending in TERMINAL
   punctuation is a finished sentence, so the reason is joined to it with an em-dash rather than by
   deleting the full stop — which is what an earlier draft did, and it silently turned "Failed to delete
   Acme Inc." into "Failed to delete Acme Inc" for an org whose NAME ends in a full stop. A trailing
   COLON is a separator the caller already wrote, so it is used as-is. Anything else takes the colon.
   AN EMPTY FALLBACK IS THE REASON ALONE: three credential call sites pass `''` deliberately, asking for
   what the error itself said and nothing else, and prefixing that with `": "` would put a stray colon at
   the head of the banner — one of those sites then splits the result on its first space to re-home the
   error onto a field, so the colon would break that too. What those sites do when there IS no reason to
   return is `statusOnly`'s business, not this function's. */
function because(fallback: string, reason: string): string {
  const context = fallback.trimEnd()
  if (!context) return reason
  if (/[.!?]$/.test(context)) return `${context} — ${reason}`
  if (context.endsWith(':')) return `${context} ${reason}`
  return `${context}: ${reason}`
}

/* THE STATUS, SAID WITHOUT CLAIMING TO KNOW WHO SENT IT. An earlier draft of this said "refused before
   the API answered" whenever no JSON came back, reasoning that this API always answers JSON. IT DOES
   NOT: `apps/api` installs no exception handler at all, so an unhandled exception is served by
   Starlette as the plain text "Internal Server Error", which parses to nothing here — and the banner
   would then have sent the next on-call to the WAF logs for a bug in the application, having just cost
   a day sending them the other way. The status is the honest discriminator and it is enough: a 403 on
   a public-UI save with nothing behind it is an edge refusal, a 500 with nothing behind it is a crash,
   and this function does not have to guess which.

   AN EMPTY FALLBACK GETS AN EMPTY STRING, and that is the one place this differs from `because`. A
   caller passing `''` is asking for what the ERROR ITSELF SAID and nothing else — the two OAuth call
   sites in `pages/credentials/CredentialEditPage.tsx` say so at the call, and route a falsy result to
   their own sentence. A validation message or a throw's message is something the error said, so those
   still come back; a status this function synthesised is not. */
const statusOnly = (fallback: string, status: number): string =>
  fallback.trimEnd() ? because(fallback, statusSentence(status)) : ''

/* A SENTENCE an operator can act on, never a bare "HTTP 500, no detail". A 5xx is a crash
   on our side whatever layer served it, so it says so and tells the operator what to do next; anything
   else is a refusal that gave no reason, and its status stays in the sentence as the one fact there is. */
function statusSentence(status: number): string {
  if (status >= 500) {
    return `the server hit an unexpected error (HTTP ${status}). Try again; if it keeps failing, report it with the time it happened`
  }
  // The public load balancer's rate limit answers 429 with an HTML page, no `detail`.
  if (status === 429) return 'too many requests from your network (HTTP 429). Try again in a few minutes'
  return `the request was refused (HTTP ${status}) without a reason`
}

/** THE ONE PLACE A FAILED REQUEST BECOMES A SENTENCE, read by every catch block in the console.
 *
 *  `fallback` is the CONTEXT ("Failed to save project") and no longer the whole message: it says which
 *  action failed, and everything this function can recover about WHY is appended to it. The previous
 *  version returned the fallback alone for anything that was not an `ApiError` carrying a string
 *  `detail`, which is three distinct silences it is worth naming, because each has cost real debugging:
 *
 *  1. A 422. FastAPI answers a validation failure with `detail` as an ARRAY of `{loc, msg}`, never a
 *     string — so the single commonest way a save fails rendered as the bare fallback, with the field
 *     and the reason both discarded.
 *  2. NO DETAIL AT ALL. A refusal from the proxy, the load balancer or the WAF carries no JSON, and so
 *     does an unhandled server exception — `apps/api` installs no exception handler, so Starlette
 *     answers plain text. `detail` is null for all of them and the STATUS is the only fact there is.
 *     THIS IS THE ONE THAT COST A DAY. A WAF rule on the public listener refused
 *     five `PUT /api/projects/9` saves AT THE EDGE, so the request never reached the API: no
 *     access-log line, no application error, nothing in the console. The browser got a non-JSON 403
 *     and the operator got a bare "Failed to save project" — a sentence equally consistent with a
 *     validation error, an outage and a bug in this page. It took a WAF-log query to find at all.
 *     What is now said is the status and the fact that nothing explained it; `statusOnly` records why
 *     this stops there rather than naming which layer refused.
 *  2b. AN EMPTY-STRING `detail`, which the API does emit, now falls into (2) rather than rendering as
 *     an empty banner. A refusal that says nothing is a refusal whose status is worth reading.
 *  3. A THROW THAT IS NOT AN `ApiError`. Every caller wraps its whole submit path in one try, so a
 *     TypeError raised while BUILDING the request body lands in the same catch as an HTTP failure.
 *     Swallowing its message made a client-side crash indistinguishable from a server refusal, with
 *     nothing in the console (it was caught) and nothing in the access log (no request was ever made).
 *     `ProjectEditPage`'s `buildBody` could throw exactly that way on a malformed stored `tech_stack`
 *     entry — a real defect found while diagnosing the above, fixed with it, and reachable by anyone
 *     who had ever written one of those rows through the API.
 *
 *  ONE OBLIGATION THIS CREATES ON THE API, because rendering a 422 is new: a request model's
 *  `field_validator` message is now shown verbatim to any authenticated operator (pydantic renders a
 *  raised `ValueError` as `Value error, <text>`). Those messages may echo the caller's own input, which
 *  is what they are for; they may NOT embed server-side state — a path, a hostname, a row from another
 *  tenant — because this function puts them on screen.
 *
 *  Always a plain string, because that is what every one of its call sites renders. */
export function apiDetail(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (typeof err.detail === 'string' && err.detail) return err.detail
    // An object detail that carries its own sentence beside its facts (a hosted machine's 409, `routes/nodes/delete.py`).
    const message = (err.detail as { message?: unknown } | null)?.message
    if (typeof message === 'string' && message) return message

    if (Array.isArray(err.detail)) {
      const items = err.detail.filter(isValidationItem)
      if (items.length) {
        const shown = items.slice(0, MAX_VALIDATION_ITEMS).map(item => {
          const field = validationField(item.loc)
          return field ? `${field}: ${item.msg}` : (item.msg as string)
        })
        const rest = items.length - shown.length
        const tail = rest > 0 ? ` (and ${rest} more)` : ''
        return because(fallback, `${shown.join('; ')}${tail}`)
      }
    }

    return statusOnly(fallback, err.status)
  }

  if (err instanceof Error && err.message) {
    const message = err.message.length > MAX_THROWN_MESSAGE
      // `…` because a hard slice can cut mid-word and reads as a complete sentence the server never
      // sent — the same trap `pages/credentials/CredentialEditPage` records against its own truncation.
      ? `${err.message.slice(0, MAX_THROWN_MESSAGE)}…`
      : err.message
    return because(fallback, message)
  }

  return fallback
}

/* `opts` exists for ONE reason and it is `signal`: `ui/AsyncLookup` aborts a keystroke the operator has
   already typed past, and every one of its fetchers is a GET. Optional and spread into `fetch`, so every
   existing caller — and every test that mocks this function with one parameter — is unaffected. */
export const get = <T,>(path: string, opts?: RequestInit) => api<T>(path, opts)

/** An org-scoped route (`/api/orgs/{org_id}/<tail>`), the id encoded once, here. */
export const orgPath = (orgId: string, tail: string) => `/api/orgs/${encodeURIComponent(orgId)}/${tail}`

/* A keyset-paged GET: the body plus the `X-Next-Cursor` header (`GET /api/tasks`), `null` on the last page.
   Same credentials, 401 handling and `ApiError` as `api`. */
export async function getPage<T>(path: string, opts: RequestInit = {}): Promise<{ data: T; next: string | null }> {
  const resp = await apiFetch(path, { credentials: 'same-origin', ...opts })
  const body = await resp.json().catch(() => null)
  if (!resp.ok) refuse(resp, body)
  return { data: body as T, next: resp.headers.get('X-Next-Cursor') }
}
export const post = <T,>(path: string, body?: unknown, headers?: HeadersInit) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body), ...(headers ? { headers } : {}) })
export const put = <T,>(path: string, body: unknown, headers?: HeadersInit) =>
  api<T>(path, headers ? { method: 'PUT', body: JSON.stringify(body), headers } : { method: 'PUT', body: JSON.stringify(body) })
export const patch = <T,>(path: string, body: unknown, headers?: HeadersInit) =>
  api<T>(path, headers ? { method: 'PATCH', body: JSON.stringify(body), headers } : { method: 'PATCH', body: JSON.stringify(body) })
/* `<T = void>` because a DELETE may now ANSWER. `DELETE /api/nodes/{node_id}` used to be a 204 whose
   only content was its status; now it deregisters the node, drops every slot row it
   declared, ends whatever held them, asks the node to erase those directories best effort and terminates
   the instance the CP launched — five outcomes an operator has to be able to read, so it answers 200 with
   a body (`NodeDeleteResult`). The default keeps every existing `del(...)` call unchanged: a 204 still
   resolves as `undefined` through `api`'s own early return. */
export const del = <T = void,>(path: string, headers?: HeadersInit) => api<T>(path, headers ? { method: 'DELETE', headers } : { method: 'DELETE' })

// Multipart upload (task attachments): the browser sets its own
// `Content-Type: multipart/form-data; boundary=...` header for a FormData body, so this must NOT send
// the `application/json` header every other verb above does (an explicit override would break the
// multipart boundary the browser computes).
export const postForm = <T,>(path: string, form: FormData) =>
  api<T>(path, { method: 'POST', body: form }, { jsonHeader: false })

/* ── DIRECT-TO-STORAGE UPLOAD: the two seams `postForm` above cannot serve ──────
   An attachment no longer travels through the api container. The browser asks for a presigned TICKET
   (JSON, ordinary same-origin call), PUTs the bytes straight at object storage, then tells the api the
   bytes landed. Only the middle step is unusual, and it is unusual in three ways at once — hence its
   own function rather than a flag on `api`. */

// The ticket the api mints: which attachment row the bytes belong to, where to PUT them, and how long
// that URL stays signed. Spelled once, here, because both the caller and its tests read the shape.
export type UploadTicket = { attachment_id: number; upload_url: string; expires_at: string }

export const postUploadTicket = (
  path: string,
  ticket: { filename: string; content_type: string; size_bytes: number; visibility?: 'public' | 'internal' },
) => api<UploadTicket>(path, { method: 'POST', body: JSON.stringify(ticket) })

/* THE RAW PUT, and the one request in this app that must NOT go through `api` above. Every one of that
   helper's three invariants is wrong here:
     • it parses every body through `resp.json()` — S3 answers XML, or nothing at all;
     • it sends `credentials: 'same-origin'` — this URL is cross-origin, and a cookie on it is at best
       ignored;
     • it sets `Content-Type: application/json` — and a presigned request here signs `host` plus, on an
       upload PUT, `content-length` (the API's S3 presigner: `SIGNED_HEADERS = "host"` and
       `SIGNABLE_CONTENT_LENGTH`, which is the only thing that bounds a query-string presign at all),
       with `content-type` named in `FORBIDDEN_HEADERS` and `reject_forbidden_headers` enforcing that on
       every presigned request the control plane and the node make. An unsigned header is at best
       outside the signature and at worst refused; either way the browser should not be the one uploader
       that ignores the rule.
   Hence no headers OF OURS, deliberately: this call sends the method and the bytes, and the one header
   that does travel is the `Content-Length` the browser computes from the Blob itself. That one is
   load-bearing rather than incidental — the upload signature covers it, so it has to be the real byte
   count, which is exactly why it is NOT ours to set: script may not touch a forbidden header name in
   fetch (also why it needs no CORS grant). Do not "helpfully" add any other — and note that the BODY
   can add one behind your back, which is why the parameter is a type-less `Blob` rather than a `File`
   (see `lib/uploadTaskAttachment`).
   Throws an `ApiError` rather than a bare `Error` so both call sites read its reason through
   `apiDetail`, exactly as they read every other refusal — and the reason is OURS to write, because an
   S3 error body is XML that `apiDetail` cannot read and no operator should be shown. */
export async function putToStorage(url: string, body: Blob): Promise<void> {
  let resp: Response
  try {
    resp = await fetch(url, { method: 'PUT', body })
  } catch {
    // Status 0: there was no HTTP response to carry one — DNS, the proxy, a dropped connection.
    throw new ApiError(0, 'the upload could not reach the storage service')
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, `the storage service refused the upload (HTTP ${resp.status})`)
  }
}
