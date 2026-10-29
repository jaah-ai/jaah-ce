"""One session's captured tokens/usage/explored-targets, folded into a task's `Task.metrics` JSONB.

The DB write behind the session-transcript report (`routes/agent_sessions.py::report_transcript`),
plus the wire validation for those numbers. jaahd derives them host-side from the runner's JSONL
transcript and sends raw per-session figures, never an aggregate.

The node is not a trust boundary: a runner appending a crafted transcript line picks these numbers,
so each ceiling below bounds a known blast radius rather than a column width.
"""
from __future__ import annotations

from typing import Annotated, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.actors import task_actor_values
# Imported, not re-spelled: a second spelling would make a captured class silently unpriceable.
from app.model_prices import (
    COST_BY_MODEL, REPORTED_CUMULATIVE_COST, TOKEN_CLASSES, lock_capture_costs, merge_usage, prices_by_model,
)
from app.models.metering import ModelPrice
from app.models.tasks import Task
# A name longer than `model_prices.name` accepts could never be priced.
from app.entity_shape import MAX_NAME as MAX_MODEL_NAME

# Above ~10**24 `Decimal.quantize` in `app.task_run_metrics` raises, a permanent 500 on run-metrics
# for the task and every ancestor epic; this ceiling turns that into a 422 naming model and class.
MAX_TOKEN_COUNT = 10 ** 15
# A real session touches one to three models. Bounded because `TaskOut` serializes `Task.metrics`
# whole, each capture stores the blob twice, and unknown names are reflected via `unpriced_models`.
MAX_USAGE_MODELS = 50
# Sessions accumulate under `metrics["sessions"]` and nothing removes one; real tasks settle in
# single digits.
MAX_METRICS_SESSIONS = 100
# Explored targets are read from a prompt-injectable runner's own transcript and stored twice.
# The list cap equals the node's `_MAX_EXPLORED_TARGETS` (`apps/jaahd/session_sweep.py`), so a
# well-behaved node never trips it; the node does not truncate length, and the per-element cap sits far
# above any real path or pattern.
MAX_EXPLORED_TARGETS = 500
MAX_EXPLORED_TARGET_LEN = 1000
# An element type: `Field(max_length=...)` on the list bounds only the list.
ExploredTarget = Annotated[str, Field(max_length=MAX_EXPLORED_TARGET_LEN)]


class CapturedUsageMixin(BaseModel):
    """The two captured-measurement fields and their validation, as a mixin a wire model inherits,
    so the route and this fold share one spelling of every bound."""

    # Per model and per priced class, the only costable shape. Absent -> `{}` is valid (cost reads 0).

    # `StrictInt` because plain `int` coerces `true` to 1, which `merge_usage` would refuse.
    usage_by_model: dict[str, dict[str, StrictInt]] = Field(default_factory=dict)
    # True when the vendor supplied a usage carrier that failed its own partition invariant. A mixed
    # valid/invalid transcript must not turn the valid subset into a confidently complete cost.
    usage_incomplete: StrictBool = False
    # Bounded on both axes (see `MAX_EXPLORED_TARGETS`).
    explored_targets: list[ExploredTarget] = Field(
        default_factory=list, max_length=MAX_EXPLORED_TARGETS)

    @field_validator("usage_by_model")
    @classmethod
    def _bounded_and_known(cls, v: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
        """Reject an unknown class key, a count outside `0 .. MAX_TOKEN_COUNT`, more than
        `MAX_USAGE_MODELS` models, or a model name longer than `MAX_MODEL_NAME`, with a 422.

        The pricing fold drops unknown keys silently, so a typo would vanish into a cost that looks complete.
        Every message names the model (and class) so the rejected capture is attributable."""
        if len(v) > MAX_USAGE_MODELS:
            raise ValueError(
                f"usage_by_model names {len(v)} models; the limit is {MAX_USAGE_MODELS}")
        for model, classes in v.items():
            if len(model) > MAX_MODEL_NAME:
                raise ValueError(
                    f"model name is {len(model)} characters long; the limit is {MAX_MODEL_NAME}")
            unknown = sorted(set(classes) - set(TOKEN_CLASSES))
            if unknown:
                raise ValueError(
                    f"model {model!r}: unknown token class(es) {unknown}; expected {list(TOKEN_CLASSES)}")
            for token_class, count in classes.items():
                if count < 0:
                    raise ValueError(f"model {model!r}: {token_class} count {count} is negative")
                if count > MAX_TOKEN_COUNT:
                    raise ValueError(
                        f"model {model!r}: {token_class} count {count} exceeds the ceiling "
                        f"{MAX_TOKEN_COUNT}")
        return v


class MetricsSessionLimit(Exception):
    """A new `session_id` on a task already holding `MAX_METRICS_SESSIONS` of them.

    Typed, not an `HTTPException`: the transcript report owns the wire status; the caller decides the cost."""


def _locked_task(session: Session, task_id: int) -> Task:
    """The Task row, locked `FOR UPDATE` and re-read: `Task.metrics` is read-modify-written whole, so two
    writers (a live post and a final report, or two sessions of one task) must serialize or one entry is
    lost. A missing Task is a `LookupError`."""
    task = session.get(Task, task_id, with_for_update=True, populate_existing=True)
    if task is None:
        raise LookupError(f"task {task_id} not found")
    return task


def _write_folded(session: Session, task_id: int, metrics: dict, sessions: dict, *, live: bool) -> None:
    """Recompute the top-level fold over every session entry, price the entries not yet priced, and write
    the blob. New information re-prices its own entry at the rates in force; nothing else is re-priced.

    `live=True` pins `Task.updated_at` (and its writer, which a fresh actor against a frozen clock would
    contradict): a progress meter is not an edit, and the board sorts and ages by that clock (precedent:
    `app/dispatch_wait.py::record`)."""
    metrics["sessions"] = sessions
    metrics["tokens_in"] = sum(s.get("tokens_in", 0) for s in sessions.values())
    metrics["tokens_out"] = sum(s.get("tokens_out", 0) for s in sessions.values())
    # `merge_usage` is the pricing side's fold and drops malformed node-written entries.
    usage: dict[str, dict[str, int]] = {}
    for s in sessions.values():
        merge_usage(usage, s.get("usage_by_model"))
    metrics["usage_by_model"] = usage
    metrics["usage_incomplete"] = any(bool(s.get("usage_incomplete")) for s in sessions.values())
    all_targets: dict[str, None] = {}
    for s in sessions.values():
        for target in s.get("explored_targets") or []:
            all_targets[target] = None
    metrics["explored_targets"] = list(all_targets)
    # Each new or replaced entry is priced now; an entry already priced keeps its figure (`lock_capture_costs`).
    lock_capture_costs(metrics, prices_by_model(session.scalars(select(ModelPrice))))
    # On the DB clock, comparable to `activity_events.created_at`. A state stamp cannot stand in:
    # `paused` -> resume -> `paused` repeats a state and would hide the second run's spend.
    metrics["captured_at"] = session.scalar(select(func.now())).isoformat()
    session.execute(
        update(Task).where(Task.id == task_id)
        .values(
            metrics=metrics,
            **(
                {"updated_at": Task.updated_at, "updated_by_id": Task.updated_by_id}
                if live else task_actor_values(session)
            ),
        )
    )


def fold_session_metrics(
    session: Session,
    task_id: int,
    session_id: str,
    *,
    tokens_in: int,
    tokens_out: int,
    usage_by_model: Mapping[str, Mapping[str, int]],
    explored_targets: Sequence[str],
    usage_incomplete: bool = False,
    live: bool = False,
    reported_cost_usd: Optional[str] = None,
    reported_cumulative_cost_usd: Optional[str] = None,
    reported_turn_cost_usd: Optional[str] = None,
    source: Optional[str] = None,
) -> bool:
    """Persist one session's capture under `metrics["sessions"][session_id]`, then recompute the
    top-level `tokens_in`/`tokens_out`/`usage_by_model`/`explored_targets` as the fold over all sessions,
    so a redispatched task is never undercounted to its last session. Returns whether it wrote.

    Idempotent per `session_id` (a re-report overwrites only its entry); this is load-bearing, since a
    session can still be reported twice (a repeated `failed` report, a lease expiry then the node's own
    report). A missing Task is a `LookupError`; a new session past the cap is `MetricsSessionLimit`.

    `tokens_in`/`tokens_out` keep their conflated meaning (the Finish report reads them); the entry's
    `usage_by_model` is priced here, once, and `app.task_run_metrics` reads the stored figure. Does not
    commit: the caller settles the transcript row in the same transaction. Report once, capture once: do
    not reintroduce a pending-capture work-list. No backfill for sessions uploaded before capture shipped (accepted gap).

    Two writers, one entry: a RUNNING session's in-progress post (`routes/agent_sessions.py::
    report_live_usage`, every few minutes) and its final transcript report both key on `session_id`, so the
    final capture REPLACES the last live one rather than adding to it. `live=True` is that post: its entry is
    marked `"live": true` (the final capture carries no mark, so a stranded live entry is recognisable —
    `mark_live_capture_incomplete`), the write pins `Task.updated_at` (`_write_folded`), and an unchanged
    capture writes nothing (returns False) so a quiet session costs no row write and no board event, and
    keeps the price it was written at.

    The Task row is locked first (`_locked_task`), on both paths, so concurrent folds serialize."""
    task = _locked_task(session, task_id)
    metrics = dict(task.metrics or {})
    sessions = dict(metrics.get("sessions") or {})
    # Refuse rather than evict: JSONB does not preserve key order and entries carry no timestamp, so
    # there is no "oldest", and evicting would understate the bill.

    # Re-capturing an existing session stays allowed at any size.
    if session_id not in sessions and len(sessions) >= MAX_METRICS_SESSIONS:
        raise MetricsSessionLimit(
            f"task {task_id} already holds {len(sessions)} captured metrics sessions; the limit "
            f"is {MAX_METRICS_SESSIONS} — re-capturing an existing session_id is still accepted"
        )
    captured: dict = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usage_by_model": dict(usage_by_model),
        "explored_targets": list(explored_targets),
    }
    if usage_incomplete:
        captured["usage_incomplete"] = True
    if source is not None:
        # Who wrote the entry (`session_events.STREAM_SOURCE`), so the stream never builds on a vendor parser's.
        captured["source"] = source
    if reported_cost_usd is not None:
        # A cost the agent reported itself (`session_events.SOURCE_AGENT_REPORTED`), kept beside the priced
        # entry rather than in it: a per-turn cost's tokens are not in `usage_by_model`, and a running total's
        # are never priced (`model_prices.prices_tokens`), so nothing prices it twice.
        captured["reported_cost_usd"] = reported_cost_usd
    # The stream's two halves of that figure (`session_events.UsageTotals`), so its next batch replaces the running
    # total rather than adding to it; `reported_cost_usd` stays the one value every reader reads.
    if reported_cumulative_cost_usd is not None:
        captured[REPORTED_CUMULATIVE_COST] = reported_cumulative_cost_usd
    if reported_turn_cost_usd is not None:
        captured["reported_turn_cost_usd"] = reported_turn_cost_usd
    if live:
        captured["live"] = True
        previous = sessions.get(session_id)
        if isinstance(previous, dict) and {k: v for k, v in previous.items() if k != COST_BY_MODEL} == captured:
            return False
    sessions[session_id] = captured
    _write_folded(session, task_id, metrics, sessions, live=live)
    return True


def mark_live_capture_incomplete(session: Session, task_id: int, session_id: str) -> bool:
    """A session that ENDED without a final capture (its transcript is `unavailable`, or a `failed` report
    carried no usage) may still hold its last live entry, an undercount of an unknown size. Flag that entry
    `usage_incomplete` so the cost reads `partial`, never a confident `measured`. Returns whether it wrote.

    A no-op when the entry is absent, already final (no `live` mark) or already flagged. A later final
    capture for the same session replaces the entry, flag and all."""
    task = _locked_task(session, task_id)
    metrics = dict(task.metrics or {})
    sessions = dict(metrics.get("sessions") or {})
    entry = sessions.get(session_id)
    if not isinstance(entry, dict) or not entry.get("live") or entry.get("usage_incomplete") is True:
        return False
    sessions[session_id] = {**entry, "usage_incomplete": True}
    _write_folded(session, task_id, metrics, sessions, live=False)
    return True
