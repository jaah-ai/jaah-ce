"""The `pagerduty` monitor adapter (contract: `app/production_qa/adapters/__init__.py`).

Vendor docs (V3 webhooks; read 2026-11-01): https://docs.pagerduty.com/developer/webhooks-overview (envelope, event
types, the `incident` data type — the test fixtures are built from its documented sample),
https://docs.pagerduty.com/developer/verifying-webhook-signatures (signature), and
https://docs.pagerduty.com/developer/webhook-behavior (`X-Webhook-Id`).

VERIFY: `X-PagerDuty-Signature` holds one or more comma-separated `v1=<hex>` signatures (more than one while the
subscription's secret rotates); one must be the hex HMAC-SHA256 of the raw body under the subscription's secret (the
trigger's sealed secret, pasted by its owner), compared in constant time.

FIELD MAP — `event.data` is an `incident`; one fingerprint per `incident_key`, else the incident id:

| Observation  | V3 webhook                                                                  |
|--------------|-----------------------------------------------------------------------------|
| fingerprint  | `data.incident_key`, else `data.id` (`pagerduty:{…}`)                       |
| title        | `data.title`                                                                |
| severity     | `data.urgency`: high → critical, low → warning (none → warning)              |
| action       | `incident.resolved` → resolve; `incident.triggered`/`incident.reopened` → occurrence; other event types record nothing |
| environment  | not sent (NULL)                                                             |
| occurred_at  | `event.occurred_at`, else when it arrived                                   |
| first_seen   | `data.created_at`                                                           |
| count        | none, `increment`                                                           |
| release      | not sent (NULL)                                                             |
| commit_sha   | not sent (NULL)                                                             |
| stack_frames | not sent (NULL)                                                             |
| vendor_url   | `data.html_url`                                                             |
| vendor_ref   | `#{data.number}`, else `data.id`                                            |
| facts        | `service` (summary), `priority` (summary), `resolve_reason`, `event_type`   |

Dedup: `event.id` — a retry resends the same event.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from app import trigger_secrets
from app.clock import utcnow
from app.models.automations import TaskTrigger
from app.production_qa import fingerprints
from app.production_qa.adapters.common import (
    json_id, json_object, lowered, observation, parse_time, text, web_url, with_secret,
)
from app.production_qa.incidents import Observation
from app.triggers import PAGERDUTY_SIGNATURE_HEADER, SOURCE_PAGERDUTY

_SEVERITY = {"high": "critical", "low": "warning"}
_OCCURRENCE_TYPES = ("incident.triggered", "incident.reopened")
_RESOLVE_TYPE = "incident.resolved"
_PREFIX = "v1="
# A header naming more signatures than this is no rotation: it is refused unread.
_MAX_SIGNATURES = 10


def verify(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> bool:
    """Whether any `v1=` signature in `X-PagerDuty-Signature` signs `body` under the trigger's secret."""
    presented = [part.strip()[len(_PREFIX):].lower() for part in
                 (lowered(headers).get(PAGERDUTY_SIGNATURE_HEADER) or "").split(",")
                 if part.strip().startswith(_PREFIX)]
    if not presented or len(presented) > _MAX_SIGNATURES:
        return False
    # Every candidate is compared (the HMAC recomputed per candidate, at most `_MAX_SIGNATURES`), so the time
    # taken never says which one matched.
    def any_signs(secret: str) -> bool:
        return sum(trigger_secrets.verify_signature(secret, body, sig, prefixed=False) for sig in presented) > 0
    return with_secret(trigger, any_signs)


def delivery_id(headers: Mapping[str, str], body: bytes) -> Optional[str]:
    """`event.id`, when the body carries one."""
    return json_id(body, "PagerDuty", "event", "id")


def normalise(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> list[Observation]:
    """The event's observation; [] for an event type that is no incident fact (acknowledged, annotated, …)."""
    event = json_object(body, "PagerDuty").get("event")
    if not isinstance(event, dict):
        raise ValueError("PagerDuty webhook: no event")
    event_type = text(event.get("event_type"))
    if event_type not in _OCCURRENCE_TYPES and event_type != _RESOLVE_TYPE:
        return []
    data = event.get("data")
    if not isinstance(data, dict):
        raise ValueError("PagerDuty webhook: the event has no incident data")
    fingerprint = fingerprints.monitor(SOURCE_PAGERDUTY, text(data.get("incident_key")) or data.get("id"))
    number = data.get("number")
    facts: dict[str, Any] = {"event_type": event_type}
    for key in ("service", "priority"):
        summary = text((data.get(key) or {}).get("summary")) if isinstance(data.get(key), dict) else None
        if summary is not None:
            facts[key] = summary
    if text(data.get("resolve_reason")) is not None:
        facts["resolve_reason"] = text(data.get("resolve_reason"))
    return [observation(
        SOURCE_PAGERDUTY, trigger, fingerprint=fingerprint, title=text(data.get("title")) or fingerprint,
        severity=_SEVERITY.get((text(data.get("urgency")) or "").lower(), "warning"),
        action="resolve" if event_type == _RESOLVE_TYPE else "occurrence",
        occurred_at=parse_time(event.get("occurred_at")) or utcnow(),
        first_seen=parse_time(data.get("created_at")), vendor_url=web_url(data.get("html_url")),
        vendor_ref=f"#{number}" if isinstance(number, int) and not isinstance(number, bool) else text(data.get("id")),
        facts=facts,
    )]
