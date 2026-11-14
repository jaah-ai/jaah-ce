/* WHERE ONE REQUEST'S TIME WENT, from the browser's own resource timing: waiting in the browser for a connection,
 * connecting, the round trip to the first byte, and — from the API's `Server-Timing: app;dur=` header
 * (`apps/api/app/server_timing.py`) — how much of that round trip the control plane itself spent. What is left,
 * `network_ms`, is the wire: a read that waited 9 s with `server_ms` 300 was the network's, one with `server_ms`
 * 8 900 was ours. Read by the slow-read and stall reports (`net/stall.ts`) and the page-load and navigation
 * reports' slowest resources (`loadTiming.ts`); every field is null where the browser did not time it. A browser
 * exposes `serverTiming` only in a secure context (HTTPS), so over plain-HTTP `server_ms` and `network_ms` are null
 * although the header was sent; and the API sends it only to a signed-in console session. */

/** `url`'s path, without its query or fragment; the origin is kept only for another site's resource. */
export function resourceName(url: string): string {
  try {
    const parsed = new URL(url, window.location.href)
    return parsed.origin === window.location.origin ? parsed.pathname : `${parsed.origin}${parsed.pathname}`
  } catch {
    return url.split(/[?#]/)[0]
  }
}

const SERVER_METRIC = 'app'
const round = (value: number) => Math.round(value)
/** `end - start`, or null when the browser did not record either mark (0) or the span is negative. A mark is 0 where
 *  it was not reached or is withheld (another site's resource without `Timing-Allow-Origin` keeps `responseEnd` but
 *  zeroes `responseStart`), and spanning from 0 would report the page's age. Every start here is a resource entry's,
 *  whose `startTime` is never 0 (only the navigation entry's is), so no real span starts at 0. */
const span = (end: number, start: number) => (end > 0 && start > 0 && end >= start ? round(end - start) : null)

export type RequestTiming = {
  /** Waiting in the browser before the request went out: a connection to reuse, or one being opened. */
  queued_ms: number | null
  connect_ms: number | null
  ttfb_ms: number | null
  download_ms: number | null
  server_ms: number | null
  network_ms: number | null
  protocol: string | null
  bytes: number
}

export function timingOf(entry: PerformanceResourceTiming): RequestTiming {
  const ttfb = span(entry.responseStart, entry.requestStart)
  const server = entry.serverTiming?.find(metric => metric.name === SERVER_METRIC)?.duration
  const serverMs = typeof server === 'number' ? round(server) : null
  return {
    queued_ms: span(entry.requestStart, entry.startTime),
    connect_ms: span(entry.connectEnd, entry.connectStart),
    ttfb_ms: ttfb,
    download_ms: span(entry.responseEnd, entry.responseStart),
    server_ms: serverMs,
    network_ms: ttfb !== null && serverMs !== null ? Math.max(0, ttfb - serverMs) : null,
    protocol: entry.nextHopProtocol || null,
    bytes: entry.transferSize,
  }
}

/** How far a resource entry's `startTime` may sit from the moment the read was sent and still be that read. */
export const SAME_REQUEST_MS = 50

/** The resource timing of the same-origin request to `path` (with its query, as sent) that was sent at `sentAt`
 *  (`performance.now()`), or null: the browser records an entry only once the body has arrived, none for a request
 *  that was aborted, and another request to the same URL is never mistaken for this one. */
export function timingOfRequest(path: string, sentAt: number): RequestTiming | null {
  try {
    const url = new URL(path, window.location.href).href
    const entries = performance.getEntriesByName(url, 'resource') as PerformanceResourceTiming[]
    let best: PerformanceResourceTiming | null = null
    for (const entry of entries) {
      const gap = Math.abs(entry.startTime - sentAt)
      if (gap <= SAME_REQUEST_MS && (best === null || gap < Math.abs(best.startTime - sentAt))) best = entry
    }
    return best ? timingOf(best) : null
  } catch {
    return null
  }
}
