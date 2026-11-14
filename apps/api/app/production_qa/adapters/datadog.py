"""The `datadog` monitor adapter (contract: `app/production_qa/adapters/__init__.py`).

Vendor docs: https://docs.datadoghq.com/integrations/webhooks/ (template variables, custom headers; read 2026-11-01).

VERIFY: Datadog offers no signing scheme (A5), so the owner adds a custom header `X-Jaah-Token` carrying the trigger's
Jaah-generated secret (kept in a hidden Datadog custom variable); it is compared in constant time.

PAYLOAD: Datadog posts whatever JSON template the owner writes. The console's Datadog setup shows the one to paste
(`DATADOG_PAYLOAD` in `apps/web/src/pages/automations/EventSourceFields.tsx`); every value is a string:

    {"id": "$ID", "aggreg_key": "$AGGREG_KEY", "transition": "$ALERT_TRANSITION", "priority": "$ALERT_PRIORITY",
     "title": "$EVENT_TITLE", "date": "$DATE", "link": "$LINK", "tags": "$TAGS", "alert_id": "$ALERT_ID",
     "scope": "$ALERT_SCOPE", "hostname": "$HOSTNAME"}

FIELD MAP — one fingerprint per `$AGGREG_KEY` (`datadog:{aggreg_key}`):

| Observation  | Datadog variable                                                                   |
|--------------|------------------------------------------------------------------------------------|
| fingerprint  | `$AGGREG_KEY` (required)                                                           |
| title        | `$EVENT_TITLE` without its leading `[Triggered]`/`[Recovered]`/… tag, else the fingerprint |
| severity     | `$ALERT_PRIORITY`: P1 → critical, P2 → error, anything else (or none) → warning     |
| action       | `$ALERT_TRANSITION` `Recovered` → resolve; any other transition → occurrence       |
| environment  | the `env:` tag of `$TAGS` (Datadog's unified service tag), else not sent (NULL)    |
| occurred_at  | `$DATE` (epoch milliseconds), else when it arrived                                 |
| count        | none, `increment` — Datadog sends no running total                                 |
| release      | the `version:` tag of `$TAGS`, else not sent (NULL)                                |
| commit_sha   | not sent (NULL)                                                                    |
| stack_frames | not sent (NULL)                                                                    |
| vendor_url   | `$LINK`                                                                            |
| vendor_ref   | `$ALERT_ID` (the monitor's id)                                                     |
| facts        | `scope` (`$ALERT_SCOPE`), `hostname`, `transition`                                 |

Dedup: `$ID` (the event's id) — a retry resends it.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional

from app import trigger_secrets
from app.clock import utcnow
from app.models.automations import TaskTrigger
from app.production_qa import fingerprints
from app.production_qa.adapters.common import (
    json_id, json_object, lowered, observation, parse_time, text, web_url, with_secret,
)
from app.production_qa.incidents import Observation
from app.triggers import DATADOG_TOKEN_HEADER, SOURCE_DATADOG

_SEVERITY = {"P1": "critical", "P2": "error"}
# `$EVENT_TITLE` opens with the transition (`[Triggered] …`, `[Re-Triggered on {host}] …`): dropped, so the incident's
# title holds still across its transitions.
_TRANSITION_TAG = re.compile(r"^\[(?:Re-)?(?:Triggered|Recovered|Warn|No Data|Renotify)\b[^\]]*\]\s*")
_RESOLVE_TRANSITION = "recovered"


def verify(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> bool:
    """Whether `X-Jaah-Token` is the trigger's secret, compared in constant time. No secret: never."""
    return with_secret(trigger, trigger_secrets.verify_token, lowered(headers).get(DATADOG_TOKEN_HEADER) or "")


def delivery_id(headers: Mapping[str, str], body: bytes) -> Optional[str]:
    """The template's `id` (`$ID`), when the body is a JSON object carrying one."""
    return json_id(body, "Datadog", "id")


def _tag(tags: Any, name: str) -> Optional[str]:
    """The value of `name:` in `$TAGS` (comma-separated `key:value`)."""
    for tag in str(tags or "").split(","):
        key, sep, value = tag.strip().partition(":")
        if sep and key == name and value.strip():
            return value.strip()
    return None


def _when(date: Any) -> Optional[datetime]:
    """`$DATE` is epoch milliseconds."""
    raw = text(date)
    if raw is None or not raw.isdigit():
        return parse_time(raw)
    return parse_time(int(raw) / 1000)


def normalise(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> list[Observation]:
    """The body's one observation; a body without `aggreg_key` raises ValueError (the delivery settles
    `record_error`)."""
    payload = json_object(body, "Datadog")
    aggreg_key = text(payload.get("aggreg_key"))
    if aggreg_key is None:
        raise ValueError("Datadog payload: aggreg_key ($AGGREG_KEY) is required")
    fingerprint = fingerprints.monitor(SOURCE_DATADOG, aggreg_key)
    transition = text(payload.get("transition"))
    facts = {key: value for key, value in (
        ("transition", transition), ("scope", text(payload.get("scope"))), ("hostname", text(payload.get("hostname"))),
    ) if value is not None}
    title = text(_TRANSITION_TAG.sub("", text(payload.get("title")) or ""))
    return [observation(
        SOURCE_DATADOG, trigger, fingerprint=fingerprint, title=title or fingerprint,
        severity=_SEVERITY.get((text(payload.get("priority")) or "").upper(), "warning"),
        action="resolve" if (transition or "").lower() == _RESOLVE_TRANSITION else "occurrence",
        environment=_tag(payload.get("tags"), "env"), release=_tag(payload.get("tags"), "version"),
        occurred_at=_when(payload.get("date")) or utcnow(),
        vendor_url=web_url(payload.get("link")), vendor_ref=text(payload.get("alert_id")), facts=facts,
    )]
