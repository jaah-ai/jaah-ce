"""The four run metrics for any task, derived in code: wall time, agentic time, tokens, dollars.

Derived from the `activity_events` transition trail and `Task.metrics`, whose captures carry their dollars as
priced when written (`app/model_prices.py::lock_capture_costs`), so they answer for any task alike and a
finished task's cost never moves. The clock reads only the visible trail
(`app/task_artifacts.py::activity_row_is_visible`), so a reset task answers for its current run.

Each number has a status (a missing capture and a genuine zero differ; don't-know is its own value).
Dollars are Efficiency's per-session rule (`app/session_efficiency.py`): the counted tokens priced, plus any
cost an `acp` agent reported itself (`reported_cost_usd`, `_reported_cost`), flagged, so the two pages agree.
Wire contract: `apps/web/src/types.ts::TaskRunMetrics`. The Details tile and the board's
`run_aggregates` (cost and token columns, one scope walk) share `fold_captures`, `price_fold` and the
recursive scope, so one task cannot read two numbers."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Protocol, TypeVar

from sqlalchemy import ARRAY, BigInteger, ColumnElement, Text, any_, bindparam, func, select
from sqlalchemy.orm import Session

from app.persona_registry import PERSONA_CHARTER
from app.entity_shape import iso_or_none
from app.model_prices import (
    PRICE_SCALE,
    TOKEN_CLASSES,
    capture_cost,
    captures,
    merge_usage,
    prices_by_model,
    read_locked,
)
from app.models.agent_sessions import JaahSession
from app.models.audit import ActivityEvent
from app.models.metering import ModelPrice, TokenUsageCall
from app.models.tasks import Task
from app.task_artifacts import activity_row_is_visible
from app.task_lifecycle import (
    ACTIVE,
    AWAITING_RESPONSE,
    DONE,
    DRAFT,
    PAUSED,
    READY,
)

log = logging.getLogger("app.task_run_metrics")

# Wire rounding for dollars, from the rate columns' own scale. The fold itself stays exact.
_MONEY_QUANTUM = Decimal(1).scaleb(-PRICE_SCALE)
_MICROS_PER_USD = Decimal(1_000_000)
# The keys that make a `Task.metrics` blob a token capture (`app/task_metrics_ingest.py` writes all three).
_TOKEN_KEYS = frozenset(("tokens_in", "tokens_out", "sessions"))

# States in which a run has not ended. Not `task_lifecycle._LIVE_STATES` (the delete guard, which omits
# `paused`). `draft` has not started; `done`/`system_failed` have ended.
_UNFINISHED_STATES = frozenset((READY, ACTIVE, AWAITING_RESPONSE, PAUSED))

# States in which no capture is due yet; not the complement of the set above. The sweep reads a
# transcript once the runner exits, so `paused` is capturable (`task_lifecycle._RUNNER_END_STATES`)
# while the clock still counts it running; one set would excuse a paused task that never captured.
_CAPTURE_PENDING_STATES = frozenset((READY, ACTIVE, AWAITING_RESPONSE))


def _count(value: Any) -> Optional[int]:
    """A JSONB token counter as an int; `None` when the blob holds no whole count.

    `Task.metrics` is node-written, so skew can put a string, bool or float there. An integral float is
    accepted; anything else is don't-know, never a silent 0 (which would read as a measured zero). An
    absent key is 0."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _own_counts(metrics: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """This one task's own conflated `(tokens_in, tokens_out)`; `None` for a count it cannot state.

    The top level is not always the row's own: `routes.tasks.execute_finish_phase` overwrites a parent's
    `tokens_in`/`tokens_out` with its children's sum. `metrics["sessions"]` (`app.task_metrics_ingest`) is
    per session of this task and untouched by that, so it is used where present; a rollup without it
    answers don't-know. All-or-nothing over sessions: a partial sum would be a confident understatement."""
    if "finish_report" not in metrics:
        return _count(metrics.get("tokens_in")), _count(metrics.get("tokens_out"))
    sessions = metrics.get("sessions")
    if not isinstance(sessions, dict) or not sessions:
        return None, None
    total_in = total_out = 0
    for entry in sessions.values():
        if not isinstance(entry, dict):
            return None, None
        entry_in, entry_out = _count(entry.get("tokens_in")), _count(entry.get("tokens_out"))
        if entry_in is None or entry_out is None:
            return None, None
        total_in, total_out = total_in + entry_in, total_out + entry_out
    return total_in, total_out


def _reported_cost(entry: Any) -> Optional[Decimal]:
    """The cost an `acp` agent reported itself on one `metrics["sessions"]` entry
    (`task_metrics_ingest.fold_session_metrics(reported_cost_usd=)`), or `None` when it reported none. Read as
    Efficiency reads it (`session_efficiency._stream_usage`): an unparseable value is no report, and so is a
    non-finite or negative one (a node's figure must not break or offset the board's total)."""
    if not isinstance(entry, dict) or entry.get("reported_cost_usd") is None:
        return None
    try:
        value = Decimal(str(entry["reported_cost_usd"]))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def _scope_tasks(
    root_id: int, session: Session, *, within: Optional[ColumnElement[bool]] = None,
) -> list[Task]:
    """Every `Task` row in `root_id`'s subtree, the root included, at any depth, id-ascending.

    One recursive CTE returning rows (callers need `state` and `metrics`). Not
    `routes.tasks.task_wire._subtree_task_ids`: that module imports this one. `union`, never `union_all`: Postgres
    has no recursion limit, so a `parent_task_id` cycle would spin until temp disk ran out; `UNION` also
    makes a self-parented row appear once. `run_aggregates` walks the same edge the same way, multi-root.

    `within` narrows the DESCENDANTS walked to the rows satisfying that `tasks` clause, exactly as
    `run_aggregates` takes it: a descendant the clause refuses is pruned with its own subtree, so a reader
    whose audience may not see every child (Live Cast) measures only what it may show."""
    # No authz filter: the route authorizes the root, and a child is filed into its parent's project and
    # cannot be moved out alone. Revision 0297 kept the rare parent/child pair it found spanning two
    # projects; their metrics read as the root's.
    tree = select(Task.id).where(Task.id == root_id).cte("run_metrics_scope", recursive=True)
    step = select(Task.id).where(Task.parent_task_id == tree.c.id)
    if within is not None:
        step = step.where(within)
    tree = tree.union(step)
    return list(session.scalars(select(Task).join(tree, Task.id == tree.c.id).order_by(Task.id)))


def _state_trails(task_ids: list[int], session: Session) -> dict[int, list[tuple[datetime, str]]]:
    """Each task's transition timeline as `(when, resulting state)`, oldest first; two queries for the whole
    id set (rows, and the Reset watermarks).

    Rows hold the post-transition state (`record_task_transition`), so a state's duration is the gap to
    the next row. Verb kinds (`task:updated`, `task:stop`) are kept: they split an interval without
    changing its total, and filtering would need every verb enumerated.

    Obeys the reset watermark (`app/task_artifacts.py::activity_row_is_visible`), keyed per owning task:
    one scalar mark would apply one task's reset to a sibling. The marks are read here rather than passed
    in, since a forgotten argument would fail open. The test runs in Python, since SQL would need the
    `(data->>'id')::int` cast `app/routes/tasks/activity.py::get_task_activity` refuses.

    The id set binds as ONE array parameter (`= ANY(...)`), not one parameter per id: the operator dashboard
    asks for a year's tasks at once, where an expanded `IN` costs more to render than to run."""
    hidden_before = dict(session.execute(
        select(Task.id, Task.artifacts_hidden_before)
        .where(Task.id == any_(bindparam("trail_task_ids", list(task_ids), type_=ARRAY(BigInteger))))
    ).all())
    rows = session.execute(
        select(
            ActivityEvent.data["id"].astext,
            ActivityEvent.data["state"].astext,
            ActivityEvent.created_at,
        )
        .where(
            ActivityEvent.kind.like("task:%"),
            ActivityEvent.data["id"].astext
            == any_(bindparam("trail_task_keys", [str(i) for i in task_ids], type_=ARRAY(Text))),
        )
        .order_by(ActivityEvent.created_at, ActivityEvent.id)
    ).all()
    trails: dict[int, list[tuple[datetime, str]]] = {tid: [] for tid in task_ids}
    for id_str, state, created_at in rows:
        # A `task:%` row with no `data.state` carries no timeline. Dropped, not defaulted.
        if state is None:
            continue
        owner = int(id_str)
        # A task id absent from the map is not hidden (as in `get_task_activity`); unreachable anyway.
        if not activity_row_is_visible(created_at, hidden_before.get(owner)):
            continue
        trails[owner].append((created_at, state))
    return trails


def _agentic_seconds(
    trail: list[tuple[datetime, str]], current_state: str, now: datetime, since: datetime
) -> float:
    """Seconds this one task spent in `active` at or after `since`, over every `active` interval.

    Other states (`ready`, `awaiting_response`, `paused`) are excluded by construction. `since` is the
    scope's go instant; clipping to it keeps an abandoned run's `active` time out, so this stays within
    the wall clock. An interval straddling `since` counts only its later part."""
    total = 0.0
    for idx, (at, state) in enumerate(trail):
        if state != ACTIVE:
            continue
        if idx + 1 < len(trail):
            end = trail[idx + 1][0]
        elif current_state == ACTIVE:
            end = now
        else:
            # The trail ends on `active` but the row has moved on (terminal event missing): score 0 rather than
            # grow to `now()` forever.
            continue
        total += max(0.0, (end - max(at, since)).total_seconds())
    return total


def active_seconds(session: Session, tasks: list[Task]) -> dict[int, float]:
    """`{task_id: seconds spent in active}` over each task's whole visible trail — what a run took, without
    the time it queued for a slot."""
    if not tasks:
        return {}
    trails = _state_trails([t.id for t in tasks], session)
    now = datetime.now(timezone.utc)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return {t.id: _agentic_seconds(trails[t.id], t.state, now, epoch) for t in tasks}


def first_active_at(session: Session, tasks: list[Task]) -> dict[int, datetime]:
    """`{task_id: when it first went active}` over each task's visible trail; a task that never ran is absent."""
    if not tasks:
        return {}
    trails = _state_trails([t.id for t in tasks], session)
    return {
        t.id: at for t in tasks
        for at in [next((at for at, state in trails[t.id] if state == ACTIVE), None)] if at is not None
    }


def _token_status(expected: int, captured: int) -> str:
    """Whether the token counts cover every task in scope that actually ran.

    `expected` counts tasks that ran a runner of their own; `captured` those whose `metrics` counts could
    be read. Nothing ran ⇒ `measured` (the zero is real); a runner ran and reported nothing ⇒ `missing`."""
    if expected and not captured:
        return "missing"
    if captured < expected:
        return "partial"
    return "measured"


class _CaptureRow(Protocol):
    """The three attributes the capture fold and the pricing read off a task row.

    A protocol rather than `Task` because the fold has two callers: `compute_run_metrics` passes ORM rows,
    `run_aggregates` passes narrow tuples (hydrating 500+ rows with unbounded JSONB is out). Read-only
    properties, because an ORM `Mapped[str]` satisfies a property but not a bare attribute annotation."""

    @property
    def id(self) -> int: ...

    @property
    def persona(self) -> str: ...

    @property
    def metrics(self) -> Optional[dict[str, Any]]: ...


_RowT = TypeVar("_RowT", bound=_CaptureRow)


def _ran(scope: Iterable[_RowT]) -> list[_RowT]:
    """The rows in `scope` that ever ran a runner of their own; containers excluded.

    `PERSONA_CHARTER` is the dispatch discriminator, so a persona absent from it (`epic`) never
    reached a runner. Not "is someone's parent": a task can be both a parent and a runner."""
    return [t for t in scope if t.persona in PERSONA_CHARTER]


def fold_captures(
    scope: Iterable[_CaptureRow],
) -> tuple[int, int, dict[str, dict[str, int]], bool, set[int]]:
    """One pass over the captures in `scope` → `(tokens_in, tokens_out, usage_by_model, unaccounted,
    unreadable_ids)`; all readings of the same blob, so read together.

    Applies `_ran` itself: containers can carry a legacy Finisher rollup equal to their children's sum,
    so folding them in reports double. The filter lives inside so no caller can forget it."""
    tokens_in = tokens_out = 0
    usage: dict[str, dict[str, int]] = {}
    unaccounted = False
    unreadable: set[int] = set()
    for task in _ran(scope):
        metrics = task.metrics or {}
        if metrics.get("usage_incomplete") is True:
            unaccounted = True
        by_model = metrics.get("usage_by_model")
        own = merge_usage({}, by_model if isinstance(by_model, dict) else None)
        merge_usage(usage, own)
        # Read past the pollutant rather than skip the row: `_own_counts` takes the row's own totals from its
        # per-session substrate, or says it cannot, so tokens and `usage_by_model` answer for the same spend.
        task_in, task_out = _own_counts(metrics)
        priced_in = sum(c[cls] for c in own.values() for cls in TOKEN_CLASSES if cls != "output")
        priced_out = sum(c["output"] for c in own.values())
        # A session whose agent reported its own cost counted those tokens in its totals but not in
        # `usage_by_model` (`session_events.UsageTotals`); the reported cost accounts for them.
        sessions = metrics.get("sessions")
        for entry in sessions.values() if isinstance(sessions, dict) else ():
            if _reported_cost(entry) is None:
                continue
            entry_usage = merge_usage({}, entry.get("usage_by_model"))
            priced_in += (_count(entry.get("tokens_in")) or 0) - sum(
                c[cls] for c in entry_usage.values() for cls in TOKEN_CLASSES if cls != "output")
            priced_out += (_count(entry.get("tokens_out")) or 0) - sum(c["output"] for c in entry_usage.values())
        # Per session the four input classes sum to `tokens_in` and `output` to `tokens_out`
        # (`apps/jaahd/session_sweep.py`, `app/task_metrics_ingest.py`), so a divergence is spend the priced
        # fold cannot see. Checked instead of `not by_model`, which missed a partially modern task.
        if (priced_in, priced_out) != (task_in or 0, task_out or 0):
            unaccounted = True
        if task_in is None or task_out is None:
            # A count this row cannot state leaves nothing to check the invariant against, so both figures are
            # demoted.
            unreadable.add(task.id)
            unaccounted = True
        tokens_in += task_in or 0
        tokens_out += task_out or 0
    return tokens_in, tokens_out, usage, unaccounted, unreadable


def jaah_charges(session: Session, task_ids: Iterable[int]) -> dict[int, int]:
    """`{task_id: Σ charge_micros}` for each of `task_ids` that ran on the Jaah account (a runner `jaah_sessions`
    row). Such a task's dollars are what the tenant was CHARGED — its ledger debits, live while it runs — not its
    captured tokens priced at the list rates (AC3.5). Two queries; none when no task ran on it."""
    ids = list(task_ids)
    if not ids:
        return {}
    jaah = set(session.scalars(
        select(JaahSession.task_id).where(JaahSession.kind == "runner", JaahSession.task_id.in_(ids)).distinct()
    ))
    if not jaah:
        return {}
    sums = dict(session.execute(
        select(TokenUsageCall.task_id, func.sum(TokenUsageCall.charge_micros))
        .where(TokenUsageCall.task_id.in_(list(jaah)))
        .group_by(TokenUsageCall.task_id)
    ).all())
    return {tid: int(sums.get(tid) or 0) for tid in jaah}


def _add_charges(
    cost: Optional[str], cost_status: str, usage: Mapping[str, Mapping[str, int]], charged: list[int],
) -> tuple[Optional[str], str]:
    """`price_fold`'s pair with the Jaah-account charges in scope added. `usage` is the fold of the scope's
    OTHER tasks: their spend nobody could price leaves the figure `partial`, and with none the charges alone
    are a measured figure."""
    if not charged:
        return cost, cost_status
    total = Decimal(sum(charged)) / _MICROS_PER_USD
    if cost is not None:
        total += Decimal(cost)
    else:
        cost_status = "partial" if any(sum(c.values()) > 0 for c in usage.values()) else "measured"
    return str(total.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)), cost_status


def live_rates(session: Session, rows: Iterable[_CaptureRow]) -> Mapping[str, Mapping[str, Decimal]]:
    """The rate table, read only when some runner row in `rows` holds a capture never priced (one written
    before captures were locked, or by the previous release mid-deploy); `{}` otherwise, so a page of
    finished tasks reads no rates at all."""
    for row in _ran(rows):
        if any(read_locked(locked) is None for _, locked in captures(row.metrics or {})):
            return prices_by_model(session.scalars(select(ModelPrice)))
    return {}


class ScopeCost(NamedTuple):
    """The runner rows' dollars, each capture as it was priced when written (`model_prices.captures`), plus
    every cost an agent reported itself (`_reported_cost`)."""

    usd: Decimal
    #: Models with spend some capture could not price: the figure is a floor.
    unpriced_models: list[str]
    #: Whether any capture priced any spend, or any agent reported a cost; without one the cost is
    #: `unavailable`, not $0.
    priced_any: bool
    #: Whether `usd` includes an agent-reported cost (Efficiency's `cost_source: agent_reported`).
    agent_reported: bool = False


class RunCost(NamedTuple):
    """One task's subtree cost on the board (`run_aggregates`): `price_fold`'s pair, and whether the figure
    includes an agent-reported cost."""

    usd: str
    status: str
    agent_reported: bool


def scope_cost(
    scope: Iterable[_CaptureRow], prices: Mapping[str, Mapping[str, Decimal]],
) -> ScopeCost:
    """`ScopeCost` over the runner rows in `scope`, capture by capture: a stored figure is never re-priced,
    and live rates price only a capture with none. Per capture, never per summed blob, since one model can
    be priced in one capture and unpriced in another. A session's agent-reported cost is ADDED to its priced
    tokens, as Efficiency adds it (`session_efficiency.compute`'s drill-down): per-turn reported tokens are
    never in `usage_by_model`, and a running total's session is no priced capture (`prices_tokens`), so
    nothing is counted twice."""
    total = Decimal(0)
    unpriced: set[str] = set()
    priced_any = False
    agent_reported = False
    for row in _ran(scope):
        metrics = row.metrics or {}
        for usage, locked in captures(metrics):
            usd, missing = capture_cost(usage, locked, prices)
            total += usd
            unpriced.update(missing)
            priced_any = priced_any or any(
                model not in missing and sum(classes.values()) > 0 for model, classes in usage.items())
        sessions = metrics.get("sessions")
        for entry in sessions.values() if isinstance(sessions, dict) else ():
            reported = _reported_cost(entry)
            if reported is not None:
                total += reported
                priced_any = agent_reported = True
    return ScopeCost(total, sorted(unpriced), priced_any, agent_reported)


def price_fold(
    priced: ScopeCost,
    unaccounted: bool,
    root_id: int,
) -> tuple[Optional[str], str]:
    """A `scope_cost` → `(cost_usd, cost_status)`. `cost_usd` is a decimal string, `None` exactly when the
    status is `unavailable`; returned as one pair to keep them consistent. `root_id` is only for the log
    line."""
    total_usd, unpriced_models, priced_any, _ = priced
    # `unavailable` means nothing could be priced. Any omitted spend makes the displayed cost a floor,
    # whether it came from a legacy capture or a model whose exact rate is unknown.
    if not priced_any:
        return None, "unavailable"
    try:
        return (
            str(total_usd.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)),
            "partial" if unaccounted or unpriced_models else "measured",
        )
    except InvalidOperation:
        # A stored count exceeding the Decimal context has no figure. Bounding belongs at ingest; here it must
        # not 500 the read (it would break the page and every ancestor's).
        log.warning(
            "run-metrics: task %s cost %s exceeds the decimal context — reporting unavailable",
            root_id, total_usd,
        )
        return None, "unavailable"


def compute_run_metrics(
    task_id: int, session: Session, *, within: Optional[ColumnElement[bool]] = None,
) -> Optional[dict[str, Any]]:
    """The four run metrics for `task_id` and every descendant (`within`: every descendant the clause admits,
    `_scope_tasks`). `None` when the task does not exist.

    At most five queries regardless of tree size: subtree, Reset watermarks (read by `_state_trails` itself),
    the transition trail, the Jaah-account sessions (a fifth, the charges, when one ran) and the price table
    only for a capture never priced (`live_rates`). No per-task I/O, which is why this is a separate endpoint
    rather than a field on `TaskOut`."""
    scope = _scope_tasks(task_id, session, within=within)
    root = next((t for t in scope if t.id == task_id), None)
    if root is None:
        return None
    scope_ids = [t.id for t in scope]
    trails = _state_trails(scope_ids, session)
    now = datetime.now(timezone.utc)

    # The go instant is the root's first transition out of `draft` after its last reset (`ready` for a
    # dispatched task, `active` for an epic), so queue wait is included and an abandoned run is not.
    # A Reset's watermark already hides the abandoned attempt, so `last_reset` is usually `-1`; the scan
    # still guards other ways a `draft` state re-enters the visible trail.
    root_trail = trails[root.id]
    last_reset = max((i for i, (_, state) in enumerate(root_trail) if state == DRAFT), default=-1)
    started_at = next((at for at, state in root_trail[last_reset + 1:] if state != DRAFT), None)

    # Still running while any task in scope is non-terminal, the root included. `_UNFINISHED_STATES`, not
    # `_LIVE_STATES`, which omits `paused` and would report a stopped run as finished.
    in_progress = any(t.state in _UNFINISHED_STATES for t in scope)
    last_done = max(
        (at for tid in scope_ids for at, state in trails[tid] if state == DONE), default=None
    )
    ended_at = None if in_progress else last_done

    # Which tasks in scope ever ran a session (see `_ran`). The fold re-applies it internally.
    ran = _ran(scope)

    total_seconds: Optional[float] = None
    agentic_seconds: Optional[float] = None
    if started_at is not None:
        # Where the clock stops for a settled scope that never reached `done`: the last event, not `now()`.
        last_event = max((at for tid in scope_ids for at, _ in trails[tid]), default=started_at)
        end = ended_at or (now if in_progress else last_event)
        total_seconds = max(0.0, (end - started_at).total_seconds())
        # Containers excluded: an epic's own `active` span covers the whole run and would nearly double the
        # figure. Concurrent siblings can legitimately push this past `total_seconds`.
        agentic_seconds = sum(
            _agentic_seconds(trails[t.id], t.state, now, started_at) for t in ran
        )

    # The trail postdates many live rows, so a run can have no timeline; only a task still in `draft` with
    # no capture reads `not_started`. A missing start otherwise is a gap in the evidence.
    if started_at is not None:
        clock_status = "measured"
    elif root.state != DRAFT or any(t.metrics is not None for t in scope):
        clock_status = "unavailable"
    else:
        clock_status = "not_started"

    # The shared capture fold (see `fold_captures`); `scope`, not `ran`, since it applies `_ran` itself.
    tokens_in, tokens_out, usage, unaccounted, unreadable = fold_captures(scope)

    # A capture is expected once a task ran and its runner stopped: the sweep reads a transcript only
    # after exit, and a task that never reached `active` never made one.
    expected = [
        t for t in ran
        if t.state not in _CAPTURE_PENDING_STATES and any(s == ACTIVE for _, s in trails[t.id])
    ]
    token_status = _token_status(
        len(expected),
        sum(1 for t in expected if t.metrics is not None and t.id not in unreadable),
    )

    # A Jaah-account task's dollars are its charges, so its capture is left out of the priced fold.
    charges = jaah_charges(session, (t.id for t in ran))
    priced_rows = [t for t in scope if t.id not in charges]
    if charges:
        _, _, usage, unaccounted, _ = fold_captures(priced_rows)
    priced = scope_cost(priced_rows, live_rates(session, priced_rows))
    cost, cost_status = price_fold(priced, unaccounted, root.id)
    cost, cost_status = _add_charges(cost, cost_status, usage, list(charges.values()))
    # Only this payload carries the unpriced names; `scope_cost` is their single home.
    unpriced_models = priced.unpriced_models

    return {
        "task_id": root.id,
        "scope_task_ids": scope_ids,
        "started_at": iso_or_none(started_at),
        "clock_status": clock_status,
        "ended_at": iso_or_none(ended_at),
        "in_progress": in_progress,
        "total_seconds": total_seconds,
        "agentic_seconds": agentic_seconds,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "token_status": token_status,
        "cost_usd": cost,
        "cost_status": cost_status,
        "unpriced_models": unpriced_models,
    }


def _token_capture(row: _CaptureRow) -> Optional[int]:
    """This row's own all-class token total, or `None` when it holds no readable token capture.

    A blob is a capture only if it carries token keys: other writers share `Task.metrics` (the merge lane's
    `merge_lane_prs`), and `_count` reads an absent key as 0, which would turn "never measured" into a
    confident zero. A count `_own_counts` cannot state is `None` too, never a partial sum."""
    metrics = row.metrics
    if not isinstance(metrics, dict) or not (_TOKEN_KEYS & metrics.keys()):
        return None
    tokens_in, tokens_out = _own_counts(metrics)
    if tokens_in is None or tokens_out is None:
        return None
    return tokens_in + tokens_out


def run_aggregates(
    session: Session, task_ids: list[int], *, within: Optional[ColumnElement[bool]] = None,
) -> tuple[dict[int, RunCost], dict[int, int]]:
    """`({task_id: RunCost}, {task_id: tokens_total})` for a whole page of tasks, in two
    queries (four when a task in it ran on the Jaah account, whose dollars are its charges — `jaah_charges`).

    The board's Cost and Tokens columns from ONE scope walk: the same figure as `compute_run_metrics`'
    tile, since the scope CTE, `fold_captures` and `price_fold` are shared. `cost_usd` is a decimal string
    (`routes/tasks/activity.py::TaskRunMetricsOut.cost_usd`).

    Rolled up over the subtree via the same recursive walk, seeded multi-root with `(root, node)` pairs.
    `union` for `_scope_tasks`' reason; it also counts a self-parented row once. Narrow columns only, and
    `_ran`'s persona predicate is pushed into SQL (containers carry the fattest blobs); `_ran` still runs
    inside the fold. One query whatever the page size: dollars are read as each capture was priced, and the
    rate table only for a capture never priced (`live_rates`).

    A task whose cost is `unavailable`, or that does not exist, is absent from the cost map (as in
    `app/task_ratings.py::rating_aggregates`); keys are only ever requested ids.

    Tokens are `tokens_in + tokens_out` (every class: the conflated counts are the four input classes plus
    `output`) over the WHOLE scope — a Jaah-account row included, since its tokens are tokens even when its
    dollars are charges. A root is in the tokens map only when some runner row in its scope is a readable
    token capture (`_token_capture`); otherwise it is absent, since "nothing captured" is not a measured 0,
    while a captured real zero stays 0. A row whose counts cannot be read contributes nothing (the tile's
    `token_status` is where that doubt is stated). Live while a session runs: the node's in-progress usage
    post (`routes/agent_sessions.py::report_live_usage`) folds into the same `metrics`.

    `within` narrows the DESCENDANTS walked to the rows satisfying that `tasks` clause — a reader whose
    audience may not see every child (Live Cast, `routes/live_public.py`) rolls up only what it may show,
    so a hidden child's spend never surfaces as the remainder of its parent's figure. The roots themselves
    are the caller's to authorize, as below; a descendant the clause refuses is pruned with its own subtree."""
    if not task_ids:
        return {}, {}
    # No authz filter, on `_scope_tasks`' grounds: the caller authorized every listed id, and every row
    # read is visible to a holder of the root's grant.
    tree = (
        select(Task.id.label("root"), Task.id.label("node"))
        .where(Task.id.in_(task_ids))
        .cte("cost_scope", recursive=True)
    )
    step = select(tree.c.root, Task.id).where(Task.parent_task_id == tree.c.node)
    if within is not None:
        step = step.where(within)
    tree = tree.union(step)
    rows = session.execute(
        select(tree.c.root, Task.id, Task.persona, Task.metrics)
        .select_from(tree)
        .join(Task, Task.id == tree.c.node)
        .where(Task.persona.in_(list(PERSONA_CHARTER)))
    ).all()
    prices = live_rates(session, rows)

    # Keyed by the requested ids, not by `rows`: a requested id with no priceable row must still be asked.
    # A row lands in several scopes when an ancestor was requested too, which is the rollup.
    scopes: dict[int, list[_CaptureRow]] = {tid: [] for tid in task_ids}
    for row in rows:
        scopes[row.root].append(row)

    charges = jaah_charges(session, {row.id for row in rows})

    costs: dict[int, RunCost] = {}
    tokens: dict[int, int] = {}
    for tid, scope in scopes.items():
        # The priced fold leaves the charged rows out (as the tile does); tokens count every row.
        priced_rows = [row for row in scope if row.id not in charges]
        _, _, usage, unaccounted, _ = fold_captures(priced_rows)
        priced = scope_cost(priced_rows, prices)
        cost, cost_status = price_fold(priced, unaccounted, tid)
        cost, cost_status = _add_charges(cost, cost_status, usage, [charges[row.id] for row in scope if row.id in charges])
        # `cost is None` is `cost_status == "unavailable"` (returned as a pair), so only figured rows are kept.
        if cost is not None:
            costs[tid] = RunCost(cost, cost_status, priced.agent_reported)
        # Absence, not 0, when nothing in scope was captured (the SQL already kept only `_ran` personas).
        captured = [counts for counts in map(_token_capture, scope) if counts is not None]
        if captured:
            tokens[tid] = sum(captured)
    return costs, tokens


def cost_aggregates(session: Session, task_ids: list[int]) -> dict[int, tuple[str, str]]:
    """`run_aggregates`' cost map alone as `(cost_usd, cost_status)` pairs, for callers that show no token
    column and no agent-reported flag (the operator dashboard)."""
    return {tid: (cost.usd, cost.status) for tid, cost in run_aggregates(session, task_ids)[0].items()}
