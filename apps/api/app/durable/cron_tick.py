"""THE ONE REGISTERED WORKFLOW every cron schedule points at.

A DBOS dynamic schedule names a workflow function and hands it `(scheduled_at, context)`. ONE workflow
serves every cron: the context carries the cron id (and, for an interval row, the interval `tick_is_due`
checks the grid with), and `app/cron_runtime.py` re-reads the row for everything else — the params are not in the context, the global zone is not either, and `enabled` may
have changed since the tick was enqueued.

SYNCHRONOUS, so DBOS runs it on a worker thread from its own pool. That is what lets the wrapper enforce
a hard wall-clock bound with a dedicated executor and hold a session-scoped advisory lock for the
duration; an async body would put both on the serving loop.

"""

# NO STEPS. The whole tick is one workflow body, deliberately: a recovered tick re-runs from the top,
# and the wrapper's advisory lock plus its deterministic `sched-cron:<uuid>-<iso>` id are what make that
# safe. Recording half a firing as a replayable step would make the ledger, not the lock, the thing
# correctness rests on.
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from dbos import DBOS

log = logging.getLogger("cockpit-cron")


def cron_id_from_context(context: Any) -> Optional[str]:
    """The cron id a schedule's context names, or None.

    A PURE SEAM, extracted so the guard is testable: the decorated workflow below cannot be called
    without a live DBOS instance, and a schedule whose context lost its id is a real state — a hand-
    written row, a deserialization that returned something else — that must log rather than raise.
    """
    if not isinstance(context, dict):
        return None
    cron_id = context.get("cron_id")
    return str(cron_id) if cron_id else None


# DBOS ids a triggered run `sched-<name>-trigger-<iso>` and a clock tick `sched-<name>-<iso>`; the suite pins
# this against DBOS's own source so an upgrade that renames it fails a test rather than dropping Run now.
TRIGGERED_ID_MARKER = "-trigger-"


def tick_is_due(context: Any, scheduled_at: datetime, workflow_id: Optional[str]) -> bool:
    """False only for a clock tick of an interval schedule that falls between two of its grid firings.

    An interval the cron syntax cannot express exactly registers a finer step
    (`app/cron_schedule.py::cron_expression_for_interval`); those in-between ticks are the engine polling,
    not firings, so they are dropped here before they stamp a heartbeat or write a ledger row. The interval
    rides the schedule CONTEXT (`app/cron_reconcile.py`), so the check is arithmetic with no database read
    to fail. A triggered run (Run now, a follow-up) runs whenever it is sent.
    """
    if workflow_id and TRIGGERED_ID_MARKER in workflow_id:
        return True
    interval = context.get("interval_seconds") if isinstance(context, dict) else None
    if not interval:
        return True
    from app.cron_schedule import interval_tick_is_due

    return interval_tick_is_due(interval, scheduled_at)


@DBOS.workflow()
def cron_tick(scheduled_at: datetime, context: Any) -> None:
    """One delivered tick for the cron named in `context`.

    Imported for its decorator side effect by `app/durable/runtime.py` — a workflow the registry has not
    seen cannot be scheduled OR recovered, so that import is load-bearing.
    """
    # Deferred: this module is imported at DBOS registration time, before the app is fully wired, and
    # the runtime pulls the ORM and the route layer behind it.
    from app.cron_runtime import run_cron_tick

    cron_id = cron_id_from_context(context)
    if cron_id is None:
        log.error("cron: a schedule fired with no cron_id in its context (%r) — nothing to run", context)
        return
    if not tick_is_due(context, scheduled_at, DBOS.workflow_id):
        return
    result = run_cron_tick(UUID(cron_id), scheduled_at)
    log.info("cron %s: tick for %s -> %s%s", cron_id, scheduled_at.isoformat(), result.outcome,
             f"/{result.skip_reason}" if result.skip_reason else "")
