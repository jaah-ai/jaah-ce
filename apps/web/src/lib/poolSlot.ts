// The pool slot's DISPLAY vocabulary for the `/worktrees` pool panel (`components/WorktreePool.tsx`), as
// pure functions over one `Worktree` row — tested directly, since each fact has a wrong answer that looks
// right ("no probe" as "down", "never scanned" as "in sync", an ended session still aging, an unbuilt slot
// "Ready"). Same split as `lib/worktree.ts`.
//
// THE INVARIANT EVERY FUNCTION BELOW KEEPS: a NULL sensed column is "not known", never its negative.
// `stack_up === null` is not "down", `dirty === null` is not "clean", `ahead/behind === null` is not "in
// sync" (apps/api/app/routes/portfolio.py never fabricates `down`, and the pool policy holds `stack_present`
// to the same rule).
import { countOf } from './humanize'
import type { Worktree } from '../types'

/** The chip tone vocabulary, mirroring `ui/StatusChip`'s `Tone` without importing a component here. */
export type SlotTone = 'neutral' | 'info' | 'success' | 'warning' | 'error' | 'unknown'

/** One rendered status atom: the word, its tone, a shape glyph, and the sentence that explains it. */
export type SlotSignal = {
  label: string
  tone: SlotTone
  /** A shape signal beside the label, so the status survives greyscale and forced-colours mode. */
  glyph: string
  /** The full sentence — the cell's tooltip and the atom's accessible name. */
  hint: string
}

// Slot 0 is the main clone, rendered `main` (mirrors `MAIN_CLONE_NUMBER` in
// `apps/api/app/routes/pool_slots.py`). Declared by an assignment's converge, never leased to a task.
export const MAIN_CLONE_NUMBER = 0

/** The slot's number as the pool speaks it — `main` for slot 0, the bare id otherwise. */
export function slotNumberLabel(slot: Worktree): string {
  return slot.number === MAIN_CLONE_NUMBER ? 'main' : `#${slot.number}`
}

/** The slot's fully-qualified IDENTITY — `project#number`, with slot 0 spelled `main`. */
export function slotIdentity(slot: Worktree): string {
  return `${slot.project}#${slot.number === MAIN_CLONE_NUMBER ? 'main' : slot.number}`
}

/* `main` is a LABEL, not an occupancy state. `lease()` refuses `number = 0` (A9,
   `apps/api/app/slot_state.py`), so the main clone's `state` is genuinely `free` and `isHeld` needs no
   carve-out. */
export const STATE_FREE = 'free'

/** Does something HOLD this slot? Single-homed (headline rollup, group bars, Held filter, row weight).
 *  `state` is closed `free` | `task` | `user`, so held = not free. */
export function isHeld(slot: Worktree): boolean {
  return slot.state !== STATE_FREE
}

/** Held by a PERSON rather than a fleet task? Gates the Owner detail row. */
export function isUserOwned(slot: Worktree): boolean {
  return slot.state === 'user'
}

/** Is this slot held by a task waiting on a HUMAN ANSWER — the hold's REASON?
 *  The server's answer (`apps/api/app/slot_title.py::task_awaiting_answer_from_state`), never re-derived.
 *  No other row signal shows it: `session_status` says `waiting` for both this and a startup wedge, and
 *  `task_active` merges `awaiting_response` with `active` — so a slot parked on an unanswered question
 *  read as a zombie. A hold ends only on a decision (a completed, deleted or reset task, or an explicit
 *  release), so the fix is a SIGNAL, never a reaper.
 *  Lags slightly by design: the panel subscribes to `worktree`/`runner`/`account`, not `task`.
 *  Not a liveness claim: `true` with no session is a dead runner holding a question, which the CP reclaims
 *  (`task_pipeline.reclaim_stale_awaiting_response_tasks`). */
export function isAwaitingAnswer(slot: Worktree): boolean {
  return slot.task_awaiting_answer
}

/** That signal's words, single-homed across its five surfaces (Session hover, `!` marker name, row name,
 *  drawer Occupancy and `Waiting on`). Names the ACTION: the reader owes the answer. */
export const AWAITING_ANSWER_WORDS =
  'waiting on a human answer — open the task and answer it, or the slot stays held'

/** A `state === 'user'` slot owner's readable identity — never the bare uuid — with a generic fallback.
 *  `null` otherwise, so each caller picks its own "not a person" rendering. */
export function userOwnerLabel(slot: Worktree): string | null {
  if (!isUserOwned(slot)) return null
  return slot.owner_user_email ?? 'a person'
}

// The `Declared…` explanation for a NUMBERED slot, single-homed across `stateSignal`'s three surfaces.
const DECLARED_HINT = 'Declared — built on the node’s next reconcile pass'

/* The main clone's declared hint: the sentence above is FALSE for it. `_unmaterialised_candidates`
   (apps/jaahd/reconcile/) excludes `is_main`; slot 0 materialises as the CLONE RUNG of the first real
   slot, never on a reconcile pass. */
const DECLARED_MAIN_HINT =
  'Declared. Returns when a numbered slot under it is next created or leased.'

/** The two-way LIFECYCLE state (has the node built the tree?), orthogonal to occupancy (`state`). No
 *  `parked`: that is `state === 'task'` with no live session (`hasLiveSession`, `lib/worktree.ts`). */
export type PoolSlotState = 'ready' | 'declared'

export function poolSlotState(slot: Worktree): PoolSlotState {
  return slot.observed_at ? 'ready' : 'declared'
}

/** The lifecycle state as a rendered atom. `Ready` needs no explanation beyond its own word. */
export function stateSignal(slot: Worktree): SlotSignal {
  const state = poolSlotState(slot)
  if (state === 'declared') {
    return {
      label: 'Declared', tone: 'neutral', glyph: '○',
      hint: slot.number === MAIN_CLONE_NUMBER ? DECLARED_MAIN_HINT : DECLARED_HINT,
    }
  }
  return { label: 'Ready', tone: 'success', glyph: '●', hint: 'Ready.' }
}

/* The STACK atom, in three lengths (table cell ~88px, drawer chip, drawer sentence), cut from ONE switch.
   `stack_state` is derived server-side (`apps/api/app/slot_stack.py`): `none` = no stack to run, NULL =
   the probe did not answer; neither is `down`. Three independent reads once disagreed about one slot in
   one viewport (and read a new server value as "not reported recently"). */
type StackVocabulary = SlotSignal & {
  /** The pill's WORD (`Stack —` is not a readable pill), from here, never the call site. */
  chipWord: string
  /** The drawer's SENTENCE: the same state with room to say what follows from it. */
  sentence: string
}

function stackVocabulary(slot: Worktree): StackVocabulary {
  switch (slot.stack_state) {
    case 'up':
      return {
        label: 'Up',
        tone: 'success',
        glyph: '●',
        hint: 'Up.',
        chipWord: 'up',
        sentence: 'up',
      }
    case 'down':
      return {
        label: 'Down',
        tone: 'neutral',
        glyph: '○',
        hint: 'Down. Data volumes kept.',
        chipWord: 'down',
        sentence: 'down · data volumes kept',
      }
    case 'none':
      return {
        label: 'None',
        tone: 'neutral',
        glyph: '–',
        hint: 'No stack in this project.',
        chipWord: 'none',
        sentence: 'none — no stack in this project',
      }
    case null:
    case undefined:
    case '':
      // `—` in the cell, not "Unknown" (a silent node would fill the column with alarms); the same "not
      // measured" mark as Git. Chip and sentence say "not reported" (the PROBE is missing). The EMPTY STRING
      // belongs here too: the API can serialise an unset probe as `''`.
      return {
        label: '—',
        tone: 'unknown',
        glyph: '?',
        hint: 'Not reported recently.',
        chipWord: 'not reported',
        sentence: 'not reported recently',
      }
    default:
      /* A value the server has grown: PASSED THROUGH verbatim (as `sessionSignal` does), and named in the
         sentence — folding it into "not reported" would be false. `◆` not `●`: at ≤480px the glyph is the
         only rendering and `●` is `up`, so tone alone would make `restarting` look running. */
      return {
        label: slot.stack_state,
        tone: 'neutral',
        glyph: '◆',
        hint: `${slot.stack_state}.`,
        chipWord: slot.stack_state,
        sentence: `“${slot.stack_state}”`,
      }
  }
}

/** The stack as the TABLE CELL renders it, stripped to the shared `SlotSignal` shape. */
export function stackSignal(slot: Worktree): SlotSignal {
  const { label, tone, glyph, hint } = stackVocabulary(slot)
  return { label, tone, glyph, hint }
}

/** The stack as the drawer's CHIP says it — one word, to sit after `Stack `. */
export function stackChipWord(slot: Worktree): string {
  return stackVocabulary(slot).chipWord
}

/** The stack as the drawer's DETAIL ROW says it (pairs with `stackSignal` as `gitWords` with `gitSummary`). */
export function stackWords(slot: Worktree): string {
  return stackVocabulary(slot).sentence
}

/* "Is there a session here", read once. `lib/worktree.ts::hasLiveSession` owns the rule (NULL/`''` is the
   only no-session value); this adds that whitespace-only is nothing too. Shared by the Session column and
   the drawer's session age. */
function liveSessionStatus(slot: Worktree): string {
  return (slot.session_status ?? '').trim()
}

/* The SESSION atom. `session_status` is `claude agents --json`'s verbatim `status`: the WORD is passed
   through; only the TONE is ours. */
export function sessionSignal(slot: Worktree): SlotSignal {
  const status = liveSessionStatus(slot)
  if (status === '') {
    return { label: 'None', tone: 'neutral', glyph: '○', hint: 'No live session.' }
  }
  /* The one thing we know that the CLI does not: the holding task awaits an answer. The WORD
     stays the CLI's (`waiting` also covers a startup wedge); amber carries it here, and the row's one `!`
     is the marker hugging its Title (`WorktreePool.tsx`) — a second `!` here read as two alarms. */
  if (isAwaitingAnswer(slot)) {
    return {
      label: status,
      tone: 'warning',
      glyph: '●',
      hint: `${status} — ${AWAITING_ANSWER_WORDS}`,
    }
  }
  // Every other live session is `info`; the WORD carries busy vs idle (green `idle` would read "fine").
  return {
    label: status,
    tone: 'info',
    glyph: '●',
    hint: `${status}.`,
  }
}

/** The slot's divergence from its project's integration branch, as the four states it can be in. */
export type GitSummary =
  | { kind: 'unscanned'; dirty: boolean | null }
  | { kind: 'sync'; dirty: boolean | null }
  | { kind: 'diverged'; ahead: number; behind: number; dirty: boolean | null }

/* `—` (never scanned) and `✓` (in sync) are distinct kinds, each explained on hover. `dirty` rides on every
   kind (orthogonal to divergence); `null` means the probe failed, never "clean". */
export function gitSummary(slot: Worktree): GitSummary {
  /* EITHER half missing is unscanned: the columns are independently nullable, and `?? 0` would turn
     `{ahead: null, behind: 0}` into a confident `✓ in sync` (the node writes the pair atomically today —
     `apps/jaahd/reconcile/observe.py`'s `_ok(ahead_behind_ok, …)`). */
  if (slot.ahead == null || slot.behind == null) return { kind: 'unscanned', dirty: slot.dirty }
  const ahead = slot.ahead
  const behind = slot.behind
  if (ahead === 0 && behind === 0) return { kind: 'sync', dirty: slot.dirty }
  return { kind: 'diverged', ahead, behind, dirty: slot.dirty }
}

/* The same divergence in WORDS for the drawer (the `↑2 ↓1` cluster needs explaining), beside the structural
   half so they cannot drift. */
export function gitWords(slot: Worktree): string {
  const summary = gitSummary(slot)
  if (summary.kind === 'unscanned') return 'not scanned yet'
  if (summary.kind === 'sync') return 'in sync'
  return [
    summary.ahead > 0 ? `${summary.ahead} ahead` : null,
    summary.behind > 0 ? `${summary.behind} behind` : null,
  ].filter(Boolean).join(', ')
}

/* The occupancy tone: `free` success, `task` info (occupancy, not liveness), `user` neutral
   (a person's slot is ordinary; the word distinguishes it). No alarms. */
export function stateTone(state: string): SlotTone {
  switch (state) {
    case 'free':
      return 'success'
    case 'task':
      return 'info'
    case 'user':
      return 'neutral'
    default:
      return 'neutral'
  }
}

/* `protected` (types.ts) is a server-computed read, never re-derived. Its old holder-based rule is gone;
   read it only as "the server flags this slot as protected". */
export function isProtected(slot: Worktree): boolean {
  return slot.protected
}

/* The panel's one duration vocabulary (`0.4h` / `7h` / `3d`), shared by Idle and session age on ADJACENT
   drawer lines so equal durations cannot round differently. The sub-hour decimal matters for session age
   (computed here, fractional); `idle_hours` arrives floored server-side. */
function durationLabel(hours: number): string {
  if (hours < 1) return `${Math.max(0, Math.round(hours * 10) / 10)}h`
  const wholeHours = Math.round(hours)
  if (wholeHours < 48) return `${wholeHours}h`
  return `${Math.round(hours / 24)}d`
}

/* IDLE. `idle_hours` is API-computed (`apps/api/app/slot_state.py::idle_hours_expr`); NULL (no hold to
   measure from) is not "0 hours". */
export function idleLabel(slot: Worktree): string {
  const hours = slot.idle_hours
  if (hours == null) return '—'
  return durationLabel(hours)
}

/* A destroy the node has not confirmed after this long is stuck: the CP re-orders an unacknowledged destroy every
   10 minutes (`apps/api/app/lease_drain.py`'s `_DESTROY_REMINT_COOLDOWN`), so three silent orders. */
export const DESTROY_STUCK_HOURS = 0.5

/** How long a retiring slot's destroy has run, in hours; null when not retiring or the stamp is absent. */
export function retiringHours(slot: Worktree, now: number = Date.now()): number | null {
  if (!slot.retiring || !slot.retiring_since) return null
  const since = Date.parse(slot.retiring_since)
  return Number.isNaN(since) ? null : Math.max(0, (now - since) / 3_600_000)
}

/** A destroy's run time: minutes under an hour (a fresh removal read `0h`), else the panel's `7h` / `3d`. */
export function retiringLabel(hours: number): string {
  return hours < 1 ? `${Math.max(1, Math.round(hours * 60))}m` : durationLabel(hours)
}

/** A measured duration and the sentence that says what it measures — the drawer's row and its tooltip.
 *  `unreported`: a LIVE session the node gave no start time for, which the drawer says in words (the row's
 *  narrow cell keeps `—`) — a bare `—` beside `running` read as "no session". */
export type SlotDuration = { label: string; hint: string; unreported?: true }

/* How long the live session has run (from `session_started_at`), NOT Idle (from `last_active`); each
   alone reads as the other.
   Liveness decides first: both fields come from one observation (`apps/jaahd/reconcile/`,
   `app/slot_columns.py`'s `SENSED`), but nothing promises they move together, and a leftover stamp must not
   age an ended session. No session → `—`, never zero.
   `now` is a parameter so the function stays pure and testable at a fixed clock. */
export function sessionAge(slot: Worktree, now: number = Date.now()): SlotDuration {
  if (liveSessionStatus(slot) === '') {
    return { label: '—', hint: 'No live session.' }
  }
  const started = slot.session_started_at == null ? NaN : Date.parse(slot.session_started_at)
  if (Number.isNaN(started)) {
    return { label: '—', hint: 'No start time reported.', unreported: true }
  }
  // A future start is node-to-browser clock skew; `-0.3h` would read as a bug.
  return {
    label: durationLabel(Math.max(0, (now - started) / 3_600_000)),
    hint: `Since ${slot.session_started_at}`,
  }
}



/* ── The panel's headline rollup ──────────────────────────────────────────
   What the pool IS before filtering: the total and the exception counts (held, then unbuilt), omitting
   `ready`. Different words from the group bar's summary (which leads with `ready`, scanned beside its own
   rows). Here rather than inline so `poolSlot.test.ts` reaches it; uses `isHeld`. */
export function poolRollup(slots: readonly Worktree[]): string {
  const parts = [countOf(slots.length, 'slot')]
  const count = (pred: (slot: Worktree) => boolean) => slots.filter(pred).length
  const declared = count(s => poolSlotState(s) === 'declared')
  const held = count(isHeld)
  if (held) parts.push(`${held} held`)
  if (declared) parts.push(`${declared} not built yet`)
  return parts.join(' · ')
}
