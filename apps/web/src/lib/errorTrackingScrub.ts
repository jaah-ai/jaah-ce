/* WHAT A SENTRY EVENT FROM THE CONSOLE MAY CARRY — the `beforeSend` of `lib/errorTracking.ts`, kept apart from it so
 * it is tested without loading the SDK. The browser twin of `apps/api/app/error_tracking.py::scrub_event`.
 *
 * Kept: the error, its stack frames (file, function, line), the page path, the user-agent header, the release and an
 * opaque user id. Dropped: the request body, cookies, query string and every other header, the user's email,
 * name and IP, breadcrumbs, frame variables and source. Every string left (message, exception value, frame path, tag, extra) loses
 * emails and tokens: `scrub` (bearer, JWT, URL queries) plus emails, vendor tokens and `password=…` pairs here. */
import { scrub } from './clientTelemetry'

const REDACTED = '[redacted]'
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g
const VENDOR_TOKEN = /\b(?:gh[pousr]_|github_pat_|sk-|xox[abposr]-|glpat-)[A-Za-z0-9_-]{8,}/g
// `password=…`, `access_token: …`, `"client_secret": "…"` in free text: the value goes, the name stays.
const SECRET_PAIR = /([A-Za-z0-9_]*(?:pass(?:word)?|passwd|secret|token|api[_-]?key))(["']?\s*[:=]\s*["']?)[^\s"'&,;]+/gi
// A URL's fragment (`#access_token=…`, OAuth's implicit flow); `scrub` already drops its query.
const URL_FRAGMENT = /(\b[a-z][a-z0-9+.-]*:\/\/[^\s#"'<>()]*)#[^\s"'<>()]*/gi
// A key NAMED for a secret (`password`, `x_auth_token`, `api-key`), matched whole word by word so `author` stays.
const SECRET_KEY = /(^|[_-])(pass(word)?|passwd|secrets?|tokens?|auth|cookies?|session|api[_-]?key|credentials?|dsn)($|[_-])/i
// URL user info (`https://user:pw@host`) and cloud access key ids.
const URL_USERINFO = /(\b[a-z][a-z0-9+.-]*:\/\/)[^/\s@]+@/gi
const ACCESS_KEY_ID = /\bAKIA[0-9A-Z]{16}\b/g

/** `text` without emails, bearer tokens, JWTs, vendor tokens or URL query strings. */
export function scrubText(text: string): string {
  return scrub(text)
    .replace(URL_FRAGMENT, `$1#${REDACTED}`)
    .replace(URL_USERINFO, `$1${REDACTED}@`)
    .replace(ACCESS_KEY_ID, REDACTED)
    .replace(EMAIL, REDACTED).replace(VENDOR_TOKEN, REDACTED).replace(SECRET_PAIR, `$1$2${REDACTED}`)
}

function scrubValue(value: unknown): unknown {
  if (typeof value === 'string') return scrubText(value)
  if (Array.isArray(value)) return value.map(scrubValue)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [k, SECRET_KEY.test(k) ? REDACTED : scrubValue(v)]),
    )
  }
  return value
}

type Frame = { filename?: unknown; abs_path?: unknown; vars?: unknown; context_line?: unknown; pre_context?: unknown; post_context?: unknown }
type Event = {
  request?: { url?: unknown; method?: unknown; headers?: Record<string, unknown> } & Record<string, unknown>
  user?: { id?: unknown } & Record<string, unknown>
  exception?: { values?: { value?: unknown; stacktrace?: { frames?: Frame[] } }[] }
  message?: unknown
  breadcrumbs?: unknown
  extra?: unknown
  tags?: unknown
  contexts?: unknown
}

/** The `beforeSend` hook: `event` with nothing personal or secret left in it. Mutates and returns it. */
export function scrubEvent<E>(input: E): E {
  const event = input as Event
  if (event.request) {
    const { url, method, headers } = event.request
    const userAgent = headers?.['User-Agent'] ?? headers?.['user-agent']
    event.request = {
      ...(typeof url === 'string' ? { url: scrubText(url.split('?')[0]) } : {}),
      ...(typeof method === 'string' ? { method } : {}),
      ...(typeof userAgent === 'string' ? { headers: { 'User-Agent': userAgent } } : {}),
    }
  }
  if (event.user) event.user = event.user.id != null ? { id: String(event.user.id) } : {}
  for (const value of event.exception?.values ?? []) {
    if (typeof value.value === 'string') value.value = scrubText(value.value)
    for (const frame of value.stacktrace?.frames ?? []) {
      // No variables and no source: a frame is its file, function and line.
      for (const key of ['vars', 'context_line', 'pre_context', 'post_context'] as const) delete frame[key]
      for (const key of ['filename', 'abs_path'] as const) {
        if (typeof frame[key] === 'string') frame[key] = scrubText(frame[key])
      }
    }
  }
  delete event.breadcrumbs
  for (const key of ['message', 'extra', 'tags', 'contexts'] as const) {
    if (key in event) event[key] = scrubValue(event[key])
  }
  return input
}
