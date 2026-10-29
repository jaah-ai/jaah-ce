"""The `Task.state` vocabulary and the operator-transition table — one home for both.

`tasks.state` is plain `Text` (no CHECK, no enum), so the vocabulary is enforced by convention: a new
state is a constant here plus a row below, never a literal at a call site.

Stdlib-only at import (not even under `TYPE_CHECKING`): the write-site drift scanner runs on the quick
tier from a bare interpreter.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "ACTIVE",
    "AWAITING_RESPONSE",
    "DONE",
    "DRAFT",
    "PAUSED",
    "READY",
    "STATES",
    "STOP_REASON_BALANCE",
    "STOP_REASON_JAAH_UNAVAILABLE",
    "NODE_AT_CAPACITY",
    "SLOT_DESTROYED",
    "SYSTEM_FAILED",
    "SystemTransition",
    "BALANCE_RESUME",
    "TRANSITIONS",
    "TRANSITION_BY_NAME",
    "Transition",
    "transitions_from",
    # The route-level guard sets.
    "_ACTIVE_TASK_STATES",
    "_ANALYSIS_ARTIFACT_SETTLE_STATES",
    "_ANSWER_ACTIONABLE_STATES",
    "_APPEND_FINISH_FOLLOW_UP_STATES",
    "_APPEND_TASKS_PLAN_STATES",
    "_BREAK_RELAUNCH_STATES",
    "_COMPLIANCE_CASCADE_TERMINALS",
    "_COMPLIANCE_MIRROR_SETTLED_STATES",
    "_DISPATCHABLE_STATES",
    "_EPIC_FAILING_CHILD_STATES",
    "_EPIC_SETTLED_STATES",
    "_FINISH_FOLLOW_UP_DEDUP_EXCLUDED_STATES",
    "_LIVE_STATES",
    "_LIVE_UNIT_STATES",
    "_NEEDS_ATTENTION_STATES",
    "_NO_RUNNER_STATES",
    "_OPEN_DISPATCH_STATES",
    "_ORG_DELETE_BLOCKING_TASK_STATES",
    "_RESOLVABLE_SYSTEM_TASK_STATES",
    "_RUNNER_END_STATES",
    "_RUNNER_RUNNING_STATES",
    "_SYSTEM_TASK_DEDUP_EXCLUDED_STATES",
    "_SYSTEM_TASK_INITIAL_STATES",
    "_SYSTEM_TASK_TERMINAL_STATES",
]

# ── The state vocabulary ──────────────────────────────────────────────────────────────────────────
# Identifiers and values agree, and nothing reads a state by spelling out its literal.
DRAFT = "draft"
READY = "ready"
ACTIVE = "active"
AWAITING_RESPONSE = "awaiting_response"  # a human owes this Task an answer
SYSTEM_FAILED = "system_failed"  # the system spent the Task's attempts
PAUSED = "paused"  # the work STOPPED and its tree is held for a resume (a human's Stop, or an idle kill)
DONE = "done"

# Every value `Task.state` may legally hold. Not a display order and not a guard: which states a verb
# accepts is `TRANSITIONS`, and only that.
STATES: frozenset[str] = frozenset({
    DRAFT,
    READY,
    ACTIVE,
    AWAITING_RESPONSE,
    SYSTEM_FAILED,
    PAUSED,
    DONE,
})


# ── The transition table ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Transition:
    """One operator verb: which states it may be invoked from, where it lands, and its UI control.

    Operator transitions only — internal `.values(state=…)` sites are out of scope. Guarantees no
    allowed operator transition lacks a button and no button is refused by the API.
    """

    # Stable id: keys the web control registry and the generated TypeScript snapshot.
    name: str
    # The states the verb is ALLOWED from. A source outside this set is refused — except where
    # `idempotent_noop` says otherwise.
    sources: frozenset[str]
    target: str
    # The UI control id; must match the web control registry both ways (drift tests).
    control: str
    # Sources that return 200 with no write rather than 409 (a double-click, stopping a parked epic),
    # so the table, not an ad-hoc branch, refuses a transition.
    idempotent_noop: frozenset[str] = frozenset()
    # Whether landing zeroes `dispatch_attempts`; otherwise a Task started from `system_failed` is
    # already at the cap and re-fails after its first attempt.
    resets_attempts: bool = False


# Five operator verbs; the rows are the guards. Nothing is an end state: `start` accepts `done` and
# `system_failed`, `complete` accepts every state but its target. `reset` accepts every state but
# `draft`; it releases the slot and mints the end-runner/wipe order in the same transaction, with the
# node enacting it shortly after (`routes/tasks/slots.py::abandon_task_slots`).
TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        "start",
        # `AWAITING_RESPONSE` is deliberately absent: it holds a live runner frozen in `AskUserQuestion`,
        # so `start` could only mean kill-and-relaunch behind a Start button. Use `answer` or `stop`.
        frozenset({DRAFT, READY, DONE, PAUSED, SYSTEM_FAILED}),
        READY,
        "task.start",
        idempotent_noop=frozenset({ACTIVE}),
        resets_attempts=True,
    ),
    Transition(
        "stop",
        frozenset({READY, ACTIVE, AWAITING_RESPONSE}),
        PAUSED,
        "task.stop",
        idempotent_noop=frozenset({DRAFT, DONE, PAUSED, SYSTEM_FAILED}),
    ),
    Transition(
        "reset",
        frozenset({DONE, PAUSED, AWAITING_RESPONSE, SYSTEM_FAILED, ACTIVE, READY}),
        DRAFT,
        "task.reset",
        resets_attempts=True,
    ),
    Transition(
        "answer",
        frozenset({AWAITING_RESPONSE}),
        READY,
        "task.answer",
        resets_attempts=True,
    ),
    Transition(
        "complete",
        # Every state but `done`. `draft`/`ready` are included so an operator can close moot work
        # without dispatching a runner first. Neither is a runner-running state, so no termination is
        # recorded. A `ready` row may still name a dead session (`answer_task`'s cold gate writes only
        # `state`), so the agent `/console-exit` door is fenced separately by
        # `routes/agent/runner_liveness.py::_CONSOLE_EXIT_SOURCES` (`apply_complete`'s `allow_sources`).
        frozenset({DRAFT, READY, ACTIVE, AWAITING_RESPONSE, PAUSED, SYSTEM_FAILED}),
        DONE,
        "task.complete",
    ),
)

# `TRANSITIONS` keyed by `name`. `routes/tasks/dispatch.py::_start` zeroes `dispatch_attempts` iff
# `resets_attempts` — defined here, applied there.
TRANSITION_BY_NAME: Mapping[str, Transition] = MappingProxyType(
    {transition.name: transition for transition in TRANSITIONS}
)


# ── System transitions ────────────────────────────────────────────────────────────────────────────
# Edges only the system takes: no control, no route, never in `TRANSITIONS` (so no button can offer them and
# no operator verb can reach them).

# The system's two pause reasons: spelled here, stdlib-side, because the guard below needs them;
# `app/task_sections.py` re-exports them as the rest of the stop-record vocabulary.
STOP_REASON_BALANCE = "balance"
STOP_REASON_JAAH_UNAVAILABLE = "jaah_unavailable"


@dataclass(frozen=True, slots=True)
class SystemTransition:
    """One system edge: its sources, its target, and the stop reasons a row must carry to take it."""

    name: str
    sources: frozenset[str]
    target: str
    stop_reasons: frozenset[str]
    resets_attempts: bool = False


# A task the system paused for the Jaah account — balance at the floor, or the platform account broken — is
# resumed by it once the condition clears (`app/jaah_routing/pause.py`). Only those two reasons: an operator
# Stop (`operator`), a wait on a customer and an idle kill (no record) are never resumed (AS4). The bound slot
# stays unless destroyed meanwhile (`SLOT_DESTROYED` below); the resume refunds the attempt the killed run was charged (`app/jaah_routing/pause.py`), so a pause
# spends none.
BALANCE_RESUME = SystemTransition(
    "balance_resume",
    frozenset({PAUSED}),
    READY,
    frozenset({STOP_REASON_BALANCE, STOP_REASON_JAAH_UNAVAILABLE}),
)


# A task whose worktree the CP destroyed under it — its project unassigned from the node — goes
# back to the queue once the node has killed its runner: no stop record needed, no attempt spent, its open
# question rounds dropped. Taken only by `app/slot_allocation.py::requeue_after_destroy`. Never from `paused`:
# a paused task has no runner to kill, and its pause is somebody's decision (an operator Stop, a balance
# pause `BALANCE_RESUME` owns) that losing its tree does not reverse — it keeps its pause and loses the hold
# (without this, a stopped chat relaunched itself when its dynamic slot was destroyed).
SLOT_DESTROYED = SystemTransition(
    "slot_destroyed",
    frozenset({ACTIVE, AWAITING_RESPONSE}),
    READY,
    frozenset(),
)


# A task whose node refused to launch it because the node already runs the most agent sessions it may (a
# community-edition node, `apps/jaahd/ce_caps.py`) goes back to the queue with its attempt refunded: nothing ran.
# Taken only by `app/node_capacity.py::return_to_queue`.
NODE_AT_CAPACITY = SystemTransition(
    "node_at_capacity",
    frozenset({ACTIVE}),
    READY,
    frozenset(),
)


def transitions_from(state: str) -> tuple[str, ...]:
    """The transition names legal from `state`, in table order — the API's `available_transitions`.

    Genuine sources only; `idempotent_noop` states are a tolerance, not an offer. An unknown state gets
    `()` (fail closed)."""
    return tuple(t.name for t in TRANSITIONS if state in t.sources)


# ── The route-level state sets ────────────────────────────────────────────────────────────────────
# Internal guard vocabulary imported by the route modules; nothing outside `apps/api` reads it.

# These are not the transition table: a guard set answers a question about the vocabulary,
# `TRANSITIONS` about a verb. Sets with identical membership are not duplication — each guards its own
# operation.

# The states a Task can be (re)dispatched against — an "open" Task. A draft or terminal state never
# resolves for the composer (`_resolve_open_task_by_id`).
_OPEN_DISPATCH_STATES = (READY, ACTIVE, AWAITING_RESPONSE)

# The state(s) the fleet scheduler treats as dispatchable — mirrored by migration 0035's partial-index
# predicate; keep in lockstep. Epic children are sequenced by `dispatchable_tasks`' dependency gate.
_DISPATCHABLE_STATES = (READY,)

# The states in which no runner should still be working. Excludes `awaiting_response`: a runner sets it
# on itself and may still be finishing, and this list drives a kill.
_NO_RUNNER_STATES = frozenset({DONE, SYSTEM_FAILED, PAUSED})

# The states in which a self-reported runner break may end the attempt by automatic relaunch (the
# relaunch decision's licence only). Includes `awaiting_response`.
_BREAK_RELAUNCH_STATES = frozenset({READY, ACTIVE, AWAITING_RESPONSE})

# Live states a Task must never be deleted out from under. Its own constant, not the `stop` row's
# `sources`, which merely coincide today.
_LIVE_STATES = (READY, ACTIVE, AWAITING_RESPONSE)

# Does the slot referencing this task still have an active task? Every state except `done` and
# `system_failed`, derived from `STATES` so a new state joins by default. Not `_LIVE_STATES`: a slot
# bound to a paused task is held for crash recovery. Single home: `app/slot_title.py`.
_ACTIVE_TASK_STATES: frozenset[str] = STATES - {DONE, SYSTEM_FAILED}

# The states the board's Focus toggle keeps among an expanded parent's children — rows an operator can
# act on now. Excludes `ready` (waiting) and `done`; includes `system_failed` (the most actionable).
# Top-level rows use `_FOCUS_TOP_LEVEL_STATES`. Not `_ACTIVE_TASK_STATES`, which answers a slot question.
_FOCUS_STATES: frozenset[str] = frozenset({DRAFT, ACTIVE, AWAITING_RESPONSE, PAUSED, SYSTEM_FAILED})

# The set Focus keeps among top-level rows: `_FOCUS_STATES` plus `ready`, since a queued top-level task
# has nothing else standing in for it. Derived (not `STATES - {DONE}`) so a future state must be
# classified in `_FOCUS_STATES` rather than joining by default.
_FOCUS_TOP_LEVEL_STATES: frozenset[str] = _FOCUS_STATES | {READY}

# ── Needs-attention view ─────────────────────────────────────────────────────────────────────────────
# The single, authoritative surface where hidden system Tasks become visible, even when entry-notify is
# a no-op.
_NEEDS_ATTENTION_STATES = (AWAITING_RESPONSE, SYSTEM_FAILED, PAUSED)

# Terminal resulting states that close the runner phase an `active` transition opened. Keyed on the
# resulting state, not the verb (`stop_task` lands `paused` under action="stop").
_RUNNER_END_STATES = {DONE, SYSTEM_FAILED, PAUSED}

# The states in which a runner is running against a Task — what a park, Stop or ended attempt actually
# terminates. Not `_LIVE_STATES` (a `ready` row has nothing launched). Includes `awaiting_response`: the
# runner is alive inside its own tool call. Not sufficient alone: an `epic` is `active` as a rollup, so
# `routes/tasks/dispatch.py::_terminates_a_running_runner` also tests the persona.
_RUNNER_RUNNING_STATES = frozenset({ACTIVE, AWAITING_RESPONSE})

# ── System Tasks ──────────────────────────────────────────────────────────────────────────────────

# A system Task is created queued, working, or as a notice — never terminal (see SystemTaskIn.state).
# `draft` is the non-dispatchable one, for operator alarms. A producer filing remediation must file
# `ready`, or nothing will run it.
_SYSTEM_TASK_INITIAL_STATES = (DRAFT, READY, ACTIVE)
# Terminal states a `dedup_open` match excludes: a finished prior row does not suppress a fresh one.
_SYSTEM_TASK_TERMINAL_STATES = (DONE, SYSTEM_FAILED, PAUSED)
# Plus `awaiting_response`: a row parked on a human decision about its current title/body is never
# silently refreshed. Shared with `find_open_system_task`.
_SYSTEM_TASK_DEDUP_EXCLUDED_STATES = _SYSTEM_TASK_TERMINAL_STATES + (AWAITING_RESPONSE,)

# ── The inline literal tuples, given a name ───────────────────────────────────────────────────────
# Each keeps its own name even where memberships coincide: they guard different operations.

# The child states that settle a child for its epic. Not `paused`: an epic with a paused child is
# waiting (its own pause-collapse rule).
_EPIC_SETTLED_STATES = (DONE, SYSTEM_FAILED)

# `settle_analysis_child_on_artifact`'s source states. Excludes `awaiting_response`: settling off an
# artifact would answer a human's question on their behalf.
_ANALYSIS_ARTIFACT_SETTLE_STATES = (ACTIVE, SYSTEM_FAILED)

# `append_tasks`'s plan-child gate: while running or once finished.
_APPEND_TASKS_PLAN_STATES = (ACTIVE, DONE)

# `append_finish_follow_up`'s finish-child gate: while running or right after its own settle. From
# `draft` or `system_failed` it would hang work off a prerequisite nobody drives.
_APPEND_FINISH_FOLLOW_UP_STATES = (ACTIVE, DONE)

# `append_finish_follow_up`'s dedup exclusion: the state in which a matched follow-up is dead, so a
# re-file inserts a fresh row. `done` and `paused` must still match (duplicate / deliberately stopped).
_FINISH_FOLLOW_UP_DEDUP_EXCLUDED_STATES = (SYSTEM_FAILED,)

# `_roll_up_epic`'s failure branch: child states that fail the parent epic — `_EPIC_SETTLED_STATES`
# minus `done`. The failure marks the epic only and touches no sibling; `Stop` is the one verb that
# cascades down.
_EPIC_FAILING_CHILD_STATES = (SYSTEM_FAILED,)

# `_compliance_group_mirrors(only_open=True)`: the settled states the `only_open` filter excludes.
_COMPLIANCE_MIRROR_SETTLED_STATES = (DONE, SYSTEM_FAILED, PAUSED)

# `routes/tasks/task_wire.py::_asking_task_keys`: states in which an answer can still be acted on. Its own
# frozenset, not a subtraction from `STATES`, so a new state is admitted deliberately.
_ANSWER_ACTIONABLE_STATES = frozenset({READY, ACTIVE, AWAITING_RESPONSE})

# `_settle_compliance_group`: the group terminals the mirror cascade maps; any other returns untouched
# (fail-closed).
_COMPLIANCE_CASCADE_TERMINALS = (DONE, AWAITING_RESPONSE, SYSTEM_FAILED)

# `list_open_tasks`: the states that track a LIVE unit for jaahd's backstop sweeps.
_LIVE_UNIT_STATES = (READY, ACTIVE, AWAITING_RESPONSE, SYSTEM_FAILED)

# `resolve_system_task`: states an outcome-criterion system Task may be resolved from — read by both the
# 409 pre-check and the atomic UPDATE's `WHERE`. Includes `draft`, where alarms are filed
# (`app/node_liveness_job.py`), so a producer can close what it raised.
_RESOLVABLE_SYSTEM_TASK_STATES = (DRAFT, READY, ACTIVE, AWAITING_RESPONSE)

# `app/tenancy/org_lifecycle.py::wind_down`: the tasks an org delete fails before the purge — anything but
# `done`/`system_failed`. Derived by subtraction so a new state is ended by default (fail-closed). Its own name though it equals
# `_ACTIVE_TASK_STATES` today — that one answers a slot question.
_ORG_DELETE_BLOCKING_TASK_STATES: frozenset[str] = STATES - {DONE, SYSTEM_FAILED}

# Per-persona settle states live in `app/persona_registry.py` (`DB_DIRECT_SETTLE_STATES`,
# `settle_states`).
