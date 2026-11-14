"""The generic incident shape — a `webhook` trigger whose `source_config.shape` is `incident` (contract:
`app/production_qa/adapters/__init__.py`). For any monitor without its own adapter: the sender posts this JSON.

AUTH: the webhook's own — `Authorization: Bearer jaah_…`, a Jaah API key of the trigger's org holding `tasks:create`
(`trigger_intake._key_verdict`), checked at ingress before anything here runs; so there is no `verify`. An optional
`X-Jaah-Delivery-Id` header names the delivery: a retry carrying the same id is recorded once.

THE SHAPE (one JSON object; unknown keys are refused, so a typo is a `record_error`, never a silently lost field):

    {
      "fingerprint": "checkout-500",           required — one incident per fingerprint (stored as `webhook:<value>`)
      "title": "Checkout returns 500",         required on an occurrence; a resolve may leave it out
      "severity": "error",                     debug | info | warning | error | critical (default error)
      "action": "occurrence",                  occurrence (default) | resolve
      "environment": "production",             optional
      "count": 12,                             optional — the monitor's own running total (the larger total is kept);
                                               left out, each occurrence adds one
      "occurred_at": "2026-11-01T12:00:00Z",   optional ISO-8601 (default: when it arrived)
      "release": "web@1.4.2",                  optional
      "commit": "9fceb02d0ae598e95dc970b74767f19372d61af8",   optional
      "url": "https://monitor.example/alerts/42",            optional http(s) link back to the monitor
      "stack_frames": [{"path": "src/checkout.ts", "function": "total", "lineno": 88, "in_app": true}]   optional,
                                               innermost first
    }

A field left out is stored as not sent (NULL), never as a placeholder.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal, Optional

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from app.clock import utcnow
from app.models.automations import TaskTrigger
from app.production_qa import fingerprints
from app.production_qa.adapters.common import MAX_FRAME_TEXT, frame
from app.production_qa.incidents import MAX_STACK_FRAMES, Observation
from app.production_qa.vocabulary import SEVERITIES
from app.trigger_config import Shape, first_error
from app.triggers import SOURCE_WEBHOOK

_TEXT = 500


class GenericFrame(Shape):
    path: Optional[str] = Field(default=None, max_length=MAX_FRAME_TEXT)
    function: Optional[str] = Field(default=None, max_length=MAX_FRAME_TEXT)
    lineno: Optional[int] = Field(default=None, ge=0, le=2**31 - 1)
    in_app: Optional[bool] = None


class GenericIncident(Shape):
    fingerprint: str = Field(min_length=1, max_length=_TEXT)
    title: Optional[str] = Field(default=None, min_length=1, max_length=_TEXT)
    severity: str = "error"
    action: Literal["occurrence", "resolve"] = "occurrence"
    environment: Optional[str] = Field(default=None, min_length=1, max_length=100)
    count: Optional[int] = Field(default=None, ge=1, le=2**31 - 1)
    occurred_at: Optional[AwareDatetime] = None
    release: Optional[str] = Field(default=None, min_length=1, max_length=200)
    commit: Optional[str] = Field(default=None, min_length=1, max_length=64)
    url: Optional[str] = Field(default=None, max_length=2000, pattern=r"^https?://")
    stack_frames: Optional[list[GenericFrame]] = Field(default=None, max_length=MAX_STACK_FRAMES)

    @field_validator("severity")
    @classmethod
    def _severity(cls, value: str) -> str:
        if value not in SEVERITIES:
            raise ValueError(f"one of {', '.join(SEVERITIES)}")
        return value

    @model_validator(mode="after")
    def _titled(self) -> "GenericIncident":
        if self.action == "occurrence" and not self.title:
            raise ValueError("title: required on an occurrence")
        return self


def normalise(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> list[Observation]:
    """The body's one observation. A body outside the shape raises ValueError naming the field."""
    try:
        sent = GenericIncident.model_validate(json.loads(body))
    except ValidationError as exc:
        raise ValueError(f"generic incident: {first_error(exc)}") from None
    fingerprint = fingerprints.monitor(SOURCE_WEBHOOK, sent.fingerprint)
    frames = [frame(f.path, f.function, f.lineno, f.in_app) for f in sent.stack_frames or ()]
    return [Observation(
        type="monitor", source_kind=SOURCE_WEBHOOK, trigger_id=trigger.id, fingerprint=fingerprint,
        title=sent.title or fingerprint, severity=sent.severity, environment=sent.environment,
        occurred_at=sent.occurred_at or utcnow(), count=sent.count,
        count_mode="absolute" if sent.count is not None else "increment", action=sent.action, release=sent.release,
        commit_sha=sent.commit, stack_frames=frames or None, vendor_url=sent.url, vendor_ref=None, facts={},
        deployment_id=None, delivery_id=None,
    )]
