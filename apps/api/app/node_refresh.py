"""Tell the nodes that cache something that it changed, so they re-read it now instead of at their slow poll.

The `refresh` frame (CP -> node, no reply) names a `topic`; the node drops that cache and re-reads it
(`apps/jaahd/main.py::_refresh_topic`). It is a latency nudge only: every topic keeps a slow poll on the node as
the durable path, and the node treats every (re)connect as "refresh everything", so a frame dropped while a socket
was down costs latency, never correctness. Sent only after the commit that made the change (`app/after_commit.py`):
a frame that overtook its commit would re-read the old value and then wait out the whole slow poll.

Topics, each with its one trigger:
- `memory` — a project's memory bundle revision advanced (`app/project_memory/bundle.py::bump_bundle_revision`),
  to every node hosting the project.
- `accounts` — an ORM write changed a field the node's account roster serves (`ROSTER_FIELDS`), to every node of
  the account's org (`note_account_roster_changes`, a flush listener registered in `app/db.py`). The roster's
  `runners` count is not a trigger: the node does not act on it.
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app import after_commit


# CP -> node, no reply. Twin of `apps/jaahd/node_bus.py::FRAME_REFRESH`; change both or neither. A node that
# does not know the frame drops it, so adding it needed no version bump.
FRAME_REFRESH = "refresh"
TOPIC_ACCOUNTS = "accounts"
TOPIC_MEMORY = "memory"

# The `accounts` columns `GET /api/agent/accounts` (`AgentAccountOut`) serves: a change to any of them changes
# what a node reads. `org_id` decides which nodes see the row at all.
ROSTER_FIELDS = ("slug", "enabled", "auth_kind", "provider", "node_id", "org_id")


def nudge_after_commit(session: Session, topic: str, node_ids: Iterable[Any], *, key: str,
                       project_id: int | None = None) -> None:
    """Owe a `refresh` frame for `topic` (scoped to `project_id` when given) to each of `node_ids` once `session`
    commits. Never raises; `key` collapses repeats in one transaction (`after_commit.on_commit`)."""
    targets = sorted({str(node_id) for node_id in node_ids if node_id})
    if not targets:
        return
    after_commit.on_commit(session, lambda: send(topic, targets, project_id=project_id), key=f"node-refresh:{key}")


def send(topic: str, node_ids: Iterable[str], *, project_id: int | None = None) -> None:
    """Push one `refresh` frame for `topic` to each node now. Fire-and-forget (`node_send_intent.send_frame`)."""
    from app import node_send_intent  # noqa: PLC0415 — `app.db` imports this module; the bus imports `app.db`

    frame: dict[str, Any] = {"t": FRAME_REFRESH, "topic": topic}
    if project_id is not None:
        frame["project_id"] = project_id
    for node_id in node_ids:
        node_send_intent.send_frame(node_id, frame)


def note_account_roster_changes(session: Session, _flush_context: Any, _instances: Any) -> None:
    """`before_flush`: owe an `accounts` refresh to every node of each org whose roster this flush changes.

    The node ids are an ORM read, so the tenancy read seam applies; it cannot recurse into this flush (a flush in
    progress skips autoflush, and `no_autoflush` says so)."""
    from app.models.credentials import Account  # noqa: PLC0415 — the models import `app.db`, which registers this
    from app.models.nodes import Node  # noqa: PLC0415 — same

    orgs: set[Any] = set()
    for obj in (*session.new, *session.deleted):
        if isinstance(obj, Account) and obj.org_id is not None:
            orgs.add(obj.org_id)
    for obj in session.dirty:
        if isinstance(obj, Account) and _roster_changed(obj):
            # Both sides of an org move: the old org's nodes lose the row, the new org's gain it.
            # The current value read off the object (loading it if expired), the prior one from history.
            previous = inspect(obj).attrs.org_id.history.deleted
            orgs.update(org for org in (obj.org_id, *previous) if org is not None)
    for org_id in orgs:
        # Not caught: a failed read has already aborted the transaction, so the write fails either way.
        with session.no_autoflush:
            node_ids = session.scalars(select(Node.id).where(Node.org_id == org_id)).all()
        nudge_after_commit(session, TOPIC_ACCOUNTS, node_ids, key=f"{TOPIC_ACCOUNTS}:{org_id}")


def _roster_changed(account: Any) -> bool:
    state = inspect(account)
    return any(state.attrs[field].history.has_changes() for field in ROSTER_FIELDS)
