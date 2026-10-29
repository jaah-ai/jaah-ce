import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MutableRefObject } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import { apiDetail, del, get, post } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { useEventStream } from '../../hooks/useEventStream'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { cachedRead, forgetCachedRead } from '../../lib/cachedRead'
import { touchesScope } from '../../lib/taskEventScope'
import { takeTaskPageSeed } from '../../lib/taskPageSeed'
import { AsyncLookup, type LookupOption } from '../../ui/AsyncLookup'
import { Banner } from '../../ui/Banner'
import { TaskLink } from '../../ui/DetailFields'
import { DisabledReason } from '../../ui/DisabledReason'
import { useAwaitingApprovalReason } from '../../lib/orgLifecycle'
import FeedList from '../../ui/feed/FeedList'
import type { FeedRowSpec, OverflowItem } from '../../ui/feed/types'
import { StatusChip } from '../../ui/StatusChip'
import type { PipelineTask, TaskDepRef, TaskDependencies as TaskDeps } from '../../types'
import { can, CONTROLS, startBody } from './controls'
import { type PushIdentity, usePushIdentity } from './PushIdentityDialog'
import { displayTitle, stateLabel, statusForState, taskLabel } from './shared'
import { HEADING_SX, PANEL_SX } from './TaskTags'

/* The two icon controls a row may carry. NO PHONE FLOOR OF THEIR OWN: `theme.ts` raises every
   `MuiIconButton` to 44x44 below `sm`, which in an ungated slot would silently set a 44px row height.
   The ✕ sits in a `compact` slot and the ⟳ in an `id`-gated one (`ui/feed/types.ts::FeedSlot.compact`).
   `&.Mui-disabled` IS NOT COSMETIC: MUI's `pointer-events: none` makes the `elementFromPoint`
   reachability oracles (mobile nav not covering an action) walk past a disabled control. */
const ROW_ICON_SX = { '&.Mui-disabled': { pointerEvents: 'auto' } } as const

// `ui/Banner`'s density, reproduced for the one direct Alert (see its call site).
const ALERT_SX = { py: 0.25, px: 1, alignItems: 'center', '& .MuiAlert-message': { py: 0.5 } } as const

// Cross-task dependency sub-UI for the task detail, in TWO halves on two surfaces:
//   • `TaskDependsOn` — prerequisites ("blocked by"), inside `TaskEditForm`'s `<form>` (Task tab,
//     Advanced disclosure). Editable, STAGED until the form saves.
//   • `TaskDependents` — the reverse half, on the Details tab. Read-only apart from Re-run: a reverse edge
//     is owned by the dependent's own prerequisite list and must never grow add/remove.
// Each mounts its own `useTaskDependencies`, but the view (GET /api/tasks/{id}/dependencies) is ONE read per
// page: both go through `lib/cachedRead` under `['task-dependencies', id]` (no provider needed, so neither host is
// coupled to the other), and the form half — remounted on every own-task event — answers from that cache.
// AUTO-UNBLOCK: the hook re-reads on a `task` SSE event that can have moved the view (see the hook).
// STALE + RE-RUN: a dep carrying `stale_reason` (see app/routes/tasks/dependencies.py's
// `_propagate_staleness`/`_dependent_ids_bfs`) gets a dirty badge and a per-row Re-run through the
// `start` control with run intent — never an automatic cascade.
// FORM SAFETY: inside a host `<form>` an untyped `<button>` submits, so every button spells
// `type="button"`, and this file opens no `<form>` of its own (nesting is invalid HTML).

// Narrow an unknown payload to a well-formed view, so the additive sub-panel renders nothing rather than
// throwing on an unexpected shape.
function isTaskDeps(data: unknown): data is TaskDeps {
  const d = data as Partial<TaskDeps> | null
  return (
    !!d &&
    Array.isArray(d.prerequisites) &&
    Array.isArray(d.dependents) &&
    Array.isArray(d.unmet_prerequisite_ids)
  )
}

// The one dependency-view fetch, shared so both halves get the same SSE refetch and out-of-order guard.
// `taskId` is null on the create surface alone: no view, so the fetch is skipped.
function useTaskDependencies(taskId: number | null): {
  data: TaskDeps | null
  error: string
  reload: () => void
} {
  // Shape guard: a non-conforming response is "no view" (renders nothing). The banner keeps this panel's
  // own words, never the server's detail.
  // Cached (see the file header): a mount inside `DEPS_STALE_MS` — the form's remount — reads no GET, and the two
  // halves' mount reads share one. The cache holds the RAW answer; the guard runs per reader.
  const { data, error, refresh } = useApiRead(
    // Its own name, never the cached read's `task-dependencies`: one key for both would have the query wait on itself.
    ['task-dependencies-view', taskId],
    () => cachedRead(['task-dependencies', taskId], async () => takeTaskPageSeed<TaskDeps>(taskId as number, 'dependencies') ?? get<TaskDeps>(`/api/tasks/${taskId}/dependencies`), DEPS_STALE_MS)
      .then(fetched => (isTaskDeps(fetched) ? fetched : null)),
    DEPS_ERROR,
    { enabled: taskId !== null },
  )

  // AUTO-UNBLOCK: re-read on a `task` event naming this task or a task it lists (a prerequisite reaching `done`
  // emits one), births included (`touchesScope`). A prerequisite the caller cannot see leaves no id to filter on,
  // so any event re-reads then. Every fleet event used to re-read this view, twice.
  // `reload` (a write here, Re-run) FORGETS the cached view first, so it reaches the server. The live re-read
  // forgets only an answer older than the last event this half heard (`heardAt`, when the PAGE first heard it):
  // both halves hear each event, and the second to fire joins the first's read in flight or takes its answer
  // (`forgetCachedRead`) — one GET per event per page, a half mounted after the event included.
  const reload = useCallback(() => {
    if (taskId === null) return
    forgetCachedRead(['task-dependencies', taskId])
    void refresh()
  }, [taskId, refresh])
  const heardAt = useRef(0)
  const liveReload = useCallback(() => {
    if (taskId === null) return
    forgetCachedRead(['task-dependencies', taskId], heardAt.current)
    void refresh()
  }, [taskId, refresh])
  const scopeRef = useRef<ReadonlySet<number> | null>(null)
  scopeRef.current =
    data && taskId !== null && !(data.hidden_unmet_prerequisite_count ?? 0)
      ? new Set([taskId, ...data.prerequisites.map(t => t.id), ...data.dependents.map(t => t.id)])
      : null
  // An edge write (`dependency_added`/`_removed`, `routes/tasks/`) is filtered like any other event: its id is the
  // DEPENDENT, and the payload carries nothing else (`app/events.py::emit_console_event` sends entity/id/action), so
  // the prerequisite cannot be matched. It reaches the dependent's own page (this task) and every page already
  // listing the dependent (a removal, a re-pointed edge); the one view it misses is a prerequisite's page gaining a
  // NEW dependent written elsewhere — read-only there (`TaskDependents`), and picked up by the next event in scope or
  // a reload. Letting every edge write through re-read this view on every page in the fleet for each one.
  useLiveRefetch(useEventStream(), liveReload, null, {
    entity: 'task',
    match: event => {
      if (!touchesScope(event, scopeRef.current)) return false
      heardAt.current = eventArrival(event)
      return true
    },
  })

  return { data, error: error ? DEPS_ERROR : '', reload }
}

const DEPS_ERROR = 'Failed to load dependencies'

// When this page first heard an event, by the event object the shared stream hands every listener
// (`hooks/useEventStream.ts::acquireStream`): the halves' live re-reads compare cached answers against it.
const eventArrivals = new WeakMap<object, number>()
function eventArrival(event: object): number {
  const known = eventArrivals.get(event)
  if (known !== undefined) return known
  const now = Date.now()
  eventArrivals.set(event, now)
  return now
}

// Long enough for the form's remount on an own-task event to hit cache; the live path forgets the entry on any
// event in scope, so this bounds only a fresh mount with no event since (a tab switch back, a re-navigation).
const DEPS_STALE_MS = 30_000

// Re-run a stale dep through the `start` control with intent `'run'` (`run_requested: true` — see
// `StartIntent` in controls.ts). Per-row, never cascaded. Shares one `busy`/`actionError` pair so one
// in-flight write disables every control.
// AN ACTING OPERATOR IS ASKED WHOM TO PUSH AS first (`PushIdentityDialog.tsx`); a dep ref carries no project, so
// only then is the row read for it. Cancel posts nothing. The panel renders `pushDialog`.
function useRerun(reload: () => void) {
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const { acting, ask, dialog: pushDialog } = usePushIdentity()

  const rerun = useCallback(
    async (depId: number) => {
      setActionError('')
      let pushAs: PushIdentity = {}
      if (acting) {
        let row: PipelineTask
        try {
          row = await get<PipelineTask>(`/api/tasks/${depId}`)
        } catch (err) {
          setActionError(apiDetail(err, 'Failed to re-run task'))
          return
        }
        const answer = await ask({ projectId: row.project_id, title: row.title, assigneeId: row.assigned_to_id })
        if (!answer) return
        pushAs = answer
      }
      setBusy(true)
      try {
        await post(CONTROLS.start.path(depId), startBody('run', pushAs))
        reload()
      } catch (err) {
        setActionError(apiDetail(err, 'Failed to re-run task'))
      } finally {
        setBusy(false)
      }
    },
    [reload, acting, ask],
  )

  return { busy, setBusy, actionError, setActionError, rerun, pushDialog }
}

/* A prerequisite stops gating only once it is done AND its PR has left the merge lane — the dispatch gate's own
   rule (`routes/tasks/dispatch.py::_has_unmet_prerequisite`), so ✓ never shows on a row that still blocks. */
function isSatisfied(dep: TaskDepRef): boolean {
  return dep.done && !dep.awaiting_merge
}

const MERGING_TITLE = 'Done; PR not merged yet'

/* ── ONE DEPENDENCY ROW, as a `ui/feed/FeedList` row ──
 *
 * Both halves render through this one spec, differing only by the declared `satisfied`. The state word
 * is the MARK (`statusForState`). The ✓/● bullet is a MARKER because satisfied is NOT the dep's state.
 * The `stale`/`unsaved` chips share ONE slot (mutually exclusive). Every verb survives twice — inline
 * control AND ⋯ item, same callback. */
function depRowSpec({
  dep,
  satisfied,
  busy,
  readOnly,
  canRerun,
  rerunBlockedReason,
  onRerun,
  onRemove,
  unsaved,
}: {
  dep: TaskDepRef
  /** Prerequisite half only: whether this edge still gates dispatch. `undefined` on a dependent row. */
  satisfied?: boolean
  busy: boolean
  // Withholds the Re-run control (`start` is `tasks:edit`); the stale FLAG stays for everyone.
  readOnly: boolean
  // A narrower, separate refusal: a `tasks:create` principal may add/remove edges on their own draft but
  // may not `start`. `readOnly` answers for edges, `canRerun` for Re-run (`hooks/useTaskAccess.ts` is the
  // single home for the rule).
  canRerun: boolean
  /** Re-run POSTs `start`: offered but DISABLED with this reason (a pending org — `lib/orgLifecycle.ts`). */
  rerunBlockedReason?: string
  onRerun: (id: number) => void
  /** Absent withholds the ✕ (a read-only panel, or the dependents half). */
  onRemove?: () => void
  unsaved?: boolean
}): FeedRowSpec {
  const name = displayTitle(dep.title, { showPersona: true })
  const status = statusForState(dep.state)
  // Gated on the transition table via `can(dep, 'start')`. A stale dep the table refuses a `start` from
  // (e.g. `active`) still shows the flag.
  const rerunnable = Boolean(dep.stale_reason) && !readOnly && canRerun && can(dep, 'start')
  const rerunLabel = `Re-run ${name}`
  const removeLabel = `Remove prerequisite ${dep.id}`

  const actions: OverflowItem[] = []
  if (rerunnable) {
    actions.push({
      key: 'rerun',
      icon: '⟳',
      label: 'Re-run',
      ariaLabel: rerunLabel,
      ...(rerunBlockedReason ? { disabled: true, disabledReason: rerunBlockedReason } : {}),
      onClick: () => onRerun(dep.id),
    })
  }
  if (onRemove) {
    actions.push({
      key: 'remove',
      icon: '✕',
      label: 'Remove',
      ariaLabel: removeLabel,
      danger: true,
      onClick: onRemove,
    })
  }

  return {
    key: unsaved ? `staged-${dep.id}` : String(dep.id),
    // `testid` is refused (`ui/feed/types.ts`); the row's handle is always `feed-row`.
    data: { dep: dep.id, staged: unsaved ? 'true' : undefined },
    indicator: { tone: status.tone, glyph: status.glyph, label: status.label, title: status.title },
    /* NO `href`: inside `TaskEditForm`'s `<form>` following a link would discard what is typed. */
    /* `#<id>` beside the disc, not `task #<id>`: the list is headed by what the rows are, and a 96px lead
       pushed a short id far right of the disc while the title truncated. 44px holds `#12345`. */
    lead: { text: `#${dep.id}`, width: 44, title: taskLabel(dep) },
    title: name,
    titleText: name,
    /* THE WHOLE ROW IN ONE SENTENCE, which makes the disc and ✓ marker decorative (`ui/feed/types.ts`). */
    ariaLabel: [
      taskLabel(dep),
      name,
      stateLabel(dep.state),
      satisfied === undefined ? null : satisfied ? 'satisfied' : 'not yet satisfied',
      dep.stale_reason ? `stale: ${dep.stale_reason}` : null,
      dep.awaiting_merge ? 'PR merging' : null,
      unsaved ? 'not saved yet' : null,
    ].filter(Boolean).join(' — '),
    markers: satisfied === undefined
      ? undefined
      : [{
          key: 'satisfied',
          glyph: satisfied ? '✓' : '●',
          label: satisfied ? 'satisfied' : 'not yet satisfied',
        }],
    busy,
    slots: [
      {
        key: 'flag',
        width: 82,
        align: 'right',
        node: dep.stale_reason ? (
          // Keeps the `role="status"` semantics; `StatusChip` takes no `role` prop.
          <Box component="span" role="status">
            <StatusChip tone="warning" glyph="🔶" label="stale" title={dep.stale_reason} />
          </Box>
        ) : dep.awaiting_merge ? (
          <Box component="span" role="status">
            <StatusChip tone="info" glyph="⇢" label="merging" title={MERGING_TITLE} />
          </Box>
        ) : unsaved ? (
          <Box component="span" role="status">
            <StatusChip
              tone="info"
              glyph="✎"
              label="unsaved"
            />
          </Box>
        ) : null,
      },
      {
        key: 'rerun',
        width: 30,
        align: 'center',
        /* Gated off the phone row; the ⋯ item carries it. */
        gate: 'id',
        node: rerunnable ? (
          <DisabledReason reason={rerunBlockedReason}>
            <IconButton
              // An untyped button inside TaskEditForm's `<form>` submits it.
              type="button"
              aria-label={rerunLabel}
              title={rerunBlockedReason ?? 'Re-run'}
              disabled={busy || rerunBlockedReason !== undefined}
              onClick={() => onRerun(dep.id)}
              sx={ROW_ICON_SX}
            >
              <Box component="span" aria-hidden="true">{busy ? '…' : '⟳'}</Box>
            </IconButton>
          </DisabledReason>
        ) : null,
      },
      {
        key: 'remove',
        width: 30,
        align: 'center',
        /* `compact` RATHER THAN A GATE: the ✕ is the edit this panel exists for; the ⋯ carries it too. */
        compact: true,
        node: onRemove ? (
          <IconButton
            // An untyped button inside TaskEditForm's `<form>` submits it.
            type="button"
            aria-label={removeLabel}
            title="Remove"
            disabled={busy}
            onClick={onRemove}
            sx={ROW_ICON_SX}
          >
            <Box component="span" aria-hidden="true">✕</Box>
          </IconButton>
        ) : null,
      },
    ],
    actions,
  }
}

/* A slot no row of the LIST fills is dropped, not reserved: a plain list's titles take the width. Decided
   per list, never per row, so every row keeps one slot set and the columns stay aligned
   (`ui/feed/types.ts`'s FeedSlot). */
function dropUnusedSlots(rows: FeedRowSpec[]): FeedRowSpec[] {
  const used = new Set(rows.flatMap(row => (row.slots ?? []).filter(slot => slot.node !== null).map(slot => slot.key)))
  return rows.map(row => ({ ...row, slots: row.slots?.filter(slot => used.has(slot.key)) }))
}

// `TaskDependents`' props (the prerequisite half has `PrereqPanelProps`).
type DepPanelProps = {
  taskId: number
  /* Every mutation declares `tasks:edit` while the view is `tasks:view`, so a read-only principal keeps
     the whole graph and loses only the controls. A PROP, not `useCan()`, so tests need no permission
     mock. */
  readOnly?: boolean
  /* The Re-run's own gate (`POST …/start` has no `tasks:create` fallback; `hooks/useTaskAccess.ts`).
     DEFAULTS TRUE.
     RESIDUAL GAP: the ✕ and the add lookup gate on `readOnly` alone, but both routes also write-check the
     FAR END of the edge, so a `tasks:create` principal's ✕ on a prerequisite they do not own answers 404.
     Closing it needs a per-row writability field from the server. */
  canRerun?: boolean
}

// The read-only prerequisite list: bare `ul`/`li`, body2, one line per edge that wraps rather than truncates.
const PLAIN_LIST_SX = { m: 0, p: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: '0.25rem' } as const
const PLAIN_ROW_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: '0.5rem', typography: 'body2' } as const

/* ── STAGED PREREQUISITE EDITS ──
   Adds and removes are STAGED and written only when the host form's Save/Go runs, in BOTH modes: create
   has no id to hang an edge on, and edit mode's Cancel leaves the graph untouched.
   THE DRAFT IS A DIFF against the fetched view: `added` (unwritten picks), `removed` (✕'d declared
   edges). An id is in at most ONE — re-adding a staged removal drops it from `removed`. */
/* A staged pick, shaped as a declared `TaskDepRef` so it renders through `depRowSpec` (differing only by
   `unsaved`). Built from the candidate the popup offered. `done` is always false and `stale_reason`
   absent, so no Re-run. */
type StagedPrereq = TaskDepRef

type PrereqDraft = { added: StagedPrereq[]; removed: number[] }

const EMPTY_DRAFT: PrereqDraft = { added: [], removed: [] }

/** What the host form holds to flush the draft when it saves — see `TaskEditForm`'s `prereqRef`. */
export type PrereqCommitHandle = {
  /** Write the staged adds/removes against `taskId`. Resolves `''` when fully written (an empty draft
   *  included), else the server's refusal. What failed STAYS staged, so Save retries the remainder. */
  commit: (taskId: number) => Promise<string>
}

/* The candidate page: bounds ONE popup (the server matches per keystroke), far inside the route's
   clamp. */
const CANDIDATE_LIMIT = 50

/** One frozen empty array: `setSiblings` on an identical reference is a React bail-out. */
const NO_SIBLINGS: readonly PipelineTask[] = []

/** One offerable row. The `task #<id>` identifier matches every other task surface and is what the
 *  sibling filter matches against. */
function candidateOption(task: PipelineTask): LookupOption {
  return {
    key: String(task.id),
    label: `${taskLabel(task)} · ${displayTitle(task.title, { showPersona: true })}`,
    hint: stateLabel(task.state),
  }
}

/** The same candidate as the row a staged pick renders as — see `StagedPrereq`. */
function candidateRow(task: PipelineTask): TaskDepRef {
  return { id: task.id, title: task.title, state: task.state, done: false }
}

type PrereqPanelProps = {
  /** `null` on the create surface: no view to fetch, no id to write against — the draft is the panel. */
  taskId: number | null
  /* The parent epic, when this is a subtask: siblings of that epic are offerable too (operator decision).
     An edge onto another parent's subtask is a plain top-level miss; one onto THIS task's subtask is a
     deadlock (`_gate_would_deadlock`). */
  parentTaskId?: number | null
  readOnly?: boolean
  /** The Re-run's own gate; see `DepPanelProps.canRerun`. `TaskEditForm` passes its `canStart`. */
  canRerun?: boolean
  /** Whether anything is staged, so the host's unsaved-work gate covers this panel. */
  onStagedChange?: (staged: boolean) => void
  /** Where the panel publishes its `commit`, for the host's submit to call once it has an id. */
  handleRef?: MutableRefObject<PrereqCommitHandle | null>
}

/**
 * The prerequisite ("blocked by") half, shown as "Depends on". Inside `TaskEditForm`'s `<form>`, so
 * every control is `type="button"`; `ui/AsyncLookup` guarantees Enter never submits.
 */
export function TaskDependsOn({
  taskId,
  parentTaskId = null,
  readOnly = false,
  canRerun = true,
  onStagedChange,
  handleRef,
}: PrereqPanelProps) {
  const { data: deps, error, reload } = useTaskDependencies(taskId)
  const { busy, setBusy, actionError, setActionError, rerun, pushDialog } = useRerun(reload)
  const rerunBlockedReason = useAwaitingApprovalReason()
  const [draft, setDraft] = useState<PrereqDraft>(EMPTY_DRAFT)

  const staged = draft.added.length > 0 || draft.removed.length > 0
  useEffect(() => { onStagedChange?.(staged) }, [staged, onStagedChange])

  /* WHAT THE LOOKUP MUST NOT OFFER: this task, its PARENT EPIC, declared edges (minus staged removals,
     pickable again), and staged picks. `done`/archived are excluded at the WIRE.
     THE PARENT EPIC IS THE ONE THE SERVER WILL NOT CATCH: `_gate_would_deadlock` walks downward and
     `_would_create_cycle` never sees the parent link, yet the edge deadlocks permanently (the epic's
     state is its children's rollup). */
  const declaredIds = useMemo(
    () => new Set((deps?.prerequisites ?? []).map(p => p.id)),
    [deps],
  )
  const excludedIds = useMemo(() => {
    const out = new Set<number>()
    if (taskId !== null) out.add(taskId)
    if (parentTaskId !== null) out.add(parentTaskId)
    for (const id of declaredIds) if (!draft.removed.includes(id)) out.add(id)
    for (const row of draft.added) out.add(row.id)
    return out
  }, [taskId, parentTaskId, declaredIds, draft])

  /* THE SIBLING SET, FETCHED ONCE PER PARENT: `?parent_id=` is the list route's unbounded branch (no `q`,
     no `limit`), so per-keystroke refetches return the same rows. State, not a ref, so an arrival
     re-renders the open popup. A FAILURE IS SILENT: the offer degrades to the top-level page, and
     `ui/AsyncLookup` owns the query's error line. */
  const [siblings, setSiblings] = useState<readonly PipelineTask[]>(NO_SIBLINGS)
  useEffect(() => {
    if (parentTaskId === null) {
      setSiblings(NO_SIBLINGS)
      return
    }
    const controller = new AbortController()
    get<PipelineTask[]>(`/api/tasks?parent_id=${parentTaskId}`, { signal: controller.signal })
      .then(rows => {
        if (controller.signal.aborted) return
        setSiblings(Array.isArray(rows) ? rows : NO_SIBLINGS)
      })
      .catch(() => { /* see above — the offer degrades, it does not report */ })
    return () => controller.abort()
  }, [parentTaskId])

  /* Read from refs at request time, so a pick or sibling arrival mid-flight cannot change what the
     in-flight request filters. */
  const excludedRef = useRef(excludedIds)
  excludedRef.current = excludedIds
  const siblingsRef = useRef(siblings)
  siblingsRef.current = siblings

  /* WHAT THE POPUP LAST OFFERED, by id — `LookupOption` cannot render a row, so `addPick` reads it back
     here. GROWS across queries: MUI can hand back an option from a replaced list. */
  const offeredRef = useRef(new Map<number, TaskDepRef>())

  /* ONE FETCHER, TWO SOURCES: `GET /api/tasks` applies `q`, `exclude_done`, the hidden-system and
     archived exclusions on its TOP-LEVEL branch only, so siblings are filtered here against the same
     rules. Both match a number (`_task_number_term` is the single home for which terms do). */
  const fetchCandidates = async (query: string, signal: AbortSignal): Promise<LookupOption[]> => {
    const term = query.trim()
    const search = term === '' ? '' : `&q=${encodeURIComponent(term)}`
    const top = await get<PipelineTask[]>(
      `/api/tasks?limit=${CANDIDATE_LIMIT}&exclude_done=true${search}`,
      { signal },
    )
    const excluded = excludedRef.current
    const needle = term.toLowerCase()
    /* SIBLINGS FIRST, FOR CORRECTNESS: the top-level page alone can fill `CANDIDATE_LIMIT`, which would
       silently drop the sibling class for every query. */
    const rows: LookupOption[] = []
    for (const sibling of siblingsRef.current) {
      if (sibling.state === 'done' || sibling.hidden || sibling.archived_at) continue
      if (excluded.has(sibling.id)) continue
      const option = candidateOption(sibling)
      /* MATCHED ON THE VISIBLE LABEL, not the stored title: `displayTitle` rewrites `[architecture]` to
         "Design — …". The label carries the id too. A sibling findable only by its BODY is unreachable
         (`TaskListOut` omits it). */
      if (needle !== '' && !option.label.toLowerCase().includes(needle)) continue
      offeredRef.current.set(sibling.id, candidateRow(sibling))
      rows.push(option)
    }
    // The server already matched these, possibly on a field the row does not render; no re-filter.
    for (const row of Array.isArray(top) ? top : []) {
      if (excluded.has(row.id)) continue
      offeredRef.current.set(row.id, candidateRow(row))
      rows.push(candidateOption(row))
    }
    return rows.slice(0, CANDIDATE_LIMIT)
  }

  /* THE PICK IS THE WHOLE INTERACTION: it lands in the draft and the form's Save commits it. `rows` rides
     along so a staged row renders with no refetch. */
  const addPick = useCallback((option: LookupOption | null) => {
    if (option === null) return
    const id = Number(option.key)
    if (!Number.isFinite(id)) return
    setActionError('')
    setDraft(prev => {
      // A staged removal is simply un-staged. Declared or already staged: no-op (no duplicate).
      if (prev.removed.includes(id)) {
        return { ...prev, removed: prev.removed.filter(d => d !== id) }
      }
      if (declaredIds.has(id) || prev.added.some(r => r.id === id)) return prev
      const row = offeredRef.current.get(id)
      // Only a row the fetcher offered can be staged (otherwise there is no title to show).
      if (row === undefined) return prev
      return { ...prev, added: [...prev.added, row] }
    })
  }, [declaredIds, setActionError])

  /** ✕ on a DECLARED edge: stage the removal. */
  const stageRemoval = useCallback((depId: number) => {
    setActionError('')
    setDraft(prev => (prev.removed.includes(depId)
      ? prev
      : { added: prev.added.filter(r => r.id !== depId), removed: [...prev.removed, depId] }))
  }, [setActionError])

  /** ✕ on a STAGED pick: drop it. */
  const dropStaged = useCallback((depId: number) => {
    setActionError('')
    setDraft(prev => ({ ...prev, added: prev.added.filter(r => r.id !== depId) }))
  }, [setActionError])

  /* THE FLUSH, published via `handleRef`. REMOVES RUN FIRST: dropping an edge can make a staged add
     legal that would otherwise close a cycle. Landed writes leave the draft; refused ones stay. The
     FIRST refusal is returned. */
  async function commit(id: number): Promise<string> {
    if (draft.added.length === 0 && draft.removed.length === 0) return ''
    let failure = ''
    const removedOk: number[] = []
    const addedOk: number[] = []
    setBusy(true)
    try {
      for (const depId of draft.removed) {
        try {
          await del(`/api/tasks/${id}/dependencies/${depId}`)
          removedOk.push(depId)
        } catch (err) {
          failure = failure || apiDetail(err, `Failed to remove prerequisite ${depId}`)
        }
      }
      for (const row of draft.added) {
        try {
          await post<TaskDeps>(`/api/tasks/${id}/dependencies`, { depends_on_task_id: row.id })
          addedOk.push(row.id)
        } catch (err) {
          // A self-edge / cycle / deadlock is a 422 naming the reason.
          failure = failure || apiDetail(err, `Failed to add prerequisite ${row.id}`)
        }
      }
    } finally {
      setBusy(false)
    }
    if (removedOk.length > 0 || addedOk.length > 0) {
      setDraft(prev => ({
        added: prev.added.filter(r => !addedOk.includes(r.id)),
        removed: prev.removed.filter(d => !removedOk.includes(d)),
      }))
      reload()
    }
    /* RETURNED, never painted here: the host form reports it (`TaskEditForm`'s `flushPrereqs`), since
       this panel's disclosure is collapsed by default. */
    return failure
  }

  /* Re-registered every render (as `TaskEditForm`'s `submitRef`) so it closes over the fresh `draft`;
     cleared on unmount. */
  useEffect(() => {
    if (!handleRef) return
    handleRef.current = { commit }
    return () => { handleRef.current = null }
  })

  if (error) return <Banner severity="error">{error}</Banner>
  // Edit mode not yet loaded (or shape-rejected): render nothing. CREATE renders on the draft alone.
  if (!deps && taskId !== null) return null

  const declared = (deps?.prerequisites ?? []).filter(p => !draft.removed.includes(p.id))
  // Only the read-only path consults this (the editable path always has its lookup). `taskId !== null`:
  // a task that does not exist has no graph to report.
  const nothingToShow = taskId !== null
    && declared.length === 0
    && (deps?.unmet_prerequisite_ids.length ?? 0) === 0
    && (deps?.hidden_unmet_prerequisite_count ?? 0) === 0

  return (
    <Box sx={PANEL_SX}>
      {pushDialog}
      <Typography variant="body2" sx={HEADING_SX}>
        Depends on
      </Typography>

      {/* NO blocked-by banner here — it lives in `TaskDependents`: this disclosure is collapsed by
          default and `waiting_on_kind` covers only an epic's children. */}

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: '0.375rem' }}>
        {/* No sub-label: the panel heading is the label. */}
        {/* READ-ONLY (another org's row): a plain list of links, not feed rows — no ✕, Re-run, ⋯ or row
            hover, since none of them can act. A link is safe here: there is no typed work to discard. The stale
            flag is a fact, so it stays. */}
        {readOnly && declared.length > 0 && (
          <Box component="ul" aria-label="Prerequisites" data-testid="task-prerequisites-list" sx={PLAIN_LIST_SX}>
            {declared.map(pre => (
              <Box component="li" key={pre.id} sx={PLAIN_ROW_SX}>
                <TaskLink taskId={pre.id} />
                <Box component="span" sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                  {displayTitle(pre.title, { showPersona: true })}
                </Box>
                <Box component="span" sx={{ color: 'text.secondary' }}>
                  · {stateLabel(pre.state)}{pre.awaiting_merge ? ' · PR merging' : ''}
                </Box>
                {pre.stale_reason && (
                  <Box component="span" role="status">
                    <StatusChip tone="warning" glyph="🔶" label="stale" title={pre.stale_reason} />
                  </Box>
                )}
              </Box>
            ))}
          </Box>
        )}
        {!readOnly && (declared.length > 0 || draft.added.length > 0) && (
          <FeedList
            ariaLabel="Prerequisites"
            testId="task-prerequisites-list"
            sections={[{
              key: 'prerequisites',
              rows: dropUnusedSlots([
                ...declared.map(pre => depRowSpec({
                  dep: pre,
                  satisfied: isSatisfied(pre),
                  busy,
                  readOnly,
                  canRerun,
                  rerunBlockedReason,
                  onRerun: id => void rerun(id),
                  onRemove: readOnly ? undefined : () => stageRemoval(pre.id),
                })),
                /* THE STAGED PICKS, same builder; `unsaved` is the whole difference. The disc shows
                   the popup's snapshot state. */
                ...draft.added.map(row => depRowSpec({
                  dep: row,
                  satisfied: isSatisfied(row),
                  busy,
                  readOnly,
                  canRerun,
                  rerunBlockedReason,
                  onRerun: id => void rerun(id),
                  onRemove: readOnly ? undefined : () => dropStaged(row.id),
                  unsaved: true,
                })),
              ]),
            }]}
          />
        )}

        {/* READ-ONLY: no lookup, and one plain line only when the panel would otherwise be an empty
            heading (indistinguishable from a failed load). */}
        {readOnly ? (
          nothingToShow && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              No prerequisites.
            </Typography>
          )
        ) : (
          /* THE LOOKUP IS ALWAYS THERE: focus offers the first page, typing filters server-side, a pick
             stages. `value={null}` makes it repeatable — `ui/AsyncLookup` empties its box for a
             null-valued caller (its own pinned contract), so no remount steals focus. */
          <AsyncLookup
            id="task-prerequisite-lookup"
            testId="prerequisite-lookup"
            value={null}
            onChange={addPick}
            fetchOptions={fetchCandidates}
            disabled={busy}
            /* LABELLED AND FULL-WIDTH: MUI pins the popper to the input's width, so a content-sized box
               truncated every suggestion to `task ... [`. A label survives typing; a placeholder does
               not. */
            label="Add a prerequisite"
            /* THE PLACEHOLDER NAMES THE NUMBER FIRST — operators come with a task number in hand. Both
               match (`list_tasks`' `?q=`, `_task_number_term`). A DONE task is offered by neither
               (`exclude_done=true`). */
            placeholder="Type a task number or title…"
            ariaLabel="Add a prerequisite task"
            // The empty query IS the first page: a click shows what is available.
            fetchOnFocus
            noOptionsText="No matching task"
          />
        )}
        {actionError && <Banner severity="error">{actionError}</Banner>}
      </Box>
    </Box>
  )
}

/**
 * The reverse half — what this task blocks — shown as "Blocks" on the Details tab. No add/remove; the
 * only write is Re-run, withheld by `readOnly` and separately by `canRerun`.
 */
export function TaskDependents({ taskId, readOnly = false, canRerun = true }: DepPanelProps) {
  const { data: deps, error, reload } = useTaskDependencies(taskId)
  const { busy, actionError, rerun, pushDialog } = useRerun(reload)
  const rerunBlockedReason = useAwaitingApprovalReason()

  if (error) return <Banner severity="error">{error}</Banner>
  // Not yet loaded (or shape-rejected): render nothing.
  if (!deps) return null

  // The visible list and the hidden count are independent: a task whose dependents are all invisible
  // still reports them. The "nothing" line shows only when neither has anything.
  const hiddenDependents = deps.hidden_dependent_count ?? 0
  const nothingToShow = deps.dependents.length === 0 && hiddenDependents === 0

  /* The blocked-by set: `unmet_prerequisite_ids` is object-scoped, the hidden count is the remainder,
     and dispatch-readiness is over BOTH. */
  const unmetCount = deps.unmet_prerequisite_ids.length
  const hiddenUnmet = deps.hidden_unmet_prerequisite_count ?? 0
  const totalUnmet = unmetCount + hiddenUnmet

  return (
    <Box sx={PANEL_SX}>
      {pushDialog}
      {/* ⛔ BLOCKED BY — the forward edge's warning, homed here because this is the always-visible
          dependency surface and already holds the counts. */}
      {totalUnmet > 0 && (
        /* `Alert` DIRECTLY, not `ui/Banner`: MUI's Alert defaults to `role="alert"` (assertive) and a
           `role="status"` wrapper does not demote it; Banner takes no `role` and is frozen. */
        <Alert severity="warning" variant="outlined" role="status" data-testid="warning-banner" sx={ALERT_SX}>
          {/* Three shapes: all-visible, visible+hidden mix, all-hidden (the banner is the only signal). The facts only:
              the state chip already says the task waits. */}
          {unmetCount > 0 ? (
            <>
              {/* NAMES EACH VISIBLE BLOCKER as a link: a count alone sent the reader hunting.
                  The title comes off `prerequisites`; an id it lacks still links, untitled. */}
              ⛔ Blocked by{' '}
              {deps.unmet_prerequisite_ids.map((id, i) => {
                const title = deps.prerequisites.find(p => p.id === id)?.title
                return (
                  <Box component="span" key={id} sx={{ overflowWrap: 'anywhere' }}>
                    {i > 0 && ', '}
                    <TaskLink taskId={id} testId={`blocked-by-${id}`} />
                    {title && ` ${displayTitle(title, { showPersona: true })}`}
                  </Box>
                )
              })}
              {hiddenUnmet > 0 && ` (+${hiddenUnmet} not visible to you)`}
            </>
          ) : (
            <>⛔ Blocked by {hiddenUnmet} prerequisite{hiddenUnmet === 1 ? '' : 's'} not visible to you</>
          )}
        </Alert>
      )}

      <Typography variant="body2" sx={HEADING_SX}>
        Blocks
      </Typography>

      {deps.dependents.length > 0 && (
        /* No ✓/● marker and no ✕: a dependent is not a gate on this task. Left off, not passed false. */
        <FeedList
          ariaLabel="Dependents"
          testId="task-dependents-list"
          sections={[{ key: 'dependents', rows: dropUnusedSlots(deps.dependents.map(d => depRowSpec({
            dep: d,
            busy,
            readOnly,
            canRerun,
            rerunBlockedReason,
            onRerun: id => void rerun(id),
          }))) }]}
        />
      )}
      {hiddenDependents > 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          + {hiddenDependents} dependent{hiddenDependents === 1 ? '' : 's'} not visible to you
        </Typography>
      )}
      {/* No Add button implies "none" here, so the empty line is said to everyone. */}
      {nothingToShow && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Nothing depends on this task.
        </Typography>
      )}
      {actionError && <Banner severity="error">{actionError}</Banner>}
    </Box>
  )
}
