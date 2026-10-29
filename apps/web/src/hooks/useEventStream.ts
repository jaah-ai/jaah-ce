import { useEffect, useState } from 'react'
import { apiHref, useApiRewriterVersion } from '../api'
import { useTabOrg, withOrgParam } from '../lib/activeOrg'

// Native EventSource on GET /api/stream. Its built-in auto-reconnect covers a dropped connection only: an HTTP
// error answer (a deploy's 502/503 from the load balancer) CLOSES it for good, and the tab sat on the 'down' poll
// until a reload. A closed source is therefore reopened here, with backoff (`REOPEN_*_MS`).
// States: 'connected'; 'reconnecting' (retrying); 'down' (retrying past DOWN_AFTER_MS — callers
// fall back to REST polling).
// ONE EventSource per `path`, refcounted and shared by every consumer: every CommandButton calls this
// hook, and per-call connections piled up dozens of `/api/stream` sockets on a single page.

export type EventStreamState = 'connected' | 'reconnecting' | 'down'

export type ConsoleEvent = {
  entity: string
  id: unknown
  action: string
}

export type UseEventStream = {
  state: EventStreamState
  lastEvent: ConsoleEvent | null
  /* EVERY event, newest last and `lastEvent` its tail, capped at RECENT_EVENTS_MAX. Events arriving
     before one render collapse into one `lastEvent`, so a reader of that alone loses all but the last: a task's
     `active` followed by a worktree or a `metered` tick left the board on its `?` until a reload.
     Optional, so a hand-built stream (a test's) degrades to `lastEvent`. */
  recentEvents?: readonly ConsoleEvent[]
  /* Bumped each time the stream opens again after an error: events sent while it was away are lost,
     so `useEventsSinceSeen` hands its readers an overrun and they re-read. Optional, like `recentEvents`. */
  resumed?: number
}

// Far above any burst one render can collapse; a `useLiveRefetch` reader that falls behind it refetches anyway.
const RECENT_EVENTS_MAX = 100

// Gives EventSource's own retry (a few seconds each) several attempts before reporting 'down'.
const DOWN_AFTER_MS = 20_000

// A CLOSED source is reopened after REOPEN_MIN_MS, doubling per failed reopen up to REOPEN_MAX_MS: a deploy's
// gap is seconds, and a refusing server (a WAF 429, a 401) is not hammered.
const REOPEN_MIN_MS = 1_000
const REOPEN_MAX_MS = 30_000

function isConsoleEvent(value: unknown): value is ConsoleEvent {
  if (!value || typeof value !== 'object') return false
  const v = value as Record<string, unknown>
  return typeof v.entity === 'string' && typeof v.action === 'string'
}

type StreamListener = {
  onOpen: () => void
  onMessage: (evt: ConsoleEvent) => void
  onError: () => void
}

type SharedStream = {
  // Null only while `acquireStream` builds it: the source's handlers need the stream they report to.
  source: EventSource | null
  listeners: Set<StreamListener>
  reopenTimer: ReturnType<typeof setTimeout> | null
}

const sharedStreams = new Map<string, SharedStream>()

// Registered once so a hard reload closes each shared connection exactly once; a half-open EventSource
// can stall the next page's initial load.
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {
    for (const stream of sharedStreams.values()) {
      if (stream.reopenTimer) clearTimeout(stream.reopenTimer)
      stream.source?.close()
    }
    sharedStreams.clear()
  })
}

function openSource(path: string, stream: SharedStream, reopenDelayMs: number): EventSource {
  const source = new EventSource(path, { withCredentials: true })
  source.onopen = () => {
    reopenDelayMs = REOPEN_MIN_MS
    for (const l of stream.listeners) l.onOpen()
  }
  source.onmessage = (evt: MessageEvent<string>) => {
    try {
      const parsed: unknown = JSON.parse(evt.data)
      if (isConsoleEvent(parsed)) for (const l of stream.listeners) l.onMessage(parsed)
    } catch {
      /* ignore malformed payloads — the keepalive comment never reaches onmessage */
    }
  }
  source.onerror = () => {
    for (const l of stream.listeners) l.onError()
    // CONNECTING: the browser retries by itself. CLOSED: it never will — reopen, unless released meanwhile.
    if (source.readyState !== EventSource.CLOSED || stream.reopenTimer || sharedStreams.get(path) !== stream) return
    stream.reopenTimer = setTimeout(() => {
      stream.reopenTimer = null
      if (sharedStreams.get(path) !== stream) return
      stream.source = openSource(path, stream, Math.min(reopenDelayMs * 2, REOPEN_MAX_MS))
    }, reopenDelayMs)
  }
  return source
}

function acquireStream(path: string, listener: StreamListener): () => void {
  let stream = sharedStreams.get(path)
  if (!stream) {
    // `path` arrives with `?org=` already on it (`useEventStream`): keyed by it, a stream opened before the pin
    // is replaced, not shared, once the pin lands.
    const created: SharedStream = { source: null, listeners: new Set(), reopenTimer: null }
    sharedStreams.set(path, created)
    created.source = openSource(path, created, REOPEN_MIN_MS)
    stream = created
  }

  stream.listeners.add(listener)
  // A reader joining a live stream hears its state now, not at the next transition: an open one says so, and one
  // that is down (closed, a reopen pending) starts this reader's 'down' clock and marks its next open a resume.
  if (stream.source?.readyState === EventSource.OPEN) listener.onOpen()
  else if (stream.reopenTimer) listener.onError()
  return () => {
    stream!.listeners.delete(listener)
    if (stream!.listeners.size === 0) {
      if (stream!.reopenTimer) clearTimeout(stream!.reopenTimer)
      stream!.source?.close()
      // `pagehide` may have cleared the map and a newer stream taken the path: delete only this one.
      if (sharedStreams.get(path) === stream) sharedStreams.delete(path)
    }
  }
}

// `entities` filters only THIS consumer's `lastEvent`; the shared connection and `state` are unaffected.
// Lets a page skip refetching on irrelevant events (`task` alone has 44 emit sites). Omit to see all.
// `basePath: null` opts out (hooks cannot be conditional): no listener, no connection, never re-rendered by an
// event; `state` reads 'down'.
export function useEventStream(
  basePath: string | null = '/api/stream',
  entities?: readonly string[],
): UseEventStream {
  /* `?org=`: the org this tab acts as, so a tab an org switch left behind is refused (`lib/activeOrg.ts`).
     Re-rendered by the pin, so a stream opened before the first live identity answer reopens carrying it. */
  useTabOrg()
  /* The live host's stream URL (`apiHref`) is rewritten once its rewriter is installed, so that re-renders here and
     the stream opens under it. In the console both are no-ops. */
  useApiRewriterVersion()
  const path = basePath === null ? '' : apiHref(withOrgParam(basePath))
  const [state, setState] = useState<EventStreamState>(path ? 'reconnecting' : 'down')
  const [recentEvents, setRecentEvents] = useState<readonly ConsoleEvent[]>([])
  const [resumed, setResumed] = useState(0)
  // Joined so an identical inline array does not re-subscribe every render.
  const entityKey = entities ? [...entities].sort().join(',') : ''

  useEffect(() => {
    let cancelled = false
    let downTimer: ReturnType<typeof setTimeout> | null = null
    let errored = false

    function clearDownTimer() {
      if (downTimer) {
        clearTimeout(downTimer)
        downTimer = null
      }
    }

    // '' is a path this host must not open (`apiHref`): no stream, and polling callers fall back as for 'down'.
    if (!path) {
      setState('down')
      return
    }
    const release = acquireStream(path, {
      onOpen: () => {
        if (cancelled) return
        clearDownTimer()
        setState('connected')
        if (errored) setResumed(n => n + 1)
        errored = false
      },
      onMessage: (evt) => {
        if (cancelled) return
        if (entityKey && !entityKey.split(',').includes(evt.entity)) return
        setRecentEvents(prev => [...prev.slice(1 - RECENT_EVENTS_MAX), evt])
      },
      onError: () => {
        if (cancelled) return
        // CONNECTING (retrying) or CLOSED (reopened by `openSource`) — unusable either way; start the 'down' clock.
        errored = true
        setState('reconnecting')
        if (!downTimer) {
          downTimer = setTimeout(() => {
            if (!cancelled) setState('down')
          }, DOWN_AFTER_MS)
        }
      },
    })

    return () => {
      cancelled = true
      clearDownTimer()
      release()
    }
  }, [path, entityKey])

  return { state, lastEvent: recentEvents[recentEvents.length - 1] ?? null, recentEvents, resumed }
}
