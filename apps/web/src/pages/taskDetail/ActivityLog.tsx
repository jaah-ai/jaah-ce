// Details' Timeline feed: one renderer over the task's unified `/activity` rows.
import { Fragment, useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { Link } from 'react-router-dom'
import Box from '@mui/material/Box'
import MuiLink from '@mui/material/Link'
import ExternalLink from '../../ui/ExternalLink'
import Typography from '@mui/material/Typography'
import { taskRoleLabel } from '../../components/tasks/personaRegistry'
import { deescapeMarkdownText, displayTitle, statusForState, TaskStatusBadge } from '../../components/tasks/shared'
import { useNow } from '../../hooks/useNow'
import { capitalize } from '../../lib/humanize'
import { absoluteTime, briefTimestamp, fmtTimestamp, formatSpan } from '../../lib/relativeTime'
import { sessionHref } from '../../lib/sessionLink'
import type { PipelineTask, TaskControlMessage } from '../../types'
import { withNotices } from './taskNotices'
import { CodeBlock } from '../../ui/CodeBlock'
import EmptyState from '../../ui/EmptyState'
import StatusDot from '../../ui/StatusDot'
import { FONT_SIZE, FONT_WEIGHT, grid } from '../../theme'
import { UNSTYLED_BUTTON_SX, ELLIPSIS_SX } from '../../ui/listStyles'
import LinkButton from '../../ui/LinkButton'
import { useCan } from '../../auth'
import { paneLogPath } from '../audit/PaneLogPage'
import Tip from '../../ui/Tip'

// One Activity feed row — mirrors ActivityLogEntryOut in apps/api/app/routes/tasks/activity.py: every `task:%`
// transition plus every `task_note:%` row, merged chronologically. On an epic `data.id` may be a CHILD's
// id; an edit row may carry `data.changes` (`{field: {before, after}}`); a session row carries
// `data.account_slug` and optionally `data.account_name` and `data.account_email`. `data` stays generic and is narrowed at render.
export type ActivityLogEntry = {
  category: string
  action: string
  label: string
  summary: string
  state: string | null
  created_at: string
  duration_seconds: number | null
  // The person who caused the row; null for a machine row (and rows from before actors were recorded).
  actor?: string | null
  // A row from before the task's last Reset: folded under one "Earlier attempts" disclosure.
  earlier_attempt?: boolean
  // Identical consecutive rows this one stands for (a node refusing the same dispatch again); `data.last_at` the latest.
  repeat_count?: number
  data: Record<string, unknown>
}

type ActivityChange = { before: unknown; after: unknown }

/** Whether a row's summary says nothing its label does not: the same words, case, spacing and `_` aside. */
export function restatesLabel(summary: string, label: string): boolean {
  const words = (text: string) => text.replace(/_/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase()
  return words(summary) !== '' && words(summary) === words(label)
}

// A changed field's label, as the task editor words it (`update.py` records the wire name). A field missing
// here reads as its wire name in sentence case, the `_id` storage suffix dropped.
const ACTIVITY_FIELD_LABELS: Readonly<Record<string, string>> = {
  title: 'Title',
  body: 'Description',
  priority: 'Priority',
  persona: 'Persona',
  autonomous: 'Involvement',
  model: 'Model',
  bound_provider: 'Agent',
  agent_id: 'Agent',
  hidden: 'Hidden',
  project_id: 'Project',
  feature_id: 'Feature',
  project_memory: 'Project memory',
  pinned_node_id: 'Node',
  playbook_id: 'Playbook',
  assigned_to_id: 'Assigned to',
}
function activityFieldLabel(field: string): string {
  const words = field.replace(/_id$/, '').replace(/_/g, ' ').trim()
  return ACTIVITY_FIELD_LABELS[field] ?? capitalize(words)
}

// The states a run has finished in: no runner will move it again unless someone starts it.
const FINISHED_STATES: ReadonlySet<string> = new Set(['done', 'system_failed'])

// A flag reads in the editor's words: Involvement's option label, any other switch On/Off.
const BOOLEAN_CHANGE_WORDS: Readonly<Record<string, readonly [string, string]>> = {
  autonomous: ['Unattended', 'Guided'],
}

// A before/after value as the editor words it, whole: it shows only in an opened row, which has the room (a description,
// the one multi-KB field, is never printed here — it links to Versions). A changed `title` reads as the trail shows it:
// stored titles may carry CommonMark escapes (`\[ops\]`).
function changeValue(field: string, v: unknown): string {
  if (v === null || v === undefined || v === '') return '(empty)'
  if (typeof v === 'boolean') {
    const [on, off] = BOOLEAN_CHANGE_WORDS[field] ?? ['On', 'Off']
    return v ? on : off
  }
  if (typeof v !== 'string') return JSON.stringify(v)
  return field === 'title' ? deescapeMarkdownText(v) : v
}

// A summary this long, or on several lines, is shown whole in the row's expansion too: beside its label and time the
// line cuts it well before 80 characters on a split pane, sooner on a phone.
const LONG_SUMMARY_CHARS = 40

// Each row's identity across re-reads: its index shifts when notices merge in after the feed. Identical rows (a
// repeated edit in the same second) are told apart by their order among themselves.
function rowKeys(entries: ActivityLogEntry[]): string[] {
  const seen = new Map<string, number>()
  return entries.map(e => {
    const base = `${e.created_at}|${e.category}|${e.action}|${String(e.data.id ?? '')}|${e.label}`
    const n = seen.get(base) ?? 0
    seen.set(base, n + 1)
    return `${base}#${n}`
  })
}

// How often an open runner phase's duration re-renders; the operator chose 30s over a per-second tick.
const RUNNING_DURATION_TICK_MS = 30_000
// The `active` transition carrying a duration is the one phase still running (a closed phase's duration
// rides its end row instead).
const isOpenRunnerPhase = (e: ActivityLogEntry) =>
  e.category === 'transition' && e.action === 'active' && e.duration_seconds !== null

/* The Activity feed's timeline rail: a left border and a dot per row; dot colour distinguishes notes
   from transitions. */
const TIMELINE_SX = { listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column' }

const TIMELINE_ITEM_SX = {
  position: 'relative',
  display: 'flex',
  gap: 1.5,
  pb: 0.5,
  pl: 0.5,
  borderLeft: 2,
  borderColor: 'divider',
  ml: 0.5,
  '&:last-of-type': { paddingBottom: 0 },
} as const

const TIMELINE_BODY_SX = { display: 'flex', flexDirection: 'column', gap: 0, pl: 1, minWidth: 0, flex: '1 1 auto' }

// The "Earlier attempts" fold's toggle: a text button in the caption tone.
const EARLIER_TOGGLE_SX = {
  ...UNSTYLED_BUTTON_SX, color: 'text.secondary', fontSize: FONT_SIZE.base,
  textAlign: 'left', '&:hover': { color: 'text.primary', textDecoration: 'underline' },
} as const

/* EVERY ROW IS ONE LINE: label, summary and time never wrap; the label and summary cut with an ellipsis (whole on
   hover), so a phone-width feed truncates instead of scrolling sideways. */
const TIMELINE_LINE_SX = { display: 'flex', alignItems: 'center', gap: 1, minWidth: 0, whiteSpace: 'nowrap' }

// What a row folds away: shown under its line once the row is opened.
const TIMELINE_EXPANSION_SX = { display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0, pb: 0.5 }

const CAPTION_SX = { color: 'text.secondary', overflowWrap: 'anywhere' } as const

function TimelineDot({ note }: { note?: boolean }) {
  return (
    <StatusDot
      color={note ? 'primary.main' : 'secondary.main'}
      sx={{ position: 'absolute', left: grid(-1), top: grid(0.5) }}
    />
  )
}

// A refused dispatch's `data.fix` value for a missing integration branch (`routes/agent/slot_intents.py::
// DISPATCH_FIX_INTEGRATION_BRANCH`): the row links to the project's Customize › Branches, where that field is shown.
const DISPATCH_FIX_INTEGRATION_BRANCH = 'integration_branch'

// A runner phase's own rows: its start, its end (the transition carrying the closed phase's duration) and a
// termination note.
const isRunnerPhaseRow = (e: ActivityLogEntry) =>
  e.category === 'transition' ? e.action === 'active' || e.duration_seconds !== null : e.action === 'runner_terminated'


// The row that parked `taskId`'s open gate: its first current-attempt transition INTO awaiting a response since it was
// last in another state. Every `task:*` row carries the task's state as written, so an edit or a re-raised question
// while parked is not the park. On an epic feed a child's rows are skipped. Null when the gate is left.
export function gateParkEvent(entries: ActivityLogEntry[], taskId: number): { at: string; actor: string | null } | null {
  let park: ActivityLogEntry | null = null
  for (const e of entries) {
    if (e.category !== 'transition' || e.earlier_attempt || Number(e.data.id) !== taskId) continue
    if (e.state !== 'awaiting_response') park = null
    else park ??= e
  }
  return park ? { at: park.created_at, actor: park.actor ?? null } : null
}

// Details' Timeline feed — the page's ONE event renderer over the unified `/activity` feed.
// `epicId` + `childTasks` label rolled-up per-child rows (`data.id !== epicId`) with a deep link. `projectId`
// is the task's own: a project move carries its subtree, so a child row shares it. `onOpenDetails` is where an
// "Outcome reported" row points: Details already shows that summary in full. `onOpenVersions` is where a
// description change points: the task's Versions view compares descriptions, the Timeline keeps none of its own. A `script` run (the merge lane) is
// no Claude session, so its rows link no session. `gateLogHref` is a merge-lane run's latest gate log
// (`components/tasks/laneRun.ts::latestGateLog`), linked from its runner rows. `currentState` is the task's
// own: a FINISHED script run's many passes through the same states would chip every row, so only the row that
// put it in its current state keeps its chip there.
export default function ActivityLog(
  {
    entries: feed, notices = null, epicId, childTasks, projectId, onOpenDetails, onOpenVersions, hasVersions = true, script,
    gateLogHref = null, currentState = null,
  }:
  {
    entries: ActivityLogEntry[]; notices?: TaskControlMessage[] | null; epicId: number; childTasks: PipelineTask[]
    projectId: number; onOpenDetails: () => void; onOpenVersions: () => void; hasVersions?: boolean; script: boolean
    gateLogHref?: string | null; currentState?: string | null
  },
) {
  // The `/activity` feed with the control-plane notices merged in by time (`taskNotices.ts`).
  const entries = useMemo(() => withNotices(feed, notices, epicId), [feed, notices, epicId])
  const keys = useMemo(() => rowKeys(entries), [entries])
  /* THE RESET ROW STARTS THE CURRENT ATTEMPT: the server flags it earlier (`task:reset` stamps before its
     own watermark, `routes/tasks/activity.py::_row_predates_its_reset`), so a fresh Reset folded the whole trail and
     showed no row at all. A task's last earlier row, when it is its Reset, stays out of the fold. */
  const lastEarlier = new Map<string, number>()
  entries.forEach((e, i) => { if (e.earlier_attempt) lastEarlier.set(String(e.data.id ?? ''), i) })
  const folded = entries.map((e, i) => Boolean(e.earlier_attempt)
    && !(e.category === 'transition' && e.action === 'reset' && lastEarlier.get(String(e.data.id ?? '')) === i))
  /* A STATE CHIP MARKS A TRANSITION: an edit or a restated row leaves the state as it was, so only a row
     whose state differs from its task's previous one carries it — per owner, so an epic's children chip their own.
     A Reset starts the trail over, as the server's own tracking does (`in_earlier`), so each task's first unfolded row
     after its folded ones chips: per owner, since an epic Reset interleaves its children's Reset rows. */
  const lastState = new Map<string, string>()
  const ownerFolded = new Map<string, boolean>()
  const movesState = entries.map((e, i) => {
    const owner = String(e.data.id ?? '')
    if (!folded[i] && ownerFolded.get(owner)) lastState.delete(owner)
    ownerFolded.set(owner, folded[i])
    if (e.state === null) return false
    const moved = lastState.get(owner) !== e.state
    lastState.set(owner, e.state)
    return moved
  })
  // The feed is chronological (`routes/tasks/activity.py::get_task_activity`), so the LAST row into the state is its cause.
  const chipOnlyAt = script && currentState !== null && FINISHED_STATES.has(currentState)
    ? entries.map((e, i) => (movesState[i] ? e.state : null)).lastIndexOf(currentState)
    : null
  // The rows opened to show what their one line folds away, by a key that survives notices merging in later.
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set())
  const toggleRow = (key: string) => setOpened(prev => {
    const next = new Set(prev)
    if (!next.delete(key)) next.add(key)
    return next
  })
  // Rows from before the last Reset fold under one disclosure, closed by default.
  const [earlierOpen, setEarlierOpen] = useState(false)
  // A runner phase still open at read time is the `active` transition the server gave a duration
  // (`routes/tasks/`, AC6.5). Its duration keeps growing on the wall clock: tick it every 30s from
  // the moment this feed arrived — elapsed on the client's own clock, so client/server skew never shows. Keyed on the
  // `/activity` read alone: a notice landing carries no fresh duration.
  const receivedAt = useMemo(() => Date.now(), [feed])
  // The pane-log viewer asks `sessions:edit`: a reader without it is not offered a link that only refuses.
  const can = useCan()
  const canOpenPaneLog = can('sessions:edit')
  // The account page asks `accounts:view`: without it the name stays text, not a link that only fails.
  const canOpenAccount = can('accounts:view')
  useNow(RUNNING_DURATION_TICK_MS)
  const now = Date.now()
  /* AN ASKING RUN'S CLOCK STOPS AT THE PARK: `awaiting_response` closes no phase (the runner waits alive in
     its question call), so the server keeps the phase open. Its first park after the open start is where the run stopped
     working; the start row then reads that span, marked paused, instead of growing while a person has the question. */
  const parkedAt = new Map<ActivityLogEntry, string>()
  const openStarts = new Map<string, ActivityLogEntry>()
  for (const e of entries) {
    if (e.category !== 'transition') continue
    const owner = String(e.data.id ?? '')
    const start = openStarts.get(owner)
    if (isOpenRunnerPhase(e)) openStarts.set(owner, e)
    else if (e.state === 'awaiting_response' && start && !parkedAt.has(start)) parkedAt.set(start, e.created_at)
  }
  const liveDuration = (e: ActivityLogEntry, seconds: number): number => {
    const parked = parkedAt.get(e)
    if (parked !== undefined) return Math.max(0, Date.parse(parked) - Date.parse(e.created_at)) / 1000
    return isOpenRunnerPhase(e) ? seconds + Math.max(0, now - receivedAt) / 1000 : seconds
  }
  if (entries.length === 0) {
    // Claims only what an empty feed proves: a Reset soft-deletes the abandoned attempt's rows
    // (`app/task_artifacts.py::activity_row_is_visible`), indistinguishable from none.
    return <EmptyState title="No activity recorded yet." />
  }
  const earlierCount = folded.filter(Boolean).length
  /* ONE NEXT STEP PER TASK: every termination row carries the same advice ("Retry, or move the task to
     another node."), and repeated down the feed it read as many open problems. Only each task's NEWEST termination
     keeps it — per owner, so on an epic each child's latest death still says what to do. The feed is chronological,
     so the last index per owner wins. */
  const newestTermination = new Map<string, number>()
  entries.forEach((e, i) => {
    if (e.category === 'note' && e.action === 'runner_terminated') newestTermination.set(String(e.data.id ?? ''), i)
  })
  const firstEarlier = folded.indexOf(true)
  const renderRow = (e: ActivityLogEntry, i: number) => {
    // One session id per attempt: the start row shows it (and a relaunch's "Runner session attached" row its
    // own); the end row's copy of the same id (`routes/tasks/activity.py::get_task_activity`) is not shown again.
    // A termination note carries its run's session too (`_close_phase`), already shown on the start row.
    const showsSession = !script && ((e.category === 'note' && e.action !== 'runner_terminated') || e.action === 'active')
    const sessionId = showsSession && typeof e.data.session_id === 'string' ? e.data.session_id : null
    // A Claude Code run opens on claude.ai; any other engine opens its Audit › Sessions row.
    const sessionLink = sessionId
      ? sessionHref(sessionId, typeof e.data.session_engine === 'string' ? e.data.session_engine : null)
      : null
    const terminated = e.category === 'note' && e.action === 'runner_terminated'
    // The runner's own reason for a termination (`runner_terminations.DETAIL_KEY`): each attempt keeps its own.
    const detail = terminated && typeof e.data.detail === 'string' && e.data.detail.trim() !== '' ? e.data.detail : null
    // Where the run died and what to do next (`routes/tasks/activity.py`): node, MicroVM, next step.
    const nodeName = terminated && typeof e.data.node_name === 'string' && e.data.node_name ? e.data.node_name : null
    // "Started fresh elsewhere" names the node the task LEFT (`data.node_name`): the row says where it
    // is no longer, beside the start row that says where it went.
    const leftNode = e.category === 'transition' && e.action === 'start_elsewhere'
      && typeof e.data.node_name === 'string' && e.data.node_name ? e.data.node_name : null
    const vmId = terminated && typeof e.data.vm_id === 'string' && e.data.vm_id ? e.data.vm_id : null
    const nextStep = terminated && newestTermination.get(String(e.data.id ?? '')) === i
      && typeof e.data.next_step === 'string' && e.data.next_step.trim() !== ''
      ? e.data.next_step : null
    // What the node logged as an unexplained end neared (`routes/tasks/activity.py::_add_node_errors`), redacted server-side.
    const nodeErrors = terminated && Array.isArray(e.data.node_errors)
      ? (e.data.node_errors as unknown[]).filter((l): l is string => typeof l === 'string' && l !== '')
      : []
    // The dead run's pane tail, captured by its node: the viewer at `/sessions/<id>/pane-log`.
    const paneLogSession = terminated
      && typeof e.data.pane_log_session === 'string' && e.data.pane_log_session !== '' ? e.data.pane_log_session : null
    const accountSlug = typeof e.data.account_slug === 'string' ? e.data.account_slug : null
    const accountEmail = typeof e.data.account_email === 'string' ? e.data.account_email : null
    const accountName = typeof e.data.account_name === 'string' && e.data.account_name ? e.data.account_name : null
    const accountId = typeof e.data.account_id === 'string' && e.data.account_id ? e.data.account_id : null
    // The run's last pane lines (`routes/tasks/activity.py::_add_node_errors`): why a launch died before its first event.
    const paneLines = terminated && Array.isArray(e.data.pane_log_lines)
      ? (e.data.pane_log_lines as unknown[]).filter((l): l is string => typeof l === 'string')
      : []
    const changedFields = Array.isArray(e.data.changed_fields) ? e.data.changed_fields as string[] : null
    const changes = e.data.changes && typeof e.data.changes === 'object'
      ? e.data.changes as Record<string, ActivityChange>
      : null
    const fixesBranch = e.action === 'dispatch_refused' && e.data.fix === DISPATCH_FIX_INTEGRATION_BRANCH
    const ownerId = typeof e.data.id === 'number' ? e.data.id : Number(e.data.id)
    const isChildRow = Number.isFinite(ownerId) && ownerId !== epicId
    const childTask = isChildRow ? childTasks.find(c => c.id === ownerId) : undefined
    /* SHORT CHILD NAME: the child's id and its role ("#2 Analyze" — its playbook stage, else its persona),
       not its whole title, which on an epic's feed would crowd every child row's line. The full title rides the
       hover. */
    const childLink = isChildRow && (
      <Tip title={childTask ? displayTitle(childTask.title, { showPersona: true }) : undefined}><MuiLink
        component={Link}
        to={`/tasks/${ownerId}`}
        sx={{ flex: '0 0 auto' }}
      >
        #{ownerId}{childTask ? ` ${taskRoleLabel(childTask)}` : ''}
      </MuiLink></Tip>
    )
    // A child's termination reads "#2 Analyze · Runner terminated: session vanished".
    const childEnd = terminated && isChildRow
    // A settle's summary is Details' Outcome: its line cuts it, and the expansion points to Details.
    const isOutcome = e.category === 'note' && e.action === 'outcome'
    /* Each fact once: a transition's summary that only restates the label ("Edit" over "edit") is
       dropped. A note's text is what someone wrote, so it always stays. */
    const summary = e.category === 'transition' && restatesLabel(e.summary, e.label) ? '' : e.summary
    const hasSummary = summary.trim() !== ''
    const longSummary = hasSummary && (summary.includes('\n') || summary.length > LONG_SUMMARY_CHARS)
    // A control-plane notice's sender and how its delivery to the runner went.
    const control = e.category === 'note' && e.action === 'control_message'
    const controlSender = control && typeof e.data.sender === 'string' ? e.data.sender : null
    const controlDelivery = control && typeof e.data.delivery === 'string' ? e.data.delivery : null
    const outcomeDetails = isOutcome && hasSummary && !isChildRow
    const changeList = changes ? Object.entries(changes) : []
    const showsPaneLog = paneLogSession !== null && canOpenPaneLog
    const showsGateLog = gateLogHref !== null && !isChildRow && isRunnerPhaseRow(e)
    const showsRun = e.duration_seconds !== null || sessionId !== null || accountSlug !== null
    const filedBy = typeof e.data.filed_by_task_id === 'number' ? e.data.filed_by_task_id : null
    // A row opens only when its line folds something away; a row with nothing more is not clickable.
    const expandable = longSummary || controlSender !== null || controlDelivery !== null || outcomeDetails
      || fixesBranch || nodeName !== null || vmId !== null || leftNode !== null || detail !== null || nextStep !== null
      || nodeErrors.length > 0 || paneLines.length > 0 || showsPaneLog || showsGateLog || changeList.length > 0
      || (changedFields !== null && changedFields.length > 0) || showsRun || filedBy !== null
    const key = keys[i]
    const open = expandable && opened.has(key)
    // A notice's text is the whole point of opening it, however short; an outcome's is Details' alone.
    const fullSummary = (longSummary && !outcomeDetails) || (control && hasSummary)
    // Opened, the whole text stands under the line, so the line drops its cut copy: the text shows once.
    const lineSummary = hasSummary && !(open && fullSummary)
    const labelText = (
      <Typography
        variant="body2"
        component="span"
        data-testid={childEnd ? 'task-timeline-child-end' : undefined}
        sx={{ ...ELLIPSIS_SX, display: 'block', fontWeight: FONT_WEIGHT.semibold, color: 'text.primary' }}
      >
        {childEnd ? e.label.replace(/^(Runner terminated) — /, '$1: ') : e.label}
      </Typography>
    )
    return (
      <Box component="li" key={i} data-testid="task-timeline-item" sx={TIMELINE_ITEM_SX}>
        <TimelineDot note={e.category === 'note'} />
        <Box sx={TIMELINE_BODY_SX}>
          {/* The button is the row's accessible toggle; a click anywhere else on the line (not on a link) opens it too. */}
          <Box
            sx={{ ...TIMELINE_LINE_SX, cursor: expandable ? 'pointer' : undefined }}
            onClick={expandable
              ? (ev: ReactMouseEvent) => { if (!(ev.target as Element).closest('a, button')) toggleRow(key) }
              : undefined}
          >
            {childLink && <>{childLink}<Typography variant="body2" component="span" aria-hidden="true">·</Typography></>}
            {expandable ? (
              <Box
                component="button"
                type="button"
                aria-expanded={open}
                data-testid="task-timeline-toggle"
                onClick={() => toggleRow(key)}
                sx={{ ...UNSTYLED_BUTTON_SX, display: 'flex', minWidth: 0, flex: '0 1 auto', textAlign: 'left' }}
              >
                {labelText}
              </Box>
            ) : <Box sx={{ display: 'flex', minWidth: 0, flex: '0 1 auto' }}>{labelText}</Box>}
            {(e.repeat_count ?? 1) > 1 && (
              <Tip title={typeof e.data.last_at === 'string' ? `Last ${absoluteTime(e.data.last_at)}` : undefined}><Typography
                variant="caption"
                data-testid="task-timeline-repeat"
                sx={{ color: 'text.secondary', fontWeight: FONT_WEIGHT.semibold, flex: '0 0 auto' }}
              >
                ×{e.repeat_count}
              </Typography></Tip>
            )}
            {/* A start row's "Running" is true only of the phase still open; a closed one says it in its label. */}
            {e.state !== null && movesState[i] && (chipOnlyAt === null || i === chipOnlyAt)
              && (e.action !== 'active' || e.category !== 'transition' || isOpenRunnerPhase(e)) && (
              <Box sx={{ flex: '0 0 auto', display: 'flex' }}><TaskStatusBadge status={statusForState(e.state)} /></Box>
            )}
            {/* A transition row arrives with an empty summary (the id/verb duplicated the label);
                prose rows and historical transitions still render theirs, cut to the line. */}
            {lineSummary ? (
              <Tip title={summary}><Typography
                variant="body2"
                component="span"
                data-testid={isOutcome ? 'task-timeline-outcome' : undefined}
                sx={{ ...ELLIPSIS_SX, flex: '1 1 0', color: 'text.secondary' }}
              >
                {summary}
              </Typography></Tip>
            ) : <Box sx={{ flex: '1 1 0' }} />}
            {/* Who caused the row rides the timestamp; a machine row names no one. */}
            <Typography variant="caption" sx={{ color: 'text.secondary', flex: '0 0 auto' }}>
              <Tip title={fmtTimestamp(e.created_at)}><time dateTime={e.created_at}>{briefTimestamp(e.created_at)}</time></Tip>
            </Typography>
            {e.actor && (
              <Tip title={e.actor}><Typography
                variant="caption"
                data-testid="task-timeline-actor"
                sx={{ ...ELLIPSIS_SX, color: 'text.secondary', flex: '0 1 auto', maxWidth: grid(20) }}
              >
                {e.actor}
              </Typography></Tip>
            )}
            {expandable && (
              <Box component="span" aria-hidden="true" sx={{ color: 'text.secondary', flex: '0 0 auto', width: grid(1.5) }}>
                {open ? '▾' : '▸'}
              </Box>
            )}
          </Box>
          {open && (
            <Box data-testid="task-timeline-expansion" sx={TIMELINE_EXPANSION_SX}>
              {fullSummary && (
                <Typography
                  variant="body2"
                  data-testid="task-timeline-full-summary"
                  sx={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}
                >
                  {summary}
                </Typography>
              )}
              {(controlSender || controlDelivery) && (
                <Typography variant="caption" data-testid="task-timeline-control-from" sx={CAPTION_SX}>
                  {controlSender && <>from {controlSender}</>}
                  {controlSender && controlDelivery && ' · '}
                  {controlDelivery}
                </Typography>
              )}
              {/* A child's outcome lives on the child's own page, which its link on the line opens. */}
              {outcomeDetails && (
                <Typography variant="body2">
                  <LinkButton
                    variant="body2"
                    onClick={onOpenDetails}
                    aria-label="Outcome in Details"
                    data-testid="task-timeline-outcome-details"
                  >
                    Details
                  </LinkButton>
                </Typography>
              )}
              {fixesBranch && (
                <Typography variant="body2">
                  <MuiLink component={Link} to={`/projects/${projectId}?tab=customize&section=branches`} data-testid="task-timeline-fix">
                    Project settings
                  </MuiLink>
                </Typography>
              )}
              {(nodeName || vmId) && (
                <Typography variant="caption" data-testid="task-timeline-where" sx={CAPTION_SX}>
                  {nodeName && <>on <Typography variant="mono">{nodeName}</Typography></>}
                  {nodeName && vmId && ' · '}
                  {vmId && <>VM <Tip title={vmId}><Typography variant="mono">{vmId.slice(0, 8)}</Typography></Tip></>}
                </Typography>
              )}
              {leftNode && (
                <Typography variant="caption" data-testid="task-timeline-left" sx={CAPTION_SX}>
                  left <Typography variant="mono">{leftNode}</Typography>
                </Typography>
              )}
              {detail && (
                <Typography variant="body2" data-testid="task-timeline-detail" sx={{ overflowWrap: 'anywhere' }}>
                  {detail}
                </Typography>
              )}
              {nextStep && (
                <Typography variant="caption" data-testid="task-timeline-next-step" sx={CAPTION_SX}>
                  {nextStep}
                </Typography>
              )}
              {nodeErrors.length > 0 && (
                <Box data-testid="task-timeline-node-errors" sx={{ minWidth: 0 }}>
                  <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                    Node log before it ended:
                  </Typography>
                  <CodeBlock tone="plain" sx={{ color: 'text.secondary' }}>{nodeErrors.join('\n')}</CodeBlock>
                </Box>
              )}
              {paneLines.length > 0 && (
                <Box data-testid="task-timeline-pane-lines" sx={{ minWidth: 0 }}>
                  <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                    Last pane output:
                  </Typography>
                  <CodeBlock tone="plain" sx={{ color: 'text.secondary' }}>{paneLines.join('\n')}</CodeBlock>
                </Box>
              )}
              {paneLogSession && canOpenPaneLog && (
                <Typography variant="body2">
                  <MuiLink component={Link} to={paneLogPath(paneLogSession)} data-testid="task-timeline-pane-log">
                    Pane log
                  </MuiLink>
                </Typography>
              )}
              {gateLogHref && showsGateLog && (
                <Typography variant="body2">
                  <MuiLink component={Link} to={gateLogHref} data-testid="task-timeline-gate-log">Gate log</MuiLink>
                </Typography>
              )}
              {changeList.length > 0 ? (
                <Box data-testid="task-timeline-changes">
                  {changeList.map(([field, change]) => field === 'body' ? (
                    // The description's versions are compared in the owner's Versions view, not here: a child's on its
                    // own page, this task's on its subtab (offered once a snapshot exists).
                    <Typography key={field} variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                      Description changed
                      {isChildRow ? (
                        <>{' '}<MuiLink
                          component={Link}
                          to={`/tasks/${ownerId}?tab=details&details=versions`}
                          data-testid="task-timeline-versions"
                          sx={{ fontSize: 'inherit' }}
                        >
                          Versions
                        </MuiLink></>
                      ) : hasVersions && (
                        <>{' '}<LinkButton
                          variant="caption"
                          onClick={onOpenVersions}
                          data-testid="task-timeline-versions"
                          sx={{ verticalAlign: 'baseline' }}
                        >
                          Versions
                        </LinkButton></>
                      )}
                    </Typography>
                  ) : (
                    // Opened, the row has the room: the whole before and after, wrapped (a phone has no hover).
                    <Typography key={field} variant="caption" sx={{ ...CAPTION_SX, display: 'block', overflowWrap: 'anywhere' }}>
                      {activityFieldLabel(field)}: {changeValue(field, change.before)} → {changeValue(field, change.after)}
                    </Typography>
                  ))}
                </Box>
              ) : changedFields && changedFields.length > 0 && (
                <Typography variant="caption" sx={CAPTION_SX}>
                  changed: {changedFields.map(activityFieldLabel).join(', ')}
                </Typography>
              )}
              {showsRun && (
                <Typography variant="caption" sx={CAPTION_SX}>
                  {e.duration_seconds !== null && <>duration: {formatSpan(liveDuration(e, e.duration_seconds) * 1000)}</>}
                  {parkedAt.has(e) && <span data-testid="task-timeline-paused"> (paused, asking)</span>}
                  {e.duration_seconds !== null && sessionId && ' · '}
                  {sessionId && sessionLink && (
                    <>
                      session:{' '}
                      {/* A Claude Code run opens on claude.ai (viewable only by the account that ran it, hence the
                          account beside it); any other run opens its Audit › Sessions row. `variant="mono"` is a
                          span, so it nests inside the anchor. */}
                      {sessionLink.external ? (
                        <ExternalLink
                          href={sessionLink.href}
                          aria-label={`Open session ${sessionId} on claude.ai`}
                          sx={{ fontSize: 'inherit' }}
                        >
                          <Typography variant="mono">{sessionId}</Typography> ↗
                        </ExternalLink>
                      ) : (
                        <MuiLink
                          component={Link}
                          to={sessionLink.href}
                          aria-label={`Open session ${sessionId} in Sessions`}
                          sx={{ fontSize: 'inherit' }}
                        >
                          <Typography variant="mono">{sessionId}</Typography>
                        </MuiLink>
                      )}
                    </>
                  )}
                  {(e.duration_seconds !== null || sessionId) && accountSlug && ' · '}
                  {/* Which credential spent this run, by its name (the slug when the read joined none). The email
                      renders only when the read-time fold knows one. */}
                  {accountSlug && (
                    <>
                      account:{' '}
                      {accountId && canOpenAccount ? (
                        <MuiLink component={Link} to={`/accounts/${encodeURIComponent(accountId)}`} sx={{ fontSize: 'inherit' }}>
                          {accountName ?? <Typography variant="mono">{accountSlug}</Typography>}
                        </MuiLink>
                      ) : accountName ?? <Typography variant="mono">{accountSlug}</Typography>}
                      {accountEmail && <> ({accountEmail})</>}
                    </>
                  )}
                </Typography>
              )}
              {/* A runner's filing names the task that filed it (`_name_machine_creators`). */}
              {filedBy !== null && (
                <Typography variant="caption" sx={CAPTION_SX}>
                  {'filed from '}
                  <MuiLink component={Link} to={`/tasks/${filedBy}`} data-testid="task-timeline-filed-by" sx={{ fontSize: 'inherit' }}>
                    #{filedBy}
                  </MuiLink>
                </Typography>
              )}
            </Box>
          )}
        </Box>
      </Box>
    )
  }
  return (
    <Box component="ol" data-testid="task-timeline" sx={TIMELINE_SX}>
      {entries.map((e, i) => {
        if (!folded[i]) return renderRow(e, i)
        // The fold sits where the first earlier row would: those rows predate the Reset, so they stay in order.
        return (
          <Fragment key={i}>
            {i === firstEarlier && (
              <Box component="li" data-testid="task-timeline-earlier" sx={TIMELINE_ITEM_SX}>
                <TimelineDot />
                <Box sx={TIMELINE_BODY_SX}>
                  <Box
                    component="button"
                    type="button"
                    aria-expanded={earlierOpen}
                    onClick={() => setEarlierOpen(open => !open)}
                    sx={EARLIER_TOGGLE_SX}
                  >
                    <Box component="span" aria-hidden="true" sx={{ display: 'inline-block', width: grid(2.5) }}>
                      {earlierOpen ? '▾' : '▸'}
                    </Box>
                    Earlier attempts ({earlierCount})
                  </Box>
                </Box>
              </Box>
            )}
            {earlierOpen && renderRow(e, i)}
          </Fragment>
        )
      })}
    </Box>
  )
}
