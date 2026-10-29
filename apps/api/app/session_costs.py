"""A session's dollars as the console shows them: the wire's fixed-scale money string, the cost an agent reported
itself, and each listed session's cost on Audit > Sessions (`routes/sessions.py`). The Efficiency aggregate
(`app/session_efficiency.py`) prices by the same rules."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model_prices import COST_BY_MODEL, capture_cost, merge_usage, prices_by_model, prices_tokens, read_locked
from app.models.metering import ModelPrice

log = logging.getLogger("app.session_costs")


MONEY_QUANTUM = Decimal("0.0001")


def money(total: Decimal) -> Optional[str]:
    """A Decimal as the wire's fixed-scale string, or `None` when it cannot be expressed as one.

    `None`, never a rounded guess; `lib/taskCost.ts` renders it as an absence.
    """
    try:
        return str(total.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))
    except InvalidOperation:
        log.warning("session cost could not be quantized; reporting it as absent")
        return None


def reported_cost(entry: dict) -> Optional[Decimal]:
    """The cost an agent reported itself on one `Task.metrics["sessions"]` entry; unparseable is none."""
    try:
        return Decimal(str(entry["reported_cost_usd"])) if entry.get("reported_cost_usd") is not None else None
    except (InvalidOperation, ValueError):
        return None


def listed_session_costs(
    session: Session, sessions: Iterable[tuple[str, Optional[int]]], metrics_by_task: dict[int, Optional[dict]]
) -> dict[str, Optional[str]]:
    """`cost_usd` for each `(agent_session_id, task_id)` of a `GET /api/sessions` page, by the Efficiency
    drill-down's rule (`session_efficiency._fold`): its counted tokens priced plus any cost it reported itself,
    `None` when a spending model has no rate or nothing was captured. Read from the session's own `Task.metrics` capture, not the transcripts, so
    the list never scans: each capture's dollars as locked when written (`model_prices.capture_cost`), live
    rates only for one never priced. `metrics_by_task` is the caller's scoped read; an unreadable task's
    session has no cost. Rates are read at most once, and only when needed."""
    entries: dict[str, dict] = {}
    for sid, task_id in sessions:
        blob = metrics_by_task.get(task_id) if task_id is not None else None
        entry = ((blob or {}).get("sessions") or {}).get(sid) if isinstance(blob, dict) else None
        if isinstance(entry, dict):
            entries[sid] = entry
    usages = {
        sid: merge_usage({}, entry.get("usage_by_model")) if prices_tokens(entry) else {}
        for sid, entry in entries.items()
    }
    prices: dict[str, dict[str, Decimal]] = {}
    if any(usages[sid] and read_locked(entry.get(COST_BY_MODEL)) is None for sid, entry in entries.items()):
        prices = prices_by_model(session.scalars(select(ModelPrice)))
    out: dict[str, Optional[str]] = {}
    for sid, entry in entries.items():
        total, unpriced = capture_cost(usages[sid], entry.get(COST_BY_MODEL), prices)
        reported = reported_cost(entry)
        if reported is not None:
            total += reported
        out[sid] = None if unpriced or (not usages[sid] and reported is None) else money(total)
    return out
