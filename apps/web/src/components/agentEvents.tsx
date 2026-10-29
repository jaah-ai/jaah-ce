import type { ReactNode } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { ApiError, get } from '../api'
import { clockTime } from '../lib/relativeTime'
import { formatZonedSpan } from '../lib/zonedTime'
import { formatCostUsd } from '../lib/taskCost'
import { Mono } from '../ui/DetailFields'
import { PHONE, PRE_WRAP_SX, monoSx } from '../ui/listStyles'
import { groupedCount, humanize } from '../lib/humanize'

/* An ACP agent session's normalised event stream (§3.1): the poll reader and the per-event renderers, shared by
 * the task page's Activity section (`TaskActivity`) and the console's Chat view (`console/ChatView`).
 * Hard rule for every renderer below: tool names, paths and commands are shown WHOLE and wrap
 * (`PRE_WRAP_SX`) — never truncated or ellipsised. Only command output scrolls, inside its own `<pre>`. */

// ── Wire types ──
export type AgentEvent = {
  v: number
  seq: number
  ts: string
  session_id: string
  native_session_ref?: string | null
  task_id?: number | null
  provider: string
  turn_id?: string | null
  item_id?: string | null
  kind: string
  status?: string | null
  data: Record<string, unknown> | null
}

type EventPage = {
  session_id: string
  status: string
  runtime: string
  events: AgentEvent[]
  last_seq: number
  more: boolean
}

/** What the events query accumulates across polls: every event so far (deduped by `seq`) and the cursor. */
/* `quiet`: reads in a row that found the session ended and brought nothing new. */
export type EventLog = { events: AgentEvent[]; last_seq: number; status: string; runtime: string; more: boolean; quiet: number }

export const POLL_MS = 1_000
/** `more` pages read back to back in one fetch; past this the next poll carries on. */
const MAX_PAGES = 20
/** The node posts its last batch (`session_end`, final usage) after the session ends, so an ended session is
 *  polled until that item arrives or this many reads in a row bring nothing. */
const QUIET_READS_AFTER_END = 10

// ── Defensive readers: any field may be missing or mistyped ──

export const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])
export const obj = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
const json = (v: unknown): string => {
  try {
    return JSON.stringify(v, null, 2) ?? ''
  } catch {
    return String(v)
  }
}

/** A quota reset: the clock alone when it falls on the viewer's today, else date, clock and zone
 *  (`Oct 19, 14:05 GMT+2`) so a reset days away never reads as this afternoon. */
export function resetTime(iso: string, now: Date = new Date()): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return iso
  return at.toDateString() === now.toDateString() ? clockTime(iso) : formatZonedSpan(iso, null, undefined, now).start
}

/** An `error` event's vendor JSON body (`API Error: 529 {"type":"error","error":{…}}`, or the bare JSON) as a
 *  one-line summary: the vendor's message, else its error type in words, plus the HTTP status when the
 *  prefix or the body names one. `null` when the message carries no parseable JSON object. */
export function vendorErrorSummary(message: string): { summary: string; raw: string } | null {
  const start = message.indexOf('{')
  const end = message.lastIndexOf('}')
  if (start < 0 || end < start) return null
  let body: unknown
  try {
    body = JSON.parse(message.slice(start, end + 1))
  } catch {
    return null
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) return null
  const outer = obj(body)
  const inner = Object.keys(obj(outer.error)).length > 0 ? obj(outer.error) : outer
  const type = str(inner.type)
  const text = str(inner.message) ?? (type ? humaniseErrorType(type) : null)
  if (!text) return null
  const status = num(outer.status) ?? num(outer.status_code) ?? Number(/\b([45]\d\d)\b/.exec(message.slice(0, start))?.[1] ?? NaN)
  return { summary: Number.isFinite(status) ? `${text} (${status})` : text, raw: message }
}

/* `overloaded_error` → `Overloaded`, `rate_limit_error` → `Rate limit`, `api_error` → `API error`. */
function humaniseErrorType(type: string): string {
  const words = type.replace(/_error$/, '')
  return words === 'api' ? 'API error' : humanize(words)
}

// ── Data ──

export const eventsKey = (taskId: number, sessionId: string) => ['task-activity-events', taskId, sessionId] as const

/* One poll: read from the cached cursor, follow `more` back to back, and merge into what is cached. */
export async function readEvents(client: QueryClient, taskId: number, sessionId: string): Promise<EventLog> {
  const prev = client.getQueryData<EventLog>(eventsKey(taskId, sessionId))
  const bySeq = new Map((prev?.events ?? []).map(e => [e.seq, e]))
  let after = prev?.last_seq ?? 0
  let page: EventPage
  let pages = 0
  do {
    page = await get<EventPage>(
      `/api/tasks/${taskId}/sessions/${encodeURIComponent(sessionId)}/events?after=${after}`,
    )
    for (const event of page.events ?? []) bySeq.set(event.seq, event)
    after = Math.max(after, num(page.last_seq) ?? after)
    pages += 1
  } while (page.more && pages < MAX_PAGES)
  const grew = after > (prev?.last_seq ?? 0)
  return {
    events: [...bySeq.values()].sort((a, b) => a.seq - b.seq),
    last_seq: after,
    status: page.status,
    runtime: page.runtime,
    more: Boolean(page.more),
    quiet: page.status === 'running' || grew ? 0 : (prev?.quiet ?? 0) + 1,
  }
}

/* Poll while the session runs, while pages are waiting, and after it ends until its `session_end` arrives. */
export function stillPolling(log: EventLog | undefined): boolean {
  if (!log) return false
  if (log.more) return true
  if (log.runtime !== 'acp') return false
  if (log.status === 'running') return true
  const ended = log.events.some(e => e.kind === 'session_end')
  return !ended && log.quiet < QUIET_READS_AFTER_END
}

/** A 503 (storage not configured) is an answer; retrying it only delays the detail. */
export const retryUnlessUnavailable = (count: number, err: unknown): boolean =>
  !(err instanceof ApiError && err.status === 503) && count < 2

// ── Folding: one display row per tool call and per turn's usage ──

/** Kinds the host reports more than once per `item_id`: `in_progress` on first sight, then the terminal event —
 *  its `output` possibly split over several events carrying `chunk = {index, count}`. */
const ITEM_KINDS = new Set(['command_execution', 'tool_call', 'file_change'])

const blank = (v: unknown): boolean =>
  v === null || v === undefined || v === '' || (Array.isArray(v) && v.length === 0)

/* Fields that describe one cost together: a later cost brings its own kind and source, blank or not, so a turn's
   cost never wears an earlier running total's `cumulative` label. */
const COST_GROUP = ['cost_kind', 'source'] as const

/* A later value fills or replaces a field; a later blank never erases what an earlier event said. */
function mergeData(into: Record<string, unknown>, data: Record<string, unknown>): void {
  for (const [key, value] of Object.entries(data)) if (!blank(value)) into[key] = value
  if (!blank(data.reported_cost_usd)) for (const key of COST_GROUP) into[key] = data[key] ?? null
}

/* A row being folded: its first event (status kept current), the merged data, and output chunks by index. */
type Folded = { event: AgentEvent; data: Record<string, unknown>; chunks: Map<number, string> }

/** The display rows of an event log: the events of one tool call (same kind and `item_id`) collapse into one row
 *  at the first one's place, carrying the latest status, their merged data and the chunked output joined in
 *  order; one turn's `usage` events collapse into one row per model. Everything else passes through. Each row
 *  keeps its first event's `seq` (the React key). Pure. */
export function foldEvents(events: AgentEvent[]): AgentEvent[] {
  const rows: (AgentEvent | Folded)[] = []
  const items = new Map<string, Folded>()
  const usage = new Map<string, Folded[]>()
  const open = (event: AgentEvent): Folded => {
    const row: Folded = { event, data: {}, chunks: new Map() }
    rows.push(row)
    return row
  }

  for (const event of events) {
    const data = obj(event.data)
    let row: Folded | undefined
    if (ITEM_KINDS.has(event.kind) && event.item_id) {
      const key = `${event.kind}\u0000${event.item_id}`
      row = items.get(key)
      if (!row) items.set(key, (row = open(event)))
    } else if (event.kind === 'usage' && event.turn_id) {
      const turn = usage.get(event.turn_id) ?? []
      usage.set(event.turn_id, turn)
      // One row per model: a model-less event joins the turn's latest row, a named one its model's or an unnamed one.
      const model = str(data.model)
      row = model === null
        ? turn[turn.length - 1]
        : turn.find(r => str(r.data.model) === model) ?? turn.find(r => str(r.data.model) === null)
      if (!row) turn.push((row = open(event)))
    } else {
      rows.push(event)
      continue
    }
    const { chunk, ...rest } = data
    const index = num(obj(chunk).index)
    if (index !== null && typeof rest.output === 'string') row.chunks.set(index, rest.output)
    mergeData(row.data, rest)
    if (!blank(event.status)) row.event = { ...row.event, status: event.status }
  }

  return rows.map(row => {
    if (!('chunks' in row)) return row
    const data = { ...row.data }
    if (row.chunks.size > 0) data.output = [...row.chunks].sort(([a], [b]) => a - b).map(([, part]) => part).join('')
    return { ...row.event, data }
  })
}

// ── Renderers ──

function Label({ children, tone }: { children: ReactNode; tone?: string }) {
  return (
    <Typography
      component="span"
      variant="caption"
      sx={{ fontWeight: 600, color: tone ?? 'text.secondary', mr: 0.75 }}
    >
      {children}
    </Typography>
  )
}

function Paths({ paths }: { paths: string[] }) {
  if (paths.length === 0) return null
  return (
    <Box component="ul" sx={{ m: 0, pl: 2 }}>
      {paths.map((p, i) => (
        <li key={`${i}-${p}`}><Mono wrap="pre">{p}</Mono></li>
      ))}
    </Box>
  )
}

/** A long block (command output, raw JSON): collapsed, and scrolls inside itself when opened. */
function Block({ summary, text }: { summary: string; text: string }) {
  return (
    <Box component="details" sx={{ mt: 0.5 }}>
      <Box component="summary" sx={{ cursor: 'pointer', typography: 'caption', color: 'text.secondary' }}>
        {summary}
      </Box>
      <Box
        component="pre"
        sx={[monoSx, PRE_WRAP_SX, {
          m: 0, mt: 0.5, p: 1, maxHeight: '20rem', overflow: 'auto', fontSize: '0.75rem',
          bgcolor: 'action.hover', borderRadius: 1,
        }]}
      >
        {text}
      </Box>
    </Box>
  )
}

const STATUS_TONE: Record<string, string> = { failed: 'error.main', declined: 'warning.main' }

function StatusWord({ status }: { status?: string | null }) {
  if (!status || !STATUS_TONE[status]) return null
  return <Label tone={STATUS_TONE[status]}>{status}</Label>
}

/** A usage row's cost, with what it covers: a `cumulative` figure is the whole session's so far, anything
 * else is this turn's alone. */
export function costLabel(cost: number, costKind: unknown): string {
  return `${formatCostUsd(cost)} ${costKind === 'cumulative' ? 'session total' : 'turn'}`
}

function usageLine(d: Record<string, unknown>): ReactNode {
  const parts: string[] = []
  const add = (label: string, v: unknown) => {
    const n = num(v)
    if (n !== null) parts.push(`${label} ${groupedCount(n)}`)
  }
  add('in', d.input_tokens)
  add('cache write', d.cache_write_tokens)
  add('cache read', d.cache_read_tokens)
  add('out', d.output_tokens)
  add('reasoning', d.reasoning_tokens)
  const used = num(d.context_used)
  const size = num(d.context_size)
  if (used !== null) parts.push(`context ${groupedCount(used)}${size !== null ? ` / ${groupedCount(size)}` : ''}`)
  const cost = num(d.reported_cost_usd)
  const agentReported = d.source === 'agent_reported'
  return (
    <>
      {str(d.model) && <Mono wrap="pre">{str(d.model)} </Mono>}
      <Box component="span" sx={PRE_WRAP_SX}>{parts.join(' · ')}</Box>
      {cost !== null && (
        <>
          {' · '}
          <Mono wrap="pre">{costLabel(cost, d.cost_kind)}</Mono>
          {agentReported && (
            <Box
              component="span"
              title="Cost reported by the agent, not priced from tokens"
              sx={{ ml: 0.5, color: 'text.secondary' }}
            >
              agent-reported
            </Box>
          )}
        </>
      )}
    </>
  )
}

function optionText(o: unknown): string {
  return str(o) ?? str(obj(o).label) ?? str(obj(o).name) ?? json(o)
}

/** A question's choices: a captioned list, each label with its description (muted) when it has one. */
function Options({ options }: { options: unknown[] }) {
  return (
    <Box component="section" aria-label="Options" sx={{ mt: 0.25 }}>
      <Typography variant="caption" sx={{ color: 'text.secondary', fontWeight: 600 }}>Options</Typography>
      <Box component="ul" sx={{ m: 0, pl: 2 }}>
        {options.map((o, i) => (
          <li key={i}>
            {optionText(o)}
            {str(obj(o).description) && (
              <Box component="span" sx={{ color: 'text.secondary' }}> — {str(obj(o).description)}</Box>
            )}
          </li>
        ))}
      </Box>
    </Box>
  )
}

/** An approval's ACP decision as words; an unknown value shows as sent. */
const DECISION_WORDS: Record<string, string> = {
  allow_once: 'Allowed once',
  allow_always: 'Allowed always',
  reject_once: 'Rejected',
  reject_always: 'Rejected',
  reject: 'Rejected',
  cancel: 'Cancelled',
}
function decisionWord(decision: string): string {
  return DECISION_WORDS[decision] ?? decision
}

/** The host's receipt for an operator's answer (`answer_received.data.decision`); an unknown value shows
 *  as sent. */
const ANSWER_WORDS: Record<string, string> = {
  queued: 'Answer queued',
  delivered: 'Answer delivered',
  dropped: 'Answer dropped',
}

/** One event's body. Unknown kinds fall through to their name and raw data. */
function EventBody({ event }: { event: AgentEvent }) {
  const d = obj(event.data)
  switch (event.kind) {
    case 'assistant_message':
      return <Typography variant="body2" sx={PRE_WRAP_SX}>{str(d.text) ?? ''}</Typography>
    case 'reasoning':
      return (
        <Typography variant="body2" sx={[PRE_WRAP_SX, { color: 'text.secondary', fontStyle: 'italic' }]}>
          {str(d.text) ?? ''}
        </Typography>
      )
    case 'command_execution': {
      const exit = num(d.exit_code)
      return (
        <>
          <Label>Command</Label>
          <StatusWord status={event.status} />
          {exit !== null && <Label tone={exit === 0 ? undefined : 'error.main'}>exit {exit}</Label>}
          {/* The first sight of a call may not carry its command yet: the tool's title, never a bare "$ ". */}
          <Box><Mono wrap="pre">{str(d.command) ? `$ ${str(d.command)}` : str(d.name) ?? ''}</Mono></Box>
          {str(d.cwd) && <Box><Mono wrap="pre" muted>{str(d.cwd)}</Mono></Box>}
          {str(d.output) && <Block summary="Output" text={str(d.output) as string} />}
        </>
      )
    }
    case 'file_change':
      return (
        <>
          <Label>{str(d.kind) ?? 'Change'}</Label>
          <StatusWord status={event.status} />
          {str(d.name) && <Mono wrap="pre" muted>{str(d.name)}</Mono>}
          <Paths paths={strs(d.paths)} />
        </>
      )
    case 'tool_call':
      return (
        <>
          <Label>Tool</Label>
          <StatusWord status={event.status} />
          <Mono wrap="pre">{str(d.name) ?? ''}</Mono>
          <Paths paths={strs(d.paths)} />
          {d.input !== undefined && d.input !== null && <Block summary="Input" text={json(d.input)} />}
        </>
      )
    case 'approval_request': {
      const call = obj(d.tool_call)
      const decision = str(d.decision)
      const by = str(d.decided_by)
      return (
        <>
          <Label tone="warning.main">Approval</Label>
          {str(call.kind) && <Label>{str(call.kind)}</Label>}
          <Mono wrap="pre">{str(call.name) ?? ''}</Mono>
          {str(call.command) && <Box><Mono wrap="pre">$ {str(call.command)}</Mono></Box>}
          <Paths paths={strs(call.paths)} />
          <Box sx={{ typography: 'body2' }}>
            {decision ? decisionWord(decision) : 'Pending'}
            {by && <> · by <Box component="span" sx={PRE_WRAP_SX}>{by}</Box></>}
          </Box>
        </>
      )
    }
    case 'user_input_request': {
      const questions = Array.isArray(d.questions)
        ? d.questions.map(obj)
        : str(d.question) ? [{ question: d.question }] : []
      return (
        <>
          <Label tone="warning.main">Question</Label>
          {questions.map((q, i) => (
            <Box key={i} sx={{ typography: 'body2', ...PRE_WRAP_SX }}>
              {str(q.header) && <Box component="span" sx={{ fontWeight: 600 }}>{str(q.header)}: </Box>}
              {str(q.question) ?? ''}
              {Array.isArray(q.options) && q.options.length > 0 && (
                <Options options={q.options} />
              )}
            </Box>
          ))}
        </>
      )
    }
    case 'subagent':
      return (
        <>
          <Label>Subagent</Label>
          {str(d.status) && <Label>{str(d.status)}</Label>}
          {str(d.name) && <Mono wrap="pre">{str(d.name)}</Mono>}
          {str(d.description) && <Typography variant="body2" sx={PRE_WRAP_SX}>{str(d.description)}</Typography>}
        </>
      )
    case 'plan': {
      const steps = (Array.isArray(d.entries) ? d.entries : Array.isArray(d.steps) ? d.steps : []).map(obj)
      return (
        <>
          <Label>Plan</Label>
          <Box component="ul" sx={{ m: 0, pl: 2, typography: 'body2' }}>
            {steps.map((s, i) => (
              <Box component="li" key={i} sx={PRE_WRAP_SX}>
                {str(s.content) ?? str(s.title) ?? ''}
                {str(s.status) && <Box component="span" sx={{ color: 'text.secondary' }}> · {str(s.status)}</Box>}
              </Box>
            ))}
          </Box>
        </>
      )
    }
    case 'usage':
      return (
        <Box sx={{ typography: 'caption', color: 'text.secondary' }}>
          <Label>Usage</Label>
          {usageLine(d)}
        </Box>
      )
    case 'rate_limit': {
      const resets = str(d.resets_at)
      return (
        <Box sx={{ typography: 'body2', color: 'warning.main', ...PRE_WRAP_SX }}>
          <Label tone="warning.main">Rate limit</Label>
          {[str(d.window), str(d.account_slug)].filter(Boolean).join(' · ')}
          {resets && <> · resets <span title={resets}>{resetTime(resets)}</span></>}
        </Box>
      )
    }
    case 'error': {
      const resets = str(d.resets_at)
      const message = str(d.message) ?? ''
      const vendor = vendorErrorSummary(message)
      return (
        <Box sx={{ color: 'error.main' }}>
          <Label tone="error.main">Error</Label>
          {str(d.class) && <Mono wrap="pre">{str(d.class)}</Mono>}
          <Typography variant="body2" sx={[PRE_WRAP_SX, { color: 'error.main' }]}>{vendor?.summary ?? message}</Typography>
          {vendor && <Block summary="Details" text={vendor.raw} />}
          {resets && (
            <Typography variant="caption">resets <span title={resets}>{resetTime(resets)}</span></Typography>
          )}
        </Box>
      )
    }
    case 'answer_received': {
      const decision = str(d.decision)
      return (
        <Box sx={{ typography: 'body2', color: decision === 'dropped' ? 'warning.main' : undefined }}>
          {decision ? ANSWER_WORDS[decision] ?? `Answer ${decision}` : 'Answer received'}
        </Box>
      )
    }
    default:
      return (
        <>
          <Label>{event.kind}</Label>
          <StatusWord status={event.status} />
          {Object.keys(d).length > 0 && (
            <Box component="pre" sx={[monoSx, PRE_WRAP_SX, { m: 0, fontSize: '0.75rem' }]}>{json(d)}</Box>
          )}
        </>
      )
  }
}

/* HOW THE SESSION OPENED: the ACP host's `session_start.data.how` (`acp/client.py::open_session`) —
   a fresh conversation, or one reopened by `session/resume` / `session/load`. Only the ACP host writes this stream
   (`acp/host.py`), so every session here opened over ACP. An unknown value shows as sent. */
const HOW_WORDS: Record<string, string> = { new: 'new', resume: 'resumed', load: 'reloaded' }

/** The engine CLI(s) the session ran, from `session_start.data.cli_versions` (`{argv0: "<exe> --version" first
 *  line}`, `acp/selfcheck.py::cli_versions`): the version line, named by the executable when the line does not
 *  name it already; a blank line (the probe failed) still names the executable. */
export function cliVersionsText(versions: unknown): string | null {
  const parts = Object.entries(obj(versions)).map(([exe, version]) => {
    const name = exe.split('/').pop() || exe
    const line = str(version)
    if (!line) return name
    return line.toLowerCase().includes(name.toLowerCase()) ? line : `${name} ${line}`
  })
  return parts.length > 0 ? parts.join(', ') : null
}

/** The engine as it names itself, from `session_start.data.agent` (`{name, version}`, ACP `initialize`'s
 *  `agentInfo`, `acp/client.py::agent_info`): preferred over `cli_versions`, which probes the launch's first
 *  argument and so cannot name an engine run through a wrapper. */
export function agentText(agent: unknown): string | null {
  const a = obj(agent)
  const name = str(a.name)
  if (!name) return null
  const version = str(a.version)
  return version ? `${name} ${version}` : name
}

/** Session and turn boundaries: one small divider line, not a row. */
function dividerText(event: AgentEvent): string | null {
  const d = obj(event.data)
  switch (event.kind) {
    case 'session_start': {
      const how = str(d.how)
      const engine = agentText(d.agent) ?? cliVersionsText(d.cli_versions)
      return ['Session start', 'ACP', how ? HOW_WORDS[how] ?? how : null, engine]
        .filter(Boolean)
        .join(' · ')
    }
    case 'turn_start':
      return 'Turn'
    case 'turn_end':
      return ['Turn end', str(d.stop_reason)].filter(Boolean).join(' · ')
    case 'session_end': {
      const exit = num(d.exit_code)
      return ['Session end', exit !== null ? `exit ${exit}` : null, str(d.cause)].filter(Boolean).join(' · ')
    }
    default:
      return null
  }
}

export function EventRow({ event }: { event: AgentEvent }) {
  const time = (
    <Typography
      component="time"
      variant="caption"
      dateTime={event.ts}
      title={event.ts}
      sx={[monoSx, { color: 'text.secondary', flexShrink: 0, [PHONE]: { fontSize: '0.6875rem' } }]}
    >
      {/* Both forms rendered, CSS picks one: seconds above the phone breakpoint, minutes below it. */}
      <Box component="span" data-precision="seconds" sx={{ [PHONE]: { display: 'none' } }}>
        {clockTime(event.ts, { seconds: true })}
      </Box>
      <Box component="span" data-precision="minutes" sx={{ display: 'none', [PHONE]: { display: 'inline' } }}>
        {clockTime(event.ts)}
      </Box>
    </Typography>
  )
  const divider = dividerText(event)
  if (divider !== null) {
    return (
      <Box
        data-testid="task-activity-event"
        data-kind={event.kind}
        sx={{ display: 'flex', alignItems: 'center', gap: 1, color: 'text.secondary', py: 0.25, [PHONE]: { gap: 0.5 } }}
      >
        {time}
        <Box sx={{ flex: '1 1 auto', borderTop: 1, borderColor: 'divider' }} />
        <Typography variant="caption" sx={PRE_WRAP_SX}>{divider}</Typography>
      </Box>
    )
  }
  return (
    <Box data-testid="task-activity-event" data-kind={event.kind} sx={{ display: 'flex', gap: 1, alignItems: 'baseline', [PHONE]: { gap: 0.5 } }}>
      {time}
      <Box sx={{ flex: '1 1 auto', minWidth: 0 }}>
        <EventBody event={event} />
      </Box>
    </Box>
  )
}
