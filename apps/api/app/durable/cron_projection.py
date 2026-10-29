"""The cron projection pass as durable work: `crons` projected onto DBOS's schedules (`app/cron_reconcile.py`).

ONE QUEUE, GLOBAL CONCURRENCY 1, so passes do not overlap — in one process or across replicas (short of a pass
past `PASS_TIMEOUT_SECONDS`, whose lane is freed; every step is idempotent, so that overlap is harmless). An
ask enqueues a pass: a cron write (`routes/crons.py`, `app/automation_service.py`), the timezone PUT, each
replica's startup (`runtime._durable_lifespan`), and a writer outside the api process (`./dc`, the dev seed)
through `DBOSClient`, inside its own transaction. A pass re-reads the whole table, so an ask made while one is
still WAITING joins it, and an ask made during a running pass queues the next (it may describe a write the
running pass missed).

A pass that fails raises its deduped alarm (`cron_reconcile._report`) and is not retried: the next ask
converges, and the alarm resolves then.

A pass that SKIPS an enabled row because this process has not registered its activity type (a rolling deploy:
the shared lane ran a new migration's pass on a still-running old replica) asks one follow-up pass
`FOLLOW_UP_DELAY_SECONDS` later, so a replica that registered the type converges it with nobody writing a cron.
Bounded: none while another pass already waits on the queue (passes never overlap, so that check cannot race
another follow-up), and none past `MAX_FOLLOW_UPS` in one chain — a type no replica will ever register stops
there, and the next ask starts a fresh chain. The skipping pass runs on the OLD code, so a deploy is covered
only once the replicas it replaces already carry this.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from dbos import DBOS, SetWorkflowTimeout

from app.durable import runtime

log = logging.getLogger("cockpit-cron")

# A person who saved a cron is waiting to see it scheduled, so it polls like interactive work.
QUEUE = runtime.declare_queue("cron-projection", concurrency=1, polling_interval_sec=runtime.INTERACTIVE_POLL_SEC)

#: The registered name an out-of-process enqueue (`DBOSClient`) addresses the workflow by.
WORKFLOW_NAME = "project_cron_schedules"
ID_PREFIX = "cron-projection"

#: A pass is a few `SELECT`s and one `apply_schedules`; this only frees the global lane from a wedged one.
PASS_TIMEOUT_SECONDS = 300.0

#: A follow-up chain covers `FOLLOW_UP_DELAY_SECONDS * MAX_FOLLOW_UPS` of rollout — longer than a dev or prod
#: deploy keeps an old replica serving.
FOLLOW_UP_DELAY_SECONDS = 60.0
MAX_FOLLOW_UPS = 10
_FOLLOW_UP_MARK = ":follow-up-"


@DBOS.step()
def _pass() -> dict[str, Any]:
    """One reconcile pass. A step, not the workflow body: `DBOS.apply_schedules` refuses a workflow context.

    Fleet-wide: the pass reads every org's `crons`, and a DBOS worker thread starts with nothing bound, so the
    operator escape needs no `detached()` beside it.
    """
    # Deferred: imported at DBOS registration time, before the app is wired.
    from app.cron_reconcile import reconcile_cron_schedules
    from app.tenancy import seam

    with seam.operator_scope():
        result = reconcile_cron_schedules()
    if result.error:
        # The alarm is already raised; this is the greppable log line beside it.
        log.error("cron: the projection failed (%s) — crons may not be scheduled", result.error)
    else:
        log.info("cron: projection applied=%d paused=%d resumed=%d deleted=%d", result.applied, result.paused,
                 result.resumed, result.deleted)
    return asdict(result)


@DBOS.step()
def _pass_waiting() -> bool:
    """Whether a pass already waits on the queue (enqueued, or a delayed follow-up); it will re-read every row."""
    try:
        return bool(DBOS.list_queued_workflows(queue_name=QUEUE, status=["ENQUEUED", "DELAYED"], limit=1,
                                               load_input=False, load_output=False))
    except Exception:  # noqa: BLE001 — no answer: ask anyway; the chain is bounded by `MAX_FOLLOW_UPS`
        return False


def _follow_up(workflow_id: str, unregistered_enabled: int, follow_ups: int) -> bool:
    """Ask one delayed pass when this one skipped an enabled, unregistered row. True when asked (module docstring)."""
    if not unregistered_enabled:
        return False
    if follow_ups >= MAX_FOLLOW_UPS:
        log.warning("cron: %d enabled row(s) still name an unregistered activity after %d follow-up pass(es) — "
                    "the next cron write or restart asks again", unregistered_enabled, follow_ups)
        return False
    if _pass_waiting():
        return False
    # Named off the chain's first pass, so a recovered workflow's re-enqueue attaches instead of duplicating.
    root = workflow_id.split(_FOLLOW_UP_MARK, 1)[0]
    with SetWorkflowTimeout(PASS_TIMEOUT_SECONDS):
        return runtime.enqueue_or_warn(
            QUEUE, f"{root}{_FOLLOW_UP_MARK}{follow_ups + 1}", project_cron_schedules, follow_ups + 1, log=log,
            failure="cron: could not enqueue a follow-up projection pass",
            consequence="the skipped crons converge on the next cron write or restart",
            enqueue_options={"delay_seconds": FOLLOW_UP_DELAY_SECONDS},
        )


@DBOS.workflow()
def project_cron_schedules(follow_ups: int = 0) -> dict[str, Any]:
    """One projection pass; `follow_ups` counts the passes before it in this chain (0 for every ask). Imported by
    `app/durable/runtime.py` for its decorator side effect."""
    result = _pass()
    _follow_up(str(DBOS.workflow_id), result.get("unregistered_enabled", 0), follow_ups)
    return result


def request_projection() -> bool:
    """Ask for a pass from inside the api process. Never raises: the write that asked has committed, and
    the next ask (or restart) converges what this one could not.

    A pass still WAITING on the queue has not read the table yet, so it will see this write: the ask joins
    it instead of queueing another fleet-wide pass behind it (a burst of writes is one pass, not N). The
    check is not atomic with the enqueue; losing that race costs one extra pass, never a missed write.
    """
    from app.entity_shape import new_uuid7

    try:
        if DBOS.list_queued_workflows(queue_name=QUEUE, status="ENQUEUED", limit=1, load_input=False,
                                      load_output=False):
            return True
    except Exception:  # noqa: BLE001 — no answer (an event loop, an engine not launched): just enqueue
        pass
    # The lane is global, so a pass wedged on a lock must not hold every org's projection behind it.
    with SetWorkflowTimeout(PASS_TIMEOUT_SECONDS):
        return runtime.enqueue_or_warn(
            QUEUE, f"{ID_PREFIX}:{new_uuid7()}", project_cron_schedules, log=log,
            failure="cron: could not enqueue a projection pass",
            consequence="the schedules converge on the next cron write or restart",
        )


def request_projection_in_transaction(session: Any) -> bool:
    """Ask for a pass from a process with no launched engine: the pass exists iff `session` commits. True when
    asked.

    A database whose engine never launched has no DBOS schema to enqueue into — the dev fixture template, a
    fresh `./dc seed` — and then no ask is owed: the api's own startup pass projects every row. The savepoint
    keeps that refusal from aborting the caller's transaction.
    """
    from app.entity_shape import new_uuid7

    try:
        with session.begin_nested():
            runtime.enqueue_in_transaction(session, QUEUE, f"{ID_PREFIX}:{new_uuid7()}", WORKFLOW_NAME)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.info("cron: no projection pass enqueued (%s) — the api's startup pass projects the rows", type(exc).__name__)
        return False
    return True
