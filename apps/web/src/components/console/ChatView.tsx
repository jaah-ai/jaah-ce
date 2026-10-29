import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, get } from '../../api'
import { useCan } from '../../auth'
import type { PipelineTask } from '../../types'
import { PHONE, PRE_WRAP_SX } from '../../ui/listStyles'
import GateForm, { type GateAnswer } from '../tasks/GateForm'
import { postRoundAnswer } from '../tasks/roundAnswer'
import {
  EventRow,
  POLL_MS,
  eventsKey,
  foldEvents,
  obj,
  readEvents,
  retryUnlessUnavailable,
  stillPolling,
  str,
  type AgentEvent,
} from '../agentEvents'
import { sendRuntimeControl, type ConsoleRuntime, type RuntimeControlType } from './consoleRuntime'

/* The console's Chat view: the slot's live ACP session as a conversation, a composer that steers it, Interrupt,
 * and the task's open questions. The event reader and renderers are the task page's (`agentEvents.tsx`), so tool
 * names and paths render WHOLE and wrap here too. Every disabled control carries its reason as visible text
 * (the CP's own copy from the runtime route), wired to the control by `aria-describedby`. */

const BUTTON_SX = { flexShrink: 0, [PHONE]: { minHeight: 44 } } as const
/** The task's open questions are re-read at this cadence; a new `user_input_request` lands within it. */
const QUESTIONS_POLL_MS = 3_000
/** The steer route's own cap on `text`; the counter shows only within `COUNTER_FROM` of it. */
const MESSAGE_MAX = 16_000
const COUNTER_FROM = MESSAGE_MAX - 1_000
/** How close to the bottom still counts as "following" the stream (auto-scroll on new events). */
const STICK_PX = 48

const SENT_COPY = 'Sent — the agent reads it on its next turn.'
/** A provider that cannot steer mid-turn: the host queues the message as its next prompt (`acp/host.py::_on_intent`). */
const QUEUED_COPY = 'Queued — sent when this turn ends.'
const NO_TURN_COPY = 'No turn is running.'
const OMITTED_COPY = 'Message too long to show.'
const READ_ONLY_COPY = "Your role can't answer questions."

/** 15200 → "15 200": digits grouped by a space, as the counter reads. */
const grouped = (n: number): string => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')

type Props = {
  projectId: number
  number: number
  nodeId?: string
  runtime: ConsoleRuntime & { task_id: number; session_id: string }
}

/** A turn is in flight while the latest `turn_start` comes after the latest `turn_end` / `session_end`. */
function turnInFlight(events: AgentEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const kind = events[i].kind
    if (kind === 'turn_end' || kind === 'session_end') return false
    if (kind === 'turn_start') return true
  }
  return false
}

/** The operator's own prompt (a `turn_start` the operator sent): their words, not a divider. A prompt the host
 * left out for length (`prompt_omitted`) is still theirs: a bubble that says so. */
function operatorPrompt(event: AgentEvent): { text: string; omitted: boolean } | null {
  if (event.kind !== 'turn_start') return null
  const d = obj(event.data)
  if (d.source !== 'operator') return null
  if (d.prompt_omitted === true) return { text: OMITTED_COPY, omitted: true }
  const text = str(d.prompt)
  return text === null ? null : { text, omitted: false }
}

function OperatorPrompt({ text, omitted }: { text: string; omitted: boolean }) {
  return (
    <Box
      data-testid="console-chat-operator-prompt"
      sx={{
        alignSelf: 'flex-end',
        maxWidth: '85%',
        minWidth: 0,
        px: 1.25,
        py: 0.75,
        borderRadius: 1.5,
        bgcolor: 'action.selected',
      }}
    >
      <Typography variant="caption" sx={{ fontWeight: 600, color: 'text.secondary' }}>You</Typography>
      <Typography variant="body2" sx={[PRE_WRAP_SX, omitted && { color: 'text.secondary', fontStyle: 'italic' }]}>
        {text}
      </Typography>
    </Box>
  )
}

/** A disabled control's reason, visible under it. */
function Reason({ id, testId, children }: { id: string; testId: string; children: string }) {
  return (
    <Typography id={id} data-testid={testId} variant="caption" sx={[PRE_WRAP_SX, { color: 'text.secondary', display: 'block' }]}>
      {children}
    </Typography>
  )
}

export default function ChatView({ projectId, number, nodeId, runtime }: Props) {
  const taskId = runtime.task_id
  const sessionId = runtime.session_id
  const client = useQueryClient()
  const can = useCan()

  const eventsQuery = useQuery({
    queryKey: eventsKey(taskId, sessionId),
    queryFn: () => readEvents(client, taskId, sessionId),
    retry: retryUnlessUnavailable,
    refetchInterval: q => (stillPolling(q.state.data) ? POLL_MS : false),
  })
  const events = eventsQuery.data?.events ?? []
  const inFlight = turnInFlight(events)
  // Display rows: a tool call's and a turn's usage events fold into one row each.
  const rows = useMemo(() => foldEvents(events), [events])
  const eventsLive = eventsQuery.isPending || stillPolling(eventsQuery.data)

  // Re-read while the session is live or a question is still open; an ended session with none asks nothing new.
  const taskQuery = useQuery({
    queryKey: ['console-chat-task', taskId],
    queryFn: () => get<PipelineTask>(`/api/tasks/${taskId}`),
    refetchInterval: q => (
      eventsLive || (q.state.data?.questions ?? []).some(x => x.status === 'open') ? QUESTIONS_POLL_MS : false
    ),
  })
  const task = taskQuery.data
  const openQuestions = (task?.questions ?? []).filter(q => q.status === 'open')

  // ── Follow the stream: scroll to the newest event unless the reader scrolled back. ──
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)
  useEffect(() => {
    const el = scrollRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [events.length, openQuestions.length])

  // ── Composer ──
  const [text, setText] = useState('')
  const [sending, setSending] = useState<RuntimeControlType | null>(null)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)

  const controlReason = runtime.control.available ? '' : runtime.control.reason
  // Mid-turn on a provider that cannot steer, Send queues instead: the CP's reason says why, as a note, not a block.
  const queues = inFlight && !runtime.steer.available
  const queueNote = queues && !controlReason ? runtime.steer.reason : ''
  const interruptReason = controlReason
    || (!inFlight ? NO_TURN_COPY : !runtime.interrupt.available ? runtime.interrupt.reason : '')

  async function send(type: RuntimeControlType, body: { text?: string }, ok: string) {
    setSending(type)
    setNotice(null)
    try {
      await sendRuntimeControl({ projectId, number, nodeId }, { session_id: sessionId, type, ...body })
      setNotice({ kind: 'ok', text: ok })
      return true
    } catch (err) {
      setNotice({ kind: 'error', text: apiDetail(err, 'Send failed') })
      return false
    } finally {
      setSending(null)
    }
  }

  async function onSend() {
    const message = text.trim()
    if (!message) {
      setNotice({ kind: 'error', text: 'Type a message first.' })
      return
    }
    if (await send('steer', { text }, queues ? QUEUED_COPY : SENT_COPY)) setText('')
  }

  // ── Open questions: the task page's answer form, posting the same per-round route. ──
  const [answering, setAnswering] = useState(false)
  const [answerError, setAnswerError] = useState('')
  async function answerQuestion(answer: GateAnswer, questionId?: number, comment?: string, attachmentIds?: number[]) {
    if (questionId === undefined) return
    setAnswering(true)
    setAnswerError('')
    try {
      const updated = await postRoundAnswer(taskId, answer, questionId, comment, attachmentIds)
      client.setQueryData(['console-chat-task', taskId], updated)
    } catch (err) {
      setAnswerError(apiDetail(err, 'Failed to post answer'))
    } finally {
      setAnswering(false)
    }
  }

  const busy = sending !== null
  const readOnly = !can('tasks:edit')
  const controlReasonId = `console-chat-control-reason-${taskId}`
  const sendReasonId = `console-chat-send-reason-${taskId}`
  const interruptReasonId = `console-chat-interrupt-reason-${taskId}`

  return (
    <Box
      data-testid="console-chat"
      data-in-flight={inFlight ? 'true' : 'false'}
      sx={{ flex: 1, minHeight: 0, minWidth: 0, display: 'flex', flexDirection: 'column', bgcolor: 'background.paper' }}
    >
      <Box
        ref={scrollRef}
        data-testid="console-chat-events"
        onScroll={e => {
          const el = e.currentTarget
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
        }}
        sx={{
          flex: '1 1 auto',
          minHeight: 0,
          overflowY: 'auto',
          overflowX: 'hidden',
          px: 1.5,
          py: 1,
          display: 'flex',
          flexDirection: 'column',
          gap: 0.75,
          [PHONE]: { px: 1 },
        }}
      >
        {eventsQuery.isError ? (
          <Typography variant="body2" role="alert" sx={[PRE_WRAP_SX, { color: 'error.main' }]}>
            {apiDetail(eventsQuery.error, 'Events failed to load')}
          </Typography>
        ) : eventsQuery.isPending ? (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>Loading…</Typography>
        ) : events.length === 0 ? (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>No events yet.</Typography>
        ) : (
          rows.map(event => {
            const prompt = operatorPrompt(event)
            return prompt !== null
              ? <OperatorPrompt key={event.seq} text={prompt.text} omitted={prompt.omitted} />
              : <EventRow key={event.seq} event={event} />
          })
        )}
        {openQuestions.length > 0 && task && (
          // In the scroll region, after the last event: the stick-to-bottom follow shows it, nothing is clipped.
          <Box data-testid="console-chat-questions" sx={{ flexShrink: 0, pt: 1, mt: 0.5, borderTop: 1, borderColor: 'divider' }}>
            {readOnly && (
              <Reason id={`console-chat-answer-reason-${taskId}`} testId="console-chat-answer-reason">{READ_ONLY_COPY}</Reason>
            )}
            <GateForm
              task={task}
              submitting={answering}
              error={answerError || undefined}
              // Never reached: with open rounds and `onAnswerQuestion` wired, GateForm answers per round.
              onSubmit={() => {}}
              onAnswerQuestion={answerQuestion}
              readOnly={readOnly}
              hideTaskHeader
            />
          </Box>
        )}
      </Box>

      <Box
        component="form"
        data-testid="console-chat-composer"
        onSubmit={e => {
          e.preventDefault()
          if (!controlReason && !busy) void onSend()
        }}
        sx={{ flexShrink: 0, borderTop: 1, borderColor: 'divider', px: 1.5, py: 1, display: 'flex', flexDirection: 'column', gap: 0.75, [PHONE]: { px: 1 } }}
      >
        {controlReason && <Reason id={controlReasonId} testId="console-chat-control-reason">{controlReason}</Reason>}
        <TextField
          multiline
          minRows={2}
          maxRows={8}
          fullWidth
          size="small"
          placeholder="Message"
          value={text}
          disabled={Boolean(controlReason)}
          onChange={e => setText(e.target.value)}
          onKeyDown={e => {
            // Ctrl/Cmd+Enter sends; plain Enter is a newline.
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !controlReason && !busy) {
              e.preventDefault()
              void onSend()
            }
          }}
          slotProps={{
            htmlInput: {
              'aria-label': 'Message',
              'aria-describedby': controlReason ? controlReasonId : undefined,
              'data-testid': 'console-chat-input',
              maxLength: MESSAGE_MAX,
            },
          }}
        />
        {text.length >= COUNTER_FROM && (
          <Typography
            data-testid="console-chat-counter"
            variant="caption"
            sx={{ alignSelf: 'flex-end', color: text.length >= MESSAGE_MAX ? 'warning.main' : 'text.secondary' }}
          >
            {grouped(text.length)} / {grouped(MESSAGE_MAX)}
          </Typography>
        )}
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <Box sx={{ minWidth: 0, flex: '1 1 10rem' }}>
            <Button
              type="submit"
              variant="contained"
              size="small"
              data-testid="console-chat-send"
              disabled={Boolean(controlReason) || busy}
              aria-describedby={controlReason ? controlReasonId : queueNote ? sendReasonId : undefined}
              sx={BUTTON_SX}
            >
              {/* Busy disables it too; the label is the reason. */}
              {busy ? 'Sending…' : queues ? 'Queue' : 'Send'}
            </Button>
            {queueNote && (
              <Reason id={sendReasonId} testId="console-chat-send-reason">{queueNote}</Reason>
            )}
          </Box>
          <Box sx={{ minWidth: 0, flex: '1 1 10rem', textAlign: 'right', [PHONE]: { textAlign: 'left' } }}>
            <Button
              type="button"
              variant="outlined"
              color="warning"
              size="small"
              data-testid="console-chat-interrupt"
              disabled={Boolean(interruptReason) || busy}
              aria-describedby={interruptReason ? (controlReason ? controlReasonId : interruptReasonId) : undefined}
              onClick={() => void send('interrupt', {}, 'Interrupt sent.')}
              sx={BUTTON_SX}
            >
              Interrupt
            </Button>
            {interruptReason && !controlReason && (
              <Reason id={interruptReasonId} testId="console-chat-interrupt-reason">{interruptReason}</Reason>
            )}
          </Box>
        </Box>
        {notice && (
          <Typography
            data-testid="console-chat-notice"
            variant="caption"
            role={notice.kind === 'error' ? 'alert' : 'status'}
            sx={[PRE_WRAP_SX, { color: notice.kind === 'error' ? 'error.main' : 'text.secondary' }]}
          >
            {notice.text}
          </Typography>
        )}
      </Box>
    </Box>
  )
}
