"""The cron engine's two alarms, raised deduped and resolved on recovery.

A wedged cron (`cron-skip:<id>`) and a reconciler that could not project the desired set
(`cron-reconcile-failed`). Each is a hidden `adhoc` system Task in the project the operator setting
"Alarm project" names (`alarm_project_id`, also the node-liveness sweep's).
"""

# In-process route calls, not new SQL: the dedup rule is single-homed in `create_system_task`, and a
# second implementation would clobber the wrong Task.
# Resolve on recovery is required: the operator has no console control to clear these rows.
# Best-effort: every function swallows and logs its own failure, so an alarm outage never turns a
# firing into an `error` `cron_runs` row.
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

# Stdlib-only at import (its suite pins that); the one home for the alarm Task's persona.
from app.node_liveness import ALARM_TASK_PERSONA

log = logging.getLogger("cockpit-cron")

#: The operator knob naming the project every control-plane alarm files into (`app/operator_settings/`).
ALARM_PROJECT_KNOB = "alarm_project_id"

# One key per condition. The per-cron key is load-bearing: the `(project, persona)` dedup would collapse
# every wedged cron onto one row, and `adhoc` without a key is a 422.
SKIP_KEY_PREFIX = "cron-skip:"
RECONCILE_FAILED_KEY = "cron-reconcile-failed"
#: The billing crons' health alarms (`app/billing/events.py`): a poll that keeps failing, a silent account,
#: an event that keeps failing. Minted only there.
BILLING_KEY_PREFIX = "billing-events-"
#: The platform Bedrock health alarms (`app/bedrock/probes.py`): the account unavailable, and each warning.
BEDROCK_KEY_PREFIX = "bedrock-health-"
#: Jaah token billing's alarms (`app/token_billing/outbox.py`, `audit.py`): an effect that keeps failing, ledger drift.
TOKEN_BILLING_KEY_PREFIX = "token-billing-"
#: The SOC 2 evidence collectors' alarms (`app/compliance_evidence/collectors.py`): one per collector, a drift or
#: an error. Minted only there.
COMPLIANCE_EVIDENCE_KEY_PREFIX = "compliance-evidence:"


def skip_dedup_key(cron_id: UUID | str) -> str:
    """The wedged-cron key for one row; derived here so raiser and resolver cannot spell it differently."""
    return f"{SKIP_KEY_PREFIX}{cron_id}"


def _own_key(dedup_key: str) -> bool:
    """Whether this module mints `dedup_key`. A dedup hit refreshes the matched Task in place, so a
    foreign key could clobber an unrelated open `adhoc` row."""
    return (
        dedup_key.startswith(SKIP_KEY_PREFIX)
        or dedup_key.startswith(BILLING_KEY_PREFIX)
        or dedup_key.startswith(BEDROCK_KEY_PREFIX)
        or dedup_key.startswith(TOKEN_BILLING_KEY_PREFIX)
        or dedup_key.startswith(COMPLIANCE_EVIDENCE_KEY_PREFIX)
        or dedup_key == RECONCILE_FAILED_KEY
    )


def alarm_project_id(session: Session) -> Optional[int]:
    """The project alarms file into: the "Alarm project" setting, when it names a project.
    None while it is unset or names no row; each caller says so at its own level.

    Open alarms are found in this project only, so re-pointing the setting leaves the old project's open cards
    to be closed by hand; the next raise opens fresh ones here."""
    # Deferred: keeps the settings catalogue and the ORM out of a DB-free importer's graph.
    from sqlalchemy import select

    from app.operator_settings import reads as settings_reads
    from app.models.projects import Project

    stored = settings_reads.get_text(ALARM_PROJECT_KNOB)
    if not stored.isdecimal():
        return None
    return session.scalar(select(Project.id).where(Project.id == int(stored)))


def unset_reason() -> str:
    """The one sentence every caller logs when `alarm_project_id` answers None."""
    return 'the "Alarm project" setting (Settings > Compliance > Alarms) is unset or names no project'


def raise_alarm(
    session: Session, *, dedup_key: str, title: str, body: str,
) -> Optional[int]:
    """Open (or refresh) the one hidden Task for `dedup_key`. Returns its id, or None if delivery failed.

    A condition re-detected every tick keeps one row carrying the latest text. The row is the Alarm
    project's org's, whichever tenant's cron it names (`app/routes/agent/task_reads.py::create_system_task`)."""
    # Deferred: keeps the route layer out of a DB-free importer's graph.
    from app.routes.agent import auth as agent_auth, task_reads as agent_task_reads

    if not _own_key(dedup_key):
        raise ValueError(f"{dedup_key!r} is not a cron alarm key — see `_own_key`")

    try:
        project_id = alarm_project_id(session)
        if project_id is None:
            # ERROR, not the delivery warning below: nothing reaches /tasks until an operator sets it.
            log.error("cron: alarm %s NOT filed — %s: %s", dedup_key, unset_reason(), title)
            return None
        out = agent_task_reads.create_system_task(
            body=agent_task_reads.SystemTaskIn(
                persona=ALARM_TASK_PERSONA,
                project_id=project_id,
                title=title,
                body=body,
                state="draft",
                dedup_open=True,
                dedup_key=dedup_key,
            ),
            # Required producer declaration: no Task behind this call, and an omission is a 422.
            x_agent_task_id=agent_auth.HOST_PRODUCER_CALLER,
            _=None,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by contract (module docstring)
        log.warning("cron: could not raise the %s alarm: %s", dedup_key, exc)
        return None
    log.warning("cron: alarm %s is open as Task %s — see /tasks", dedup_key, out.id)
    return out.id


def resolve_alarm(session: Session, *, dedup_key: str, summary: str) -> bool:
    """Close the open Task for `dedup_key` if there is one. True iff this call resolved a row.

    A no-op when nothing is open; it costs two reads, so the wrapper only asks after a recovery.
    """
    from app.routes.agent import task_reads as agent_task_reads

    try:
        project_id = alarm_project_id(session)
        if project_id is None:
            # `raise_alarm` already said so at ERROR; nothing can be open where nothing was filed.
            log.debug("cron: not resolving the %s alarm — %s", dedup_key, unset_reason())
            return False
        found = agent_task_reads.find_open_system_task(
            persona=ALARM_TASK_PERSONA, project_id=project_id, dedup_key=dedup_key,
            _=None, session=session,
        )
        if found.id is None:
            return False
        out = agent_task_reads.resolve_system_task(
            task_id=found.id,
            body=agent_task_reads.ResolveSystemTaskIn(outcome_summary=summary[:280]),
            _=None,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        # A 404 (the Task already gone) is the ordinary quiet case; anything else warns.
        quiet = getattr(exc, "status_code", None) == 404
        (log.debug if quiet else log.warning)(
            "cron: could not resolve the %s alarm: %s", dedup_key, exc
        )
        return False
    if out.resolved:
        log.info("cron: alarm %s resolved (Task %s) — the condition is clear", dedup_key, found.id)
    return out.resolved
