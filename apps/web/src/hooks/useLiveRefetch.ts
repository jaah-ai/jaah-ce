import { createContext, useContext, useEffect, useRef } from 'react'
import { useDebouncedCallback } from './useDebouncedCallback'
import type { ConsoleEvent, UseEventStream } from './useEventStream'
import { isPageHidden, useHiddenDeferral } from './useHiddenDeferral'

const LIVE_REFETCH_DEBOUNCE_MS = 150
const LIVE_REFETCH_MAX_WAIT_MS = 1000

// R9: while the stream is `down`, live events have stopped, so a live view polls REST at this rate until it recovers —
// the board and the open task page alike. 30s matches Nodes.tsx, so the console polls at one rate.
export const STREAM_DOWN_POLL_MS = 30_000

/* QUIET actions: frequent machinery signals that change one figure, not a row's state — a running task's
   `metered` usage arrives every few minutes per runner (`app/routes/agent_sessions.py::report_live_usage`), and
   a pane message's mint and delivery (`control_message`, `app/live_runner.py`) moves only the task's message list.
   They reach only a consumer that names them in `actions`, so the console's many `entity: 'task'` listeners
   do not each re-read their whole view per runner tick. */
const QUIET_ACTIONS: readonly string[] = ['metered', 'control_message']

/* A SUBTREE WHOSE SUBJECT IS GOING AWAY re-reads nothing: the task page provides `true` while its row's
   DELETE is in flight, so the row's own `task` event — which can beat the DELETE's answer — sends no panel back for
   a row already gone (one console 404 per panel). Read at event time and at fire time; the default is live. */
export const LiveRefetchPausedContext = createContext(false)

/* EVERY event since this consumer's last read, not only the render's `lastEvent`: events arriving before
   one render collapse into one `lastEvent`, so an effect keyed on it alone hears only the last. `onEvents` runs once
   per new `lastEvent` (latest closure) with the unread events, oldest first; `overrun` says the last event read has
   left `recentEvents`' window, so unread events went with it — a refetching reader re-reads anyway. A hand-built
   stream without `recentEvents` (a test's) hands over `lastEvent` alone and never overruns. A stream that opened
   again after an error (`resumed` moved) is an overrun too: what it sent meanwhile never arrived. */
export function useEventsSinceSeen(
  { lastEvent, recentEvents, resumed = 0 }: UseEventStream,
  onEvents: (events: readonly ConsoleEvent[], overrun: boolean) => void,
): void {
  const onEventsRef = useRef(onEvents)
  onEventsRef.current = onEvents
  const seenRef = useRef<ConsoleEvent | null>(null)
  const resumedRef = useRef(resumed)
  useEffect(() => {
    const recent = recentEvents ?? (lastEvent ? [lastEvent] : [])
    const seen = seenRef.current
    seenRef.current = lastEvent
    const resumedNow = resumed !== resumedRef.current
    resumedRef.current = resumed
    const at = seen ? recent.lastIndexOf(seen) : -1
    const overrun = resumedNow || (seen !== null && at < 0 && recentEvents !== undefined)
    const events = recent.slice(at + 1)
    if (events.length > 0 || overrun) onEventsRef.current(events, overrun)
  }, [lastEvent, resumed])
}

// Live refetch over the shared SSE stream: a burst of events (optionally only `entity`'s) collapses to one
// debounced `refetch` (an unthrottled refetch-per-event turns a burst into a storm); while the stream is
// `down`, `poll` (default: `refetch`) runs every `pollMs` until recovery — `null` when the caller polls by
// other means (e.g. a query's `refetchInterval`) or not at all. `minIntervalMs` swaps the debounce for a
// throttle: the first event refetches at once, later ones at most once per `minIntervalMs`. Both callbacks
// are read at fire time (latest closure), so the interval is keyed on the stream state alone and never
// re-armed by a render. `actions` narrows to those actions only, and is the one way to hear a QUIET action.
// `match` narrows further by the event itself (e.g. its `id`), read at fire time. A HIDDEN TAB refetches nothing:
// the refetch is deferred to its return (`useHiddenDeferral`) and the down-stream poll skips its ticks.
export function useLiveRefetch(
  stream: UseEventStream,
  refetch: () => void,
  pollMs: number | null,
  {
    entity,
    actions,
    poll,
    minIntervalMs,
    match,
  }: {
    entity?: string
    actions?: readonly string[]
    poll?: () => void
    minIntervalMs?: number
    match?: (event: ConsoleEvent) => boolean
  } = {},
): void {
  const streamState = stream.state
  const pausedRef = useRef(false)
  pausedRef.current = useContext(LiveRefetchPausedContext)
  const deferred = useHiddenDeferral(() => {
    if (!pausedRef.current) refetch()
  })
  const { trigger } = useDebouncedCallback(deferred, LIVE_REFETCH_DEBOUNCE_MS, LIVE_REFETCH_MAX_WAIT_MS)
  const throttle = useRef<{ last: number; timer: ReturnType<typeof setTimeout> | null }>({ last: 0, timer: null })
  useEffect(() => () => {
    if (throttle.current.timer !== null) clearTimeout(throttle.current.timer)
  }, [])

  useEventsSinceSeen(stream, (events, overrun) => {
    if (pausedRef.current) return
    // `filter`, not `some`: a `match` with effects (the task page's seed drop) hears each event.
    const heard = events.filter(event =>
      (entity === undefined || event.entity === entity)
      && (actions ? actions.includes(event.action) : !QUIET_ACTIONS.includes(event.action))
      && (!match || match(event)))
    if (heard.length === 0 && !overrun) return
    if (minIntervalMs === undefined) return trigger()
    const t = throttle.current
    const run = () => {
      t.timer = null
      t.last = Date.now()
      deferred()
    }
    const wait = minIntervalMs - (Date.now() - t.last)
    if (wait <= 0) run()
    else if (t.timer === null) t.timer = setTimeout(run, wait)
  })

  const pollRef = useRef(poll ?? refetch)
  pollRef.current = poll ?? refetch
  useEffect(() => {
    if (streamState !== 'down' || pollMs === null) return
    const timer = setInterval(() => {
      if (!isPageHidden() && !pausedRef.current) pollRef.current()
    }, pollMs)
    return () => clearInterval(timer)
  }, [streamState, pollMs])
}
