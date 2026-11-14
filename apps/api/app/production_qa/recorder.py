"""The trigger-intake entry point: records a deploy or monitor fact before any gate or cap, and settles the delivery.

`record` runs first in `trigger_intake._Processor._handle` when `handles` says so, inside `session.begin_nested()`;
the frame there (`_Processor._record`) turns a raise into a rolled-back savepoint and a `failed`/`record_error`
delivery, and settles a success of a record-only trigger `filtered`/`recorded`; any other trigger's row goes on to
file as before (adopting an automation as it always has), its recording in the same transaction. The intents `record` returns are flushed after the row's commit (`events.flush`).

A trigger that ONLY records (`records_only`: every monitor adapter and deploy recorder kind, a `webhook` whose
`source_config.shape` is `incident` — the generic monitor shape, `adapters/generic.py` — every `gitlab`,
`azure_devops` and `bitbucket` trigger, and a `github` trigger whose `source_config` says
`record_only: true`) never adopts an automation (`automation_service.sync_trigger`, architecture A-2): adopted,
it would file a task per event. Each owning child registers its kind's handler (`register`); a record-only delivery
whose kind has no handler yet records nothing and still settles `recorded`. A handler is keyed by source kind, except
the incident-shaped webhook's (`INCIDENT_WEBHOOK`): a plain webhook has none.
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models.automations import TaskTrigger, TriggerDelivery
from app.production_qa import load_owners
from app.production_qa.events import RaiseIntent
from app.triggers import DEPLOY_SOURCE_KINDS, MONITOR_SOURCE_KINDS, SOURCE_GITHUB, SOURCE_WEBHOOK

Handler = Callable[[Session, TaskTrigger, TriggerDelivery], list[RaiseIntent]]

_HANDLERS: dict[str, Handler] = {}

# The handler key of a `webhook` trigger carrying the generic incident shape.
INCIDENT_WEBHOOK = "webhook:incident"


def is_incident_webhook(trigger: TaskTrigger) -> bool:
    return trigger.source_kind == SOURCE_WEBHOOK and (trigger.source_config or {}).get("shape") == "incident"


def _key(trigger: TaskTrigger) -> str:
    return INCIDENT_WEBHOOK if is_incident_webhook(trigger) else trigger.source_kind


def register(source_kind: str, handler: Handler) -> None:
    """The recording handler of one source kind (C3: `github`; C4/C8: the monitor kinds, `INCIDENT_WEBHOOK`); the last
    one wins."""
    _HANDLERS[source_kind] = handler


def records_only(trigger: TaskTrigger) -> bool:
    """Whether `trigger` exists to record facts and never files a task of its own."""
    return source_records_only(trigger.source_kind, trigger.source_config)


def source_records_only(source_kind: object, source_config: object) -> bool:
    """`records_only` over a When's raw `source_kind` / `source_config` — before any trigger row exists (an automation
    create, `automation_service.create`)."""
    config = source_config if isinstance(source_config, dict) else {}
    if source_kind in MONITOR_SOURCE_KINDS + DEPLOY_SOURCE_KINDS or (source_kind == SOURCE_WEBHOOK and config.get("shape") == "incident"):
        return True
    return source_kind == SOURCE_GITHUB and config.get("record_only") is True


def handles(trigger: TaskTrigger, row: TriggerDelivery) -> bool:
    """Whether this delivery is a fact to record: a fresh inbound delivery (not a preflight item, and not one coming
    back from a preflight or a preflight retry — it was recorded on its first pass) of a record-only trigger or of a
    kind with a registered handler."""
    if row.parent_delivery_id is not None or row.preflighted_at is not None or row.attempts:
        return False
    load_owners()
    return records_only(trigger) or _key(trigger) in _HANDLERS


def record(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> list[RaiseIntent]:
    """Record the delivery's fact through its kind's handler (none registered: nothing). May raise; the intake frame
    owns the savepoint and the settle."""
    load_owners()
    handler = _HANDLERS.get(_key(trigger))
    return handler(session, trigger, row) if handler is not None else []
