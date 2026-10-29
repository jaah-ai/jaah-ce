"""A merged PR's diff stats — lines added, lines removed, files changed — recorded at its task's settle (R9).

The node reads the stats off the forge and sends them as `SettleIn.pr_stats` on a `merged_pr` settle; the CP only
accepts and stores them (no CP→forge call), one `task_pr_stats` row per task. Capture never blocks a
settle: a payload this module cannot read is stored `not_captured` with reason `malformed`, and an absent one
writes no row (read as "not captured"). No backfill: a row exists only for a settle that carried the payload.

The wire shape:
    {status: "captured"|"partial"|"not_captured", reason: str|null (required unless captured),
     additions, deletions (after exclusions), files_changed (ALL files), excluded_additions, excluded_deletions:
     int|null, forge: "github"|"gitlab"|"bitbucket"|"azure_devops"}
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.forges import FORGES, repo_ref_or_none

log = logging.getLogger(__name__)

PR_STATS_TABLE = "task_pr_stats"

STATUS_CAPTURED, STATUS_PARTIAL, STATUS_NOT_CAPTURED = "captured", "partial", "not_captured"
#: `task_pr_stats.status` (`ck_task_pr_stats_status`).
PR_STATS_STATUSES = (STATUS_CAPTURED, STATUS_PARTIAL, STATUS_NOT_CAPTURED)

#: The reason a payload this module could not read is stored under.
REASON_MALFORMED = "malformed"

#: The integer fields, all `int|null` on the wire; the first three are required for `captured`.
COUNT_FIELDS = ("additions", "deletions", "files_changed", "excluded_additions", "excluded_deletions")
_CAPTURED_REQUIRES = ("additions", "deletions", "files_changed")

# Postgres `integer`: a larger count would fail the INSERT, which must never reach the settle.
_INT_MAX = 2**31 - 1
# A reason is a short operator-facing line; a longer one is cut, never refused.
_REASON_MAX = 500

# Stored when neither the payload nor the task's project names a forge (only reachable by a direct caller: the
# settle route has already resolved the project's repo before a `merged_pr` is accepted).
FORGE_UNKNOWN = "unknown"


def _count(value: Any) -> tuple[bool, Optional[int]]:
    """`(ok, value)`: None, or a non-negative int within Postgres `integer`. A bool is not a count."""
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _INT_MAX:
        return False, None
    return True, value


def normalize_pr_stats(raw: Any) -> Optional[dict[str, Any]]:
    """The row values for a well-formed payload (`forge` only when the payload named one), or None when
    malformed. Unknown keys are ignored, so a newer node's extra field does not discard the stats."""
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if status not in PR_STATS_STATUSES:
        return None
    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        return None
    try:
        # A lone surrogate is valid JSON and unencodable as UTF-8, so the INSERT would fail on it.
        (reason or "").encode("utf-8")
    except UnicodeEncodeError:
        return None
    # Postgres `text` refuses NUL, which would fail the INSERT.
    reason = (reason or "").replace("\x00", "").strip()[:_REASON_MAX] or None
    if status != STATUS_CAPTURED and reason is None:
        return None
    values: dict[str, Any] = {"status": status, "reason": reason}
    for field in COUNT_FIELDS:
        ok, count = _count(raw.get(field))
        if not ok:
            return None
        values[field] = count
    if status == STATUS_CAPTURED and any(values[field] is None for field in _CAPTURED_REQUIRES):
        return None
    forge = raw.get("forge")
    if forge is not None and forge not in FORGES:
        return None
    if forge is not None:
        values["forge"] = forge
    return values


def record_pr_stats(session: Session, task_id: int, org_id: Any, project: Any, pr_url: str, raw: Any) -> None:
    """Upsert the task's one `task_pr_stats` row from `raw` (the settle's `pr_stats`) WITHOUT committing — the
    caller's settle transaction carries it. A malformed payload is stored `not_captured`/`malformed`; the caller
    skips this entirely when no payload was sent. The write runs in a savepoint and a failure is logged, never
    raised: losing the stats is acceptable, losing the settle is not (AC9.2)."""
    from app.models.task_extras import TaskPrStats

    try:
        values = normalize_pr_stats(raw)
        if values is None:
            values = {"status": STATUS_NOT_CAPTURED, "reason": REASON_MALFORMED, **dict.fromkeys(COUNT_FIELDS)}
        # The project's own forge is authoritative; the payload's is a fallback for a repo that does not resolve.
        ref = repo_ref_or_none(project)
        values["forge"] = ref.forge if ref is not None else values.get("forge", FORGE_UNKNOWN)
        row = {"task_id": task_id, "org_id": org_id, "pr_url": pr_url, **values}
        statement = insert(TaskPrStats).values(**row)
        with session.begin_nested():
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[TaskPrStats.task_id],
                    set_={**{key: statement.excluded[key] for key in row if key != "task_id"},
                          "captured_at": func.now()},
                )
            )
    except Exception:  # noqa: BLE001 — any failure here must leave the settle standing (AC9.2)
        log.exception("task %s: could not store its PR stats; the settle proceeds without them", task_id)
