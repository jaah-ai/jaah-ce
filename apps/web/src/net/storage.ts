import { ApiError } from './errors'
import { abortAfter } from './send'

/* THE RAW PUT to object storage, and the one request in this app that must NOT go through `api.ts::api`. Every one of that
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
   (see `lib/uploadTaskAttachment.ts`).
   Throws an `ApiError` rather than a bare `Error` so both call sites read its reason through
   `apiDetail`, exactly as they read every other refusal — and the reason is OURS to write, because an
   S3 error body is XML that `apiDetail` cannot read and no operator should be shown.

   BOUNDED, NEVER A HANG. The URL is cross-origin S3, so a dead console connection does not share its pool and the
   WebSocket relay cannot carry it; what a dead connection gets instead is a deadline and a fresh attempt. Each attempt
   is aborted after `attemptBoundMs(size)`: 15 s, or longer at a 256 KiB/s floor for a big file. One that is cut, or
   fails with no HTTP answer, goes once more on a fresh request; a second miss throws `ApiError(0, …)`. So a small
   upload on a dead connection fails within 30 s, never an endless spinner. An HTTP refusal is an answer, never retried. */

/** Two attempts at this floor fit the 30 s a stuck upload may take before it fails visibly. */
export const MIN_ATTEMPT_BOUND_MS = 15_000
/** The slowest upload rate an attempt allows for before it cuts a big file. */
export const UPLOAD_FLOOR_BYTES_PER_S = 256 * 1024

/** One attempt's bound: `max(15 s, size / 256 KiB·s)`. */
export function attemptBoundMs(size: number): number {
  return Math.max(MIN_ATTEMPT_BOUND_MS, Math.ceil((size / UPLOAD_FLOOR_BYTES_PER_S) * 1000))
}

/** One PUT, aborted after `ms`: its response, or a throw when no HTTP answer came in time. */
async function putOnce(url: string, body: Blob, ms: number): Promise<Response> {
  const bound = abortAfter(ms)
  try {
    return await fetch(url, { method: 'PUT', body, signal: bound.signal })
  } finally {
    bound.clear()
  }
}

export async function putToStorage(url: string, body: Blob): Promise<void> {
  const ms = attemptBoundMs(body.size)
  let resp: Response
  try {
    resp = await putOnce(url, body, ms).catch(() => putOnce(url, body, ms))
  } catch {
    // Status 0: there was no HTTP response to carry one — DNS, the proxy, a dropped or dead connection, the bound.
    throw new ApiError(0, 'the upload could not reach the storage service')
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, `the storage service refused the upload (HTTP ${resp.status})`)
  }
}
