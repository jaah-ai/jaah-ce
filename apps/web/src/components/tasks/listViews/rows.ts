/* One derivation of what a list-view style needs to know about a task row. Every style in `./types.ts`
 * reads the same `ViewRow`, so switching styles changes layout, never data. State word/tone/glyph come
 * from `shared.tsx`, never forked here.
 */
import { startOfZonedDay, startOfZonedWeek } from '../../../lib/zonedDay'
// The ratability predicate from its single home.
import { isRatableTask } from '../../../lib/taskRating'
import { compactAge } from '../../../lib/relativeTime'
import type { OrgRef, PipelineTask } from '../../../types'
import type { Tone } from '../../../ui/StatusChip'
import {
  LIVE_STATES,
  STATE_GLYPH,
  STATE_LABELS,
  STATE_TITLES,
  STATE_TONE,
  answerHref,
  displayTitle,
  isHiddenSystemRow,
  taskStatus,
} from '../shared'
import { taskRoleLabel } from '../personaRegistry'

export type ViewRow = {
  task: PipelineTask
  id: number
  /** Which tenant owns this row (`types.ts`'s `OrgRef`; drawn only by `ui/OrgIndicator.tsx`). Carried
   *  here so no style reads `row.task.org` and forgets the null case. A straight projection, no gate:
   *  `null` is real (a row created under the OPERATOR sentinel) and is never defaulted — a wrong tenant is
   *  worse than a blank. */
  org: OrgRef | null
  /** The persona prefix already stripped. */
  title: string
  href: string
  /** The `?` indicator's target, or null for exactly the rows `stateLabel` does not read as "Asking".
   *  Separate from `href` (the title opens the row's own task); on a container it points at the subtask
   *  holding the gate. `shared.tsx`'s `answerHref` owns that choice and the `?tab=answer` link. */
  answerHref: string | null
  state: string
  /** State word, tone and glyph from the shared vocabulary, off `state` plus the two refinements below. */
  stateLabel: string
  tone: Tone
  glyph: string
  /** What the state means (`TASK_STATUS`'s sentence), from the same refined status as `stateLabel`;
   *  falls back to the word. */
  stateTitle: string
  /** A runner is on it right now: `state === 'active'` only. Not `LIVE_STATES`, which is the
   *  delete/archive safety set and would pulse a third of the board and file stale rows under "Running
   *  now". */
  live: boolean
  /** Not settled (`LIVE_STATES`): "still in the pipeline" rather than "executing". */
  inFlight: boolean
  done: boolean
  awaiting: boolean
  failed: boolean
  /** An epic, whose progress is its children's. */
  epic: boolean
  childCount: number
  doneChildCount: number
  /** 0–100 for an epic, `null` for a leaf. */
  pct: number | null
  persona: string
  /** The age slot's figure (`compactAge`): time since the RUN started on an `active` row that says when
   *  (`run_started_at` — the deleted Queue style's running age), else since `updated_at`. */
  age: string
  /** The age slot's hover: `Running 12m` on a run-aged row, else `null` (the slot carries no hover). */
  ageTitle: string | null
  /** Sort/bucket key: parsed `updated_at`, or 0 when unparseable. */
  updatedMs: number
  tags: string[]
  system: boolean
  /** `archived_at` is set, nothing else. Not `isArchiveRow`, which asks whether the cleanup verb reads
   *  Archive and is true for every settled row (it stamped 🗄 on every done task). */
  archived: boolean
  /** The creator's resolved display name (never an id; `types.ts`'s `Provenance`), or `null`. Rendered as
   *  initials plus a hue (`lib/authorBadge.ts`), with the full name on hover and as the accessible name. */
  createdBy: string | null
  /** The operator's five-star verdict: mean and vote count (`types.ts`'s `rating_average`/`rating_count`).
   *  Ratability is folded in — no `ratable` flag: a row not `done`, or a pipeline child (not top-level,
   *  epic or plannable — `lib/taskRating.ts`), reads as unrated (`null`, `0`). A separate flag could be
   *  forgotten and draw a rating on a `coder` child; folded, a style needs no predicate beside the badge.
   *  The count is the presence test, never the average (absence is not a state: an unrated row has no
   *  average to read); `RatingBadge` draws nothing at `count === 0`. */
  ratingAverage: number | null
  ratingCount: number
  /** What this row cost: the wire fields as the shape every cost helper takes (`types.ts`'s
   *  `cost_usd`/`cost_status` block owns the meaning; it is the rolled-up subtree cost). No gate, unlike
   *  the rating: a pipeline child that spent money shows it. One field holding the pair so styles hand it
   *  to `lib/taskCost.ts`'s `hasCost`/`costLabel`/`costTitle`/`CostBadge` rather than testing a field
   *  alone. Carried, never pre-rendered: rounding is `lib/taskCost.ts`'s single home. */
  cost: Pick<PipelineTask, 'cost_usd' | 'cost_status' | 'cost_agent_reported'>
  /** What this row spent in tokens: the wire field as the shape every token helper takes (`types.ts`'s
   *  `tokens_total` owns the meaning; it is the rolled-up subtree sum the cost is priced from). No gate, like
   *  the cost. Carried, never pre-rendered: the compact figure and the exact-count sentence are
   *  `lib/taskTokens.ts`'s single home (`hasTokens`/`tokensLabel`/`tokensTitle`). */
  tokens: Pick<PipelineTask, 'tokens_total'>
}

const AWAITING_STATES = new Set(['awaiting_response'])
const FAILED_STATES = new Set(['system_failed'])

/* The state word, hoisted because the hover sentence falls back to it. */
function stateLabel(task: PipelineTask, refined: { label: string } | null): string {
  return refined?.label ?? STATE_LABELS[task.state] ?? task.state.replace(/_/g, ' ')
}

/** `parent`: a CHILD row's epic, whose `transition_mode` and unmet prerequisites gate it (`taskStatus`'s
 *  options). Passed by a host listing one epic's children (`../SubtaskList.tsx`). */
export function toViewRow(task: PipelineTask, parent?: PipelineTask): ViewRow {
  const childCount = task.child_count ?? 0
  const doneChildCount = task.done_child_count ?? 0
  const updatedMs = Date.parse(task.updated_at)
  /* The done gate, read twice below. */
  const done = task.state === 'done'
  /* The run's start, read only while it runs: a finished row's age stays its last move. */
  const runStart = task.state === 'active' ? task.run_started_at ?? null : null
  /* Asked of `lib/taskRating.ts`'s `isRatableTask`, the single home of the rule; never re-spelled here. */
  const ratable = isRatableTask(task)
  /* Only these refinements from `taskStatus` are taken, deliberately:
  Delivering: a warm-answered gate stays `awaiting_response` until the node acks, and must not read
  "Asking" anywhere.
  Asking: an epic with a parked subtask carries `gated_child_id` while its `state` stays `active`; the
  `?` must show in every view.
  With a `parent`, its gating is taken too (Queued → Held/Waiting): a `ready` child under a held epic must
  not read Queued.
  Starting: claimed or hand-Run but no runner session yet (Queued/Running → Starting), so a launch reads as one. */
  const status = parent
    ? taskStatus(task, {
        parentTransitionMode: parent.transition_mode,
        parentUnmetPrerequisites: parent.unmet_prerequisite_count,
      })
    : taskStatus(task)
  /* A third: the org's hold on a ready row ("Waiting", its sentence as the hover), since nothing else
  explains why a queued row never starts. */
  const orgHold = status.key === 'waiting' && !!task.wait_reason
  const parentGate = parent !== undefined && (status.key === 'held' || status.key === 'waiting')
  /* A fourth: the row's own prerequisites, the dispatch gate's own count (a merging one included), so a blocked row
  never reads Queued. */
  const ownGate = status.key === 'waiting' && (task.unmet_prerequisite_count ?? 0) > 0
  const refined = status.key === 'delivering' || status.key === 'asking' || status.key === 'starting' || orgHold
    || parentGate || ownGate
    ? status
    : null
  return {
    task,
    id: task.id,
    // Verbatim; no default org stands in for null.
    org: task.org ?? null,
    title: displayTitle(task.title),
    href: `/tasks/${task.id}`,
    answerHref: answerHref(task),
    state: task.state,
    stateLabel: stateLabel(task, refined),
    tone: refined?.tone ?? STATE_TONE[task.state] ?? 'neutral',
    glyph: refined?.glyph ?? STATE_GLYPH[task.state] ?? '●',
    // A failed or paused row hovers its reason after the word (`taskStatus`), whichever word this view draws.
    stateTitle: refined?.title
      ?? (status.key === 'failed' || status.key === 'paused' || status.key === 'queued' ? status.title : STATE_TITLES[task.state])
      ?? stateLabel(task, refined),
    live: task.state === 'active',
    inFlight: LIVE_STATES.has(task.state),
    done,
    awaiting: AWAITING_STATES.has(task.state),
    failed: FAILED_STATES.has(task.state),
    epic: childCount > 0,
    childCount,
    doneChildCount,
    pct: childCount > 0 ? Math.round((doneChildCount / childCount) * 100) : null,
    persona: taskRoleLabel(task),
    age: compactAge(runStart ?? task.updated_at),
    ageTitle: runStart ? `Running ${compactAge(runStart)}` : null,
    updatedMs: Number.isNaN(updatedMs) ? 0 : updatedMs,
    tags: (task.tags ?? []).map(tag => tag.name),
    system: isHiddenSystemRow(task),
    // `archived_at` itself, never the cleanup-verb predicate.
    archived: task.archived_at != null,
    createdBy: task.created_by ?? null,
    // Both through `ratable` (see the fields' note).
    ratingAverage: ratable ? task.rating_average ?? null : null,
    ratingCount: ratable ? task.rating_count ?? 0 : 0,
    /* No gate, unlike the rating. Verbatim: `hasCost` treats absent and `null` alike, so coercing would
    claim something about the payload. */
    cost: { cost_usd: task.cost_usd, cost_status: task.cost_status, cost_agent_reported: task.cost_agent_reported },
    // Verbatim for the same reason: `hasTokens` reads absent and `null` alike as "no claim", never 0.
    tokens: { tokens_total: task.tokens_total },
  }
}

export function toViewRows(tasks: PipelineTask[]): ViewRow[] {
  return tasks.map(task => toViewRow(task))
}

/** One activity-feed heading and its rows. Empty buckets are dropped. */
export type TimeBucket = { label: string; rows: ViewRow[] }

const MINUTE = 60_000
const HOUR = 60 * MINUTE

/* The activity feed's buckets. "Today" and "This week" are calendar boundaries (midnight, and the Monday
 * opening the week), not rolling 24h/7d windows, which named the wrong day.
 * Pure in `now`; the view memoises, so `lib/officialZone::useOfficialDayStart` invalidates it at the
 * boundary. Cut in the control plane's zone (a parameter), so a shared board names the same day for
 * everyone (`lib/officialZone.ts`); `lib/zonedDay.ts` owns the arithmetic and DST.
 * "This week" is legitimately short on a Monday morning.
 */
export function bucketByRecency(
  rows: ViewRow[],
  zone: string,
  now: number = Date.now(),
  /* Whether the board is focused; the `Queued` bucket exists only then. Defaulted, so unfocused output is
  unchanged. */
  focus: boolean = false,
): TimeBucket[] {
  const buckets: TimeBucket[] = [
    { label: 'Running now', rows: [] },
    { label: 'Last hour', rows: [] },
    { label: 'Today', rows: [] },
    { label: 'This week', rows: [] },
    { label: 'Older', rows: [] },
    /* The one state-based bucket pinned last, mirroring `Running now`: on a focused board the operator
    wants queued work below actionable work. A bucket, not a sort key: this function re-sorts by
    `updatedMs` within buckets, discarding the server's `queued_last` order. Empty (and dropped) unless
    focused. */
    { label: 'Queued', rows: [] },
  ]
  /* Resolved once above the loop: each costs two `formatToParts` passes (`zonedDay.ts`). */
  const dayStart = startOfZonedDay(new Date(now), zone).getTime()
  const weekStart = startOfZonedWeek(new Date(now), zone).getTime()
  for (const row of [...rows].sort((a, b) => b.updatedMs - a.updatedMs)) {
    const age = now - row.updatedMs
    /* A queued row goes to the bottom regardless of timestamp, tested first; only when focused. */
    if (focus && row.state === 'ready') buckets[5].rows.push(row)
    // A running row goes to the top regardless of timestamp; holds because `live` means `active`.
    else if (row.live) buckets[0].rows.push(row)
    /* "Last hour" stays a rolling window: it names a duration, not a calendar period. Do not "fix" it. */
    else if (age < HOUR) buckets[1].rows.push(row)
    /* Since midnight / the week's Monday in `zone`, never `now - 24h` / `now - 7d` (see above). */
    else if (row.updatedMs >= dayStart) buckets[2].rows.push(row)
    else if (row.updatedMs >= weekStart) buckets[3].rows.push(row)
    else buckets[4].rows.push(row)
  }
  return buckets.filter(bucket => bucket.rows.length > 0)
}
