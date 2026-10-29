"""Which `activity_events` rows a tenant may read, and how — the one rule the Audit page (`routes/audit.py`) and the
SIEM export (`siem_export.py`) both apply, so an org never receives a row its own Audit page hides.

* `row_visible` — the SQL clause: no `ACTIVITY_EXCLUDED_NOTE_KINDS`, no artifact note a Reset soft-deleted.
* `tenant_worded` — a tenant's reading of a row's `data`: the operator's access word rewritten.
* `visible_payload` — a row's `data` without an `account_slug` its reader may not see.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import Integer, and_, case, cast, not_, or_, select

from app import request_caller
from app.authz.catalog import Access
from app.models.audit import ActivityEvent
from app.models.tasks import Task
from app.task_artifacts import ACTIVITY_EXCLUDED_NOTE_KINDS, RESET_HIDDEN_NOTE_KINDS

#: The text `tasks.id::text` renders for a positive id: no sign, no leading zero. At most nine digits, so the
#: cast cannot overflow `INTEGER` (ids past 10^9 are three orders of magnitude off).
_CANONICAL_TASK_ID = r"^[1-9][0-9]{0,8}$"

#: What a tenant reads in `data.access` instead of `superadmin` (`app/tenancy/copy.py`).
TENANT_ACCESS_WORD = "operator"


def row_visible() -> Any:
    """The rows a reader may be served at all: the Audit list, its by-id read and the SIEM export alike.

    Excludes `ACTIVITY_EXCLUDED_NOTE_KINDS` (`app/task_artifacts.py`) and artifact notes a
    Reset soft-deleted (`artifacts_hidden_before`). In SQL rather than a Python post-filter because the Audit
    page's rows and `total` must match (AC5.2); `tests/test_audit_routes.py` pins it against
    `_is_hidden_artifact_note`.

    The probe keys `tasks.id` (the primary key) on the payload's id, cast to an integer only when it is
    the canonical spelling of one (`_CANONICAL_TASK_ID`), so a malformed id matches nothing and never
    raises. Keyed the other way round (`tasks.id::text = data->>'id'`) no index applies, and every
    note row re-read the whole `tasks` table: the count went linear in both tables (4.4 s per count
    at 231k rows × 2.5k tasks)."""
    hideable = and_(
        ActivityEvent.kind.in_(RESET_HIDDEN_NOTE_KINDS),
        or_(
            ActivityEvent.kind != "task_note:outcome",
            # `data IS NOT NULL` first: `NULL ? 'summary'` would propagate through `not_()` and drop the
            # row.
            and_(ActivityEvent.data.isnot(None), ActivityEvent.data.has_key("summary")),
        ),
    )
    raw_id = ActivityEvent.data["id"].astext
    soft_deleted = select(1).where(
        Task.id == case((raw_id.regexp_match(_CANONICAL_TASK_ID), cast(raw_id, Integer))),
        Task.artifacts_hidden_before.isnot(None),
        ActivityEvent.created_at < Task.artifacts_hidden_before,
    ).exists()
    return and_(
        ActivityEvent.kind.notin_(ACTIVITY_EXCLUDED_NOTE_KINDS),
        not_(and_(hideable, soft_deleted)),
    )


def tenant_worded(payload: dict[str, Any], tenant: bool) -> dict[str, Any]:
    """`payload` as a tenant may read it: `data.access == "superadmin"` becomes `"operator"`, and an operator's
    acting-as row keeps no caller facts (`request_caller.CALLER_KEYS`: staff addresses, browsers and sessions).

    The capture writes the operator's access word onto a tenant's own refused rows; the
    verdict is the reader's org (`tenancy/ids.py::is_tenant_org`). Everything else passes through."""
    if not tenant:
        return payload
    if "acting_as" in payload:
        payload = {key: value for key, value in payload.items() if key not in request_caller.CALLER_KEYS}
    if payload.get("access") != Access.SUPERADMIN.value:
        return payload
    return {**payload, "access": TENANT_ACCESS_WORD}


def visible_payload(data: Optional[dict[str, Any]], allowed: Any) -> dict[str, Any]:
    """One row's `data`, with `account_slug` dropped when `allowed` (an `authz.AllowedObjects` over slugs) does not
    contain that account.

    `record_task_note(..., "runner_session", ...)` writes `data.account_slug`, which account access control strips
    deny-by-default on the per-task feed; same here. The Audit page passes the reader's bare account grant
    (`granted_account_slugs`), since an activity row names no node; the SIEM export, whose reader is the org, the
    org's own accounts — the slug is the name the node reported, never checked against `accounts`. Everything else
    passes through (D4)."""
    # The column is plain JSONB; an array would fail response validation.
    payload = data if isinstance(data, dict) else {}
    if allowed.unrestricted:
        return payload
    slug = payload.get("account_slug")
    if slug is None or (isinstance(slug, str) and allowed.contains(slug)):
        return payload
    return {key: value for key, value in payload.items() if key != "account_slug"}
