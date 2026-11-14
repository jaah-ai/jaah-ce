/* THE CHOKE POINT'S ERRORS: what a request that reached no usable answer throws, and the two sentences the user is
 * shown for it. `api.ts::apiDetail` reads both constants, so every page's own fallback ("Failed to save project") is
 * joined to the same words wherever the request started. */

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

/** A write neither the direct connection nor the relay answered within its bound (`send.ts`). Nothing reached the
 *  page, so whatever the user typed is still in the form. */
export const NO_ANSWER = 'no answer from the server. Not saved — your changes are still here; retry'

/** A write the server may or may not have applied: its first copy's process died mid-run, its stored answer is too
 *  big to replay, or the other copy was still running when this one gave up (`apps/api/app/idempotency.py`). */
export const OUTCOME_UNKNOWN = 'may have saved — reload to check'

/** A write whose relay copy the server took but never answered within the bound: it may have been applied. */
export const OUTCOME_UNKNOWN_CODE = 'idempotency_outcome_unknown'

/** The idempotency refusals whose outcome the browser cannot know (`apps/api/app/idempotency.py`). */
export const UNKNOWN_OUTCOME_CODES: ReadonlySet<string> = new Set([
  'idempotency_in_progress',
  OUTCOME_UNKNOWN_CODE,
  'idempotency_replay_unavailable',
])

/** A key the server saw with a different body: one key is minted per call, so only a bug in `send.ts` sends it. */
export const KEY_REUSED = 'idempotency_key_reused'

export class NoAnswerError extends ApiError {
  constructor() {
    super(0, NO_ANSWER)
    this.name = 'NoAnswerError'
  }
}
