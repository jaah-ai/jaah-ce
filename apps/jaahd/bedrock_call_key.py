"""K5 — the key one metered model call is recorded under: the ONE home, read by the node's live tail
(`bedrock_usage_tail`) and borrowed by path by the control plane's AWS invocation-log reader
(the token billing `call_key` module via `app/shared_module.py`; the node never imports the API), so
the two sources compute the SAME key for the same call and meet on one row. Stdlib only: loaded by path.

PRIMARY: AWS's `requestId`, once a dev probe shows the invocation log's equals the transcript's.
FALLBACK, live until then (`USE_REQUEST_ID`): `sha256(role_session_name | occurred_at to the second, UTC |
output tokens)`. Pure.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

PRIMARY_PREFIX = "req:"
FALLBACK_PREFIX = "h:"

#: Which mode is live — the CP's AWS correction (`token_billing/metering.py`) keys a record with it. False until
#: a dev probe records that the ids match.
USE_REQUEST_ID = False


def from_request_id(request_id: str) -> str:
    value = request_id.strip()
    if not value:
        raise ValueError("an empty requestId cannot key a call")
    return f"{PRIMARY_PREFIX}{value}"


def fallback(role_session_name: str, occurred_at: datetime, output_tokens: int) -> str:
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    second = occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha256(f"{role_session_name}|{second}|{int(output_tokens)}".encode()).hexdigest()
    return f"{FALLBACK_PREFIX}{digest}"


def call_key(
    *, request_id: Optional[str], role_session_name: str, occurred_at: datetime, output_tokens: int,
    use_request_id: bool = False,
) -> str:
    if use_request_id and request_id:
        return from_request_id(request_id)
    return fallback(role_session_name, occurred_at, output_tokens)
