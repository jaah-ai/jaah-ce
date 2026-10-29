// GENERATED FILE — DO NOT EDIT BY HAND.
//
// Source:      apps/api/app/task_lifecycle.py (the single home of the transition table)
// Generator:   the task-transitions generator beside the repo's other gate tools; re-run it
//              to regenerate. Its test, a quick-tier gate stage with NO dependency probe, fails
//              on drift: a stage that can skip green never fires on the turn that drifts.
//
// Editing this file is not how you change a transition: edit the Python table and re-run the
// generator. An edit here reds the gate on the next turn (that is the point of the gate).

// Every spelling the transition table knows. ONE vocabulary since PR C: it was old ∪ new while
// the rename shipped expand/contract (PR A carried the new IDENTIFIERS while writing the old
// VALUES, PR B flipped them), and collapsed when PR C deleted the compatibility maps.
//
// This is the vocabulary, NOT the type of a `state` FIELD: `tasks.state` is unconstrained `Text`
// with no CHECK and no enum, deliberately (`task_lifecycle.py`'s docstring), and the web still
// renders legacy values from before the stage states retired. `types.ts` widens this into the
// field type `TaskState`; what is closed here is what the TABLE may name — a source or target
// outside it is a generator bug.
export type KnownTaskState =
  | 'draft'
  | 'ready'
  | 'active'
  | 'awaiting_response'
  | 'system_failed'
  | 'paused'
  | 'done'

// The same list as a VALUE, because a TypeScript type cannot be iterated at runtime and the web's
// three state maps (`shared.tsx`'s badge class, glyph and label) must be asserted to cover the
// vocabulary EXACTLY. Their drift test hand-mirrored this list before PR C, with a comment saying
// no cross-language import was possible — true of the Python module, not of this generated file,
// and the hand-copy is precisely the shape of drift this snapshot exists to remove.
export const KNOWN_TASK_STATES: readonly KnownTaskState[] = ['draft', 'ready', 'active', 'awaiting_response', 'system_failed', 'paused', 'done']

// One operator verb. Mirrors `task_lifecycle.Transition` field for field; see that dataclass for
// what each one means and why the table is scoped to OPERATOR transitions only (internal writers —
// dispatch, settle, rollup, reclaim, the sweeps — are allowed no control by design).
export type TaskTransition = {
  /** Stable id, and the key of the control registry (`components/tasks/controls.ts`). */
  readonly name: string
  /** The states the verb is allowed from; any other source is refused. */
  readonly sources: readonly KnownTaskState[]
  readonly target: KnownTaskState
  /** The UI control id. Every one exists in the registry, and vice versa — asserted both ways. */
  readonly control: string
  /** Sources that return 200 with no write rather than a 409. */
  readonly idempotentNoop: readonly KnownTaskState[]
  /** Whether landing this transition zeroes `dispatch_attempts` (R2). */
  readonly resetsAttempts: boolean
}

export const TRANSITIONS: readonly TaskTransition[] = [
  {
    name: 'start',
    sources: ['draft', 'ready', 'system_failed', 'paused', 'done'],
    target: 'ready',
    control: 'task.start',
    idempotentNoop: ['active'],
    resetsAttempts: true,
  },
  {
    name: 'stop',
    sources: ['ready', 'active', 'awaiting_response'],
    target: 'paused',
    control: 'task.stop',
    idempotentNoop: ['draft', 'system_failed', 'paused', 'done'],
    resetsAttempts: false,
  },
  {
    name: 'reset',
    sources: ['ready', 'active', 'awaiting_response', 'system_failed', 'paused', 'done'],
    target: 'draft',
    control: 'task.reset',
    idempotentNoop: [],
    resetsAttempts: true,
  },
  {
    name: 'answer',
    sources: ['awaiting_response'],
    target: 'ready',
    control: 'task.answer',
    idempotentNoop: [],
    resetsAttempts: true,
  },
  {
    name: 'complete',
    sources: ['draft', 'ready', 'active', 'awaiting_response', 'system_failed', 'paused'],
    target: 'done',
    control: 'task.complete',
    idempotentNoop: [],
    resetsAttempts: false,
  },
]

/** The transition names, in table order — the key set the control registry must equal. */
export const TRANSITION_NAMES: readonly string[] = TRANSITIONS.map((t) => t.name)

// The delete/archive safety membership — the web mirror of `_LIVE_STATES`, and DELIBERATELY not a
// projection of the table above. Spec R4.2 and the comment at `routes/tasks/delete.py::delete_task_core` refuse to
// merge it with `stop`'s sources *even though the values coincide today*: one protects a delete, the
// other a park, and they must be free to diverge. `isArchiveRow` is rebuilt on this in B4. It held
// both spellings of each renamed state while the rename shipped; PR C collapsed it to one.
// Typed on `string`, not `KnownTaskState`, because it is a MEMBERSHIP TEST against an unconstrained
// column: `isArchiveRow` asks it about whatever the server sent, legacy values included.
export const LIVE_STATES: ReadonlySet<string> = new Set(['ready', 'active', 'awaiting_response'])
