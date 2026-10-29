"""A merged PR's provenance — its merge commit, its commits and the paths it changed — recorded at its task's settle.

The twin of `app/pr_stats.py`, on the same seam: the node reads the PR off the forge with the task's own login and
stamps it as `SettleIn.pr_provenance` on a `merged_pr` settle (`apps/jaahd/pr_provenance_capture.py`, a body key
the model cannot write); the CP only accepts and stores it (no CP→forge call), one `task_pr_changes`
row per `(task, PR)`. Capture never blocks a settle: a payload this module cannot read is stored `not_captured`
with reason `malformed`, and an absent one writes no row (read as "not captured"). No backfill.

The wire shape (compliance-manifest architecture §2.7):
    {status: "captured"|"partial"|"not_captured", reason: str|null (required unless captured),
     forge, pr_number, merge_sha: str|null,
     commits: [{sha, author_name, author_login: str|null, message}] (message ≤ 4 KiB; ≤ 250 commits),
     files: [path] (≤ 3000)}
Commit messages are stored as the forge returned them; redacting them for a manifest is the manifest's job.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.forges import FORGES, repo_ref_or_none
from app.pr_stats import (
    FORGE_UNKNOWN, PR_STATS_STATUSES, REASON_MALFORMED, STATUS_CAPTURED, STATUS_NOT_CAPTURED,
)

log = logging.getLogger(__name__)

PR_CHANGES_TABLE = "task_pr_changes"

#: `task_pr_changes.status` (`ck_task_pr_changes_status`): the same set as `task_pr_stats`.
PR_CHANGES_STATUSES = PR_STATS_STATUSES

#: The caps the node applies (`apps/jaahd/forge_cli/model.py`); a payload past them is malformed.
MAX_COMMITS = 250
MAX_FILES = 3000
MAX_MESSAGE_BYTES = 4096

# A path or a name is a line, not a document: anything past this is not a forge's answer.
_TEXT_MAX = 4096
_REASON_MAX = 500
_INT_MAX = 2**31 - 1
# A git object id: SHA-1 (40) or SHA-256 (64) hex; a forge may abbreviate, never below 7.
_SHA = re.compile(r"[0-9a-f]{7,64}")


class _Malformed(Exception):
    pass


def _text(value: Any, *, cap: int = _TEXT_MAX, optional: bool = False) -> Optional[str]:
    """`value` as storable text — NUL stripped (neither `text` nor `jsonb` stores one) — or `_Malformed`."""
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _Malformed
    try:
        # A lone surrogate is valid JSON and unencodable as UTF-8, so the INSERT would fail on it.
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _Malformed from None
    if len(encoded) > cap:
        raise _Malformed
    return value.replace("\x00", "")


def _sha(value: Any, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _Malformed
    return value


def _commit(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _Malformed
    return {
        "sha": _sha(raw.get("sha")),
        "author_name": _text(raw.get("author_name")),
        "author_login": _text(raw.get("author_login"), optional=True),
        "message": _text(raw.get("message"), cap=MAX_MESSAGE_BYTES),
    }


def normalize_pr_provenance(raw: Any) -> Optional[dict[str, Any]]:
    """The row values for a well-formed payload (`forge`/`pr_number` only when the payload named them), or None
    when malformed. Unknown keys are ignored, so a newer node's extra field does not discard the capture."""
    if not isinstance(raw, dict) or raw.get("status") not in PR_CHANGES_STATUSES:
        return None
    status = raw["status"]
    commits, files = raw.get("commits", []), raw.get("files", [])
    if not isinstance(commits, list) or not isinstance(files, list):
        return None
    if len(commits) > MAX_COMMITS or len(files) > MAX_FILES:
        return None
    try:
        reason = (_text(raw.get("reason"), optional=True) or "").strip()[:_REASON_MAX] or None
        values: dict[str, Any] = {
            "status": status, "reason": reason,
            "merge_sha": _sha(raw.get("merge_sha"), optional=True),
            "commits": [_commit(c) for c in commits],
            "files": [_text(f) for f in files],
        }
    except _Malformed:
        return None
    if status != STATUS_CAPTURED and reason is None:
        return None
    number = raw.get("pr_number")
    if number is not None:
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= _INT_MAX:
            return None
        values["pr_number"] = number
    forge = raw.get("forge")
    if forge is not None and forge not in FORGES:
        return None
    if forge is not None:
        values["forge"] = forge
    return values


def record_pr_provenance(session: Session, task_id: int, org_id: Any, project: Any, pr_url: str, raw: Any) -> None:
    """Upsert the task's `task_pr_changes` row for this PR from `raw` (the settle's `pr_provenance`) WITHOUT
    committing — the caller's settle transaction carries it. A malformed payload is stored
    `not_captured`/`malformed`; the caller skips this entirely when no payload was sent. The write runs in a
    savepoint and a failure is logged, never raised: losing the provenance is acceptable, losing the settle is
    not."""
    from app.models.task_extras import TaskPrChanges

    try:
        values = normalize_pr_provenance(raw)
        if values is None:
            values = {"status": STATUS_NOT_CAPTURED, "reason": REASON_MALFORMED, "merge_sha": None,
                      "commits": [], "files": []}
        # The project's own repo is authoritative for both the forge and the PR number; the payload's are a
        # fallback for a repo that does not resolve. 0 keys a row whose PR number nobody could name.
        ref = repo_ref_or_none(project)
        values["forge"] = ref.forge if ref is not None else values.get("forge", FORGE_UNKNOWN)
        number = ref.pr_number(pr_url) if ref is not None else None
        values["pr_number"] = number or values.get("pr_number") or 0
        row = {"task_id": task_id, "org_id": org_id, "pr_url": pr_url, **values}
        statement = insert(TaskPrChanges).values(**row)
        with session.begin_nested():
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[TaskPrChanges.task_id, TaskPrChanges.pr_number],
                    set_={**{key: statement.excluded[key] for key in row if key not in ("task_id", "pr_number")},
                          "captured_at": func.now()},
                )
            )
    except Exception:  # noqa: BLE001 — any failure here must leave the settle standing
        log.exception("task %s: could not store its PR provenance; the settle proceeds without it", task_id)
