"""The board's state sections — Stopped / In progress / Draft / Done — and the stop record that says why a row stopped.

A VIEW over `Task.state`, never a state of its own: `app/task_lifecycle.py` keeps its seven states and its
transition table untouched. The one thing a state cannot say is "Waiting on customer" — a pause reason
(D-waiting) — so it lives in the STOP RECORD (`tasks.stop_reason`/`stop_note`/`stopped_by_id`/
`stopped_at`), written by `routes/tasks/transitions.py::_stop_task_core` and the record-only `wait-on-customer` path,
and cleared by the database the moment the row leaves for a working or finished state
(`trg_tasks_clear_stop_record` below), so every state writer — roll-up, force-start, the agent and
portfolio routes — clears it without being edited.

THE MAPPING EXISTS HERE ONLY: in Python as `section_of`/`stop_kind_of`, in SQL as `section_sql` (and the
ordering terms built beside it), the SQL generated from the Python rule so the two cannot drift.

A PARENT IS PLACED BY ITS CHILDREN (D6-state, "an epic moves with its stage tasks"): `done` wins; else
its own stop; else the first stopped non-archived child by (`STOP_KIND_ORDER`, `stage_rank`); else Draft
when its own state is `draft`; else In progress. Leaves have no children, so for them this is the row mapping.
The stored rollup (`_roll_up_epic`) is untouched — this is display placement only.

A HIDDEN ROW STOPPED AS `failed` IS PLACED IN DONE: hidden failures are system evidence (a broken-session
record), not work anyone resumes, and hundreds of them buried the board's Stopped section.
The row keeps its stop kind, so it still draws its failed mark, and it sorts by recency like any Done row.

A QUESTION WHOSE ANSWER IS IN FLIGHT IS NOT A STOP: a warm answer leaves the row `awaiting_response` until the
node acks the `deliver_answer` intent, so the row is working again, not waiting on anyone, and sits in In
progress (after running, before queued) — unless a newer round is open, which still asks
(`routes/tasks/task_wire.py::_asking_task_keys`). "In flight" is single-homed here as `answer_delivering_keys`.

Every name the revision, `app/models/` and the catalog tests share is spelled once, here.
"""
from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional

from sqlalchemy import Text, and_, case, cast, exists, func, literal, or_, select

from app.task_lifecycle import (
    ACTIVE,
    AWAITING_RESPONSE,
    DONE,
    DRAFT,
    PAUSED,
    READY,
    STATES,
    STOP_REASON_BALANCE,
    STOP_REASON_JAAH_UNAVAILABLE,
    SYSTEM_FAILED,
    BALANCE_RESUME,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

Section = Literal["stopped", "in_progress", "draft", "done"]
StopKind = Literal["question", "failed", "paused", "balance", "jaah_unavailable", "waiting_on_customer"]

# Display order of the sections, and of the rows inside Stopped and In progress (D6-state).
SECTIONS: tuple[Section, ...] = ("stopped", "in_progress", "draft", "done")
STOP_KIND_ORDER: tuple[StopKind, ...] = (
    "question", "failed", "paused", "balance", "jaah_unavailable", "waiting_on_customer",
)
# The stop kind a HIDDEN row is placed in Done for rather than Stopped (`place`).
HIDDEN_DONE_KIND: StopKind = "failed"
# `awaiting_response` reaches In progress only while its answer is delivering (`stop_kind_of`).
IN_PROGRESS_ORDER: tuple[str, ...] = (ACTIVE, AWAITING_RESPONSE, READY)

# ── The stop record ───────────────────────────────────────────────────────────────────────────────────
# `operator` is a person's Stop; `waiting_on_customer` the pause reason. NULL is "no record" — an idle
# kill, an epic-cascade stop, or a row that never stopped. `balance` and `jaah_unavailable` (spelled in
# `task_lifecycle`, whose `BALANCE_RESUME` guards on them) are the SYSTEM's pauses of work on the Jaah account
# (`app/jaah_routing/pause.py`): the balance reached the floor, or the platform Bedrock account
# broke. No person acts, so they alone may carry no `stopped_by_id`, and they alone are ever resumed
# automatically — an operator Stop writes `operator` and is never resumed (AS4).
STOP_REASON_OPERATOR = "operator"
STOP_REASON_WAITING = "waiting_on_customer"
SYSTEM_STOP_REASONS: tuple[str, ...] = (STOP_REASON_BALANCE, STOP_REASON_JAAH_UNAVAILABLE)
assert frozenset(SYSTEM_STOP_REASONS) == BALANCE_RESUME.stop_reasons
STOP_REASONS: tuple[str, ...] = (STOP_REASON_OPERATOR, STOP_REASON_WAITING, *SYSTEM_STOP_REASONS)
STOP_NOTE_MAX = 2000

TASK_STOP_REASON_CHECK = "ck_tasks_stop_reason"
TASK_STOP_RECORD_CHECK = "ck_tasks_stop_record_complete"
TASK_STOP_NOTE_CHECK = "ck_tasks_stop_note"
TASK_STOPPED_BY_FK = "fk_tasks_stopped_by_id"

TASK_STOP_REASON_CHECK_SQL = (
    "stop_reason IS NULL OR stop_reason IN (" + ", ".join(f"'{r}'" for r in STOP_REASONS) + ")"
)
# The record is whole or absent: reason, who and when together — except that a system pause names no one.
TASK_STOP_RECORD_CHECK_SQL = (
    "(stop_reason IS NULL) = (stopped_at IS NULL) AND (stopped_by_id IS NULL OR stop_reason IS NOT NULL) "
    "AND (stopped_by_id IS NOT NULL OR stop_reason IS NULL OR stop_reason IN ("
    + ", ".join(f"'{r}'" for r in SYSTEM_STOP_REASONS) + "))"
)
# A note belongs to a wait only, and is bounded.
TASK_STOP_NOTE_CHECK_SQL = (
    f"stop_note IS NULL OR (stop_reason = '{STOP_REASON_WAITING}' AND char_length(stop_note) <= {STOP_NOTE_MAX})"
)

# ── The clearing trigger ──────────────────────────────────────────────────────────────────────────────
# Leaving for a working or finished state ends the stop: Start, a claim, a Reset, Complete, a roll-up.
CLEARING_STATES: tuple[str, ...] = (READY, ACTIVE, DONE, DRAFT)
CLEAR_STOP_RECORD_FUNCTION = "public.clear_task_stop_record"
CLEAR_STOP_RECORD_TRIGGER_NAME = "trg_tasks_clear_stop_record"

CLEAR_STOP_RECORD_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {CLEAR_STOP_RECORD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state
       AND NEW.state IN ({", ".join(f"'{s}'" for s in CLEARING_STATES)}) THEN
        NEW.stop_reason := NULL;
        NEW.stop_note := NULL;
        NEW.stopped_by_id := NULL;
        NEW.stopped_at := NULL;
    END IF;
    RETURN NEW;
END;
$$
"""
CLEAR_STOP_RECORD_TRIGGER_SQL = (
    f"CREATE TRIGGER {CLEAR_STOP_RECORD_TRIGGER_NAME} BEFORE UPDATE OF state ON tasks "
    f"FOR EACH ROW EXECUTE FUNCTION {CLEAR_STOP_RECORD_FUNCTION}()"
)


# ── The row mapping (Python) ──────────────────────────────────────────────────────────────────────────
def stop_kind_of(state: str, stop_reason: Optional[str], delivering: bool = False) -> Optional[StopKind]:
    """Why a row is Stopped, or None when it is not. Exhaustive over `STATES`; an unknown state raises.
    `delivering`: the row's answer is in flight (`answer_delivering_keys`) and no newer round is open, which
    ends its question."""
    if state not in STATES:
        raise ValueError(f"unknown task state {state!r}")
    if state in (DONE, ACTIVE, READY):
        # `done` always wins, and a working row is In progress whatever record it still carries.
        return None
    if state == AWAITING_RESPONSE and delivering:
        return None
    if stop_reason == STOP_REASON_WAITING and state in (DRAFT, PAUSED, SYSTEM_FAILED):
        return "waiting_on_customer"
    if state == PAUSED and stop_reason == STOP_REASON_BALANCE:
        return "balance"
    if state == PAUSED and stop_reason == STOP_REASON_JAAH_UNAVAILABLE:
        return "jaah_unavailable"
    if state == AWAITING_RESPONSE:
        return "question"
    if state == SYSTEM_FAILED:
        return "failed"
    if state == PAUSED:
        return "paused"
    return None  # draft: Not started (the Draft section)


def section_of(state: str, stop_reason: Optional[str], delivering: bool = False) -> Section:
    """The section one row belongs to on its own (a leaf, or a parent before its children are read)."""
    if state == DONE:
        return "done"
    if stop_kind_of(state, stop_reason, delivering) is not None:
        return "stopped"
    return "draft" if state == DRAFT else "in_progress"


@dataclass(frozen=True, slots=True)
class StageCandidate:
    """The facts `place` needs about one non-archived child."""

    id: int
    state: str
    stop_reason: Optional[str]
    stage_rank: int
    delivering: bool = False


def place(
    state: str, stop_reason: Optional[str], children: Iterable[StageCandidate] = (), delivering: bool = False,
    hidden: bool = False,
) -> tuple[Section, Optional[StopKind], Optional[StageCandidate]]:
    """A row placed with its children: (section, why it is stopped, the child that stopped it).

    `done` wins; else the row's own stop (no child named); else the first stopped child by
    (`STOP_KIND_ORDER`, `stage_rank`); else Draft by the row's own state; else In progress. `children` must
    be the NON-ARCHIVED ones. A `hidden` row stopped as `HIDDEN_DONE_KIND` is placed in Done, keeping its kind."""
    section, kind, child = _place(state, stop_reason, children, delivering)
    if hidden and kind == HIDDEN_DONE_KIND:
        return "done", kind, child
    return section, kind, child


def _place(
    state: str, stop_reason: Optional[str], children: Iterable[StageCandidate], delivering: bool,
) -> tuple[Section, Optional[StopKind], Optional[StageCandidate]]:
    if state == DONE:
        return "done", None, None
    own = stop_kind_of(state, stop_reason, delivering)
    if own is not None:
        return "stopped", own, None
    stopped = [(k, c) for c in children if (k := stop_kind_of(c.state, c.stop_reason, c.delivering)) is not None]
    if stopped:
        kind, child = min(stopped, key=lambda kc: (STOP_KIND_ORDER.index(kc[0]), kc[1].stage_rank, kc[1].id))
        return "stopped", kind, child
    return ("draft" if state == DRAFT else "in_progress"), None, None


# ── The same mapping in SQL, generated from the Python rule ───────────────────────────────────────────
_REASON_DOMAIN: tuple[Optional[str], ...] = (None, *STOP_REASONS)


def _reason_is(t: Any, reason: Optional[str]):
    return t.stop_reason.is_(None) if reason is None else t.stop_reason == reason


def answer_delivering_keys(task_ids: Optional[Iterable[int]] = None):
    """The task ids (as text, never NULL) with a gate answer STILL IN FLIGHT to a live runner: a
    `deliver_answer` slot intent the node has not retired, or a resolved round waiting for a launched
    runner to receive it — and no visible open round. The single home of "delivering" — the board's
    section (`stop_kind_of`'s `delivering`) and `TaskOut.answer_delivering` both read it. Derived from the
    intent row the node drains, so it needs no cleanup or timeout. Pending uses the drain's own
    `SLOT_INTENT_RETIRING_OUTCOMES`; `outcome IS NULL` is explicit because `NOT IN` over NULL drops the
    newest rows. Uncorrelated, so a board query evaluates it once (a hashed subplan), not per row.

    The key is read only under the verb test, inside a CASE: Postgres reorders plain quals, and when it
    ran the `->>` first it detoasted every `lease_slot` payload (~74 KB each) on every board read — a
    production DB-load incident (~300 ms per scan, four scans per rail query). `task_ids` narrows
    to those rows through the same guarded key."""
    from app import fleet_verdicts
    from app.models.tasks import Task, TaskQuestion
    from app.models.worktrees import SlotIntent
    from app.task_artifacts import visible_artifact_clause_joined

    ids = None if task_ids is None else tuple(task_ids)
    narrowed_rounds = (TaskQuestion.task_id.in_(ids),) if ids is not None else ()
    # A visible open round outranks any delivery: the runner is asking again, so the row reads "Asking" on
    # every surface (the `_asking_task_keys` rule — a Reset-hidden round asks nothing). Never NULL, so the
    # `NOT IN` below is safe.
    asking = select(cast(TaskQuestion.task_id, Text)).join(Task, Task.id == TaskQuestion.task_id).where(
        TaskQuestion.status == "open",
        visible_artifact_clause_joined(TaskQuestion.created_at),
        *narrowed_rounds,
    )
    is_deliver = SlotIntent.verb == fleet_verdicts.VERB_DELIVER_ANSWER
    key = case((is_deliver, SlotIntent.payload["task_id"].astext))
    narrowed = () if ids is None else (key.in_([str(i) for i in ids]),)
    in_flight = select(key).where(
        is_deliver,
        key.is_not(None),
        *narrowed,
        or_(
            SlotIntent.outcome.is_(None),
            SlotIntent.outcome.notin_(fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES),
        ),
        key.not_in(asking),
    )
    waiting = select(cast(TaskQuestion.task_id, Text)).join(Task, Task.id == TaskQuestion.task_id).where(
        Task.state == AWAITING_RESPONSE,
        TaskQuestion.status != "open",
        TaskQuestion.delivered_at.is_(None),
        visible_artifact_clause_joined(TaskQuestion.created_at),
        *narrowed_rounds,
        cast(TaskQuestion.task_id, Text).not_in(asking),
    )
    return in_flight.union(waiting)


def _delivering(t: Any):
    """`stop_kind_of`'s `delivering` over alias `t`: an answer in flight (`answer_delivering_keys`, which
    already yields to a visible open round)."""
    return cast(t.id, Text).in_(answer_delivering_keys())


def _kind_predicate(t: Any, kind: StopKind):
    """The (state, reason, delivering) triples `stop_kind_of` maps to `kind`, as one predicate over alias
    `t`. `delivering` is read only for the state it can change (`awaiting_response`)."""
    arms = []
    for state in sorted(STATES):
        domain = (False, True) if state == AWAITING_RESPONSE else (False,)
        matches = {d: [r for r in _REASON_DOMAIN if stop_kind_of(state, r, d) == kind] for d in domain}
        # `delivering` that changes nothing for this state needs no term.
        reads_delivering = len({tuple(reasons) for reasons in matches.values()}) > 1
        if not reads_delivering:
            matches = {False: matches[False]}
        for d, reasons in matches.items():
            if not reasons:
                continue
            terms = [t.state == state]
            if reads_delivering:
                terms.append(_delivering(t) if d else ~_delivering(t))
            if len(reasons) != len(_REASON_DOMAIN):
                terms.append(or_(*(_reason_is(t, r) for r in reasons)))
            arms.append(and_(*terms))
    return or_(*arms)


def _row_stopped(t: Any):
    return or_(*(_kind_predicate(t, kind) for kind in STOP_KIND_ORDER))


@functools.cache
def _task_row_stopped():
    """`_row_stopped(Task)`, built once: a static predicate (every arm a constant over `Task`) whose construction
    is a few ms of each board read."""
    from app.models.tasks import Task

    return _row_stopped(Task)


def _row_kind_rank(t: Any):
    """The row's own `STOP_KIND_ORDER` position, NULL when it is not stopped."""
    return case(*((_kind_predicate(t, kind), i) for i, kind in enumerate(STOP_KIND_ORDER)), else_=None)


def _default_child_alias() -> Any:
    from sqlalchemy.orm import aliased

    from app.models.tasks import Task

    return aliased(Task)


def _live_children_of(t: Any, c: Any):
    return and_(c.parent_task_id == t.id, c.archived_at.is_(None))


def section_sql(task_alias: Any, child_alias_factory: Callable[[], Any] = _default_child_alias):
    """`section_of` + `place` as one SQL CASE over `task_alias`, children read through one correlated
    EXISTS on the indexed `parent_task_id` (a fresh alias per call, from `child_alias_factory`)."""
    t = task_alias
    c = child_alias_factory()
    child_stopped = exists(select(literal(1)).select_from(c).where(_live_children_of(t, c), _row_stopped(c)))
    return case(
        (t.state == DONE, literal("done")),
        (_hidden_done(t, child_alias_factory), literal("done")),
        (_row_stopped(t), literal("stopped")),
        (child_stopped, literal("stopped")),
        (t.state == DRAFT, literal("draft")),
        else_=literal("in_progress"),
    )


def _hidden_done(t: Any, child_alias_factory: Callable[[], Any]):
    """`place`'s hidden rule over alias `t`: a hidden row whose placed stop kind is `HIDDEN_DONE_KIND`."""
    return and_(
        t.hidden.is_(True),
        stop_kind_rank_sql(t, child_alias_factory) == STOP_KIND_ORDER.index(HIDDEN_DONE_KIND),
    )


def stop_kind_rank_sql(task_alias: Any, child_alias_factory: Callable[[], Any] = _default_child_alias):
    """The Stopped section's first ordering term: the placed row's `STOP_KIND_ORDER` position (its own
    stop, else its children's first). NULL for a row that is not stopped."""
    t = task_alias
    c = child_alias_factory()
    child_rank = (
        select(func.min(_row_kind_rank(c))).select_from(c).where(_live_children_of(t, c)).scalar_subquery()
    )
    return func.coalesce(_row_kind_rank(t), child_rank)


def in_progress_rank_sql(task_alias: Any):
    """The In progress section's first ordering term: running → delivering an answer → queued."""
    return case(
        *((task_alias.state == state, i) for i, state in enumerate(IN_PROGRESS_ORDER)),
        else_=len(IN_PROGRESS_ORDER),
    )


def board_rank_sql(task_alias: Any, child_alias_factory: Callable[[], Any] = _default_child_alias):
    """The board's SCREEN order as one integer: `SECTIONS` order, and inside Stopped / In progress the
    `STOP_KIND_ORDER` / `IN_PROGRESS_ORDER` rank — what `GET /api/tasks?order=section` sorts by, so a
    lazy-loaded window fills in the order the State board draws it. `section_sql`'s placement, with ONE
    correlated child read: the children's first stop kind, NULL when none is stopped."""
    t = task_alias
    c = child_alias_factory()
    child_rank = (
        select(func.min(_row_kind_rank(c))).select_from(c).where(_live_children_of(t, c)).scalar_subquery()
    )
    in_progress_base = len(STOP_KIND_ORDER)
    draft_rank = in_progress_base + len(IN_PROGRESS_ORDER) + 1
    working = case((t.state == DRAFT, draft_rank), else_=in_progress_base + in_progress_rank_sql(t))
    stop_rank = func.coalesce(_row_kind_rank(t), child_rank)
    hidden_done = and_(t.hidden.is_(True), stop_rank == STOP_KIND_ORDER.index(HIDDEN_DONE_KIND))
    return case(
        (t.state == DONE, draft_rank + 1), (hidden_done, draft_rank + 1), else_=func.coalesce(stop_rank, working),
    )


# ── Stage rank: an epic's children in pipeline order, from the dependency edges ───────────────────────
def stage_ranks(
    children: Sequence[tuple[int, Optional[int]]], edges: Iterable[tuple[int, int]],
) -> dict[int, int]:
    """`{child_id: rank}` — each child's 0-based position when the children are sorted by (longest-path
    depth over the `(task_id, depends_on_task_id)` edges among them, `phase_index`, `id`).

    `children` is `(id, phase_index)` of the NON-ARCHIVED children; edges leaving that set are ignored.
    Nothing here reads a persona: the pipeline shape is the edges. A cycle (the app layer refuses one)
    counts its closing edge as absent rather than recursing forever."""
    ids = {cid for cid, _ in children}
    prereqs: dict[int, list[int]] = {cid: [] for cid in ids}
    for task_id, depends_on in edges:
        if task_id in ids and depends_on in ids:
            prereqs[task_id].append(depends_on)
    depth: dict[int, int] = {}
    visiting: set[int] = set()

    def depth_of(cid: int) -> int:
        if cid in depth:
            return depth[cid]
        visiting.add(cid)
        d = 0
        for p in prereqs[cid]:
            if p not in visiting:
                d = max(d, depth_of(p) + 1)
        visiting.discard(cid)
        depth[cid] = d
        return d

    order = sorted(children, key=lambda ch: (depth_of(ch[0]), ch[1] is not None, ch[1] or 0, ch[0]))
    return {cid: i for i, (cid, _) in enumerate(order)}


def stage_ranks_for(session: "Session", parent_ids: Sequence[int]) -> dict[int, int]:
    """`stage_ranks` for every non-archived child of `parent_ids`, in two queries."""
    from app.models.tasks import Task, TaskDependency

    if not parent_ids:
        return {}
    rows = session.execute(
        select(Task.id, Task.parent_task_id, Task.phase_index)
        .where(Task.parent_task_id.in_(parent_ids), Task.archived_at.is_(None))
    ).all()
    if not rows:
        return {}
    ids = [r.id for r in rows]
    edges = session.execute(
        select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(TaskDependency.task_id.in_(ids))
    ).all()
    by_parent: dict[int, list[tuple[int, Optional[int]]]] = {}
    for r in rows:
        by_parent.setdefault(r.parent_task_id, []).append((r.id, r.phase_index))
    ranks: dict[int, int] = {}
    edge_list = [(e.task_id, e.depends_on_task_id) for e in edges]
    for kids in by_parent.values():
        ranks.update(stage_ranks(kids, edge_list))
    return ranks


def stopping_children(session: "Session", parent_ids: Sequence[int]) -> Mapping[int, Any]:
    """`{parent_id: child Task}` — for each parent whose own row is NOT stopped, the non-archived child
    that places it in Stopped (`place`'s choice). Parents with no stopped child are absent. Three queries
    for the whole page, whatever its size."""
    from app.models.tasks import Task

    if not parent_ids:
        return {}
    stopped = session.scalars(
        select(Task).where(Task.parent_task_id.in_(parent_ids), Task.archived_at.is_(None), _task_row_stopped())
    ).all()
    if not stopped:
        return {}
    ranks = stage_ranks_for(session, sorted({t.parent_task_id for t in stopped}))
    chosen: dict[int, Any] = {}
    by_id = {t.id: t for t in stopped}
    grouped: dict[int, list[StageCandidate]] = {}
    for t in stopped:
        grouped.setdefault(t.parent_task_id, []).append(
            StageCandidate(t.id, t.state, t.stop_reason, ranks.get(t.id, 0))
        )
    for parent_id, candidates in grouped.items():
        # The parent's own state is irrelevant to the child choice; `place` is asked as a working row.
        _, _, child = place(ACTIVE, None, candidates)
        if child is not None:
            chosen[parent_id] = by_id[child.id]
    return chosen
