"""Per-org quotas: the agent-request rate limit, the task-attachment byte quotas (white-box M7, M8) and the
largest file one upload may carry.

THE SINGLE HOME of the four quotas, what each counts, which value governs it, and the refusals built on them.
They sit beside `app/org_limits.py`'s caps — same Limits section of the customer's Plan tab, same `orgs.limit_<kind>` override spelling,
same 0-means-unlimited — and are plan terms like them, resolved in order:

1. `override` — the org's own `orgs.limit_<kind>` when non-NULL.
2. `plan` — the org's plan's `billing_plans.limit_<kind>` when non-NULL and not inherited (`plan_terms.field_row`).
3. `default` — the `(default)` plan row's value (`plan_terms.defaults`), else `DEFAULTS`' constant.

Kinds:
* `agent_requests_per_minute` — every `/api/agent/*` call authenticated by one of the org's node tokens,
  counted fleet-wide in a 60-second sliding window (`ratelimit.SharedRateLimiter`, bucket `agent-org`).
  Enforced at the agent plane's one auth door (`routes/agent/auth.py::_check_token`); refused with 429 and
  `Retry-After`.
* `task_attachment_mb` — the bytes stored against ONE task: human input files, comment images and runner
  artifacts, superseded versions included, pending (not yet confirmed) rows counted at their declared size.
* `org_attachment_gb` — the same bytes summed over every task of the org. Support-ticket and support-chat
  files count toward neither.
* `attachment_file_mb` — the largest ONE file an upload may carry, at every door that stores a file: task,
  automation and support files alike (`file_cap_bytes`). 0 = unlimited, which is still `MAX_FILE_BYTES`: one
  presigned PUT cannot carry more. Usage is the org's largest stored file.

Both byte quotas are checked at every door that creates a task's attachment row from a caller's bytes
(`routes/attachments.py`): the presigned tickets and the buffered uploads. Refused with 409 before the row
is written; nothing stored is ever removed by a lower quota.

Org zero (Operator; `tenancy/ids.py::OPERATOR_PLANE_ORG_IDS`) and the control plane's own node-less
agent arms are exempt: the point is that one TENANT cannot degrade the control plane or run up storage for
everyone.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app import db, plan_terms
from app.models.orgs import Org
from app.models.task_extras import TaskAttachment
from app.ratelimit import SharedRateLimiter
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

AGENT_REQUESTS = "agent_requests_per_minute"
TASK_ATTACHMENT = "task_attachment_mb"
ORG_ATTACHMENT = "org_attachment_gb"
FILE_SIZE = "attachment_file_mb"

#: The four quotas, in the order the Plan tab's Limits section lists them after `org_limits.LIMIT_KINDS`
#: (`app/models/orgs.py::ORG_QUOTA_COLUMNS` pairs with it).
QUOTA_KINDS: tuple[str, ...] = (AGENT_REQUESTS, TASK_ATTACHMENT, ORG_ATTACHMENT, FILE_SIZE)

#: The quotas a plan carries (`app/models/orgs.py::BILLING_PLAN_QUOTA_COLUMNS` pairs with it): all four.
PLAN_QUOTA_KINDS: tuple[str, ...] = QUOTA_KINDS

#: Each quota with no `(default)` row (or a NULL there): the platform constants the knobs once defaulted to.
DEFAULTS: dict[str, int] = {AGENT_REQUESTS: 3000, TASK_ATTACHMENT: 100, ORG_ATTACHMENT: 100, FILE_SIZE: 100}

_LABELS: dict[str, str] = {
    AGENT_REQUESTS: "Requests per minute",
    TASK_ATTACHMENT: "Task storage",
    ORG_ATTACHMENT: "Storage",
    FILE_SIZE: "Max file size",
}

#: The unit a kind's value and usage count in, shown beside the number ("50 MB"); none for a plain count.
_UNITS: dict[str, str] = {
    AGENT_REQUESTS: "",
    TASK_ATTACHMENT: "MB",
    ORG_ATTACHMENT: "GB",
    FILE_SIZE: "MB",
}

#: The agent-request window, seconds. Fixed: the knob is a per-minute budget.
AGENT_WINDOW = 60.0

_MB = 1024 * 1024
_GB = 1024 * _MB

#: The ceiling on one file whatever any tier says: S3's single-PUT limit, which every upload ticket signs.
MAX_FILE_BYTES = 5 * _GB
#: `MAX_FILE_BYTES` in the kind's unit — the most any tier may store.
MAX_FILE_MB = MAX_FILE_BYTES // _MB

#: How long one process trusts an org's resolved request budget. The agent door is the hottest path in the
#: control plane; re-reading the org row and its plan on every call would add queries per request to save
#: an operator's edit at most this many seconds of lag.
_BUDGET_TTL_SECONDS = 5.0

_agent_limiter = SharedRateLimiter(1, AGENT_WINDOW, bucket="agent-org")
_budget_cache: dict[UUID, tuple[float, int]] = {}
_budget_lock = threading.Lock()


def column(kind: str) -> str:
    """The `orgs` column holding one org's own value for `kind`."""
    return f"limit_{kind}"


def label(kind: str) -> str:
    return _LABELS[kind]


def is_exempt(org_id: Optional[UUID]) -> bool:
    """Org zero (Operator) is not governed; nor is a caller with no org (the CP's own arms)."""
    return org_id is None or org_id in OPERATOR_PLANE_ORG_IDS


def maximum(kind: str) -> Optional[int]:
    """The most any tier may set `kind` to, when tighter than the shared override range."""
    return MAX_FILE_MB if kind == FILE_SIZE else None


def _default_value(session: Session, kind: str) -> int:
    """`(default)`'s value for `kind`, else its constant."""
    value = getattr(plan_terms.defaults(session), column(kind), None)
    return DEFAULTS[kind] if value is None else value


def fallback(session: Session, org: Optional[Org], kind: str) -> tuple[int, str]:
    """What `kind` reads with no override (0 = unlimited) and whose value it is: the org's plan's own (`plan`), else
    `(default)`'s (`default`). An org on `(default)` itself, or none, reads `default`."""
    plan = plan_terms.plan_row(session, plan_terms.terms_org(session, org).plan_key) if org is not None else None
    dflt = plan_terms.defaults(session)
    row = plan_terms.field_row(plan, dflt, kind)
    value = getattr(row, column(kind)) if row is not None and row is not dflt else None
    if value is not None:
        return value, "plan"
    return _default_value(session, kind), "default"


def _raw(session: Session, org: Optional[Org], kind: str) -> tuple[int, str]:
    """`kind`'s raw value for `org` (0 = unlimited) and its source: the org's own column, else `fallback`."""
    own = getattr(org, column(kind)) if org is not None else None
    if own is not None:
        return own, "override"
    return fallback(session, org, kind)


@dataclass(frozen=True)
class Quota:
    """One quota as it governs one org. `limit` None = unlimited; `usage` in the kind's own unit, rounded up
    (one stored byte reads as 1 MB/GB, never 0)."""

    kind: str
    label: str
    limit: Optional[int]
    source: str
    usage: int
    unit: str


# ── Agent requests ────────────────────────────────────────────────────────────────────────────────────


def _agent_budget(session: Session, org_id: UUID) -> int:
    """`org_id`'s request budget per window (0 = unlimited), cached per process for `_BUDGET_TTL_SECONDS`."""
    now = time.monotonic()
    with _budget_lock:
        hit = _budget_cache.get(org_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    budget, _ = _raw(session, session.get(Org, org_id), AGENT_REQUESTS)
    with _budget_lock:
        _budget_cache[org_id] = (now + _BUDGET_TTL_SECONDS, budget)
    return budget


def invalidate_budget(org_id: Optional[UUID] = None) -> None:
    """Forget cached budgets — one org's on an override edit, every org's for tests. This process only:
    every other process (and a plan or `(default)` edit) catches up within `_BUDGET_TTL_SECONDS`."""
    with _budget_lock:
        if org_id is None:
            _budget_cache.clear()
        else:
            _budget_cache.pop(org_id, None)


def admit_agent_request(session: Session, org_id: Optional[UUID]) -> None:
    """Count one agent call against `org_id`'s budget, or raise 429 when the window is full.

    Called by the agent auth door with the org derived from the node token, before the request's scope is
    bound; `orgs` is the tenancy axis itself, not a tenant-owned table, so the read needs no scope. Exempt
    orgs and an unlimited budget cost nothing."""
    if is_exempt(org_id):
        return
    budget = _agent_budget(session, org_id)
    if budget <= 0:
        return
    # The limiter counts on its own pooled connection; hand the request's back first, or a burst of agent
    # calls each holding one while waiting for a second exhausts the pool for the whole process.
    db.release_connection(session)
    if not _agent_limiter.allow(str(org_id), limit=budget):
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit reached: {budget} agent requests per minute for this organization. "
                "Retry shortly, or contact us to raise it."
            ),
            headers={"Retry-After": str(int(AGENT_WINDOW))},
        )


def _agent_usage(session: Session, org_id: UUID) -> int:
    """Calls counted in `org_id`'s current window — what the Plan tab's limit row shows as in use."""
    return int(session.execute(
        text(
            "SELECT count(*) FROM rate_limit_hits WHERE bucket = :bucket AND key = :key "
            "AND hit_at >= now() - make_interval(secs => :window)"
        ),
        {"bucket": _agent_limiter.bucket, "key": str(org_id), "window": AGENT_WINDOW},
    ).scalar_one())


# ── Attachment bytes ──────────────────────────────────────────────────────────────────────────────────


def _task_bytes(session: Session, task_id: Optional[int], automation_id: Optional[UUID] = None) -> int:
    subject = TaskAttachment.task_id == task_id if task_id is not None else TaskAttachment.automation_id == automation_id
    return int(session.scalar(select(func.coalesce(func.sum(TaskAttachment.size_bytes), 0)).where(subject)))


def _org_bytes(session: Session, org_id: UUID) -> int:
    return int(session.scalar(
        select(func.coalesce(func.sum(TaskAttachment.size_bytes), 0)).where(
            TaskAttachment.org_id == org_id,
            or_(TaskAttachment.task_id.is_not(None), TaskAttachment.automation_id.is_not(None)),
        )
    ))


def _largest_task_bytes(session: Session, org_id: UUID) -> int:
    per_task = (
        select(func.sum(TaskAttachment.size_bytes).label("total"))
        .where(TaskAttachment.org_id == org_id, TaskAttachment.task_id.is_not(None))
        .group_by(TaskAttachment.task_id)
        .subquery()
    )
    return int(session.scalar(select(func.coalesce(func.max(per_task.c.total), 0))))


def size_text(n: int) -> str:
    """Bytes as a person reads them: KB under a megabyte (rounded up, so a file is never "0"), MB under a
    gigabyte, else GB."""
    kb = math.ceil(n / 1024)
    if kb < 1024:
        return f"{kb} KB"
    return f"{n / _MB:.1f} MB" if n < _GB else f"{n / _GB:.1f} GB"


def require_attachment_room(
    session: Session, *, task_id: Optional[int], org_id: UUID, adding: int, automation_id: Optional[UUID] = None
) -> None:
    """409 when storing `adding` more bytes on `task_id` would take the task or its org past a quota. An
    automation's Task-action files (`automation_id`, no task) are held to the per-task cap too — every task it fires
    carries them — and count toward the org's.

    Locks the org row first (held to the caller's commit, `org_limits.lock_org`'s serialization), so two
    concurrent uploads in one org are counted one after the other instead of both fitting the last room."""
    if is_exempt(org_id):
        return
    org = session.scalars(select(Org).where(Org.id == org_id).with_for_update()).one()
    task_cap, _ = _raw(session, org, TASK_ATTACHMENT)
    if task_cap > 0:
        used = _task_bytes(session, task_id, automation_id)
        if used + adding > task_cap * _MB:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Attachment limit reached: this task stores {size_text(used)} of its {task_cap} MB, and "
                    f"this file is {size_text(adding)}. Delete an attachment, or contact us to raise it."
                ),
            )
    org_cap, _ = _raw(session, org, ORG_ATTACHMENT)
    if org_cap > 0:
        used = _org_bytes(session, org_id)
        if used + adding > org_cap * _GB:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Attachment limit reached: your organization stores {used / _GB:.2f} GB of its "
                    f"{org_cap} GB, and this file is {size_text(adding)}. Delete attachments, or contact us to "
                    "raise it."
                ),
            )


# ── File size ─────────────────────────────────────────────────────────────────────────────────────────


def file_cap_bytes(session: Session, org_id: Optional[UUID]) -> int:
    """The largest file `org_id` may upload, in bytes: its override → plan → `(default)`, 0 = `MAX_FILE_BYTES`,
    and never above it. Exempt orgs (and a caller with no org) get the ceiling."""
    if is_exempt(org_id):
        return MAX_FILE_BYTES
    # Never fail open to the ceiling: an org this session cannot see reads `(default)`'s.
    mb, _ = _raw(session, session.get(Org, org_id), FILE_SIZE)
    return MAX_FILE_BYTES if mb <= 0 else min(mb * _MB, MAX_FILE_BYTES)


def _largest_file_bytes(session: Session, org_id: UUID) -> int:
    return int(session.scalar(
        select(func.coalesce(func.max(TaskAttachment.size_bytes), 0)).where(TaskAttachment.org_id == org_id)
    ))


# ── The Plan tab's Limits section ────────────────────────────────────────────────────────────────────────────────────


def effective_quotas(session: Session, org_id: UUID) -> list[Quota]:
    """All four quotas as they govern `org_id`, in `QUOTA_KINDS` order, each with its current usage. An
    exempt org reads unlimited whatever is stored, since nothing enforces it."""
    org = session.get(Org, org_id)
    if org is None:
        raise ValueError(f"no org {org_id}")
    usage = {
        AGENT_REQUESTS: _agent_usage(session, org_id),
        TASK_ATTACHMENT: math.ceil(_largest_task_bytes(session, org_id) / _MB),
        ORG_ATTACHMENT: math.ceil(_org_bytes(session, org_id) / _GB),
        FILE_SIZE: math.ceil(_largest_file_bytes(session, org_id) / _MB),
    }
    out = []
    for kind in QUOTA_KINDS:
        raw, source = _raw(session, org, kind)
        limit = None if raw == 0 or is_exempt(org_id) else raw
        if kind == FILE_SIZE:
            # Never unlimited: past the ceiling, one PUT cannot carry it.
            limit = MAX_FILE_MB if limit is None else min(limit, MAX_FILE_MB)
        out.append(Quota(kind=kind, label=_LABELS[kind], limit=limit, source=source, usage=usage[kind],
                         unit=_UNITS[kind]))
    return out
