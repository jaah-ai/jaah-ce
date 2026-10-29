"""Data-access events: one `activity_events` row each time a protected value or object is HANDED OVER.

Four kinds, the first three names pinned by the SOC 2 epic's contract (CH-12) so the security-event export can list them:

* `credential_read` — a credential's config was opened for a consumer. `opened_config_for` is the ONE
  seam: it opens the row and records the read together, so no consumer can decrypt without recording
  (`tests/test_data_access_events.py` pins `opened_config_for` as the only app caller of `opened_config`).
* `transcript_downloaded` — a `sessions:edit` holder pulled a session transcript archive.
* `artifact_downloaded` — a task artifact's bytes were handed over as a FILE (not an inline preview).
* `phi_read` — a PHI-bearing record of a HIPAA org was read through the API; recorded only by
  `app/hipaa.py::record_phi_read`, which owns when it applies.

`data` names WHAT was reached and by WHOM — ids, names, a consumer label — never a value: a row here is
stored in cleartext and exported. Rows are staged on the caller's session; the caller commits before it
hands the thing over, so a failed record fails the hand-off rather than letting it go unrecorded.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.credential_crypto import opened_config
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser

CREDENTIAL_READ = "credential_read"
TRANSCRIPT_DOWNLOADED = "transcript_downloaded"
ARTIFACT_DOWNLOADED = "artifact_downloaded"
PHI_READ = "phi_read"
KINDS: tuple[str, ...] = (CREDENTIAL_READ, TRANSCRIPT_DOWNLOADED, ARTIFACT_DOWNLOADED, PHI_READ)


def record(
    session: Session,
    kind: str,
    summary: str,
    *,
    data: dict[str, Any],
    org_id: Optional[UUID],
    user: Optional[AdminUser] = None,
    user_id: Optional[UUID] = None,
    actor_label: Optional[str] = None,
) -> None:
    """Stage one data-access row. `org_id` is the OBJECT's org, named (the table is `Stored`, and an
    operator-plane caller binds no scope to stamp it from). The actor is `user`, or its `user_id` and
    `actor_label` read off it earlier by a caller that has since expired it."""
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a data-access event kind")
    if user is not None:
        user_id, actor_label = user.id, user.email
    session.add(
        ActivityEvent(
            user_id=user_id,
            actor_label=actor_label,
            action_type="read",
            kind=kind,
            summary=summary,
            data=data,
            org_id=org_id,
        )
    )


def record_committed(session: Session, kind: str, summary: str, **fields: Any) -> None:
    """`record`, then COMMIT — for a hand-off that must not happen unrecorded. The one write the
    cookie-authenticated download GETs make (`tests/test_git_access.py` exempts exactly this call)."""
    record(session, kind, summary, **fields)
    session.commit()


def opened_config_for(
    session: Session,
    row: Any,
    *,
    consumer: str,
    task_id: Optional[int] = None,
    node_id: Optional[UUID] = None,
    keep_nodevault: bool = False,
) -> dict[str, Any]:
    """`opened_config(row)`, with its `credential_read` row staged on `session` — the caller commits it
    before the config leaves the process. `consumer` names the path (`task`, `site_enquiry`); `task_id`
    the consuming task, when there is one; `node_id` the node it is handed to, when there is one (the
    runner fetch, AC-5.6). Opened FIRST: a row that cannot be opened was never read.

    `keep_nodevault` leaves each NodeVault envelope SEALED in the result (`credential_crypto.opened_config`)
    for a consumer that hands it to a node to open; every other consumer gets `NodeVaultSealed`."""
    config = opened_config(row, keep_nodevault=keep_nodevault)
    who = f"task {task_id}" if task_id is not None else consumer
    data: dict[str, Any] = {
        "name": row.name,
        "credential_id": str(row.id),
        "credential_type": row.type,
        "consumer": consumer,
        "task_id": task_id,
    }
    if node_id is not None:
        data["node_id"] = str(node_id)
    record(
        session,
        CREDENTIAL_READ,
        f"Credential {row.name} read by {who}",
        data=data,
        org_id=row.org_id,
    )
    return config
