"""Monitor adapters, one module per source: `verify(trigger, headers, body)` checks the sender, and
`normalise(trigger, headers, body)` turns the body into `incidents.Observation`s ([] for a confirmation or a resource
that is no incident fact; a malformed body raises ValueError), and `delivery_id(headers, body)` names the vendor's own
delivery id (a retry resends it), or None. The served ones: `sentry`, `datadog`, `pagerduty`, `cloudwatch` (each its
own kind) and `generic` (a `webhook` whose `source_config.shape` is `incident`, authenticated by the webhook's API key,
so it has no `verify`). An `azure_devops` deploy recorder (`triggers.ADAPTER_SOURCE_KINDS`) is verified and
deduped here too, but records through its own handler (`app/production_qa/azure_devops.py`), not `record_delivery`.

`ingest_http` calls `verify` for every `triggers.ADAPTER_SOURCE_KINDS` kind (a refusal is the webhook's 401 row) and
dedups on `delivery_id`; `record_delivery` is the recorder handler every one registers (`recorder.register`): it
normalises the delivery's stored event and folds each observation onto the trigger's project (`incidents.record`).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from types import ModuleType
from typing import Optional

from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models.automations import TaskTrigger, TriggerDelivery
from app.production_qa import azure_devops, incidents, recorder
from app.production_qa.adapters import cloudwatch, datadog, generic, pagerduty, sentry
from app.production_qa.events import RaiseIntent
from app.triggers import SOURCE_AZURE_DEVOPS, SOURCE_CLOUDWATCH, SOURCE_DATADOG, SOURCE_PAGERDUTY, SOURCE_SENTRY

_MONITORS: dict[str, ModuleType] = {
    SOURCE_SENTRY: sentry, SOURCE_DATADOG: datadog, SOURCE_PAGERDUTY: pagerduty, SOURCE_CLOUDWATCH: cloudwatch,
}
_BY_KIND: dict[str, ModuleType] = {**_MONITORS, SOURCE_AZURE_DEVOPS: azure_devops}


def adapter_for(trigger: TaskTrigger) -> Optional[ModuleType]:
    """The adapter that verifies `trigger`'s deliveries, or None for a trigger that is no adapter's."""
    if recorder.is_incident_webhook(trigger):
        return generic
    return _BY_KIND.get(trigger.source_kind)


def delivery_id(source_kind: str, headers: Mapping[str, str], body: bytes) -> Optional[str]:
    """The vendor's own id of a delivery to a `source_kind` adapter (its natural dedup key), or None."""
    adapter = _BY_KIND.get(source_kind)
    return adapter.delivery_id(headers, body) if adapter is not None else None


def record_delivery(session: Session, trigger: TaskTrigger, row: TriggerDelivery) -> list[RaiseIntent]:
    """Normalise `row`'s stored event and record each observation; the intents the transitions raise."""
    adapter = _MONITORS.get(trigger.source_kind) if not recorder.is_incident_webhook(trigger) else generic
    event = row.event or {}
    if adapter is None or "body" not in event:
        return []
    body = event["body"]
    if isinstance(body, dict) and set(body) == {"raw"}:
        raise ValueError("the body is not JSON")
    # Re-serialised from the stored event: the sender was verified on the raw bytes at ingress, so these bytes are
    # for `normalise` only — never hand them to `verify`.
    raw = json.dumps(body).encode()
    now = utcnow()
    intents: list[RaiseIntent] = []
    for observation in adapter.normalise(trigger, event.get("headers") or {}, raw):
        observation.delivery_id = row.id
        # A sender's clock ahead of ours would make every on-time event after it read as stale.
        observation.occurred_at = min(observation.occurred_at, now)
        _recorded, raised = incidents.record(session, trigger.project_id, observation)
        intents.extend(raised)
    return intents


for _kind in _MONITORS:
    recorder.register(_kind, record_delivery)
recorder.register(recorder.INCIDENT_WEBHOOK, record_delivery)
