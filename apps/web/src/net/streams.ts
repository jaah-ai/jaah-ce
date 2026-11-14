/* THE CHOKE POINT FOR LONG-LIVED CONNECTIONS. The console's event stream (`hooks/useEventStream.ts`) and its terminal
 * socket (`components/ConsolePane.tsx`) are opened here, never with a bare constructor, so a rule every long-lived
 * connection needs lands in one place. Each caller keeps its own, proven self-heal: the stream's silent-source
 * reopen (`useEventStream.ts::SILENT_AFTER_MS`), the terminal's keepalive and reconnect ladder. What lives here is the
 * shared part: the URL the browser may open (the live host's rewriter, `send.ts::apiHref`) and the silence rule — a
 * connection whose server answers its heartbeat, and has gone quiet past a bound, is dead (`silenceWatchdog`). */

import { apiHref } from './send'

/** The URL a stream for `path` opens on this host: unchanged in the console, rewritten on the live host, '' when
 *  this host must not open it at all. */
export const streamUrl = (path: string): string => apiHref(path)

/** Open the server-sent event stream at `url` (from `streamUrl`). */
export function openEventSource(url: string, init?: EventSourceInit): EventSource {
  return new EventSource(url, init)
}

/** Open a WebSocket to `url`, offering `protocols` (a direct console's grant rides them, `lib/directConsole.ts`). */
export function openSocket(url: string, protocols?: string | string[]): WebSocket {
  return protocols === undefined ? new WebSocket(url) : new WebSocket(url, protocols)
}

/** How often a watchdog looks at its clock: a second, so it fires within a second of its bound. */
const SILENCE_CHECK_MS = 1_000

export type SilenceWatchdog = {
  /** A frame arrived: the connection is alive. */
  heard(): void
  /** The server answered a heartbeat, so its silence now means something. Until then the watchdog never fires: a
   *  server that sends no answer (an older control plane) is never torn down for being quiet. */
  arm(): void
  /** Stop watching (the connection closed, or its owner is gone). Idempotent. */
  stop(): void
}

/** Call `onSilent` once, after an armed connection has heard nothing for `silentAfterMs`. A hidden tab is not
 *  watched: the browser throttles its heartbeat to about one a minute, so its quiet proves nothing, and the clock
 *  restarts when the tab is seen again. */
export function silenceWatchdog(silentAfterMs: number, onSilent: () => void): SilenceWatchdog {
  const hasDocument = typeof document !== 'undefined'
  let heardAt = Date.now()
  let armed = false
  // Seen again: the throttled heartbeat has not caught up yet, so the clock starts now.
  const onVisibility = () => {
    if (document.visibilityState !== 'hidden') heardAt = Date.now()
  }
  const stop = () => {
    clearInterval(timer)
    if (hasDocument) document.removeEventListener('visibilitychange', onVisibility)
  }
  const timer = setInterval(() => {
    if (hasDocument && document.visibilityState === 'hidden') {
      heardAt = Date.now()
      return
    }
    if (!armed || Date.now() - heardAt < silentAfterMs) return
    stop()
    onSilent()
  }, SILENCE_CHECK_MS)
  if (hasDocument) document.addEventListener('visibilitychange', onVisibility)
  return {
    heard: () => { heardAt = Date.now() },
    arm: () => { armed = true; heardAt = Date.now() },
    stop,
  }
}
