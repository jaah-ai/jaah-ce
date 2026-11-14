/* CAN THE CONSOLE REACH ITS API AT ALL? One answer for the whole tab, fed by every request `send.ts` carries and read
 * by the one outage notice (`components/OutageNotice.tsx`) and the identity read on boot (`auth.tsx`).
 *
 * "No answer" means the request reached nothing that could answer: the network refused it (`fetch` rejects with a
 * `TypeError`) or a gateway in front of the API said it could not reach it (502/503/504 from CloudFront, the ALB,
 * nginx or the dev proxy). Every other status, a 401/403/404 or a 500 included, is an answer: the API is up.
 *
 *   ok       nothing says otherwise
 *   suspect  one request got no answer; the notice probes `/api/health` at once before it says anything, so a single
 *            blip (or a route's own 503) never flashes the notice
 *   down     the probe got no answer either; the notice shows and keeps probing with backoff
 *
 * The first answer from anywhere (a page's own read, or the probe) puts it back to `ok`, and bumps `recoveries`, which
 * the boot identity read watches to try again. */
import { useState } from 'react'
import { createExternalStore, useExternalStore } from '../lib/externalStore'
import { ApiError } from './errors'

export type Reachability = 'ok' | 'suspect' | 'down'

const OUTAGE_STATUSES: ReadonlySet<number> = new Set([502, 503, 504])

export const reachability = createExternalStore<Reachability>('ok')
/** How many times the API came back after going unanswered (`suspect` or `down` → `ok`). */
export const recoveries = createExternalStore(0)

export const isOutageStatus = (status: number): boolean => OUTAGE_STATUSES.has(status)

/** A thrown request failure that means "no answer" (module header). An abort is the caller's, never an outage. */
export const isOutageError = (err: unknown): boolean =>
  err instanceof TypeError || (err instanceof ApiError && isOutageStatus(err.status))

/** Something answered. */
export function noteAnswered(): void {
  if (reachability.get() === 'ok') return
  reachability.set('ok')
  recoveries.set(recoveries.get() + 1)
}

/** A request got no answer: `suspect` until the probe confirms. */
export function noteUnanswered(): void {
  if (reachability.get() === 'ok') reachability.set('suspect')
}

/** The probe confirmed it: no answer. */
export function noteDown(): void {
  reachability.set('down')
}

/** Record one `fetch` outcome; hands it back unchanged. */
export function observe(pending: Promise<Response>): Promise<Response> {
  return pending.then(
    resp => {
      if (isOutageStatus(resp.status)) noteUnanswered()
      else noteAnswered()
      return resp
    },
    err => {
      if (isOutageError(err)) noteUnanswered()
      throw err
    },
  )
}

/** Did `/api/health` (unauthenticated, never relayed, never cached) get an answer? The outage notice's probe. */
export async function probeApi(): Promise<boolean> {
  try {
    const resp = await fetch('/api/health', { cache: 'no-store', credentials: 'omit' })
    return !isOutageStatus(resp.status)
  } catch {
    return false
  }
}

export const useReachability = (): Reachability => useExternalStore(reachability)
export const useRecoveries = (): number => useExternalStore(recoveries)

/** Does the outage notice speak for this failed READ, so the page draws no banner of its own? Yes when `cause` (the
 *  error the read threw; `null` while none) is "no answer" and the API is not answering (`suspect` or `down`), and
 *  STAYS yes for that same failure once the API was `down` while it stood: it predates the recovery, and the page's
 *  next read after it (the event stream's reconnect, its down-poll) replaces it. A route's own 503 whose probe got an
 *  answer never went `down`, so its banner shows once the probe answers; a 500 or 403 is an answer and always shows.
 *  Reads only: a failed WRITE keeps its banner, it must say nothing was saved. */
export function useOutageCovers(cause: unknown): boolean {
  const state = useReachability()
  const [covered, setCovered] = useState<unknown>(null)
  const outage = cause != null && isOutageError(cause)
  if (outage && state === 'down' && covered !== cause) setCovered(cause)
  return outage && (state !== 'ok' || covered === cause)
}
