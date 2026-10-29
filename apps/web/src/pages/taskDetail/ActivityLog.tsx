// The Activity tab's Timeline feed: one renderer over the task's unified `/activity` rows.
import { Fragment, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { taskRoleLabel } from '../../components/tasks/personaRegistry'
import { deescapeMarkdownText, displayTitle, statusForState, TaskStatusBadge } from '../../components/tasks/shared'
import { useMinuteTick } from '../../hooks/useMinuteTick'
import { absoluteTime } from '../../lib/relativeTime'
import { sessionHref } from '../../lib/sessionLink'
import { formatSpan } from '../../lib/spanTime'
import type { PipelineTask } from '../../types'
import EmptyState from '../../ui/EmptyState'
import StatusDot from '../../ui/StatusDot'
import TextDiffDialog from '../../ui/TextDiffDialog'
import { useCan } from '../../auth'
import { paneLogPath } from '../audit/PaneLogPage'

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
  return ACTIVITY_FIELD_LABELS[field] ?? (words.charAt(0).toUpperCase() + words.slice(1))
}

// The states a run has finished in: no runner will move it again unless someone starts it.
const FINISHED_STATES: ReadonlySet<string> = new Set(['done', 'system_failed'])

// A flag reads in the editor's words: Involvement's option label, any other switch On/Off.
const BOOLEAN_CHANGE_WORDS: Readonly<Record<string, readonly [string, string]>> = {
  autonomous: ['Unattended', 'Guided'],
}

// A before/after value as a single readable line — never dumps a full multi-KB body edit verbatim.
function fmtChangeValue(value: unknown, field = ''): string {
  if (value === null || value === undefined || value === '') return '(empty)'
  if (typeof value === 'boolean') {
    const [on, off] = BOOLEAN_CHANGE_WORDS[field] ?? ['On', 'Off']
    return value ? on : off
  }
  const oneLine = (typeof value === 'string' ? value : JSON.stringify(value)).replace(/\s+/g, ' ').trim()
  return oneLine.length > 80 ? `${oneLine.slice(0, 80)}…` : oneLine
}

// A text change the one-line form above cannot show whole (cut, or whitespace/lines collapsed) — the rows
// that get a Compare. Non-text values (ids, flags) always fit.
function changeNeedsDiff(change: ActivityChange): boolean {
  const lossy = (v: unknown) => typeof v === 'string' && v !== '' && fmtChangeValue(v) !== v
  const text = (v: unknown) => v === null || v === undefined || typeof v === 'string'
  return text(change.before) && text(change.after) && (lossy(change.before) || lossy(change.after))
}

const asText = (v: unknown): string => (typeof v === 'string' ? v : '')

// A changed `title` reads as the trail shows it: stored titles may carry CommonMark escapes (`\[ops\]`).
const shownChangeValue = (field: string, v: unknown): string =>
  fmtChangeValue(field === 'title' && typeof v === 'string' ? deescapeMarkdownText(v) : v, field)

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
  gap: 1.75,
  padding: '0 0 0.75rem 0.25rem',
  borderLeft: 2,
  borderColor: 'divider',
  marginLeft: '0.25rem',
  '&:last-of-type': { paddingBottom: 0 },
} as const

const TIMELINE_BODY_SX = { display: 'flex', flexDirection: 'column', gap: 0.35, pl: 1.35, minWidth: 0 }

// The "Earlier attempts" fold's toggle: a text button in the caption tone.
const EARLIER_TOGGLE_SX = {
  border: 0, p: 0, background: 'none', color: 'text.secondary', font: 'inherit', fontSize: '0.875rem',
  textAlign: 'left', cursor: 'pointer', '&:hover': { color: 'text.primary', textDecoration: 'underline' },
} as const

const TIMELINE_HEAD_SX = { display: 'flex', alignItems: 'center', gap: 1.35, flexWrap: 'wrap' }

function TimelineDot({ note }: { note?: boolean }) {
  return (
    <StatusDot
      color={note ? 'primary.main' : 'secondary.main'}
      sx={{ position: 'absolute', left: '-5px', top: '0.2rem' }}
    />
  )
}

// A refused dispatch's `data.fix` value for a missing integration branch (`routes/agent/slot_intents.py::
// DISPATCH_FIX_INTEGRATION_BRANCH`): the row links to the project's Governance tab, where that field is edited.
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

// The Activity tab's Timeline feed — the page's ONE event renderer over the unified `/activity` feed.
// `epicId` + `childTasks` label rolled-up per-child rows (`data.id !== epicId`) with a deep link. `projectId`
// is the task's own: a project move carries its subtree, so a child row shares it. `onOpenDetails` is where an
// "Outcome reported" row points: Details already shows that summary in full. A `script` run (the merge lane) is
// no Claude session, so its rows link no session. `gateLogHref` is a merge-lane run's latest gate log
// (`components/tasks/laneRun.ts::latestGateLog`), linked from its runner rows. `currentState` is the task's
// own: a FINISHED script run's many passes through the same states would chip every row, so only the row that
// put it in its current state keeps its chip there.
export default function ActivityLog(
  { entries, epicId, childTasks, projectId, onOpenDetails, script, gateLogHref = null, currentState = null }:
  {
    entries: ActivityLogEntry[]; epicId: number; childTasks: PipelineTask[]; projectId: number
    onOpenDetails: () => void; script: boolean; gateLogHref?: string | null; currentState?: string | null
  },
) {
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
  // The open Compare dialog: which field of which row.
  const [diff, setDiff] = useState<{ field: string; change: ActivityChange } | null>(null)
  // Rows from before the last Reset fold under one disclosure, closed by default.
  const [earlierOpen, setEarlierOpen] = useState(false)
  // A runner phase still open at read time is the `active` transition the server gave a duration
  // (`routes/tasks/`, AC6.5). Its duration keeps growing on the wall clock: tick it every 30s from
  // the moment this feed arrived — elapsed on the client's own clock, so client/server skew never shows.
  const receivedAt = useMemo(() => Date.now(), [entries])
  // The pane-log viewer asks `sessions:edit`: a reader without it is not offered a link that only refuses.
  const can = useCan()
  const canOpenPaneLog = can('sessions:edit')
  // The account page asks `accounts:view`: without it the name stays text, not a link that only fails.
  const canOpenAccount = can('accounts:view')
  useMinuteTick(RUNNING_DURATION_TICK_MS)
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
    // A child's termination reads as ONE line: which child, what ended it, where
    // ("#2 Analyze · Runner terminated: session vanished · dev-node").
    const childEnd = terminated && isChildRow
    /* SHORT CHILD NAME: the child's id and its role ("#2 Analyze" — its playbook stage, else its persona),
       not its whole title, which on an epic's feed wrapped every child row over several lines. The full title
       rides the hover. */
    const childLink = isChildRow && (
      <MuiLink
        component={Link}
        to={`/tasks/${ownerId}`}
        title={childTask ? displayTitle(childTask.title, { showPersona: true }) : undefined}
      >
        #{ownerId}{childTask ? ` ${taskRoleLabel(childTask)}` : ''}
      </MuiLink>
    )
    // A settle's summary is Details' Outcome: here it collapses to one line (whole on hover).
    const isOutcome = e.category === 'note' && e.action === 'outcome'
    /* Each fact once: a transition's summary that only restates the label ("Edit" over "edit") is
       dropped. A note's text is what someone wrote, so it always stays. */
    const summary = e.category === 'transition' && restatesLabel(e.summary, e.label) ? '' : e.summary
    return (
      <Box component="li" key={i} data-testid="task-timeline-item" sx={TIMELINE_ITEM_SX}>
        <TimelineDot note={e.category === 'note'} />
        <Box sx={TIMELINE_BODY_SX}>
          <Box sx={TIMELINE_HEAD_SX}>
            {childEnd ? (
              <Typography
                variant="body2"
                data-testid="task-timeline-child-end"
                sx={{ fontWeight: 600, color: 'text.primary', minWidth: 0, overflowWrap: 'anywhere' }}
              >
                {childLink} · {e.label.replace(/^(Runner terminated) — /, '$1: ')}
                {nodeName && <> · <Typography variant="mono" sx={{ fontWeight: 400 }}>{nodeName}</Typography></>}
                {vmId && <> · VM <Typography variant="mono" title={vmId} sx={{ fontWeight: 400 }}>{vmId.slice(0, 8)}</Typography></>}
              </Typography>
            ) : (
              <Typography variant="body2" sx={{ fontWeight: 600, color: 'text.primary' }}>{e.label}</Typography>
            )}
            {(e.repeat_count ?? 1) > 1 && (
              <Typography
                variant="caption"
                data-testid="task-timeline-repeat"
                title={typeof e.data.last_at === 'string' ? `Last ${absoluteTime(e.data.last_at)}` : undefined}
                sx={{ color: 'text.secondary', fontWeight: 600 }}
              >
                ×{e.repeat_count}
              </Typography>
            )}
            {/* A start row's "Running" is true only of the phase still open; a closed one says it in its label. */}
            {e.state !== null && movesState[i] && (chipOnlyAt === null || i === chipOnlyAt)
              && (e.action !== 'active' || e.category !== 'transition' || isOpenRunnerPhase(e)) && (
              <TaskStatusBadge status={statusForState(e.state)} />
            )}
          </Box>
          {isChildRow && !childEnd && (
            // AD-12: the `#` is KEPT in prose, so the number reads as a task id.
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>on {childLink}</Typography>
          )}
          {/* A transition row arrives with an empty summary (the id/verb duplicated the label);
              prose rows and historical transitions still render theirs. */}
          {isOutcome && summary.trim() !== '' ? (
            <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, minWidth: 0 }}>
              <Typography
                variant="body2"
                title={summary}
                data-testid="task-timeline-outcome"
                sx={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              >
                {summary}
              </Typography>
              {/* A child's outcome lives on the child's own page, which the "on #id" link above opens. */}
              {!isChildRow && (
                <MuiLink
                  component="button"
                  type="button"
                  variant="body2"
                  onClick={onOpenDetails}
                  aria-label="Outcome in Details"
                  data-testid="task-timeline-outcome-details"
                  sx={{ flex: '0 0 auto' }}
                >
                  Details
                </MuiLink>
              )}
            </Box>
          ) : summary.trim() !== '' && (
            <Typography
              variant="body2"
              // An agent's message keeps its own line breaks.
              sx={{ overflowWrap: 'anywhere', whiteSpace: e.action === 'agent_message' ? 'pre-wrap' : undefined }}
            >
              {summary}
            </Typography>
          )}
          {fixesBranch && (
            <Typography variant="body2">
              <MuiLink component={Link} to={`/projects/${projectId}?tab=governance`} data-testid="task-timeline-fix">
                Project settings
              </MuiLink>
            </Typography>
          )}
          {!childEnd && (nodeName || vmId) && (
            <Typography variant="caption" data-testid="task-timeline-where" sx={{ color: 'text.secondary' }}>
              {nodeName && <>on <Typography variant="mono">{nodeName}</Typography></>}
              {nodeName && vmId && ' · '}
              {vmId && <>VM <Typography variant="mono" title={vmId}>{vmId.slice(0, 8)}</Typography></>}
            </Typography>
          )}
          {leftNode && (
            <Typography variant="caption" data-testid="task-timeline-left" sx={{ color: 'text.secondary' }}>
              left <Typography variant="mono">{leftNode}</Typography>
            </Typography>
          )}
          {detail && (
            <Typography variant="body2" data-testid="task-timeline-detail" sx={{ overflowWrap: 'anywhere' }}>
              {detail}
            </Typography>
          )}
          {nextStep && (
            <Typography
              variant="caption"
              data-testid="task-timeline-next-step"
              sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}
            >
              {nextStep}
            </Typography>
          )}
          {nodeErrors.length > 0 && (
            <Box data-testid="task-timeline-node-errors" sx={{ minWidth: 0 }}>
              <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                Node log before it ended:
              </Typography>
              {nodeErrors.map((line, j) => (
                <Typography
                  key={j}
                  variant="mono"
                  component="div"
                  sx={{ color: 'text.secondary', fontSize: '0.75rem', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}
                >
                  {line}
                </Typography>
              ))}
            </Box>
          )}
          {paneLines.length > 0 && (
            <Box data-testid="task-timeline-pane-lines" sx={{ minWidth: 0 }}>
              <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                Last pane output:
              </Typography>
              <Typography
                variant="mono"
                component="pre"
                sx={{ m: 0, color: 'text.secondary', fontSize: '0.75rem', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}
              >
                {paneLines.join('\n')}
              </Typography>
            </Box>
          )}
          {paneLogSession && canOpenPaneLog && (
            <Typography variant="body2">
              <MuiLink component={Link} to={paneLogPath(paneLogSession)} data-testid="task-timeline-pane-log">
                Pane log
              </MuiLink>
            </Typography>
          )}
          {gateLogHref && !isChildRow && isRunnerPhaseRow(e) && (
            <Typography variant="body2">
              <MuiLink component={Link} to={gateLogHref} data-testid="task-timeline-gate-log">Gate log</MuiLink>
            </Typography>
          )}
          {changes && Object.keys(changes).length > 0 ? (
            <Box data-testid="task-timeline-changes">
              {Object.entries(changes).map(([field, change]) => (
                <Typography key={field} variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                  {activityFieldLabel(field)}: {shownChangeValue(field, change.before)} → {shownChangeValue(field, change.after)}
                  {changeNeedsDiff(change) && (
                    <Button
                      size="small"
                      data-testid="task-timeline-compare"
                      aria-label={`Compare ${activityFieldLabel(field)} before and after`}
                      onClick={() => setDiff({ field, change })}
                      sx={{ ml: 0.5, py: 0, minWidth: 0, verticalAlign: 'baseline', fontSize: 'inherit' }}
                    >
                      Compare
                    </Button>
                  )}
                </Typography>
              ))}
            </Box>
          ) : changedFields && changedFields.length > 0 && (
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              changed: {changedFields.map(activityFieldLabel).join(', ')}
            </Typography>
          )}
          {(e.duration_seconds !== null || sessionId || accountSlug) && (
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
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
                    <MuiLink
                      href={sessionLink.href}
                      target="_blank"
                      rel="noreferrer"
                      aria-label={`Open session ${sessionId} on claude.ai`}
                      sx={{ fontSize: 'inherit' }}
                    >
                      <Typography variant="mono">{sessionId}</Typography> ↗
                    </MuiLink>
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
          {/* Who caused the row rides the timestamp line; a machine row names no one. */}
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            <time dateTime={e.created_at} title={e.created_at}>{absoluteTime(e.created_at)}</time>
            {e.actor && <span data-testid="task-timeline-actor"> · {e.actor}</span>}
            {/* A runner's filing names the task that filed it (`_name_machine_creators`). */}
            {typeof e.data.filed_by_task_id === 'number' && (
              <>
                {' · from '}
                <MuiLink
                  component={Link}
                  to={`/tasks/${e.data.filed_by_task_id}`}
                  data-testid="task-timeline-filed-by"
                  sx={{ fontSize: 'inherit' }}
                >
                  #{e.data.filed_by_task_id}
                </MuiLink>
              </>
            )}
          </Typography>
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
                    <Box component="span" aria-hidden="true" sx={{ display: 'inline-block', width: '1.1em' }}>
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
      {diff && (
        <TextDiffDialog
          title={`${activityFieldLabel(diff.field)} changes`}
          before={asText(diff.change.before)}
          after={asText(diff.change.after)}
          onClose={() => setDiff(null)}
        />
      )}
    </Box>
  )
}
