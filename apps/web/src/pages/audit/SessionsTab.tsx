import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useTheme } from '@mui/material/styles'
import { del, get, post } from '../../api'
import { useCan } from '../../auth'
import { FilterRow, FilterSelect } from '../../ui/FilterRow'
import { ACTION_ICON, elapsedLabel } from '../../components/tasks/shared'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { useEventStream } from '../../hooks/useEventStream'
import { useMinuteTick } from '../../hooks/useMinuteTick'
import { compactCount, countOf } from '../../lib/humanize'
import { absoluteTime, compactAge, humanDuration, NO_VALUE } from '../../lib/relativeTime'
import { formatZonedSpan, localInputToIso } from '../../lib/zonedTime'
import { DateTimeBoundFields } from '../../ui/DateTimeBoundFields'
import Banner from '../../ui/Banner'
import { useConfirm } from '../../components/ConfirmDialog'
import EmptyState from '../../ui/EmptyState'
import NoMatchesState from '../../ui/NoMatchesState'
import type { FeedColumn, FeedRowSpec, OverflowItem } from '../../ui/feed/types'
import FilterBar from '../../ui/FilterBar'
import type { Tone } from '../../ui/StatusChip'
import { toneFor } from '../../ui/statusTone'
import { FEED_NO_META, PHONE, SESSIONS_LIST_CONTAINER_SX, fieldTapTargetSx, monoSx, tapTargetSx } from '../../ui/listStyles'
import { contentSlotWidth, monoFamily } from '../../ui/feed/geometry'
import { PageNav } from '../../ui/Pager'
import { usePageClamp } from '../../hooks/usePageClamp'
import { usePagedLogFilters } from '../../hooks/usePagedLogFilters'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { useApiRead } from '../../hooks/useApiRead'
import { useRowActions } from '../../hooks/useBusyIds'
import SortMenu from '../../ui/SortMenu'
import SearchBox from '../../ui/SearchBox'
import ListStates from '../../ui/feed/ListStates'
import { paneLogPath } from './PaneLogPage'
import { agentVersionLabel, shortCommit } from '../agents/api'
import { formatCostUsd } from '../../lib/taskCost'

/* Audit > Sessions: every agentic session, and what the control plane can and cannot hand back
 * about each. A tab panel: `AuditPage` owns the tab strip; the sort control leads this panel
 * (see `SortMenu`). Still at `/sessions`, so task-page `?task=` links work.
 * Graded on one outcome: with nothing typed, answer "which sessions ran last night, which have no
 * transcript, and why". Constraints:
 *   1. The first request sends no `sort` and no `status`, buying the server's running-first order; `sort`
 *      stays `null` until the operator sorts, with a way back (`SORTABLE_COLUMNS`).
 *   2. Every non-`uploaded` transcript state renders its reason as visible text (`TRANSCRIPT_REASON`).
 *   3. A `pending`/`uploading` row past `overdue_after_seconds` (from the lease TTL) renders warning and
 *      "— overdue".
 *   4. A row nothing can ever finish (dispatched, never started) is offered for deletion: `stale_count`
 *      names the set (every session the principal can see, node-grant scoped), the Transcript filter's
 *      `stale` value shows it, and only then the button. The set is narrow on purpose (see the stale
 *      block below).
 * `name` is never truncated in JS; CSS clips, the full string is on the tooltip.
 * Permissions (reasoning single-homed in `components/Sidebar.tsx`'s Audit row): the list is
 * `sessions:view` (route guard); download, row delete and bulk delete are `sessions:edit`, asked at the
 * row or banner. The subject is `sessions`, not `system` (`apps/api/app/authz/`, "THE TIER KEY BECOMES
 * THE DOOR"); keys are read off `apps/api/app/routes/sessions.py`'s decorators. A view-only principal
 * sees no download or delete at all, but still sees the stale banner. The server's 403/409 is the guard.
 */

// ── The wire types, page-local ──
// One consumer, so they live beside it (as `pages/credentials/api.ts` keeps `Credential`).

export type Session = {
  id: string
  /** The READABLE identifier the server writes ("task 42 · 382a6756"). Never shortened in JS. */
  name: string
  agent_session_id: string
  agent_kind: string
  /** Which SDK agent ran an `sdk_agent` session; null on every built-in session. Optional for rolling deploys. */
  agent_ref?: AgentRef | null
  /** The built-in engine's label ("Claude Code"); null on `sdk_agent` rows and an unknown kind. Optional for rolling deploys. */
  engine?: string | null
  /** The runtime the dispatch chose (`acp` = the ACP host), even for a run that died before its first event.
   *  Optional for rolling deploys. */
  runtime?: 'acp' | 'legacy'
  /** The runtime the dispatch chose, display only (`models.AgentSession.dispatched_runtime`); null when not known. */
  dispatched_runtime?: 'acp' | 'legacy' | null
  /** Why an unexplained end happened, as the node's own journal says it: up to five redacted warning-or-worse
   *  lines from just before the end (`node_log_history.end_lines`), newest first. Optional for rolling deploys. */
  node_errors?: string[]
  /** The dead run's pane tail, as its node sent it: `captured` opens in `PaneLogPage`, `missing` = the
   *  node had none, null = not asked. Optional for rolling deploys. */
  pane_log_state?: 'captured' | 'missing' | null
  /** The recorded cause of the end, one sentence. Optional for rolling deploys. */
  end_detail?: string | null
  /** The last event sequence received; null when none. Optional for rolling deploys. */
  events_last_seq?: number | null
  /** An ended ACP session that received no events. Optional for rolling deploys. */
  no_events?: boolean
  /** The node's sentence for this run's MicroVM when it failed; null otherwise. Optional for rolling deploys. */
  vm_failure?: string | null
  /** Why a `running` row's task attempt is over though no end was reported; null otherwise. Optional for rolling
   *  deploys. */
  attempt_over?: string | null
  /** The node's presence (`online`/`offline`/`unknown`) on a row still owing a transcript; null otherwise. */
  node_status?: string | null
  /** `end_reason` in words ("Session vanished"). Optional for rolling deploys. */
  end_reason_label?: string | null
  /** The last event batch the control plane refused with a 4xx. Optional for rolling deploys. */
  events_refused?: EventsRefused | null
  /** The node's display name; `node_id` its uuid, what `?node=` filters on. */
  node: string | null
  node_id: string | null
  /** The credential account it ran on, by name; null when none was recorded or it is not readable to the caller. */
  account?: string | null
  task_id: number | null
  task_title: string | null
  worktree_ref: string | null
  branch: string | null
  holder: string | null
  status: string
  end_reason: string | null
  started_at: string
  ended_at: string | null
  model: string | null
  tokens_in: number | null
  tokens_out: number | null
  /* Dollars as a decimal string (Efficiency's per-session rule); null when unpriced or unknown. Optional: an older
  server omits it. */
  cost_usd?: string | null
  subagent_count: number | null
  transcript_status: string
  transcript_error: string | null
  transcript_bytes: number | null
  transcript_attempts: number
  transcript_leased_at: string | null
  /* Whether the control plane still holds this session live (a task's runner session, or a non-`free`
  slot naming it). The server's verdict (`app/sessions_cleanup.py`'s liveness conjunct); `task_id` and
  `worktree_ref` are stamped at dispatch and prove nothing. Optional for rolling deploys; absent reads as
  "not held", which is what that older api did. */
  held_live?: boolean
  created_by: string | null
  updated_by: string | null
}

export type EventsRefused = { status: number; detail: string; at: string; first_seq: number | null }

/** An attempt that ended before a session row was written. */
export type UnsessionedAttempt = {
  task_id: number
  task_title: string | null
  at: string
  cause: string | null
  label: string
  detail: string | null
  node: string | null
}

/** `routes/sessions.py::AgentRefOut`: shown as name, version and short commit, never a raw id or slug (AC-4). */
export type AgentRef = { agent_id?: string | null; display_name: string; version: string; commit?: string | null }

/** "Report bot · v0.2.0 · 3f2a9c1" — the agent a session ran, as people name it. */
export function agentRefLabel(ref: AgentRef): string {
  const commit = shortCommit(ref.commit)
  return [agentVersionLabel(ref.display_name || 'Unnamed agent', ref.version), commit].filter(Boolean).join(' · ')
}

export type SessionList = {
  items: Session[]
  /** What the filters select; the pager's total. */
  total: number
  page: number
  per: number
  /** The whole table, unfiltered — separates the two empty states. */
  total_unfiltered: number
  window: string
  storage_available: boolean
  storage_reason: string | null
  redaction_confidence: string
  /** Age at which a `pending`/`uploading` row reads as late, from the lease TTL server-side; see
   *  `DEFAULT_OVERDUE_MS`. */
  overdue_after_seconds: number
  /* The staleness threshold, the server's (`app/sessions_cleanup.py`, an operator knob in
  Settings). Read it; never re-decide it (`DEFAULT_STALE_AFTER_SECONDS` is a pre-payload placeholder). */
  stale_after_seconds: number
  /** Stale rows across every session the principal can see — not this page, filters or window, because
   *  that is what the bulk button deletes. Node-grant scoped only (`routes/sessions.py`), hence "every
   *  session you can see". */
  stale_count: number
  /* Rows one delete pass takes (`app/sessions_cleanup.py::MAX_DELETE_PER_CALL`), served because the bulk
  dialog renders it before any delete response. Same name as `StaleDeleteResult.max_per_call`. Optional
  for rolling deploys; degrades to `STALE_DELETE_CEILING`, never `NaN`. */
  max_per_call?: number
  orphans: { node: string | null; count: number }[]
  /** Attempts that ended with no session row. Optional for rolling deploys. */
  unsessioned_attempts?: UnsessionedAttempt[]
  /** The End reason filter's vocabulary (`routes/sessions.py::_end_reason_options`). Optional for rolling deploys. */
  end_reasons?: EndReasonOption[]
  /** The Node filter's choices: every node holding a session under the other filters (`routes/sessions.py::_node_options`).
   *  Optional for rolling deploys: without it the rows on screen supply them. */
  node_options?: { value: string; label: string; count?: number }[]
  /** Each wider window's count under these filters, served only when this window is tiny
   *  (`routes/sessions.py::_WIDER_HINT_AT_MOST`). Optional for rolling deploys. */
  wider_windows?: { window: string; total: number }[]
}

/** A shown `end_reason` and its words; `terminal` is a break cause no relaunch clears; `count` is the sessions showing
 *  it under every other filter (optional for rolling deploys). */
export type EndReasonOption = { value: string; label: string; terminal: boolean; count?: number }

/** How many groups of attempts with no session show before "Show all (N)": the newest few answer "did a launch die". */
const UNSESSIONED_SHOWN = 5

/** A task's attempts first, so a task that ended twice for different reasons is ONE row naming each cause; then the
 *  tasks whose causes and nodes match, so seven tasks crashing on one node read "7 × process crashed" once.
 *  `attempts` is newest first (the first is the one shown); `causes` counts each label in the
 *  order it first appears, `nodes` the distinct nodes likewise. */
export type AttemptGroup = {
  key: string
  attempts: UnsessionedAttempt[]
  causes: { label: string; count: number }[]
  nodes: string[]
}

const distinct = (values: string[]): string[] => [...new Set(values)]

/** Each distinct runner reason of these attempts, newest first. */
const reasonsOf = (attempts: UnsessionedAttempt[]): string[] => distinct(attempts.flatMap(a => (a.detail ? [a.detail] : [])))

/** "×N" after a task that ended more than once in its group; nothing for a task that ended once. */
function EndedTimes({ attempts, taskId }: { attempts: UnsessionedAttempt[]; taskId: number }): JSX.Element | null {
  const times = attempts.filter(a => a.task_id === taskId).length
  if (times < 2) return null
  return (
    <Box component="span" data-testid="unsessioned-attempt-times" aria-label={`ended ${times} times`}
      sx={{ ml: 0.5, color: 'text.secondary', whiteSpace: 'nowrap', flexShrink: 0 }}>
      ×{times}
    </Box>
  )
}

export function groupAttempts(attempts: UnsessionedAttempt[]): AttemptGroup[] {
  const byTask = new Map<number, UnsessionedAttempt[]>()
  for (const attempt of attempts) {
    const own = byTask.get(attempt.task_id)
    if (own) own.push(attempt)
    else byTask.set(attempt.task_id, [attempt])
  }
  const groups = new Map<string, UnsessionedAttempt[]>()
  for (const own of byTask.values()) {
    const key = [...distinct(own.map(a => a.label)).sort(), '\u0001', ...distinct(own.map(a => a.node ?? '')).sort()]
      .join('\u0000')
    const group = groups.get(key)
    if (group) group.push(...own)
    else groups.set(key, [...own])
  }
  return [...groups].map(([key, unsorted]) => {
    // Merged tasks interleave in time; newest first again, so the first is the newest of the whole row.
    const members = unsorted.sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
    const counts = new Map<string, number>()
    for (const { label } of members) counts.set(label, (counts.get(label) ?? 0) + 1)
    return {
      key,
      attempts: members,
      causes: [...counts].map(([label, count]) => ({ label, count })),
      nodes: distinct(members.flatMap(a => (a.node ? [a.node] : []))),
    }
  })
}

/* Attempts with no session: task (takes the slack), age, cause, node; two columns on the phone. Each row is a
subgrid of the one container, so the columns align across rows. */
const UNSESSIONED_GRID_SX = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 2fr) 3.5rem minmax(0, 2fr) minmax(0, 1fr)',
  columnGap: 1.5,
  rowGap: 0.5,
  fontSize: '0.8125rem',
  minWidth: 0,
  [PHONE]: { gridTemplateColumns: 'minmax(0, 1fr) auto' },
} as const
const UNSESSIONED_ROWS_SX = {
  display: 'grid', gridColumn: '1 / -1', gridTemplateColumns: 'subgrid', columnGap: 'inherit', rowGap: 0.25,
  m: 0, p: 0, listStyle: 'none', alignItems: 'baseline',
} as const
/* A rule between rows, so a row whose reasons or "+N" list are open still reads as one unit (most needed on the phone). */
const UNSESSIONED_ROW_SX = {
  '&:not(:first-of-type)': { borderTop: 1, borderColor: 'divider', pt: 0.5, mt: 0.25 },
} as const
/* "+N" right after the title: the phone's 44px target is kept, but its glyph sits at the target's left edge (a
centred one floated ~16px off the title) and negative block margins keep the target from heightening the row.
A left margin parts it from an open row's "×N", which otherwise read as one token ("×2+2"). */
const UNSESSIONED_MORE_SX = {
  font: 'inherit', whiteSpace: 'nowrap', verticalAlign: 'baseline', flexShrink: 0, ml: 0.5,
  [PHONE]: { minWidth: 44, minHeight: 44, my: '-12px', textAlign: 'left', ml: 1 },
} as const
/* A row's runner reasons, spanning the row beneath it. */
const UNSESSIONED_DETAIL_SX = {
  gridColumn: '1 / -1', pl: 1.5, mb: 0.5, color: 'text.secondary', fontSize: '0.75rem', overflowWrap: 'anywhere',
  whiteSpace: 'pre-line',
} as const
/* An open "+N" task's own runner reason: in the Cause and Node columns, beneath it on the phone. */
const UNSESSIONED_OTHER_DETAIL_SX = {
  gridColumn: '3 / -1', minWidth: 0, color: 'text.secondary', fontSize: '0.75rem', overflowWrap: 'anywhere',
  whiteSpace: 'pre-line',
  [PHONE]: { gridColumn: '1 / -1', pl: 3 },
} as const
/* With "+N" open the lead task's reason joins the others' column, so every task's reason reads in one place;
on the phone, beneath, at the others' indent. */
const UNSESSIONED_LEAD_DETAIL_OPEN_SX = { mb: 0.5 } as const
const UNSESSIONED_HEAD_SX = {
  fontSize: '0.6875rem', textTransform: 'uppercase', letterSpacing: '0.04em', color: 'text.secondary',
} as const

// ── Filter vocabulary ─────────────────────────────────────────────────────────────────────────────

/* Transcript sizes to its "Stale (never started, over …)" option and End reason to its longest served label, which pushed
Node and Search to a second line at 1280px. Each is capped just above its "Any …" label, so the row fits one line and
only a long chosen value truncates; the open list stays whole. Desktop only: a phone line shares its slack (`grow`). */
const TRANSCRIPT_SELECT_CAP = 150
const END_REASON_SELECT_CAP = 170

/* `custom` is page-only: it travels as `window=all` plus `since`/`until` (`routes/sessions.py`). */
type WindowValue = '24h' | '7d' | '30d' | 'all' | 'custom'

/** The default window: "last night". */
const DEFAULT_WINDOW: WindowValue = '24h'

const WINDOW_OPTIONS: { value: string; label: string }[] = [
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
]

/* Every value `window` takes in this page's URL; `custom` is the page's own (see `WindowValue`). */
const WINDOW_VALUES: readonly WindowValue[] = ['24h', '7d', '30d', 'all', 'custom']

/* The page's URL keys: the API's names, except `transcript`, which also carries the stale pseudo-state (mapped to
`stale=true` at the request boundary). */
const URL_KEYS = {
  window: 'window', status: 'status', transcript: 'transcript', endReason: 'end_reason', node: 'node',
} as const

/* Clear filters' URL write for the four narrowings (the window is scope, not a narrowing). */
const NARROWINGS_CLEARED: Record<string, string> = {
  [URL_KEYS.status]: '', [URL_KEYS.transcript]: '', [URL_KEYS.endReason]: '', [URL_KEYS.node]: '',
}

/** A URL value the page offers, else `null` (so a stale or hand-typed link falls back rather than 422s). */
function urlChoice<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null
}

/* Apart from `WINDOW_OPTIONS`: the orphan clause looks labels up there, and orphans are not counted over a
custom range. */
const CUSTOM_WINDOW_OPTION = { value: 'custom', label: 'Custom range' }

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: 'running', label: 'Running' },
  { value: 'ended', label: 'Ended' },
  { value: 'unknown', label: 'Unknown' },
]

const TRANSCRIPT_OPTIONS: { value: string; label: string }[] = [
  { value: 'pending', label: 'Pending' },
  { value: 'uploading', label: 'Uploading' },
  { value: 'uploaded', label: 'Uploaded' },
  { value: 'failed', label: 'Failed' },
  { value: 'gave_up', label: 'Gave up' },
  { value: 'unavailable', label: 'Unavailable' },
  { value: 'not_applicable', label: 'Not applicable' },
]

/* The stale pseudo-state: a filter value, not an eighth transcript state. It is a compound predicate
 * single-homed in `app/sessions_cleanup.py::stale_clause`, never restated here. It rides the Transcript
 * dropdown and is mapped at the URL boundary to `stale=true` (as "All time" → `window=all`).
 * It lets the bulk delete's target set be looked at first. */
const STALE_FILTER_VALUE = 'stale'

/* Live refresh as in `CommandLog`: SSE drives it, the poll is the fallback when the stream is `down`.
 * The `session` entity fires at every write that changes this page
 * (`app/agent_session_lifecycle.py::_in_a_savepoint`, `app/routes/agent_sessions.py::session_changed`)
 * with the `id=0` sentinel, so the page refetches whole. */
const POLL_MS = 15_000

// ── Sorting ───────────────────────────────────────────────────────────────────────────────────────

type SortKey =
  | 'name'
  | 'node'
  | 'task'
  | 'status'
  | 'started_at'
  | 'ended_at'
  | 'tokens'
  | 'transcript_status'

type SortDir = 'asc' | 'desc'

type SortState = { key: SortKey; dir: SortDir }

/* The sort vocabulary in table order: the server's eight keys only (an unknown `sort` is a 422). One
 * list for desktop headings and the phone menu (as `pages/Projects.tsx`); each heading names its key,
 * hence the `Ended` column. `Duration` is derived and not sortable.
 * `initial` is the first-click direction per column: newest-first for timestamps and counts, A-Z for
 * names. */
const SORTABLE_COLUMNS: { key: SortKey; label: string; initial: SortDir }[] = [
  { key: 'name', label: 'Session', initial: 'asc' },
  { key: 'node', label: 'Node', initial: 'asc' },
  { key: 'task', label: 'Task', initial: 'asc' },
  { key: 'status', label: 'Status', initial: 'asc' },
  { key: 'started_at', label: 'Started', initial: 'desc' },
  { key: 'ended_at', label: 'Ended', initial: 'desc' },
  { key: 'tokens', label: 'Tokens', initial: 'desc' },
  { key: 'transcript_status', label: 'Transcript', initial: 'asc' },
]

/* The header row (`FeedList`'s `columns`): each slot's value is mono shorthand ("—", "17m", "1h") that reads
 * only under its name. Keys match the row's slots; a sortable one names its server key and first direction from
 * `SORTABLE_COLUMNS`. `Status` rides the title's marker and `SortMenu` alone. */
const sortColumn = (key: SortKey) => SORTABLE_COLUMNS.find(c => c.key === key)!
const headedBy = (slot: string, key: SortKey, label?: string): FeedColumn =>
  ({ key: slot, label: label ?? sortColumn(key).label, sortKey: key, firstDir: sortColumn(key).initial })
const COLUMNS: { title: FeedColumn; slots: FeedColumn[] } = {
  title: headedBy('name', 'name'),
  slots: [
    headedBy('tokens', 'tokens'),
    { key: 'duration', label: 'Time', ariaLabel: 'Duration' },
    { ...headedBy('ended', 'ended_at', 'End'), ariaLabel: 'Ended' },
    headedBy('node', 'node'),
    headedBy('task', 'task'),
    headedBy('transcript', 'transcript_status'),
    { ...headedBy('started', 'started_at', 'Start'), ariaLabel: 'Started' },
  ],
}

/** The "server's order" copy, shared by both affordances. */
const DEFAULT_ORDER_LABEL = 'Default order (running first, then newest)'

// ── Transcript state → what the operator is told ─────────────────────────────────────────────────

/* The seven states, each with a sentence; only `uploaded` is downloadable. `failed` takes its tone via
 * `toneFor` (`badge-failed` → error); the other six have no `badge-*` token, so they carry an explicit
 * `Tone` (neutral fallthrough would make "gave up" look like "not applicable"). Labels stay single words:
 * `ui/StatusChip` never shrinks; the sentence goes beside the pill. */
type TranscriptPresentation = { tone: Tone; label: string; reason: string | null }

const TRANSCRIPT_REASON: Record<string, TranscriptPresentation> = {
  pending: { tone: 'info', label: 'pending', reason: 'Waiting for the node to upload' },
  uploading: { tone: 'info', label: 'uploading', reason: 'Upload in progress' },
  // The only downloadable state, so nothing to explain.
  uploaded: { tone: 'success', label: 'uploaded', reason: null },
  failed: { tone: toneFor('badge-failed'), label: 'failed', reason: 'Upload failed' },
  gave_up: { tone: 'error', label: 'gave up', reason: 'Gave up' },
  /* Fallback only: the server always records a reason (`routes/agent_sessions.py`), and the row's
  `transcript_error` renders — e.g. a transcript over the 100 MiB read ceiling still on disk. */
  unavailable: { tone: 'neutral', label: 'unavailable', reason: 'The transcript could not be read from the node' },
  not_applicable: { tone: 'neutral', label: 'n/a', reason: 'This session has no transcript' },
}

/* The disc glyph per state (shape, not tint): ○ pending, » uploading, ✓ uploaded, ✕ failed, ⊘ gave_up
 * (distinct from failed), ? unavailable, · not_applicable. Unknown → `!`, never a recognised shape. */
const TRANSCRIPT_GLYPH: Record<string, string> = {
  pending: '○',
  uploading: '»',
  uploaded: '✓',
  failed: '✕',
  gave_up: '⊘',
  unavailable: '?',
  not_applicable: '·',
}
const UNKNOWN_TRANSCRIPT_GLYPH = '!'

/* Pre-payload placeholder only; the threshold is the server's `overdue_after_seconds`
 * (from `transcripts.LEASE_TTL`). */
const DEFAULT_OVERDUE_MS = 15 * 60 * 1000

/* When there is something to upload at all, or null. A row is written at session start, so falling back
 * to `started_at` for every row called long-running sessions "overdue" for a transcript that cannot
 * exist yet. A lease means a node took the work; `ended_at` means there is a finished session.
 * The condition for null is `status === 'running'`, not "neither timestamp": a reconstructed orphan
 * (`agent_session_lifecycle.py::record_observed`) is `ended`, `pending`, no lease, and may have
 * `ended_at` NULL. Its bytes are owed, so it counts from `started_at`. */
function uploadableSince(row: Session): string | null {
  if (row.transcript_leased_at) return row.transcript_leased_at
  if (row.ended_at) return row.ended_at
  return isRunning(row) ? null : row.started_at
}

/** Running as the page shows it: a `running` row whose attempt the server says is over reads ended. */
export function isRunning(row: Session): boolean {
  return row.status === 'running' && !row.attempt_over
}

/** An owed upload waits on its node: while that node is offline it is not late, it is waiting — a calm tone. */
const NODE_OFFLINE_REASON = 'Node offline — the upload waits for it'


/** A `pending` row whose session has not ended: nobody has asked the node for anything yet. */
const STILL_RUNNING_REASON = 'The session is still running — there is nothing to upload yet'

/** A row's transcript state as stated: the map plus the row's facts. `overdueAfterMs` comes from the
 *  envelope. */
function transcriptPresentation(
  row: Session,
  now: number,
  overdueAfterMs: number = DEFAULT_OVERDUE_MS,
): TranscriptPresentation {
  const base = TRANSCRIPT_REASON[row.transcript_status]
  if (!base) {
    // A state this build has never heard of; the enum is closed server-side, so the server moved ahead.
    return {
      tone: 'warning',
      label: row.transcript_status,
      reason: 'This console build does not recognise this transcript state',
    }
  }

  if (row.transcript_status === 'gave_up') {
    /* The server already counts attempts: `routes/agent_sessions.py::_fail` appends "(attempt 5 of 5; …)"
    to the stored reason, so with an error this says only "Gave up". The counted form is for a row with no
    recorded error, and only with a non-zero count: "after 0 attempts" contradicts itself. */
    if (row.transcript_error) {
      return { ...base, reason: withError(base.reason ?? 'Gave up', row.transcript_error) }
    }
    const attempts = row.transcript_attempts
    if (!attempts) return { ...base, reason: 'Gave up' }
    const plural = attempts === 1 ? 'attempt' : 'attempts'
    return { ...base, reason: `Gave up after ${attempts} ${plural}` }
  }
  if (row.transcript_status === 'failed') {
    return { ...base, reason: withError(base.reason ?? 'Upload failed', row.transcript_error) }
  }
  if (row.transcript_status === 'unavailable') {
    /* The row's own reason, not the map's: `unavailable` covers unreadable transcripts too (over the
    104857600-byte read ceiling, an errno), whose bytes may be recoverable on disk. */
    return { ...base, reason: withError(base.reason ?? 'The transcript is unavailable', row.transcript_error) }
  }
  if (row.transcript_status === 'pending' || row.transcript_status === 'uploading') {
    /* An ended run that never produced anything to upload says why, rather than waiting on an upload that
    cannot come: its MicroVM failed, or its stream stored no event. */
    const nothing = !isRunning(row) && nothingToUpload(row)
    if (nothing) return { tone: 'neutral', label: 'none', reason: nothing }
    /* A run the server says is over, with no end reported: its upload starts only once its node ends it, so it is
    not late by any clock here — only an offline node is worth saying. */
    if (row.attempt_over) {
      return row.node_status === 'offline' ? { ...base, reason: NODE_OFFLINE_REASON } : base
    }
    const since = uploadableSince(row)
    if (since === null) {
      /* `null` from `uploadableSince` means still running, so this sentence is true here; the gate lives in
      the predicate, not re-spelled. `uploading` cannot reach this branch. */
      return row.transcript_status === 'pending' ? { ...base, reason: STILL_RUNNING_REASON } : base
    }
    const at = Date.parse(since)
    if (!Number.isNaN(at) && now - at > overdueAfterMs) {
      if (row.node_status === 'offline') return { ...base, reason: NODE_OFFLINE_REASON }
      return { ...base, tone: 'warning', reason: `${base.reason} — overdue` }
    }
  }
  return base
}

/** Why an ended run has no transcript to upload, or null when one may still come. A run with no events already
 *  says so on its own error line ("No events received"), so its reason does not repeat it. */
function nothingToUpload(row: Session): string | null {
  if (row.vm_failure) return `Nothing to upload — MicroVM failed: ${row.vm_failure}`
  // A run whose batches were refused did run, and may still upload: only a silent one has nothing.
  if (row.no_events && !row.events_refused) return 'Nothing to upload'
  return null
}

function withError(sentence: string, error: string | null): string {
  // An error that IS the sentence (the server's fallback words) is said once.
  return error && error !== sentence ? `${sentence} — ${error}` : sentence
}

// ── Session status → tone ─────────────────────────────────────────────────────────────────────────

/* `running`/`ended` take their tone via `toneFor` (`badge-running`, `badge-idle`). `unknown` deliberately
 * does not use `badge-node-unknown` (a benign presence gap): a lost session gets the warning tone. */
const STATUS_PRESENTATION: Record<string, { tone: Tone; glyph: string }> = {
  running: { tone: toneFor('badge-running'), glyph: '▶' },
  ended: { tone: toneFor('badge-idle'), glyph: '■' },
  unknown: { tone: 'warning', glyph: '?' },
}

// ── The row's slot widths ─────────────────────────────────────────────────────────────────────────

/* The row: a disc (transcript state, tinted by `transcriptPresentation`, shaped by `TRANSCRIPT_GLYPH`), a
 * title, a status marker (word and `end_reason` as its name/title), and seven fixed slots.
 * The reason is a note (`FeedNote`), not a slot: a slot would clip it (property #2).
 * Widths are priced against `ui/listStyles.ts`'s cluster sums: meta at ≥900px, node, task and transcript to
 * 768px, `started` on the phone row. */
/* The Node column's floor; it widens to the page's longest node name (`contentSlotWidth`), up to `NODE_MAX_CQW`
 * percent of the list, so "worker-eu-west-1a" reads whole at desktop rather than "worker-e…" in 64px. */
const NODE_SLOT = 64
const NODE_MAX_CQW = 22
const TASK_SLOT = 116
/* Fits its header, "TRANSCRIPT" (`FeedList`'s 11px tracked caps); its values are shorter. */
const TRANSCRIPT_SLOT = 84
const NUM_SLOT = 48
/* Tokens with the cost beside it ("1.2M · $3.40") widens the column to the page's widest value, up to this share. */
const TOKENS_MAX_CQW = 14
/* `durationLabel`'s widest value is seven mono characters ("23h 59m", "13d 23h", ~46px at 11px), which
 * filled `NUM_SLOT` edge to edge and butted against Tokens; the spare keeps a gap. */
const DURATION_SLOT = 56
/* The name's cap (~40 characters); a longer name ellipsises, its hover whole. */
const TITLE_MAX = 280
/* `started` and `ended` fit their headers ("START", "END") with the gap a neighbour's header needs: at
`AGE_SLOT` (28) "END" rode the Time header ("TIME END"). */
const CLOCK_SLOT = 40

/* Age slots carry `compactAge` (max `364d`, 26.4px): verbose `relativeAge` at 11px mono overflowed 44px and is
unbounded. `absoluteTime` stays on `title`. */

/** Skeleton rows: a handful, not `per`, so the page does not shrink on settle. */
const PAGE_SKELETON_ROWS = 8

// ── Small formatters ──────────────────────────────────────────────────────────────────────────────

/* Token counts are `humanize.compactCount`'s; the tooltip keeps the exact number. Durations/ages come from
 * `components/tasks/shared` and `lib/relativeTime`. */
function tokenTotal(row: Session): number | null {
  if (row.tokens_in == null && row.tokens_out == null) return null
  return (row.tokens_in ?? 0) + (row.tokens_out ?? 0)
}

/* "1.2M · $3.40": tokens, then the cost when the server priced it. An unknown cost shows tokens only; an
 * unparseable one is unknown, never `$0`. */
function usageLabel(row: Session): string {
  const tokens = tokenTotal(row)
  const cost = row.cost_usd == null || row.cost_usd.trim() === '' ? NaN : Number(row.cost_usd)
  const dollars = Number.isFinite(cost) ? formatCostUsd(cost) : null
  if (tokens == null) return dollars ?? NO_VALUE
  return dollars ? `${compactCount(tokens)} · ${dollars}` : compactCount(tokens)
}

/** The real span once ended, live elapsed while running (re-read by `useMinuteTick`); never a dash for a
 *  running session. */
function durationLabel(row: Session): string {
  if (row.ended_at) {
    const span = (Date.parse(row.ended_at) - Date.parse(row.started_at)) / 1000
    return Number.isNaN(span) ? NO_VALUE : humanDuration(span, true)
  }
  return elapsedLabel(row.started_at)
}

/* The orphan banner's sentence: an honesty banner that makes "we track every session" falsifiable.
 * The reason once plus a compact per-node breakdown; a clause per node took ten lines at 390px and pushed
 * the table below the fold. A single node keeps the plain sentence. */
function orphanBanner(entries: { node: string | null; count: number }[]): string {
  if (entries.length === 1) {
    const [only] = entries
    const where = only.node ?? 'an unattributed node'
    const noun = only.count === 1 ? 'session' : 'sessions'
    const them = only.count === 1 ? 'it' : 'them'
    return `${only.count} ${noun} on ${where} had no record until the node reported ${them}.`
  }
  const total = entries.reduce((sum, entry) => sum + entry.count, 0)
  const noun = total === 1 ? 'session' : 'sessions'
  const breakdown = entries
    .map(entry => `${entry.node ?? 'unattributed'} (${entry.count})`)
    .join(', ')
  return `${total} ${noun} had no record until a node reported them — ${breakdown}.`
}

/** The window clause: orphan counts share the rows' window, so the number moves with it. Empty for All
 *  time. */
function orphanWindowClause(window: WindowValue): string {
  const label = WINDOW_OPTIONS.find(option => option.value === window)?.label
  return label ? ` Counted over the same window as the rows (${label.toLowerCase()}).` : ''
}

/** A served window's name as the dropdown words it ("Last 7 days", "All time"). */
function windowLabel(window: string): string {
  return WINDOW_OPTIONS.find(option => option.value === window)?.label ?? 'All time'
}

/** The empty table's title, in the window's terms: "recorded yet" under a bounded window would contradict the
 *  attempts listed above it. */
function emptyTitleFor(window: WindowValue): string {
  if (window === 'custom') return 'No sessions in this range'
  const label = WINDOW_OPTIONS.find(option => option.value === window)?.label
  return label ? `No sessions in the ${label.toLowerCase()}` : 'No sessions recorded yet'
}

// ── The stale rows: what they are, and the copy for saying so ──

/* What a stale row is. The predicate is the server's (`app/sessions_cleanup.py::stale_clause`: status
 * columns, an age cutoff and a liveness conjunct) and is not spelled here. In operator terms: the row was
 * written at dispatch and the run never started — no transcript file, no upload request, nothing holds it.
 * Nothing can finish one: it never ended, so it is never offered for upload (`awaiting_transcript` takes
 * `ended` rows only), and there is no `.jsonl` for the node's sweep.
 * A running session looks identical, which is why the clause checks liveness (`held_live`; reason 4
 * below) and age only narrows.
 * Safe to delete because of the upload ticket: requesting one is the only thing that creates an object
 * key, and a row with no attempts and no lease never requested one. (`transcript_status != 'uploaded'` was
 * wrong: members are PUT one at a time and `uploaded` lands last, and with no bucket every row matched.)
 * An operator-aimed delete, not a cron: this file owes the count, a view of the set, and plain copy. */

/** Pre-payload placeholder only; the threshold is the server's knob
 *  (`sessions_cleanup.DEFAULT_STALE_SESSION_AFTER_HOURS`). */
const DEFAULT_STALE_AFTER_SECONDS = 6 * 60 * 60

/* A fallback signal for a response that names neither `truncated` nor `max_per_call`, so a truncated
 * pass is never reported as finished. Home: `app/sessions_cleanup.py::MAX_DELETE_PER_CALL`. */
const STALE_DELETE_CEILING = 5000

/** `POST /api/sessions/stale/delete`'s response. Only `deleted` is required; the ceiling fields are read
 *  defensively so drift degrades to "more remain". */
type StaleDeleteResult = {
  deleted: number
  /** Stale rows the server still sees after this pass. */
  remaining?: number
  /** The server's own word for "this call hit its ceiling". */
  truncated?: boolean
  /** The server's ceiling, so the inference measures against the real bound. */
  max_per_call?: number
}

/* The threshold in words ("6 hours"); `humanDuration(…, true)` renders a measurement ("6h 0m"). */
function thresholdLabel(seconds: number): string {
  if (seconds >= 86_400 && seconds % 86_400 === 0) return countOf(seconds / 86_400, 'day')
  if (seconds >= 3_600 && seconds % 3_600 === 0) return countOf(seconds / 3_600, 'hour')
  if (seconds >= 60) return countOf(Math.round(seconds / 60), 'minute')
  return countOf(Math.max(0, Math.round(seconds)), 'second')
}

/** "session row(s)": the row is what goes, nothing is stopped. */
function rowNoun(count: number): string {
  return count === 1 ? 'session row' : 'session rows'
}

/* The banner's sentence: the shape of the set — a dispatch whose run never started — and why nothing
 * settles it. Not "no transcript": `failed`/`gave_up` rows share that and are not in the set.
 * The last clause names the scope ("every session you can see"): the count ignores filters and window
 * and is node-grant scoped (`routes/sessions.py`). */
function staleBanner(count: number, thresholdSeconds: number): string {
  const noun = count === 1 ? 'session' : 'sessions'
  const were = count === 1 ? 'was' : 'were'
  const them = count === 1 ? 'it' : 'them'
  return (
    `${count.toLocaleString()} ${noun} ${were} dispatched more than ${thresholdLabel(thresholdSeconds)} ago ` +
    `and never started — the run wrote no transcript file and never asked to upload one. Nothing will ever ` +
    `finish ${them}. Counted across every session you can see, not just this page or window.`
  )
}

/* What happened, including a capped pass (the call deletes at most `STALE_DELETE_CEILING` rows). */
/* `remaining > 0` is not evidence of a capped pass: it is a fresh count, and rows age in during the call.
 * So `truncated` (or reaching the ceiling) earns "run it again"; a non-zero `remaining` on an untruncated
 * pass is drift, stated without an instruction; and `remaining === 0` outranks both — never re-derive
 * truncation from `deleted >= ceiling` when `remaining` was named. */
function staleDeleteReport(result: StaleDeleteResult | null): string {
  const deleted = Number.isFinite(result?.deleted) ? Number(result?.deleted) : 0
  const remaining = Number.isFinite(result?.remaining) ? Number(result?.remaining) : null
  // The server's ceiling wins; `STALE_DELETE_CEILING` is only for a response naming neither field.
  const ceiling = Number.isFinite(result?.max_per_call) && Number(result?.max_per_call) > 0
    ? Number(result?.max_per_call)
    : STALE_DELETE_CEILING
  const took = `Deleted ${deleted.toLocaleString()} ${rowNoun(deleted)}`
  // The server said the backlog is empty; no capped-pass sentence can be true.
  const backlogCleared = remaining === 0
  if (!backlogCleared && (result?.truncated === true || deleted >= ceiling)) {
    const rest = remaining !== null && remaining > 0 ? `${remaining.toLocaleString()} more remain` : 'More remain'
    return `${took} — the most one pass removes. ${rest}; run it again to clear the rest.`
  }
  if (remaining !== null && remaining > 0) {
    const has = remaining === 1 ? 'has' : 'have'
    const them = remaining === 1 ? 'it' : 'them'
    return `${took}. ${remaining.toLocaleString()} ${has} since crossed the threshold; the banner counts ${them}.`
  }
  return `${took}.`
}

/* Why a row may not be deleted: five ways to be outside the stale set, each a 409 with its own sentence
 * (`routes/sessions.py::_why_protected`, mirrored in order). The reason must be true of this row, not
 * merely of the refusal.
 * 1. It has an upload ticket (a ticketed status, an attempt, or a lease), so it may have transcript
 *    members in the bucket that only this row points to (`models/agent_sessions.py::AgentSession` FKs are `SET NULL`).
 *    Deleting it strands them.
 * 2. Its transcript settled without a ticket (`unavailable`/`not_applicable`, no attempt or lease):
 *    `apps/jaahd/session_sweep.py` reports those without a ticket, so saying it may have storage data
 *    is false. It is a finished audit record (`app/agent_sessions.py`: the expected fate of an old one).
 * 3. The run started: ended rows are offered for upload, so it is not stuck.
 * 4. The control plane holds it live (`held_live`): an `advisor` chat or an `awaiting_response` runner
 *    can be arbitrarily old.
 * 5. It is too young to tell "never started" from "still going".
 * Each a sentence, capitalised like Download's. No em-dash inside any: `RowActionsOverflow` joins verb and
 * reason with one. */
const TICKETED_UNDELETABLE_REASON =
  'This session may already have transcript data in object storage, and this row is the only pointer to it'
const SETTLED_UNDELETABLE_REASON =
  'This session\'s transcript is settled and the row is a completed audit record; the prune only removes '
  + 'rows that are stuck'
const STARTED_UNDELETABLE_REASON =
  'This session ran and ended, so its transcript may still be uploaded'
// Scope-neutral: tenant admins with sessions:edit read it, and a tenant reads no control plane.
const HELD_LIVE_UNDELETABLE_REASON =
  'This session is still held as live, so a runner may be executing inside it right now'

/** Reason 5, carrying the served threshold. */
function tooYoungUndeletableReason(thresholdSeconds: number): string {
  return `It started less than ${thresholdLabel(thresholdSeconds)} ago, so it may still be running`
}

/* States reachable only through an upload ticket — `routes/sessions.py::_TICKETED_TRANSCRIPT_STATUSES`,
 * member for member. It excludes `unavailable`/`not_applicable` (reported without a ticket); see
 * reason 2. */
const TICKETED_TRANSCRIPT_STATUSES: ReadonlySet<string> = new Set([
  'uploading', 'uploaded', 'failed', 'gave_up',
])

/* Has this row ever asked for an upload ticket: the object-storage half of
 * `app/sessions_cleanup.py::stale_clause`. Any of a ticketed status, a recorded attempt or a lease.
 * Read defensively (`?? 0`): an absent field must never read as "never ticketed". */
function hasUploadTicket(row: Session): boolean {
  return TICKETED_TRANSCRIPT_STATUSES.has(row.transcript_status)
    || (row.transcript_attempts ?? 0) > 0
    || Boolean(row.transcript_leased_at)
}

/* The download as an anchor click, not a `fetch` (which would buffer an unbounded `.tar.gz` and lose the
 * server's filename) nor `location` assignment (which visibly leaves the SPA on a slow request). No
 * `download` attribute, which would override `Content-Disposition`. Same-origin cookie. */
function startTranscriptDownload(sessionId: string): void {
  const a = document.createElement('a')
  a.href = `/api/sessions/${encodeURIComponent(sessionId)}/transcript.tar.gz`
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

// ── The panel ─────────────────────────────────────────────────────────────────────────────────────

export default function SessionsTab() {
  /* Unconditionally and first, value unused: Started, Duration and the overdue test depend on the
  current time, and nothing else re-renders on a clock. */
  useMinuteTick()

  const can = useCan()
  /* One key, asked under two names: `sessions:edit` gates the download
  (`GET /api/sessions/{id}/transcript.tar.gz`) and both deletions (`POST /api/sessions/stale/delete`,
  `DELETE /api/sessions/{id}`) — all `@feature("sessions", "edit")` in `app/routes/sessions.py` — but
  they are separate decisions, and one boolean invites a future widening onto the wrong verb. A
  read-only principal is offered neither. */
  const canDownload = can('sessions:edit')
  const canPrune = can('sessions:edit')

  /* Every filter lives in the URL (`/sessions?task=123` from the task page, `/sessions?q=<session id>` from a
  run's link, `/sessions?window=all` pasted by hand): a link opens the same view. A value the page does not offer
  reads as its default. Only a custom range's From/To stay local. */
  const [searchParams, setFilters] = useUrlFilters()
  const taskParam = searchParams.get('task')
  const query = searchParams.get('q') ?? ''

  /* With no `window` in the URL: a deep link names one session or task, which may have ended long before the
  default window, so it opens on all time and the row it points at is there; so does a stale link, whose count
  ignores the window (see `showStaleRows`). Fixed at mount, so typing a search does not widen the window under
  the operator. */
  const [defaultWindow] = useState<WindowValue>(() => (
    query || taskParam || searchParams.get(URL_KEYS.transcript) === STALE_FILTER_VALUE ? 'all' : DEFAULT_WINDOW
  ))
  const windowValue = urlChoice(searchParams.get(URL_KEYS.window), WINDOW_VALUES) ?? defaultWindow
  const status = urlChoice(searchParams.get(URL_KEYS.status), STATUS_OPTIONS.map(o => o.value)) ?? ''
  const transcriptStatus = urlChoice(
    searchParams.get(URL_KEYS.transcript), [...TRANSCRIPT_OPTIONS.map(o => o.value), STALE_FILTER_VALUE],
  ) ?? ''
  const endReason = searchParams.get(URL_KEYS.endReason) ?? ''
  const node = searchParams.get(URL_KEYS.node) ?? ''

  /* A custom range's From/To are not in the URL, so a link naming `custom` would claim a range it cannot carry:
  it opens on the default window instead. Once, at mount: choosing Custom range later writes `custom` legitimately. */
  useEffect(() => {
    if (searchParams.get(URL_KEYS.window) === CUSTOM_WINDOW_OPTION.value) setFilters({ [URL_KEYS.window]: '' })
    // Mount only, by design (see above).
  }, [])
  /* Search and page; every filter change resets the page through `applyFilter` (server-side paging). */
  const {
    zone, from, to, page, setPage, narrow: applyFilter, clearShared, sharedFiltersSet, searchFields, rangeFields,
  } = usePagedLogFilters(1)
  const [sort, setSort] = useState<SortState | null>(null)

  const [confirm, confirmDialog] = useConfirm()
  const navigate = useNavigate()

  /* A delete's outcome is not `error`: `load` clears `error` on the refetch that follows a delete; the
  row actions' `notice` (success — a pass may be capped) and `deleteError` survive it. */
  const { busy: deletes, error: deleteError, setError: setDeleteError, notice, setNotice, run: runDelete } = useRowActions()
  const deleting = deletes.size > 0

  /* The request, built in one place. First load is exactly `page=1&window=24h`: no `sort`, no `status`,
  no `per` (the server's PER_PAGE comes back on the envelope). */
  /* The custom range's bounds as sent. An inverted pair sends neither (the server 422s it) and says so. */
  const since = windowValue === 'custom' ? localInputToIso(from) : null
  const until = windowValue === 'custom' ? localInputToIso(to) : null
  const rangeInverted = typeof since === 'string' && typeof until === 'string' && Date.parse(since) > Date.parse(until)

  const url = useMemo(() => {
    const params = new URLSearchParams()
    params.set('page', String(page))
    params.set('window', windowValue === 'custom' ? 'all' : windowValue)
    if (!rangeInverted) {
      if (typeof since === 'string') params.set('since', since)
      if (typeof until === 'string') params.set('until', until)
    }
    if (sort) {
      params.set('sort', sort.key)
      params.set('dir', sort.dir)
    }
    if (status) params.set('status', status)
    /* The one mapping at this boundary: `stale` is not a transcript state (see `STALE_FILTER_VALUE`). */
    if (transcriptStatus === STALE_FILTER_VALUE) params.set('stale', 'true')
    else if (transcriptStatus) params.set('transcript_status', transcriptStatus)
    if (endReason) params.set('end_reason', endReason)
    if (node) params.set('node', node)
    if (taskParam) params.set('task', taskParam)
    if (query) params.set('q', query)
    return `/api/sessions?${params.toString()}`
  }, [page, windowValue, since, until, rangeInverted, sort, status, transcriptStatus, endReason, node, taskParam, query])

  /* Three triggers (filters, the live stream, a delete) read through one generation fence, so a superseded
  response never lands; only a filter change re-shows the skeleton (`refresh` does not). */
  const { data, loading, error, refresh: load } = useApiRead(
    ['audit-sessions', url],
    () => get<SessionList>(url),
    'Failed to load sessions',
  )

  /* An end reason the server no longer knows (an old link, a hand-typed one) is its 422
  (`routes/sessions.py::_list_filters`, "unknown end reason"): drop it and show the list rather than an error. */
  useEffect(() => {
    if (endReason && error.startsWith('unknown end reason')) setFilters({ [URL_KEYS.endReason]: '' })
  }, [endReason, error, setFilters])

  const unsessioned = useMemo(() => groupAttempts(data?.unsessioned_attempts ?? []), [data?.unsessioned_attempts])
  const [showAllAttempts, setShowAllAttempts] = useState(false)
  const shownUnsessioned = showAllAttempts ? unsessioned : unsessioned.slice(0, UNSESSIONED_SHOWN)
  const unsessionedHasNode = shownUnsessioned.some(row => row.nodes.length > 0)
  /* Groups whose "+N" is open, listing the other tasks as links (a hover title is unreachable on touch). */
  const [openAttemptGroups, setOpenAttemptGroups] = useState<ReadonlySet<string>>(new Set())

  /* Scoped to this page's entity; `load` re-reads the current request. */
  useLiveRefetch(useEventStream('/api/stream', ['session']), () => void load(), POLL_MS)

  const clearTaskFilter = useCallback(() => {
    setFilters({ task: '' })
    setPage(1)
  }, [setFilters, setPage])

  /* Three-state heading: `initial`, reversed, then the server's default order (why `sort` is nullable). The
  phone menu spells the same exit. */
  const onSort = useCallback((key: SortKey) => {
    setSort(current => {
      if (!current || current.key !== key) {
        return { key, dir: SORTABLE_COLUMNS.find(c => c.key === key)?.initial ?? 'desc' }
      }
      const initial = SORTABLE_COLUMNS.find(c => c.key === key)?.initial ?? 'desc'
      if (current.dir === initial) return { key, dir: initial === 'asc' ? 'desc' : 'asc' }
      return null
    })
    setPage(1)
  }, [])

  /* Every heading shows from 900px (`FEED_NO_META`), so there the menu holds only what no heading sorts:
  Status, the default order, and the current key so the trigger still names it. Narrower, some headings drop
  and the menu lists every key. */
  const headingsPartial = useMediaQuery(FEED_NO_META)
  // `noSsr`: read on the first render, so a phone never flashes the long placeholder.
  const phone = useMediaQuery(PHONE, { noSsr: true })
  const theme = useTheme()
  const menuColumns = headingsPartial
    ? SORTABLE_COLUMNS
    : SORTABLE_COLUMNS.filter(c => c.key === 'status' || c.key === sort?.key)

  const clearSort = useCallback(() => {
    setSort(null)
    setPage(1)
  }, [])

  /* The window is not cleared: the empty state offers "clear the filters, or set Window to All time", and
  resetting the window here undid the second. The window is the scope, not a narrowing. One URL write for every
  key: two `setSearchParams` calls in one tick read the same `prev`, so the second would restore what the first
  dropped. */
  const clearFilters = useCallback(() => {
    /* The window is written out: a deep link's all-time default lives only in this mount, so the address would
    otherwise reopen on 24h. */
    setFilters({ ...NARROWINGS_CLEARED, task: '', q: '', [URL_KEYS.window]: windowValue })
    clearShared()
    setPage(1)
  }, [setFilters, clearShared, windowValue])

  /* See the set before deleting it. It widens the window too: `stale_count` ignores the window, so 24h
  would show a subset of what the button deletes. Every other narrowing goes as well. */
  const showStaleRows = useCallback(() => {
    setFilters({
      ...NARROWINGS_CLEARED, task: '', q: '', [URL_KEYS.window]: 'all', [URL_KEYS.transcript]: STALE_FILTER_VALUE,
    })
    clearShared()
    setPage(1)
  }, [setFilters, clearShared])

  /* One filter's URL write, through `applyFilter` so the page resets. */
  const setUrlFilter = useCallback(
    (key: string, value: string) => applyFilter(() => setFilters({ [key]: value })),
    [applyFilter, setFilters],
  )

  /* Confirm closes first, the list refetches, then the outcome lands in `notice`, so the banner is about
  the call that returned (a refusal is the server's words when it gave any — a revoked grant's 403, a 503 —
  never a silent no-op). `staleDeleteReport` owns the wording. */
  const deleteStaleSessions = useCallback(() => runDelete(
    'stale',
    'Could not delete the stale sessions. Nothing was removed.',
    async () => {
      const result = await post<StaleDeleteResult>('/api/sessions/stale/delete')
      await load()
      return result
    },
    staleDeleteReport,
  ), [load, runDelete])

  /** One row's delete, same shape and ordering as the bulk one. */
  const deleteSession = useCallback((row: Session) => runDelete(
    row.id,
    `Could not delete the session row for ${row.name}. Nothing was removed.`,
    async () => {
      await del(`/api/sessions/${encodeURIComponent(row.id)}`)
      await load()
    },
    () => `Deleted the session row for ${row.name}.`,
  ), [load, runDelete])

  const items = data?.items ?? []
  const per = data?.per ?? 25
  const total = data?.total ?? 0

  /* The wider windows worth naming, each holding more than the last (an equal count offers the same rows again). */
  const widerWindows = useMemo(() => {
    let most = total
    return (data?.wider_windows ?? []).filter(wider => {
      if (wider.total <= most) return false
      most = wider.total
      return true
    })
  }, [data?.wider_windows, total])

  /* `total` makes the last page known; a result that shrank (narrowed filter, rows ageing out) clamps back. */
  const totalPages = usePageClamp(page, setPage, total, per)

  /* Node options are the server's (every node holding a session under the other filters), valued by uuid (`?node=` is
  uuid-only; a name filters nothing) and labelled by name. An older server sends none, so the rows on screen stand in.
  `nodeNames` remembers every name seen, so the active selection keeps its label when no option carries it; it is
  unioned in, labelled by uuid only if never seen. */
  const [nodeNames, setNodeNames] = useState<Record<string, string>>({})
  const servedNodes = data?.node_options
  const seenNames = useMemo(
    () => Object.fromEntries(servedNodes
      ? servedNodes.map(o => [o.value, o.label])
      : items.flatMap(row => (row.node_id && row.node ? [[row.node_id, row.node]] : []))) as Record<string, string>,
    [servedNodes, items],
  )
  if (Object.entries(seenNames).some(([id, name]) => nodeNames[id] !== name)) setNodeNames({ ...nodeNames, ...seenNames })
  const nodeOptions = useMemo(() => {
    const ids = new Set(Object.keys(seenNames))
    if (node) ids.add(node)
    const label = (id: string) => seenNames[id] ?? nodeNames[id] ?? id
    return Array.from(ids).map(id => ({ value: id, label: label(id) })).sort((a, b) => a.label.localeCompare(b.label))
  }, [seenNames, nodeNames, node])

  const now = Date.now()
  /* The server's threshold; `DEFAULT_OVERDUE_MS` only without one. `Number.isFinite`, not `data != null`:
  a missing field is `undefined * 1000 = NaN`, which would silently stop every overdue verdict. */
  const served = data != null ? data.overdue_after_seconds * 1000 : NaN
  const overdueAfterMs = Number.isFinite(served) && served > 0 ? served : DEFAULT_OVERDUE_MS
  const storageBlocked = data != null && !data.storage_available
  /* The one sentence a disabled download gives, by priority: an unusable store outranks row state. Takes
  the caller's presentation so the button and pill cannot explain themselves differently. */
  function downloadBlockedReason(row: Session, transcript: TranscriptPresentation): string | null {
    // Scope-neutral, like the server's `storage_reason` (a tenant reads it too, so it names
    // no fleet, deployment or control plane; `apps/api/app/tenancy/copy.py`).
    if (storageBlocked) return data?.storage_reason ?? 'Transcript storage is unavailable here'
    if (row.transcript_status !== 'uploaded') {
      return transcript.reason ?? 'This transcript is not available to download'
    }
    return null
  }

  /* Guarded like `overdue_after_seconds`. A missing count reads as zero (no banner), the safe direction. */
  const servedStaleAfter = data != null ? data.stale_after_seconds : NaN
  const staleAfterSeconds = Number.isFinite(servedStaleAfter) && servedStaleAfter > 0
    ? servedStaleAfter
    : DEFAULT_STALE_AFTER_SECONDS
  const servedStaleCount = data != null ? data.stale_count : NaN
  const staleCount = Number.isFinite(servedStaleCount) && servedStaleCount > 0 ? Math.floor(servedStaleCount) : 0
  /* The per-pass ceiling, guarded likewise: the dialog renders it before any delete. */
  const servedCeiling = data?.max_per_call
  const staleDeleteCeiling = Number.isFinite(servedCeiling) && Number(servedCeiling) > 0
    ? Number(servedCeiling)
    : STALE_DELETE_CEILING

  // The row's name on its own mono line under the prose, in both row confirms.
  const nameLine = (row: Session) => <Box sx={[monoSx, { mt: 1.5, fontSize: '0.75rem' }]}>{row.name}</Box>

  /* The redaction caveat before the bytes: the redactor counts what it caught, not what it missed. A
  dialog, so it must be acknowledged before the download starts. */
  async function confirmDownload(row: Session) {
    const confirmed = await confirm({
      title: 'Transcript redaction is best effort',
      message: (
        <>
          Redaction confidence for this archive is <strong>{data?.redaction_confidence ?? 'unknown'}</strong>.
          The redactor counts what it caught; it cannot count what it missed. Treat this transcript as
          if it still contains credentials, tokens and customer data — this is not an assurance that it
          does not.
          {nameLine(row)}
        </>
      ),
      confirmLabel: 'Download',
    })
    if (confirmed) startTranscriptDownload(row.id)
  }

  async function confirmDeleteSession(row: Session) {
    const confirmed = await confirm({
      title: 'Delete this session row?',
      message: (
        <>
          This permanently deletes the audit row for this session. There is no undo: the row is the
          only record that it was dispatched. It never asked to upload a transcript, so nothing in
          object storage is affected.
          {nameLine(row)}
        </>
      ),
      confirmLabel: 'Delete session',
      destructive: true,
    })
    if (confirmed) await deleteSession(row)
  }

  /* The bulk confirm: the exact count, that the row is the only record of the session, and why no stored
  transcript is at risk. The capped-pass line only when the backlog could reach the ceiling. */
  async function confirmDeleteStale() {
    const confirmed = await confirm({
      title: `Delete ${staleCount.toLocaleString()} ${rowNoun(staleCount)}?`,
      message: (
        <>
          This permanently deletes {staleCount.toLocaleString()} {rowNoun(staleCount)} — every session
          dispatched more than {thresholdLabel(staleAfterSeconds)} ago whose run never started. There
          is no undo: the row is the only record that the session was dispatched at all.
          {/* Why the set is safe: an upload ticket is the only thing that creates an object key, and no row
          here ever asked for one. */}
          <Box sx={{ mt: 1.5 }}>
            No stored transcript is touched. None of these rows ever asked for an upload ticket — the
            only thing that ever creates an object key — so none of them has a byte in object storage.
          </Box>
          {staleCount > staleDeleteCeiling && (
            <Box sx={{ mt: 1.5 }}>
              At most {staleDeleteCeiling.toLocaleString()} go in one pass, so this will not clear the
              backlog in one press — run it again until the count reaches zero.
            </Box>
          )}
        </>
      ),
      confirmLabel: `Delete ${staleCount.toLocaleString()} ${staleCount === 1 ? 'session' : 'sessions'}`,
      destructive: true,
    })
    if (confirmed) await deleteStaleSessions()
  }

  /* Why this row may not be deleted: disabled with its reason rather than vanishing. The deletable set is
  the stale set whole, transcribed from `sessions_cleanup.stale_clause` and ordered by consequence:
  storage first (irreversible, off-box), then what the row is, then whether it is still moving.
  Not transcribed: the node-grant scope (`deletable_node_ref_clause` drops the NULL-`node_id` arm; this
  page cannot evaluate grants). A retired node's stale row may render an enabled Delete for a confined
  operator and 404, surfaced verbatim. The `?stale=true` view applies the delete's clause. */
  function deleteBlockedReason(row: Session): string | null {
    if (hasUploadTicket(row)) return TICKETED_UNDELETABLE_REASON
    /* Settled, never ticketed — after the ticket test so a row with both is refused for the ticket. With it,
    the two tests partition the enum as `_why_protected`'s first two branches do. */
    if (row.transcript_status !== 'pending') return SETTLED_UNDELETABLE_REASON
    if (row.status !== 'running') return STARTED_UNDELETABLE_REASON
    /* Liveness before age: a held session can be older than any threshold. */
    if (row.held_live === true) return HELD_LIVE_UNDELETABLE_REASON
    const startedAt = Date.parse(row.started_at)
    /* An unparseable `started_at` refuses: `NaN` comparisons are false and would offer the delete. */
    if (Number.isNaN(startedAt) || now - startedAt < staleAfterSeconds * 1000) {
      return tooYoungUndeletableReason(staleAfterSeconds)
    }
    return null
  }

  /* What Clear filters would actually clear — not the window, which `clearFilters` keeps. Otherwise the common default-24h empty state offered a button that changed
  nothing. */
  const hasClearableFilter = Boolean(status || transcriptStatus || endReason || node || taskParam || query || sharedFiltersSet)

  /* The reasons present under the other filters, each with its count, not the whole ~20-value vocabulary;
  an uncounted (older) server offers them all. The active value always stays, unioned in so its selection keeps a
  label before the first page. */
  const endReasonOptions = useMemo(() => {
    const served = data?.end_reasons ?? []
    const shown = served
      .filter(o => o.count == null || o.count > 0 || o.value === endReason)
      .map(o => (o.count == null ? o : { ...o, label: `${o.label} (${o.count})` }))
    return endReason && !served.some(o => o.value === endReason) ? [...shown, { value: endReason, label: endReason }] : shown
  }, [data?.end_reasons, endReason])
  const terminalEnds = useMemo(
    () => new Set((data?.end_reasons ?? []).filter(o => o.terminal).map(o => o.value)),
    [data?.end_reasons],
  )

  /* The engine line distinguishes rows only when the page mixes engines; one shared engine on every row is noise. */
  const mixedEngines = new Set(items.map(row => row.engine).filter(Boolean)).size > 1

  /* The session list as feed rows; trade-offs at the slot-width block above. */
  const nodeSlotWidth = contentSlotWidth(items.map(row => row.node ?? ''), NODE_SLOT, NODE_MAX_CQW, monoFamily(theme))
  const tokensSlotWidth = contentSlotWidth(items.map(usageLabel), NUM_SLOT, TOKENS_MAX_CQW, monoFamily(theme))
  const sessionRows: FeedRowSpec[] = items.map((row): FeedRowSpec => {
    const transcript = transcriptPresentation(row, now, overdueAfterMs)
    const times = formatZonedSpan(row.started_at, row.ended_at)
    // A run the server says is over reads ended, its reason the marker's title.
    const shownStatus = row.status === 'running' && !isRunning(row) ? 'ended' : row.status
    const statusMark = STATUS_PRESENTATION[shownStatus] ?? { tone: 'neutral' as Tone, glyph: '·' }
    const blockedReason = downloadBlockedReason(row, transcript)
    const undeletableReason = deleteBlockedReason(row)
    const tokens = tokenTotal(row)
    const usage = usageLabel(row)
    /* A terminal cause with its sentence (a refused model) explains the row's other alarms: nothing ran, so no
    event and no transcript came. Its line is then the one alarm; the transcript's reason stays on the disc and
    the Transcript slot. */
    const causeExplains = Boolean(row.end_detail && row.end_reason && terminalEnds.has(row.end_reason))
    // R2.1's shape: verb, which session, and (when disabled) why — `RowActionsOverflow` appends
    // `disabledReason` to the name.
    const downloadName = `Download transcript for ${row.name}`
    /* The row press opens what the session left to read, the same verb its ⋯ offers: the transcript (behind its
    redaction confirm), else the pane tail its node sent. None for a reader without `sessions:edit`. */
    const paneLog = row.pane_log_state === 'captured' && canDownload
    const opensTranscript = canDownload && blockedReason === null
    const rowPress = opensTranscript
      ? { onRowClick: () => { void confirmDownload(row) }, rowClickLabel: downloadName }
      : paneLog ? { onRowClick: () => navigate(paneLogPath(row.id)), rowClickLabel: `Open the pane log for ${row.name}` } : {}
    return {
      ...rowPress,
      key: row.id,
      /* No row `ariaLabel`: it would make the disc and markers decorative, and they carry the state. */
      indicator: {
        tone: transcript.tone,
        glyph: TRANSCRIPT_GLYPH[row.transcript_status] ?? UNKNOWN_TRANSCRIPT_GLYPH,
        // The state only; the reason is a note.
        label: `Transcript ${transcript.label}`,
        /* `uploaded` defers to `blockedReason`: "available to download" is false without transcript storage. A tap
        shows it too: the glyph alone ("?") says nothing by touch. */
        title: transcript.reason ?? blockedReason ?? 'Uploaded',
        tapTitle: true,
        /* Tone and raw state as data for the row's oracles. */
        data: { tone: transcript.tone, 'transcript-status': row.transcript_status },
      },
      /* The full name; `FeedList` clips with CSS. Never a `slice`. */
      title: row.name,
      titleText: row.name,
      // The facts follow the name, not a wide dead band away from a short one.
      titleMaxWidth: TITLE_MAX,
      /* The phone drops the Tokens slot (`gate: 'meta'`), so its figure hugs the name there. */
      titleAside: phone && usage !== NO_VALUE ? (
        <Box component="span" data-testid="session-usage-aside" sx={[monoSx, { color: 'text.disabled', fontSize: 11, ml: 0.75, whiteSpace: 'nowrap' }]}>
          {usage}
        </Box>
      ) : undefined,
      data: { status: shownStatus },
      markers: [{
        key: 'status',
        glyph: statusMark.glyph,
        // The word plus `end_reason`, so the glyph is never the only carrier.
        label: row.end_reason ? `${shownStatus} — ${row.end_reason_label ?? row.end_reason}` : row.attempt_over ? `${shownStatus} — not reported` : shownStatus,
        ...(row.attempt_over ? { title: row.attempt_over } : {}),
      }],
      slots: [
        {
          key: 'tokens', width: tokensSlotWidth, gate: 'meta', align: 'right', mono: true, dim: true,
          title: tokens == null
            ? undefined
            : `in ${(row.tokens_in ?? 0).toLocaleString()} · out ${(row.tokens_out ?? 0).toLocaleString()}`,
          node: usage,
        },
        {
          key: 'duration', width: DURATION_SLOT, gate: 'meta', align: 'right', mono: true, dim: true,
          title: 'Duration', node: durationLabel(row),
        },
        {
          key: 'ended', width: CLOCK_SLOT, gate: 'meta', align: 'right', mono: true, dim: true,
          title: absoluteTime(row.ended_at), node: compactAge(row.ended_at),
        },
        {
          key: 'node', width: nodeSlotWidth, gate: 'id', mono: true,
          title: 'Node', node: row.node ?? NO_VALUE,
        },
        {
          key: 'task', width: TASK_SLOT, gate: 'id',
          // The way back to the work; the task page links here (`/sessions?task=<id>`). The slot is `gate: 'id'`
          // (gone below 768px), so the same destination is in the row's ⋯ (`ui/feed/types.ts`).
          title: row.task_id == null
            ? undefined
            : row.task_title ? `#${row.task_id} · ${row.task_title}` : `Task #${row.task_id}`,
          node: row.task_id == null ? NO_VALUE : (
            <MuiLink component={RouterLink} to={`/tasks/${row.task_id}`} color="inherit" underline="hover">
              {row.task_title ?? `#${row.task_id}`}
            </MuiLink>
          ),
        },
        {
          /* The state's word: seven glyphs cannot be told apart at 11px. Gated at `id`. */
          key: 'transcript', width: TRANSCRIPT_SLOT, gate: 'id',
          title: transcript.reason ?? blockedReason ?? 'Uploaded',
          node: transcript.label,
        },
        {
          key: 'started', width: CLOCK_SLOT, align: 'right', mono: true, dim: true,
          title: absoluteTime(row.started_at), node: compactAge(row.started_at),
        },
      ],
      /* The row's notes (`FeedNote`), on its hover and the disc's tap. `error` for failures, `muted` for
      not-yet. An SDK agent's run names its agent (AC-4): the row's title is the session's name, and the agent is what
      tells two `sdk_agent` rows apart. A built-in run names its engine the same way, but only on a page that mixes
      engines (`mixedEngines`). Absolute, zone-named times: the slots carry only compact ages; the span form drops
      what the pair repeats. */
      notes: [
        {
          key: 'times',
          text: times.end === null ? `Started ${times.start}` : `Started ${times.start} · Ended ${times.end}`,
          tone: 'muted' as const,
        },
        ...(row.agent_ref ? [{ key: 'agent-ref', text: `Agent: ${agentRefLabel(row.agent_ref)}`, tone: 'muted' as const }]
          : row.engine && mixedEngines ? [{ key: 'engine', text: `Engine: ${row.engine}`, tone: 'muted' as const }] : []),
        /* The ACP host is named on its own runs, even one that died before its first event; the legacy
        path is every other run, so naming it on each row would be noise. */
        ...(row.runtime === 'acp' || row.dispatched_runtime === 'acp' ? [{ key: 'runtime', text: 'Runtime: ACP host', tone: 'muted' as const }] : []),
        ...(row.account ? [{ key: 'account', text: `Account: ${row.account}`, tone: 'muted' as const }] : []),
        // The recorded cause of the end, whole.
        ...(row.end_detail ? [{
          key: 'end-detail',
          text: row.end_reason ? `${row.end_reason_label ?? row.end_reason}: ${row.end_detail}` : row.end_detail,
          tone: causeExplains ? 'error' as const : 'muted' as const,
        }] : []),
        ...(row.no_events && !causeExplains ? [{ key: 'no-events', text: 'No events received', tone: 'error' as const }] : []),
        // The refusal, when it came and from which event.
        ...(row.events_refused ? [{
          key: 'events-refused',
          text: `Events refused: ${[
            `${row.events_refused.status} ${row.events_refused.detail}`,
            absoluteTime(row.events_refused.at),
            row.events_refused.first_seq == null ? null : `from seq ${row.events_refused.first_seq}`,
          ].filter(Boolean).join(' · ')}`,
          tone: 'error' as const,
        }] : []),
        /* Why it ended, when the node's log says: one note per line. */
        ...(row.node_errors ?? []).map((line, i) => ({ key: `node-error-${i}`, text: `Node log: ${line}`, tone: 'error' as const })),
        // The pane tail its node sent opens from the ⋯; a node that sent none says so.
        ...(row.pane_log_state === 'missing' ? [{ key: 'pane-log', text: 'No pane log on node', tone: 'muted' as const }] : []),
        ...(transcript.reason && !(causeExplains && transcript.tone !== 'error') ? [{
          key: 'transcript-reason',
          text: transcript.reason,
          // Red only for a real failure: a wait, even an overdue one, reads muted beside its amber disc.
          tone: transcript.tone === 'error' ? 'error' as const : 'muted' as const,
        }] : []),
      ],
      /* The row's verbs. The task link first, for every principal: it lets the Task slot be gated (hover-only
      `title` is unreachable by touch). `to`, not `href` (`href` items open a new tab); absent without a task.
      Then the download: none for a read-only principal; disabled with its reason when blocked (a state, not
      a permission, as `pages/Nodes.tsx`). The server's 403/404 is the real guard. */
      actions: [
        ...(row.task_id == null ? [] : [{
          key: 'task',
          icon: '↗',
          label: `Open task #${row.task_id}`,
          /* Names destination and row (as `components/WorktreePool.tsx`). */
          ariaLabel: `Open task #${row.task_id} for ${row.name}`,
          to: `/tasks/${row.task_id}`,
        } as OverflowItem]),
        ...(canDownload ? [{
          key: 'download',
          icon: '⭳',
          label: 'Download',
          // `RowActionsOverflow` appends `disabledReason`, so the reason is stated once.
          ariaLabel: downloadName,
          disabled: blockedReason !== null,
          disabledReason: blockedReason ?? undefined,
          onClick: () => { if (!blockedReason) void confirmDownload(row) },
        } as OverflowItem] : []),
        /* The pane tail its node sent, behind the download's key: it is raw terminal output. */
        ...(paneLog ? [{
          key: 'pane-log',
          label: 'Pane log',
          ariaLabel: `Open the pane log for ${row.name}`,
          testId: 'session-pane-log-link',
          to: paneLogPath(row.id),
        } as OverflowItem] : []),
        /* The delete, last and `danger`-toned. None for a read-only principal; a row outside the stale set gets
        it disabled with its own reason (`deleteBlockedReason`). The server is the real guard. */
        ...(canPrune ? [{
          key: 'delete',
          icon: ACTION_ICON.delete,
          label: 'Delete',
          danger: true,
          // `RowActionsOverflow` appends `disabledReason`, so the reason is stated once.
          ariaLabel: `Delete the session row for ${row.name}`,
          // In flight, every delete waits: the confirm closes before the call returns.
          disabled: deleting || undeletableReason !== null,
          disabledReason: undeletableReason ?? undefined,
          onClick: () => { if (!undeletableReason) void confirmDeleteSession(row) },
        } as OverflowItem] : []),
      ],
    }
  })

  return (
    /* The panel's gap clears the filter row's outlined labels, which ride ~9px above their fields while
    `FilterBar` pads only 3px: at `spacing={1}` they touched the banner above. `pt: 1` as Slot Intents, so with no
    banner the labels clear the Sessions/Efficiency strip's indicator. */
    <Stack spacing={2} sx={{ pt: 1, ...SESSIONS_LIST_CONTAINER_SX }}>
      {error && <Banner severity="error">{error}</Banner>}

      {/* What the last deletion did. Dismissible, outlives the refetch (see `notice`), and leads the stack. */}
      {(notice || deleteError) && (
        <Banner severity={notice ? 'success' : 'error'} onDismiss={() => { setNotice(''); setDeleteError('') }} testId="stale-notice">
          {notice || deleteError}
        </Banner>
      )}

      {/* The control plane cannot serve transcripts (normal on a local ./dc stack). The list still renders;
      downloads go dead and the server's reason is stated once. */}
      {storageBlocked && data?.storage_reason && (
        <Banner severity="warning" testId="storage-banner">{data.storage_reason}</Banner>
      )}

      {/* The orphan banner: `info`, since reconstructing a row is the system working; it makes "we track
      every session" falsifiable. */}
      {data && data.orphans.length > 0 && (
        <Banner severity="info" testId="orphan-banner">
          {orphanBanner(data.orphans)}{orphanWindowClause(windowValue)}
        </Banner>
      )}

      {/* The stale banner: `warning`, since a stale row is a "pending" that will never resolve.
      Look first, then delete: `Show them` is offered to every principal, including `sessions:view`
      readers. A view-only principal gets no Delete at all, never a greyed one. */}
      {staleCount > 0 && (
        <Banner severity="warning" testId="stale-banner">
          <Stack direction="row" sx={{ alignItems: 'center', flexWrap: 'wrap', gap: 1 }}>
            <Box component="span">{staleBanner(staleCount, staleAfterSeconds)}</Box>
            <Button
              type="button"
              onClick={showStaleRows}
              variant="outlined"
              size="small"
              aria-label={`Show the ${staleCount.toLocaleString()} stale ${staleCount === 1 ? 'session' : 'sessions'}`}
              sx={tapTargetSx}
            >
              Show them
            </Button>
            {canPrune && (
              <Button
                type="button"
                onClick={() => void confirmDeleteStale()}
                variant="contained"
                color="error"
                size="small"
                disabled={deleting}
                sx={tapTargetSx}
              >
                Delete {staleCount.toLocaleString()} {staleCount === 1 ? 'session' : 'sessions'}
              </Button>
            )}
          </Stack>
        </Banner>
      )}

      <FilterBar>
        <FilterRow>
          {/* `FilterRow` hands `ui/FilterBar` one wrapping child so controls pack two per line at 390px; on the phone each
          select grows into its line's slack, so no line strands one beside dead space; a desktop line keeps each
          select its own width, as growing let a long label clip. */}
          <FilterSelect
            label="Window"
            ariaLabel="Filter by window"
            /* The empty option is "All time" (`window=all` has no bound); mapped here so `windowValue` stays the
            server's vocabulary. */
            value={windowValue === 'all' ? '' : windowValue}
            onChange={next => setUrlFilter(URL_KEYS.window, next || 'all')}
            allLabel="All time"
            options={[...WINDOW_OPTIONS, CUSTOM_WINDOW_OPTION]}
            minWidth={150}
            grow={phone}
          />
          {windowValue === 'custom' && <DateTimeBoundFields {...rangeFields} />}
          <FilterSelect
            label="Status"
            ariaLabel="Filter by status"
            value={status}
            onChange={next => setUrlFilter(URL_KEYS.status, next)}
            allLabel="Any status"
            options={STATUS_OPTIONS}
            minWidth={130}
            grow={phone}
          />
          <FilterSelect
            label="Transcript"
            ariaLabel="Filter by transcript status"
            value={transcriptStatus}
            /* The stale value goes through `showStaleRows`, so dropdown and "Show them" land on one set (the
            dropdown alone kept the 24h window and showed a subset). */
            onChange={next => {
              if (next === STALE_FILTER_VALUE) { showStaleRows(); return }
              setUrlFilter(URL_KEYS.transcript, next)
            }}
            allLabel="Any transcript"
            /* Seven states plus the compound one, whose label carries the served threshold. "Never started", not
            "none": `failed` rows also lack a transcript and are not in the set. */
            options={[
              ...TRANSCRIPT_OPTIONS,
              {
                value: STALE_FILTER_VALUE,
                label: `Stale (never started, over ${thresholdLabel(staleAfterSeconds)})`,
              },
            ]}
            minWidth={150}
            maxWidth={phone ? undefined : TRANSCRIPT_SELECT_CAP}
            grow={phone}
          />
          <FilterSelect
            label="End reason"
            ariaLabel="Filter by end reason"
            value={endReason}
            onChange={next => setUrlFilter(URL_KEYS.endReason, next)}
            allLabel="Any end reason"
            options={endReasonOptions}
            minWidth={150}
            maxWidth={phone ? undefined : END_REASON_SELECT_CAP}
            grow={phone}
          />
          <FilterSelect
            label="Node"
            ariaLabel="Filter by node"
            value={node}
            onChange={next => setUrlFilter(URL_KEYS.node, next)}
            allLabel="All nodes"
            options={nodeOptions}
            minWidth={130}
            grow={phone}
          />
          <SearchBox
            {...searchFields}
            value={query}
            onChange={next => applyFilter(() => setFilters({ q: next }))}
            label="Search"
            ariaLabel="Search sessions"
            /* What `q` actually searches: session columns only. `tasks.title` was removed (a LIKE over a column the
            caller may not read is an oracle); a task number still matches via `name`. Three words, what either
            width's floor holds whole: four truncated ("name, session id, bra…") whenever the desktop row was full. */
            placeholder="name, id, slot"
            /* The placeholder's three words, whole; the full set (`routes/sessions.py`'s `q` clause) on hover and on focus. */
            title="Searches name, session id, row id, slot, branch and holder"
            /* `m: 0` cancels the dense margin; `fieldTapTargetSx` is the 44px floor on the native input. 150px on
            the phone so the ⇅ after it shares its line at 390px rather than wrapping to a band of its own; 160px on
            the desktop (the placeholder whole) so the whole row fits one line at 1280px. */
            sx={[{ m: 0, minWidth: { xs: 150, sm: 160 }, flex: { xs: '1 1 150px', sm: '1 1 160px' } }, fieldTapTargetSx]}
          />
          {taskParam && (
            <Button
              type="button"
              onClick={clearTaskFilter}
              variant="outlined"
              aria-label={`Clear the task #${taskParam} filter`}
              sx={tapTargetSx}
            >
              Task #{taskParam} ✕
            </Button>
          )}
          {sort && (
            /* The signposted way back, shown only once the default order is left. */
            <Button type="button" onClick={clearSort} variant="text" aria-label={DEFAULT_ORDER_LABEL} sx={tapTargetSx}>
              Default order
            </Button>
          )}
          {/* The sort control ends the filter row, not a band of its own above the banners: the
          strip is `AuditPage`'s, and this panel's controls live here. Every width keeps it: Status has no heading
          (it rides the title's marker), so this is its only sort; `menuColumns` drops what a heading sorts. See
          `SortMenu`. */}
          <Box sx={{ ml: 'auto' }}>
            <SortMenu
              noun="sessions"
              columns={menuColumns}
              sort={sort}
              onSort={onSort}
              defaultItem={{ label: DEFAULT_ORDER_LABEL, onSelect: clearSort }}
            />
          </Box>
        </FilterRow>
      </FilterBar>

      {/* From/To take a zoneless wall clock; the zone is named, as on `PagedLogView`. */}
      {windowValue === 'custom' && (
        <Typography
          variant="caption"
          data-testid="sessions-range-note"
          sx={{ display: 'block', mt: 0.75, mb: 1, color: rangeInverted ? 'error.main' : 'text.secondary' }}
        >
          {rangeInverted ? 'From is after To — range not applied.' : `Times in ${zone}.`}
        </Typography>
      )}

      {/* Attempts that ended before any session row was written: invisible in the table, so listed apart. */}
      {unsessioned.length > 0 && (
        <Box component="section" aria-labelledby="unsessioned-heading" data-testid="unsessioned-attempts">
          <Typography id="unsessioned-heading" variant="subtitle2" component="h3" sx={{ mb: 0.5 }}>
            Attempts with no session
          </Typography>
          {/* One grid, every attempt a subgrid row, so task, time, cause and node line up down the list; the
              runner's own reason spans the row beneath (in the Cause and Node columns once "+N" is open). On the phone two columns: task · time, cause · node. */}
          <Box sx={UNSESSIONED_GRID_SX}>
            {/* No listed attempt names a node: no Node heading over an empty column; Cause takes its width, as each
                row's cause does. */}
            {!phone && ['Task', 'When', 'Cause', ...(unsessionedHasNode ? ['Node'] : [])].map(heading => (
              <Box key={heading} component="span" aria-hidden
                sx={[UNSESSIONED_HEAD_SX, heading === 'Cause' && !unsessionedHasNode ? { gridColumn: 'span 2' } : {}]}>
                {heading}
              </Box>
            ))}
            <Box component="ul" id="unsessioned-list" sx={UNSESSIONED_ROWS_SX}>
              {shownUnsessioned.map(({ key, attempts, causes, nodes }, index) => {
                const [attempt] = attempts
                // Each other task once, by its own newest attempt (attempts are newest first), so its age reads true.
                const others = attempts.filter((a, i) => a.task_id !== attempt.task_id
                  && attempts.findIndex(b => b.task_id === a.task_id) === i)
                const othersOpen = openAttemptGroups.has(key)
                const othersId = `unsessioned-others-${index}`
                // Each distinct runner reason, newest first: the row's newest alone hid an older "exit 137". Open, each
                // task keeps its own, so a reason reads beside the task it came from.
                const details = reasonsOf(othersOpen ? attempts.filter(a => a.task_id === attempt.task_id) : attempts)
                return (
                  <Box component="li" key={key} data-testid="unsessioned-attempt" sx={[UNSESSIONED_ROWS_SX, UNSESSIONED_ROW_SX]}>
                    <Box component="span" sx={{ minWidth: 0, display: 'flex', gap: 0.5, alignItems: 'baseline' }}>
                      <MuiLink
                        component={RouterLink}
                        to={`/tasks/${attempt.task_id}`}
                        underline="hover"
                        title={attempt.task_title ?? undefined}
                        sx={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                      >
                        {attempt.task_title ?? `#${attempt.task_id}`}
                      </MuiLink>
                      {othersOpen && <EndedTimes attempts={attempts} taskId={attempt.task_id} />}
                      {others.length > 0 && (
                        <MuiLink component="button" type="button" data-testid="unsessioned-attempt-more" underline="hover"
                          aria-expanded={othersOpen} aria-controls={othersId}
                          aria-label={`+${countOf(others.length, 'task')}`}
                          onClick={() => setOpenAttemptGroups(open => {
                            const next = new Set(open)
                            if (next.has(key)) next.delete(key)
                            else next.add(key)
                            return next
                          })}
                          sx={UNSESSIONED_MORE_SX}>
                          {/* The noun on the desktop, where the row has room; the phone keeps the bare count. */}
                          {phone ? `+${others.length}` : `+${countOf(others.length, 'task')}`}
                        </MuiLink>
                      )}
                    </Box>
                    <Box component="span" title={absoluteTime(attempt.at)} sx={[monoSx, { color: 'text.secondary', textAlign: 'right' }]}>
                      {compactAge(attempt.at)}
                    </Box>
                    {/* A note naming no node (or one this viewer cannot see) leaves no empty Node cell: the cause takes
                        it. */}
                    <Box component="span" title={distinct(attempts.flatMap(x => (x.cause ? [x.cause] : []))).join('\n') || undefined}
                      data-testid="unsessioned-attempt-cause"
                      sx={{ color: 'error.main', minWidth: 0, overflowWrap: 'anywhere', ...(nodes.length > 0 ? null : { gridColumn: 'span 2' }) }}>
                      {/* The heading says these runs ended; the column keeps only what ended them, each counted once. */}
                      {causes.map(({ label, count }) => `${count > 1 ? `${count} × ` : ''}${label.replace(/^Runner terminated — /, '')}`)
                        .join(' · ')}
                    </Box>
                    {nodes.length > 0 && (
                      <Box component="span" data-testid="unsessioned-attempt-node" sx={[monoSx, { color: 'text.secondary', minWidth: 0, overflowWrap: 'anywhere', [PHONE]: { textAlign: 'right' } }]}>
                        {nodes.join(', ')}
                      </Box>
                    )}
                    {details.length > 0 && (
                      <Box component="span" data-testid="unsessioned-attempt-detail"
                        sx={othersOpen ? [UNSESSIONED_OTHER_DETAIL_SX, UNSESSIONED_LEAD_DETAIL_OPEN_SX] : UNSESSIONED_DETAIL_SX}>
                        {details.join('\n')}
                      </Box>
                    )}
                    {othersOpen && (
                      <Box component="ul" id={othersId} data-testid="unsessioned-attempt-others" sx={[UNSESSIONED_ROWS_SX, { mb: 0.5 }]}>
                        {/* Each task's own last attempt sits in the When column, so the most recent reads at a glance. */}
                        {others.map(other => {
                          const reasons = reasonsOf(attempts.filter(a => a.task_id === other.task_id))
                          return (
                            <Box component="li" key={other.task_id} data-testid="unsessioned-attempt-other" sx={UNSESSIONED_ROWS_SX}>
                              <Box component="span" sx={{ pl: 1.5, minWidth: 0 }}>
                                <MuiLink component={RouterLink} to={`/tasks/${other.task_id}`} underline="hover" sx={{ overflowWrap: 'anywhere' }}>
                                  {other.task_title ?? `#${other.task_id}`}
                                </MuiLink>
                                <EndedTimes attempts={attempts} taskId={other.task_id} />
                              </Box>
                              <Box component="span" data-testid="unsessioned-attempt-other-age" title={absoluteTime(other.at)}
                                sx={[monoSx, { color: 'text.secondary', textAlign: 'right' }]}>
                                {compactAge(other.at)}
                              </Box>
                              {reasons.length > 0 && (
                                <Box component="span" data-testid="unsessioned-attempt-other-detail" sx={UNSESSIONED_OTHER_DETAIL_SX}>
                                  {reasons.join('\n')}
                                </Box>
                              )}
                            </Box>
                          )
                        })}
                      </Box>
                    )}
                  </Box>
                )
              })}
            </Box>
          </Box>
          {unsessioned.length > UNSESSIONED_SHOWN && (
            <Button size="small" variant="text" sx={{ mt: 0.5 }} data-testid="unsessioned-attempts-toggle"
              aria-expanded={showAllAttempts} aria-controls="unsessioned-list" onClick={() => setShowAllAttempts(v => !v)}>
              {showAllAttempts ? 'Show fewer' : `Show all (${unsessioned.length})`}
            </Button>
          )}
        </Box>
      )}

      {/* The clamp has queued a correction; hold loading rather than claim no match for a frame. A load that
          never landed is not an empty table: with no payload this would fall to "No sessions recorded yet", a
          false claim beside the error banner, so that state claims nothing. `total_unfiltered` separates
          "nothing ever recorded" from "nothing matches" (opposite next actions); Clear is offered only when it
          would do something. `sessions-table` is kept from the `<Table>` era — suites and e2e read it. */}
      <ListStates
        loading={loading || page > totalPages}
        skeleton={{ rows: PAGE_SKELETON_ROWS }}
        failed={error && data === null && <EmptyState title="Could not load sessions" />}
        empty={total === 0}
        noMatch={(data?.total_unfiltered ?? 0) > 0 && (
          <NoMatchesState title="No sessions match these filters" onClear={hasClearableFilter ? clearFilters : undefined} />
        )}
        emptyTitle={emptyTitleFor(windowValue)}
        emptyScoped
        feed={{
          ariaLabel: 'Sessions',
          testId: 'sessions-table',
          sections: [{ key: 'sessions', rows: sessionRows }],
          columns: COLUMNS,
          sort: { key: sort?.key ?? null, dir: sort?.dir ?? 'desc' },
          onSort: key => onSort(key as SortKey),
        }}
      />

      {/* An empty or near-empty window says what the wider ones hold, so one stray row never reads as the whole
      history; each count switches to its window. */}
      {!loading && widerWindows.length > 0 && (
        <Stack direction="row" data-testid="sessions-wider-windows" sx={{ alignItems: 'center', flexWrap: 'wrap', columnGap: 1, mt: 1 }}>
          {/* A line of its own on the phone, so the three counts share the next one rather than stranding the last. */}
          <Typography variant="body2" component="span" sx={{ color: 'text.secondary', flexBasis: { xs: '100%', sm: 'auto' } }}>
            Wider windows:
          </Typography>
          {widerWindows.map(wider => (
            <Button
              key={wider.window}
              type="button"
              variant="text"
              size="small"
              onClick={() => setUrlFilter(URL_KEYS.window, wider.window)}
              sx={tapTargetSx}
            >
              {windowLabel(wider.window)}: {wider.total.toLocaleString()}
            </Button>
          ))}
        </Stack>
      )}

      {/* A named landmark, and buttons that say what they page. */}
      <Box component="nav" aria-label="Sessions pages">
        <PageNav testId="pagination" noun="sessions" page={page} setPage={setPage} totalPages={totalPages} />
      </Box>

      {confirmDialog}
    </Stack>
  )
}
