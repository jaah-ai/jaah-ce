"""What every monitor adapter reads a payload with: case-insensitive headers, a vendor timestamp, one stack frame."""
from __future__ import annotations

import json

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from app import clock, trigger_secrets
from app.models.automations import TaskTrigger
from app.production_qa.incidents import Observation

_MAX_URL = 2000
# A frame's path or function longer than this is cut (the generic shape refuses it outright).
MAX_FRAME_TEXT = 500


def lowered(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def parse_time(value: Any) -> Optional[datetime]:
    """An ISO-8601 string or epoch seconds as an aware UTC datetime; None when absent or unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return clock.parse_utc(value.strip()) if isinstance(value, str) else None


def web_url(value: Any) -> Optional[str]:
    """`value` when it is an http(s) link, else None: a vendor link is rendered as a link (`javascript:` would run)."""
    text = str(value).strip() if value is not None else ""
    return text if text.lower().startswith(("https://", "http://")) and len(text) <= _MAX_URL else None


def frame(path: Any, function: Any, lineno: Any, in_app: Any) -> dict[str, Any]:
    """One `incidents.stack_frames` entry — the shape the Incidents page reads (`StackFrame` in
    `apps/web/src/lib/productionQa.ts`). A field the vendor leaves out stays None."""
    return {
        "path": str(path)[:MAX_FRAME_TEXT] if path not in (None, "") else None,
        "function": str(function)[:MAX_FRAME_TEXT] if function not in (None, "") else None,
        "lineno": lineno if isinstance(lineno, int) and not isinstance(lineno, bool) else None,
        "in_app": in_app if isinstance(in_app, bool) else None,
    }


def text(value: Any) -> Optional[str]:
    """`value` as stripped text; None when absent or blank."""
    out = str(value).strip() if value is not None else ""
    return out or None


def observation(kind: str, trigger: TaskTrigger, **fields: Any) -> Observation:
    """A monitor `Observation` of `kind` from `trigger`: every field an adapter leaves out is not sent (None), the
    count an increment of one."""
    base: dict[str, Any] = dict(
        type="monitor", source_kind=kind, trigger_id=trigger.id, environment=None, count=None,
        count_mode="increment", action="occurrence", release=None, commit_sha=None, stack_frames=None,
        vendor_url=None, vendor_ref=None, facts={}, deployment_id=None, delivery_id=None,
    )
    base.update(fields)
    return Observation(**base)


def with_secret(trigger: TaskTrigger, verify: Callable[..., bool], *args: Any) -> bool:
    """`verify(secret, *args)` under the trigger's opened secret, dropped on the way out. No secret: never."""
    try:
        secret = trigger_secrets.open_secret(trigger)
    except ValueError:
        return False
    try:
        return verify(secret, *args)
    finally:
        del secret


def json_object(body: bytes, vendor: str) -> dict[str, Any]:
    """The body as a JSON object, or ValueError naming `vendor` (the delivery settles `record_error`)."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError(f"a {vendor} webhook body is a JSON object")
    return payload


def json_id(body: bytes, vendor: str, *keys: str) -> Optional[str]:
    """The text at `keys` down a `vendor` body (`json_object`), the dedup key a `delivery_id` names; None when the
    body is no JSON object or any step is not one, or the value is absent or blank."""
    try:
        value: Any = json_object(body, vendor)
    except ValueError:
        return None
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return text(value)
