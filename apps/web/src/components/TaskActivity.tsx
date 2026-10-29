import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail, apiHref, get } from '../api'
import { useCan } from '../auth'
import { useNow } from '../hooks/useMinuteTick'
import { type StalledAnswer, lastHandOver, pickedUp, stalledAnswer } from '../lib/answerPickup'
import { clockTime } from '../lib/relativeTime'
import type { PipelineTask } from '../types'
import { DetailSection } from '../ui/detail/DetailPage'
import { PHONE, PRE_WRAP_SX } from '../ui/listStyles'
import {
  type AgentEvent,
  type EventLog,
  EventRow,
  POLL_MS,
  eventsKey,
  foldEvents,
  readEvents,
  retryUnlessUnavailable,
  stillPolling,
} from './agentEvents'

export type { AgentEvent } from './agentEvents'

/* The task page's Activity section: the normalised event stream (§3.1) of the task's ACP agent sessions,
 * live while a session runs. Legacy sessions have no stream, so a task with none renders nothing.
 * Permissions: `sessions:view` (the list) AND `sessions:edit` (both events reads, live and `.jsonl` — they carry
 * the agent's text, like the transcript download). Without both, nothing renders and nothing is read.
 * The reader and the per-event renderers (whole, wrapped tool names and paths) live in `agentEvents.tsx`.
 * The list follows its newest event unless the reader scrolled back, and renders only the newest `WINDOW`
 * events until asked for earlier ones. With `inChat` the console dock already shows this run, so the section
 * points there instead of opening a second live feed. */

// ── Wire types (this section and its sessions hook, so they live here) ──

/** The subset of `/api/sessions` items this section reads. */
export type SessionItem = {
  agent_session_id: string
  status: string
  started_at: string
  engine?: string | null
  model?: string | null
  runtime?: 'acp' | 'legacy'
}

type SessionList = { items: SessionItem[] }

/** Sessions list refresh while the task runs, so a new session appears without a reload. */
const SESSIONS_POLL_MS = 10_000
/** Within this many px of the bottom counts as "at the newest event" (as the console chat). */
const STICK_PX = 48
/** Events rendered at first; each "Show earlier" reveals this many more. */
const WINDOW = 300

// ── The sessions: shared with the task page, which shows the Agent tab only when there is one ──

function useActivityAllowed(): boolean {
  const can = useCan()
  return can('sessions:view') && can('sessions:edit')
}

/** The task's ACP sessions, newest first. One query for the page and the section (same key).
 *  `enabled: false` reads nothing, for a caller whose task has not loaded yet. */
export function useTaskAgentSessions(
  taskId: number,
  live: boolean,
  enabled = true,
): { sessions: SessionItem[]; hasSessions: boolean } {
  const allowed = useActivityAllowed() && enabled
  const sessionsQuery = useQuery({
    queryKey: ['task-activity-sessions', taskId],
    // `window=all`: the list defaults to the last 24 h, and a task's first session can be older.
    queryFn: () => get<SessionList>(`/api/sessions?task=${taskId}&window=all`),
    enabled: allowed,
    refetchInterval: live ? SESSIONS_POLL_MS : false,
  })
  const sessions = (sessionsQuery.data?.items ?? [])
    .filter(s => s.runtime === 'acp')
    .sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))
  return { sessions, hasSessions: allowed && sessions.length > 0 }
}

/** How often the page re-reads the stream (and its clock) while an answer waits to be picked up. */
const PICKUP_POLL_MS = 10_000

/** An answer handed to this task's live ACP agent with no agent event since, past the grace window
 *  (`lib/answerPickup.ts`). Reads the live session's events under the section's own query key, so the Agent
 *  tab and this share one cache; stops reading once an agent event lands after the hand-over (the node's receipt is
 *  not one: it says the answer arrived, not that the agent resumed). Null without the `sessions:*`
 *  pair: no stream to judge by, so no claim. */
export function useStalledAnswer(task: PipelineTask | null): StalledAnswer | null {
  const client = useQueryClient()
  const allowed = useActivityAllowed()
  const taskId = task?.id ?? 0
  // The run's OWN session, never the list's newest: the list lags a retry by a poll.
  const session = task !== null && (task.state === 'active' || task.state === 'awaiting_response')
    && task.live_session?.runtime === 'acp' ? task.live_session : null
  const questions = task?.questions ?? []
  const handedAt = session ? lastHandOver(questions, session.started_at) : null
  const sessionId = session?.id ?? ''
  const cached = client.getQueryData<{ events: AgentEvent[] }>(eventsKey(taskId, sessionId))
  const heard = handedAt !== null && cached !== undefined && pickedUp(handedAt, cached.events)
  const watching = allowed && handedAt !== null && !heard
  const eventsQuery = useQuery({
    queryKey: eventsKey(taskId, sessionId),
    queryFn: () => readEvents(client, taskId, sessionId),
    enabled: watching,
    retry: retryUnlessUnavailable,
    refetchInterval: watching ? PICKUP_POLL_MS : false,
  })
  // A clock for the grace window: the stream can stay unchanged while the minute passes.
  const now = useNow(watching ? PICKUP_POLL_MS : null)
  if (!watching || !session || eventsQuery.data === undefined) return null
  return stalledAnswer(questions, eventsQuery.data.events, now, session.started_at)
}

// ── The section ──

/** Why the session's event log cannot be downloaded yet, or null when it can. */
export function downloadBlockedReason(status: string, log: EventLog | undefined, error: unknown): string | null {
  // A live stream is still growing: the file would end mid-turn.
  if (status === 'running') return 'Still running'
  // `events.jsonl` 503s exactly as the poll does (`routes/task_session_events.py::_where`).
  if (error instanceof ApiError && error.status === 503) return 'No event storage'
  // `events.jsonl` 409s for a session whose host never posted an event (`events_last_seq` unset).
  if (log !== undefined && log.last_seq === 0 && log.events.length === 0) return 'No event log recorded'
  return null
}

function sessionLabel(s: SessionItem): string {
  return [clockTime(s.started_at), s.engine, s.model, s.status === 'running' ? 'running' : null]
    .filter(Boolean)
    .join(' · ')
}

export default function TaskActivity({
  taskId,
  live = false,
  fill = false,
  onOpenInChat,
  inChat = false,
}: {
  taskId: number
  live?: boolean
  /** Fill the container (a tab's whole panel): the list becomes the scroll region. */
  fill?: boolean
  /** Shows "Open in chat" in the section's actions. */
  onOpenInChat?: () => void
  /** The console dock already shows this run: no second live feed, a pointer there instead. */
  inChat?: boolean
}) {
  const allowed = useActivityAllowed()
  const client = useQueryClient()
  const [picked, setPicked] = useState<string | null>(null)

  const { sessions } = useTaskAgentSessions(taskId, live)
  const current = sessions.find(s => s.agent_session_id === picked) ?? sessions[0] ?? null
  const sessionId = current?.agent_session_id ?? ''

  const eventsQuery = useQuery({
    queryKey: eventsKey(taskId, sessionId),
    queryFn: () => readEvents(client, taskId, sessionId),
    enabled: current !== null && !inChat,
    retry: retryUnlessUnavailable,
    refetchInterval: q => (stillPolling(q.state.data) ? POLL_MS : false),
  })

  if (!allowed || current === null) return null

  const events = eventsQuery.data?.events ?? []
  const blocked = downloadBlockedReason(eventsQuery.data?.status ?? current.status, eventsQuery.data, eventsQuery.error)
  const openInChat = onOpenInChat && (
    <Button size="small" onClick={onOpenInChat}>Open in chat</Button>
  )
  const actions = (
    <Stack direction="row" spacing={1.5} useFlexGap sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
      {sessions.length > 1 && (
        <TextField
          select
          size="small"
          label="Session"
          value={sessionId}
          onChange={e => setPicked(e.target.value)}
          slotProps={{ select: { native: true } }}
          sx={{ maxWidth: '100%' }}
        >
          {sessions.map(s => (
            <option key={s.agent_session_id} value={s.agent_session_id}>{sessionLabel(s)}</option>
          ))}
        </TextField>
      )}
      {/* DOWNLOAD SAYS WHY NOT: the bare link opened the endpoint's JSON refusal (409 no stream, 503 no
          storage) or a half-written file mid-run. A finished session with a stream gets the link; any other names
          the reason beside a disabled Download. The events read's own status is fresher than the list's poll. */}
      {blocked ? (
        <Typography variant="body2" data-testid="task-activity-download-blocked" sx={{ color: 'text.secondary' }}>
          <Box component="span" aria-disabled="true" sx={{ color: 'text.disabled' }}>Download</Box>
          {' · '}{blocked}
        </Typography>
      ) : (
        <MuiLink
          href={apiHref(`/api/tasks/${taskId}/sessions/${encodeURIComponent(sessionId)}/events.jsonl`)}
          rel="noopener"
          variant="body2"
          aria-label={`Download events for session ${sessionId}`}
        >
          Download
        </MuiLink>
      )}
      {!inChat && openInChat}
    </Stack>
  )

  return (
    <DetailSection testId="task-page-panel" title="Agent activity" headingLevel="h3" actions={actions} fill={fill}>
      {inChat ? (
        <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>Following live in the console chat.</Typography>
          {openInChat}
        </Stack>
      ) : eventsQuery.isError ? (
        <Typography variant="body2" role="alert" sx={[PRE_WRAP_SX, { color: 'error.main' }]}>
          {apiDetail(eventsQuery.error, 'Events failed to load')}
        </Typography>
      ) : eventsQuery.isPending ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>Loading…</Typography>
      ) : events.length === 0 ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>No events yet.</Typography>
      ) : (
        // Keyed by session: a new session starts at its newest event with a fresh window.
        <EventList key={sessionId} events={events} fill={fill} />
      )}
    </DetailSection>
  )
}

// ── The list: follows the newest event, windowed to the newest `shown` ──

function EventList({ events: log, fill }: { events: AgentEvent[]; fill: boolean }) {
  // Display rows: a tool call's and a turn's usage events fold into one row each, so the window counts rows.
  const events = useMemo(() => foldEvents(log), [log])
  const [shown, setShown] = useState(WINDOW)
  const [away, setAway] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)
  // Distance from the bottom before revealing earlier events, so the reader's place holds.
  const fromBottomRef = useRef<number | null>(null)
  const seenRef = useRef(events.length)

  /* New events: a follower is taken to them before paint; a reader scrolled back keeps the window's START, so the
     rows under them never slide up as the oldest would drop off a fixed-length window. Keyed on the rows, not their
     count: an event folding into a row already shown grows it without adding one. */
  useLayoutEffect(() => {
    const grew = events.length - seenRef.current
    seenRef.current = events.length
    const el = scrollRef.current
    if (!el) return
    if (stickRef.current) el.scrollTop = el.scrollHeight
    else if (grew > 0) setShown(n => n + grew)
  }, [events])

  /* THE PANEL MOUNTS HIDDEN (the task page keeps every tab's panel mounted), so the first scroll-to-bottom ran on a
     zero-height box and the tab opened on the oldest shown event. Re-follow whenever the box's size changes —
     the tab shown, a rotated phone — while the reader has not scrolled back. */
  useEffect(() => {
    const el = scrollRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (stickRef.current) el.scrollTop = el.scrollHeight
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (el && fromBottomRef.current !== null) el.scrollTop = el.scrollHeight - fromBottomRef.current
    fromBottomRef.current = null
  }, [shown])

  const hidden = Math.max(0, events.length - shown)
  const more = Math.min(WINDOW, hidden)

  function jump() {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
    stickRef.current = true
    setAway(false)
  }

  return (
    <Box sx={{ position: 'relative', minWidth: 0, display: 'flex', flexDirection: 'column', ...(fill ? { flex: '1 1 auto', minHeight: 0 } : {}) }}>
      <Stack
        ref={scrollRef}
        spacing={0.75}
        data-testid="task-activity"
        onScroll={e => {
          const el = e.currentTarget
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
          setAway(!stickRef.current)
        }}
        /* On a phone the page grows to its content and the shell scrolls, so a filling list would never be the scroller
           it follows in: there it is capped like the unfilled one. */
        sx={{
          minWidth: 0,
          overflowY: 'auto',
          overflowX: 'hidden',
          ...(fill ? { flex: '1 1 auto', minHeight: 0, [PHONE]: { maxHeight: '70vh' } } : { maxHeight: '70vh' }),
        }}
      >
        {hidden > 0 && (
          <Button
            size="small"
            onClick={() => {
              const el = scrollRef.current
              if (el) fromBottomRef.current = el.scrollHeight - el.scrollTop
              setShown(n => n + WINDOW)
            }}
            sx={{ alignSelf: 'center' }}
          >
            Show {more} earlier events
          </Button>
        )}
        {events.slice(-shown).map(event => <EventRow key={event.seq} event={event} />)}
      </Stack>
      {away && (
        <Button
          variant="contained"
          size="small"
          data-testid="task-activity-jump"
          onClick={jump}
          sx={{ position: 'absolute', bottom: 8, left: '50%', transform: 'translateX(-50%)' }}
        >
          Jump to latest
        </Button>
      )}
    </Box>
  )
}
