import { TRANSITIONS, type TaskTransition } from '../../generated/taskTransitions'
import { ACTION_ICON, ANSWER_WORD, TASK_ACTIONS } from './shared'
import type { PushIdentity } from './PushIdentityDialog'

// The task-control registry — one entry per operator transition in `task_lifecycle.py`, keyed by name.
// Invariant: no allowed transition without a button, no button the API refuses; `controls.test.ts`
// asserts both key sets equal against the generated snapshot. Every task-mutating POST reads `path` here.
//
// PER-ROW ENABLEMENT (`allowedTransitions`/`can`) prefers the API's `available_transitions` and falls
// back to the generated snapshot's `sources` — never a HAND-WRITTEN source set.
//
// PERSONA is not in the table: a persona-guarded control carries its `t.persona` test at the render site
// beside `can(...)`.

export type TaskControl = {
  /** The table's `control` id, carried for cross-checking. */
  readonly control: string
  /** Present-tense verb; from `TASK_ACTIONS` in `shared.tsx` where it has one. */
  readonly label: string
  /** In-flight label, same convention as `TASK_ACTIONS[*].busy`. */
  readonly busy: string
  /** From `ACTION_ICON` in `shared.tsx` (`aria-hidden`; never the accessible name). */
  readonly icon: string
  /** A function, because every route is task-scoped. */
  readonly path: (taskId: number) => string
}

// Structural, not `PipelineTask`: `TaskDepRef` carries the same two fields and must use this answer.
export type TransitionableRow = {
  readonly state: string
  readonly available_transitions?: readonly string[] | null
}

// Start/stop/reset read their words from `shared.tsx` (`TASK_ACTIONS`, `ACTION_ICON`, their single home).
export const CONTROLS: Record<string, TaskControl> = {
  start: {
    control: 'task.start',
    label: TASK_ACTIONS.start.verb,
    busy: TASK_ACTIONS.start.busy,
    icon: ACTION_ICON.start,
    path: (id) => `/api/tasks/${id}/start`,
  },
  stop: {
    control: 'task.stop',
    label: TASK_ACTIONS.stop.verb,
    busy: TASK_ACTIONS.stop.busy,
    icon: ACTION_ICON.stop,
    path: (id) => `/api/tasks/${id}/stop`,
  },
  reset: {
    control: 'task.reset',
    label: TASK_ACTIONS.reset.verb,
    busy: TASK_ACTIONS.reset.busy,
    icon: ACTION_ICON.reset,
    path: (id) => `/api/tasks/${id}/reset`,
  },
  answer: {
    control: 'task.answer',
    label: ANSWER_WORD,
    busy: 'Sending…',
    // This `path` is the single home of the route; no call site spells it.
    icon: ACTION_ICON.answer,
    path: (id) => `/api/tasks/${id}/answer`,
  },
  complete: {
    control: 'task.complete',
    label: 'Complete',
    busy: 'Completing…',
    icon: '✓',
    // `complete` is the direct `→ done` transition.
    path: (id) => `/api/tasks/${id}/complete`,
  },
}

/**
 * What a `start` POST declares in `run_requested` — the ONE place both bodies are spelled.
 *  • `'run'`   — the per-subtask Run (tree subtask Start, dependency Re-run): releases a `hold`-mode parent
 *                (`run_requested_at`) — MUST be `true`. Prerequisites, throttle and backoff still gate it
 *                (`_sequencing_floors`).
 *  • `'queue'` — claims no run-intent: an epic's Start (its route ignores the field), bulk "Send to build".
 * Both bodies are sent EXPLICITLY, never omitted: the server default was flipped relying on that.
 */
export type StartIntent = 'run' | 'queue'

/** `pushAs` is the Start ask's answer (`PushIdentityDialog.tsx::usePushIdentity`): an acting operator's "push as
 *  whom" (`push_as_id`), and `without_repo` when the starter runs without a repository they can't see
 *  (`TaskStartIn.without_repo`); `{}` sends neither. */
export function startBody(intent: StartIntent, pushAs: PushIdentity = {}): { run_requested: boolean } & PushIdentity {
  return { run_requested: intent === 'run', ...pushAs }
}

/**
 * WHICH intent a row's Start means — the single home every Start call site reads. Every row's Start is a Run
 * except an EPIC container's (its start branch never reads or clears the flag). A Run releases a parent's hold
 * and nothing else, so it is safe on any other row.
 */
export function startIntentFor(task: { persona?: string | null }): StartIntent {
  return task.persona === 'epic' ? 'queue' : 'run'
}

/**
 * The transition names this row may take now: the API's `available_transitions`, else the generated
 * snapshot's `sources`. `idempotentNoop` states are NOT included — a 200-with-no-write button does nothing.
 */
export function allowedTransitions(
  row: TransitionableRow,
  transitions: readonly TaskTransition[] = TRANSITIONS,
): string[] {
  if (row.available_transitions != null) {
    // Filtered so an unknown name from an older/newer API cannot conjure an unregistered control.
    return row.available_transitions.filter((name) => name in CONTROLS)
  }
  // Widened to `string`: the server may send legacy values outside the closed vocabulary.
  return transitions
    .filter((t) => (t.sources as readonly string[]).includes(row.state))
    .map((t) => t.name)
}

/** The per-control render gate. */
export function can(row: TransitionableRow, name: string): boolean {
  return allowedTransitions(row).includes(name)
}

/**
 * Dispatch routes that are NOT transitions, kept BESIDE `CONTROLS` because `controls.test.ts` pins its key
 * set to the transition table: a forecast is a read, force-start a compound (stop one, start another).
 */
export const DISPATCH_ROUTES = {
  /** `GET` — would this task dispatch if started now (`DispatchForecast`, `types.ts`). */
  forecast: (id: number) => `/api/tasks/${id}/dispatch-forecast`,
  /** `POST` — stop `stop_task_id` and start this one in its slot; the stopped task is re-queued server-side. */
  forceStart: (id: number) => `/api/tasks/${id}/force-start`,
} as const

/** `POST /api/tasks/{id}/force-start`'s body. `run_requested` carries the same intent `startBody` does. */
export type ForceStartBody = { stop_task_id: number; run_requested: boolean } & PushIdentity
