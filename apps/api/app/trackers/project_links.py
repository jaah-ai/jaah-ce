"""A Jaah project's links to its org's tracker connections — the one link model (`TrackerMapping` rows), edited from
the project page (`routes/project_trackers.py`) and from the connection's Mapping tab (`connections.apply_settings`).

A row links one tracker container (team, project, space) of a connection to one Jaah project; a row with no
container is a project linked to the connection with none picked yet. A container may be linked to several projects:
each picks its issues, and exactly one link per container is the PRIMARY, the project tracker-started tasks land in
(`primary_project_for`). The primary rule holds under a lock on the connection row: a container's first link is
primary, deleting the primary promotes the container's oldest remaining link, and Make primary moves it in one
transaction.

The same links bound the issue picker (§ Issues): a task form searches and links issues only from the containers its
project is linked to — the write-back's security boundary, since a linked task posts to its issue. A hand-linked task
is one more `tracker_issue_links` row (`origin` `manual`), told what an intake-started task is (`trackers.writeback`).

Every write ends in `_after_write`: the connection's default automation is retargeted to the primary projects
(`connections._retarget`). Flushes; the caller commits.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.actors import stamp_actor
from app.clock import utcnow
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task
from app.models.trackers import TrackerConnection, TrackerIssueLink, TrackerMapping, TrackerWriteback
from app.task_lifecycle import DONE, SYSTEM_FAILED
from app.trackers import protocol
from app.trackers.vocabulary import ORIGIN_INTAKE, ORIGIN_MANUAL, SCOPE_FULL, SCOPE_OUTCOME, TRACKER_LABELS

log = logging.getLogger("tracker-project-links")

NO_CONNECTION = "no such tracker connection"
NO_LINK = "this project is not linked to that tracker project"


class LinkNotFound(LookupError):
    """The connection is not one of the project's org's, or the project holds no such link: a 404."""


# ── Reads ─────────────────────────────────────────────────────────────────────────────────────────────


def _connection_wire(conn: TrackerConnection) -> dict[str, Any]:
    return {"id": str(conn.id), "tracker": conn.tracker, "tracker_label": TRACKER_LABELS[conn.tracker],
            "name": conn.name, "auth_kind": conn.auth_kind}


def available_connections(session: Session, project: Project) -> list[dict[str, Any]]:
    """The project's org's connections a project editor may link to, oldest first. Labels only: never a credential,
    a token or the settings."""
    rows = session.scalars(
        select(TrackerConnection).where(TrackerConnection.org_id == project.org_id)
        .order_by(TrackerConnection.created_at, TrackerConnection.id)
    ).all()
    return [_connection_wire(conn) for conn in rows]


def links_for(session: Session, project: Project) -> list[dict[str, Any]]:
    """Each connection the project is linked to (oldest link first): the connection's labels and its last outbound
    error, the linked containers (primary or not, and how many other projects link each), and `needs_pick` while
    no container is picked."""
    rows = session.scalars(
        select(TrackerMapping).where(TrackerMapping.project_id == project.id)
        .order_by(TrackerMapping.created_at, TrackerMapping.id)
    ).all()
    if not rows:
        return []
    connection_ids = list(dict.fromkeys(row.connection_id for row in rows))
    conns = {conn.id: conn for conn in session.scalars(
        select(TrackerConnection).where(TrackerConnection.id.in_(connection_ids))
    ).all()}
    # How many OTHER projects link each container (one tenant's rows: the seam scopes the count to the org).
    shared = {(cid, container): count for cid, container, count in session.execute(
        select(TrackerMapping.connection_id, TrackerMapping.container_id, func.count())
        .where(TrackerMapping.connection_id.in_(connection_ids), TrackerMapping.container_id.is_not(None),
               TrackerMapping.project_id != project.id)
        .group_by(TrackerMapping.connection_id, TrackerMapping.container_id)
    ).all()}
    out = []
    for connection_id in connection_ids:
        conn = conns.get(connection_id)
        if conn is None:
            continue
        mine = [row for row in rows if row.connection_id == connection_id and row.container_id is not None]
        out.append({
            "connection": {**_connection_wire(conn), "site": conn.site, "error": conn.last_error},
            "containers": [{
                "container_id": row.container_id, "name": row.container_name or row.container_id,
                "primary": row.is_primary, "shared": shared.get((connection_id, row.container_id), 0),
            } for row in mine],
            "needs_pick": not mine,
        })
    return out


def primary_project_for(session: Session, connection_id: UUID, container_id: str) -> Optional[int]:
    """The Jaah project tracker-started tasks of this container land in (its primary link), or None."""
    return session.scalar(select(TrackerMapping.project_id).where(
        TrackerMapping.connection_id == connection_id, TrackerMapping.container_id == str(container_id),
        TrackerMapping.is_primary.is_(True),
    ))


def primary_rows(session: Session, connection_id: UUID) -> list[TrackerMapping]:
    """The connection's primary links, oldest first: what the Mapping tab lists and the mirror writes."""
    return list(session.scalars(
        select(TrackerMapping).where(TrackerMapping.connection_id == connection_id,
                                     TrackerMapping.is_primary.is_(True))
        .order_by(TrackerMapping.created_at, TrackerMapping.id)
    ).all())


def mappings_wire(session: Session, connection_id: UUID) -> list[dict[str, Any]]:
    """The primary links as the admin wire's `settings.mappings` (`{container_id, project_id}`)."""
    return [{"container_id": row.container_id, "project_id": row.project_id}
            for row in primary_rows(session, connection_id)]


# ── Outbound bookkeeping ──────────────────────────────────────────────────────────────────────────────


def record_outbound(session: Session, connection_id: UUID, error: Optional[str]) -> None:
    """Keep an outbound call's result on the connection: the failure's sentence (a `TrackerError` message, never a
    token), or clear it on success. Flushes."""
    conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
    if conn is None:
        return
    if error is None:
        if conn.last_error is None:
            return
        conn.last_error, conn.last_error_at = None, None
    else:
        conn.last_error, conn.last_error_at = error, utcnow()
    session.flush()


def refresh_names(session: Session, connection_id: UUID, pairs: Iterable[tuple[str, str]]) -> None:
    """Cache the tracker's current container names on the connection's links (read whenever the list is): one
    bulk UPDATE per renamed container, so a link deleted meanwhile is no error. Flushes."""
    now = utcnow()
    for container_id, name in {str(cid): name for cid, name in pairs}.items():
        session.execute(
            update(TrackerMapping).where(
                TrackerMapping.connection_id == connection_id, TrackerMapping.container_id == container_id,
                TrackerMapping.container_name.is_distinct_from(name),
            ).values(container_name=name, updated_at=now).execution_options(synchronize_session=False)
        )
    session.flush()


# ── Writes (the primary rule under the connection's lock) ─────────────────────────────────────────────


def _lock(session: Session, connection_id: UUID, org_id: Optional[UUID]) -> TrackerConnection:
    """The connection row, locked for this transaction; `LinkNotFound` when it is not `org_id`'s."""
    conn = session.scalar(
        select(TrackerConnection).where(TrackerConnection.id == connection_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if conn is None or conn.org_id != org_id:
        raise LinkNotFound(NO_CONNECTION)
    return conn


def _settle_primary(session: Session, connection_id: UUID, container_id: Optional[str]) -> None:
    """A container with links and no primary gets one: its oldest link (a first link, or a deleted primary's heir)."""
    if container_id is None:
        return
    rows = session.scalars(
        select(TrackerMapping).where(TrackerMapping.connection_id == connection_id,
                                     TrackerMapping.container_id == container_id)
        .order_by(TrackerMapping.created_at, TrackerMapping.id)
    ).all()
    if rows and not any(row.is_primary for row in rows):
        rows[0].is_primary = True
        rows[0].updated_at = utcnow()
        session.flush()


def _add(session: Session, actor: AdminUser, conn: TrackerConnection, project_id: int,
         container_id: Optional[str]) -> TrackerMapping:
    known = session.scalar(select(TrackerMapping.container_name).where(
        TrackerMapping.connection_id == conn.id, TrackerMapping.container_id == container_id,
        TrackerMapping.container_name.is_not(None),
    ).limit(1)) if container_id is not None else None
    if container_id is not None:  # a picked container replaces the project's "none picked yet" row
        session.execute(delete(TrackerMapping).where(
            TrackerMapping.connection_id == conn.id, TrackerMapping.project_id == project_id,
            TrackerMapping.container_id.is_(None),
        ).execution_options(synchronize_session="fetch"))
    row = TrackerMapping(org_id=conn.org_id, connection_id=conn.id, project_id=project_id,
                         container_id=container_id, container_name=known, is_primary=False)
    stamp_actor(row, actor, created=True)
    session.add(row)
    session.flush()
    return row


def _delete(session: Session, rows: Iterable[TrackerMapping]) -> set[str]:
    """Delete the rows; answers the containers whose primary went (each needs `_settle_primary`)."""
    orphaned = {row.container_id for row in rows if row.is_primary and row.container_id is not None}
    ids = [row.id for row in rows]
    if ids:
        session.execute(delete(TrackerMapping).where(TrackerMapping.id.in_(ids)).execution_options(
            synchronize_session="fetch"))
        session.flush()
    return orphaned


def _after_write(session: Session, conn: TrackerConnection, actor: AdminUser) -> None:
    """Stamp the connection and retarget the default automation."""
    from app.trackers import connections

    stamp_actor(conn, actor)
    session.flush()
    connections._retarget(session, conn)
    session.flush()


def _project_rows(session: Session, conn: TrackerConnection, project_id: int) -> list[TrackerMapping]:
    return list(session.scalars(select(TrackerMapping).where(
        TrackerMapping.connection_id == conn.id, TrackerMapping.project_id == project_id,
    ).order_by(TrackerMapping.created_at, TrackerMapping.id)).all())


def set_link(session: Session, actor: AdminUser, project: Project, connection_id: UUID,
             container_ids: Iterable[str]) -> None:
    """Replace the project's links to the connection with `container_ids` (an empty list keeps the project linked
    with none picked: one row with no container). `LinkNotFound` when the connection is not the project's org's."""
    conn = _lock(session, connection_id, project.org_id)
    wanted = list(dict.fromkeys(cid for cid in (str(c).strip() for c in container_ids) if cid))
    current = _project_rows(session, conn, project.id)
    keep: set[Optional[str]] = set(wanted) if wanted else {None}
    orphaned = _delete(session, [row for row in current if row.container_id not in keep])
    held = {row.container_id for row in current if row.container_id in keep}
    picks: list[Optional[str]] = list(wanted) if wanted else [None]
    for container_id in picks:
        if container_id not in held:
            _add(session, actor, conn, project.id, container_id)
    for container_id in orphaned | set(wanted):
        _settle_primary(session, conn.id, container_id)
    _after_write(session, conn, actor)


def remove_link(session: Session, actor: AdminUser, project: Project, connection_id: UUID) -> None:
    """Unlink the project from the connection: its links go, each primary among them passes to the container's
    oldest remaining link. Tasks already linked to an issue stay linked."""
    conn = _lock(session, connection_id, project.org_id)
    for container_id in _delete(session, _project_rows(session, conn, project.id)):
        _settle_primary(session, conn.id, container_id)
    _after_write(session, conn, actor)


def unlink_project(session: Session, actor: AdminUser, project_id: int) -> None:
    """Before a project is deleted: unlink it from every connection (`remove_link`), so each primary it held passes
    on, the mirror and the default automation drop it, rather than the row CASCADE leaving a container with none."""
    project = session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        return
    connection_ids = list(dict.fromkeys(session.scalars(
        select(TrackerMapping.connection_id).where(TrackerMapping.project_id == project_id)
        .order_by(TrackerMapping.connection_id)
    ).all()))
    for connection_id in connection_ids:
        remove_link(session, actor, project, connection_id)


def make_primary(session: Session, actor: AdminUser, project: Project, connection_id: UUID,
                 container_id: str) -> None:
    """Make the project's link of `container_id` the container's primary, the old primary demoted, in one
    transaction. `LinkNotFound` when the project holds no such link."""
    conn = _lock(session, connection_id, project.org_id)
    row = session.scalar(select(TrackerMapping).where(
        TrackerMapping.connection_id == conn.id, TrackerMapping.project_id == project.id,
        TrackerMapping.container_id == str(container_id),
    ))
    if row is None:
        raise LinkNotFound(NO_LINK)
    if not row.is_primary:
        _set_primary(session, actor, conn, row)
    _after_write(session, conn, actor)


def _set_primary(session: Session, actor: AdminUser, conn: TrackerConnection, row: TrackerMapping) -> None:
    now = utcnow()
    for old in session.scalars(select(TrackerMapping).where(
        TrackerMapping.connection_id == conn.id, TrackerMapping.container_id == row.container_id,
        TrackerMapping.is_primary.is_(True), TrackerMapping.id != row.id,
    )).all():
        old.is_primary, old.updated_at = False, now
        stamp_actor(old, actor)
    session.flush()
    row.is_primary, row.updated_at = True, now
    stamp_actor(row, actor)
    session.flush()


def apply_admin_mappings(session: Session, actor: AdminUser, conn: TrackerConnection,
                         mappings: list[tuple[str, int]]) -> None:
    """The Mapping tab's save (`connections.apply_settings`): `mappings` is the connection's whole container →
    project list, as it reads them (the primary links). Each pair it names is linked and made its container's
    primary (an earlier primary project stays linked, demoted); a container it leaves out loses its primary link,
    which passes to the container's oldest other link. Links a project page added that are not primary stay.
    Projects are the caller's to validate (the connection's org's)."""
    conn = _lock(session, conn.id, conn.org_id)
    wanted = dict(mappings)
    primaries = primary_rows(session, conn.id)
    # A container the save leaves out is unlinked from its primary project; one it maps elsewhere keeps the old
    # project linked, demoted (`_set_primary` below), as Make primary would.
    orphaned = _delete(session, [row for row in primaries if row.container_id not in wanted])
    for container_id, project_id in wanted.items():
        row = session.scalar(select(TrackerMapping).where(
            TrackerMapping.connection_id == conn.id, TrackerMapping.project_id == project_id,
            TrackerMapping.container_id == container_id,
        ))
        if row is None:
            row = _add(session, actor, conn, project_id, container_id)
        if not row.is_primary:
            _set_primary(session, actor, conn, row)
    for container_id in orphaned - set(wanted):
        _settle_primary(session, conn.id, container_id)
    _after_write(session, conn, actor)



# ── Issues: the task form's picker and the hand link ─────────────────────────────────────────────────

NOT_LINKED_ISSUE = "That issue isn't in a tracker project linked to this project."
# One connection's search or read; a slow tracker answers as an error while the others still list.
ISSUE_CALL_TIMEOUT_SECONDS = 8.0
MAX_SEARCH_RESULTS = 50


class IssueNotFound(LookupError):
    """The connection is not linked to the project, or the issue is not in one of its linked containers: a 404
    (`NOT_LINKED_ISSUE`), the same answer either way."""


class LinkRefused(ValueError):
    """The task's link was made by the tracker's intake: the form may not replace or clear it (a 409)."""


@dataclass(frozen=True)
class LinkedConnection:
    """A connection the project is linked to (detached), and the containers it picked there."""
    connection: TrackerConnection
    container_ids: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PickedIssue:
    """An issue read from its tracker and proven to sit in one of the project's linked containers."""
    connection_id: UUID
    tracker: str
    issue: protocol.IssueDetail

    def wire(self) -> dict[str, Any]:
        return {"connection_id": str(self.connection_id), "tracker": self.tracker,
                "tracker_label": TRACKER_LABELS[self.tracker], "key": self.issue.key, "title": self.issue.title,
                "url": self.issue.url, "body_markdown": self.issue.body_markdown}


def linked_connections(session: Session, project_id: int) -> list[LinkedConnection]:
    """The connections the project is linked to with at least one container picked, oldest link first, each
    detached from `session` (a tracker call holds no database connection)."""
    rows = session.execute(
        select(TrackerMapping.connection_id, TrackerMapping.container_id)
        .where(TrackerMapping.project_id == project_id, TrackerMapping.container_id.is_not(None))
        .order_by(TrackerMapping.created_at, TrackerMapping.id)
    ).all()
    containers: dict[UUID, list[str]] = {}
    for connection_id, container_id in rows:
        containers.setdefault(connection_id, []).append(str(container_id))
    if not containers:
        return []
    project_org = session.scalar(select(Project.org_id).where(Project.id == project_id))
    conns = {conn.id: conn for conn in session.scalars(
        select(TrackerConnection).where(TrackerConnection.id.in_(list(containers)))
    ).all()}
    out = []
    for connection_id, ids in containers.items():
        conn = conns.get(connection_id)
        if conn is None or conn.org_id != project_org:
            continue
        session.expunge(conn)
        out.append(LinkedConnection(conn, tuple(dict.fromkeys(ids))))
    return out


def _call_failed(conn: TrackerConnection, exc: BaseException) -> str:
    from app.trackers.writeback import outbound_sentence

    if not isinstance(exc, (protocol.TrackerError, TimeoutError)):
        log.warning("tracker connection %s: issue call failed", conn.id, exc_info=exc)
    return outbound_sentence(conn.tracker, exc)


async def _record_all(db: Any, outcomes: dict[UUID, Optional[str]]) -> None:
    """Keep each connection's call result as its `last_error` (a sentence, or cleared), in one commit."""
    def keep(session: Session) -> None:
        for connection_id, error in outcomes.items():
            record_outbound(session, connection_id, error)
        session.commit()

    if outcomes:
        await db.run_sync(keep)


def _interleaved(per_connection: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Every connection's answers taken in turn (a busy tracker never crowds another out), at most `limit`. One issue
    reached through two connections (an API-token and a Forge connection on one Jira site) is listed once, through
    the older link: the two rows would read the same."""
    def same(item: dict[str, Any]) -> str:
        return item["url"] or f"{item['connection_id']}:{item['key']}"

    owner: dict[str, int] = {}  # each issue's older link, whatever rank either connection answered it at
    for index, items in enumerate(per_connection):
        for item in items:
            owner.setdefault(same(item), index)
    out: list[dict[str, Any]] = []
    for rank in range(max((len(items) for items in per_connection), default=0)):
        for index, items in enumerate(per_connection):
            if rank < len(items) and owner[same(items[rank])] == index:
                out.append(items[rank])
    return out[:limit]


async def search(db: Any, project_id: int, query: str, limit: int) -> SearchResult:
    """Issues matching `query` (a key or title words) across every connection the project is linked to, only from
    its linked containers. Connections answer concurrently, each bounded by `ISSUE_CALL_TIMEOUT_SECONDS`; one that
    fails is an `errors` entry (its sentence) while the rest still answer. `db` is the route's `AsyncSession`; its
    connection is released before any tracker call."""
    from app import db_async

    query = " ".join(query.split())
    limit = max(1, min(limit, MAX_SEARCH_RESULTS))
    linked = await db.run_sync(lambda session: linked_connections(session, project_id))
    await db_async.release(db)
    if not query or not linked:
        return SearchResult()

    async def one(link: LinkedConnection) -> list[protocol.IssueSummary]:
        adapter = protocol.adapter_for(link.connection.tracker)
        if adapter is None:
            raise protocol.TrackerError(f"{TRACKER_LABELS[link.connection.tracker]} can't be reached yet")
        return await asyncio.wait_for(adapter.search_issues(link.connection, query, link.container_ids, limit),
                                      ISSUE_CALL_TIMEOUT_SECONDS)

    answers = await asyncio.gather(*(one(link) for link in linked), return_exceptions=True)
    per_connection: list[list[dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    outcomes: dict[UUID, Optional[str]] = {}
    for link, answer in zip(linked, answers, strict=True):
        conn = link.connection
        label = TRACKER_LABELS[conn.tracker]
        if isinstance(answer, BaseException):
            if not isinstance(answer, Exception):
                raise answer  # a cancellation is the request's, never one tracker's error
            sentence = _call_failed(conn, answer)
            outcomes[conn.id] = sentence
            errors.append({"connection_id": str(conn.id), "tracker_label": label, "name": conn.name,
                           "detail": sentence})
            continue
        outcomes[conn.id] = None
        # The adapter narrows already; held again here, the boundary a linked task's write-back relies on.
        per_connection.append([{
            "connection_id": str(conn.id), "tracker": conn.tracker, "tracker_label": label,
            "key": item.key, "title": item.title, "url": item.url,
        } for item in protocol.keep_containers(list(answer), link.container_ids)])
    await _record_all(db, outcomes)
    return SearchResult(_interleaved(per_connection, limit), errors)


async def issue(db: Any, project_id: int, connection_id: UUID, key: str) -> PickedIssue:
    """One issue read from its tracker: `IssueNotFound` unless the connection is linked to the project and the
    issue's container is one the project linked there (judged on the tracker's answer, never the key);
    `protocol.TrackerError` with the tracker's sentence when the call fails or times out."""
    from app import db_async

    key = key.strip()
    linked = await db.run_sync(lambda session: linked_connections(session, project_id))
    await db_async.release(db)
    link = next((item for item in linked if item.connection.id == connection_id), None)
    if link is None or not protocol.key_shaped(key):
        raise IssueNotFound(NOT_LINKED_ISSUE)
    conn = link.connection
    adapter = protocol.adapter_for(conn.tracker)
    try:
        if adapter is None:
            raise protocol.TrackerError(f"{TRACKER_LABELS[conn.tracker]} can't be reached yet")
        detail = await asyncio.wait_for(adapter.get_issue(conn, key), ISSUE_CALL_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — every failure is the connection's sentence, never a 500
        sentence = _call_failed(conn, exc)
        await _record_all(db, {conn.id: sentence})
        raise protocol.TrackerError(sentence) from exc
    await _record_all(db, {conn.id: None})
    if str(detail.container_id) not in link.container_ids:
        raise IssueNotFound(NOT_LINKED_ISSUE)
    return PickedIssue(conn.id, conn.tracker, detail)


def task_link(session: Session, task_id: int) -> Optional[TrackerIssueLink]:
    return session.scalar(select(TrackerIssueLink).where(TrackerIssueLink.task_id == task_id))


def same_issue(link: Optional[TrackerIssueLink], connection_id: UUID, key: str) -> bool:
    """True when `link` names this issue (keys compared without case: every tracker's keys are upper case)."""
    return (link is not None and link.connection_id == connection_id
            and link.issue_key.upper() == key.strip().upper())


def _refuse_intake(link: TrackerIssueLink) -> None:
    if link.origin == ORIGIN_INTAKE:
        raise LinkRefused(f"This task was started from {link.issue_key}; its issue can't be changed.")


def _drop(session: Session, link: TrackerIssueLink) -> None:
    """Delete the task's link and what was posted under it, so a next link starts its write-back afresh."""
    session.execute(delete(TrackerWriteback).where(TrackerWriteback.task_id == link.task_id)
                    .execution_options(synchronize_session=False))
    session.delete(link)
    session.flush()


def link_task(session: Session, task: Task, picked: PickedIssue) -> TrackerIssueLink:
    """Link `task` to the picked issue by hand (`origin` `manual`), replacing its earlier hand link and that link's
    write-backs; the same issue again is no change. A task that had already finished is owed its outcome only
    (`writeback_scope` `outcome`). `LinkRefused` for a task the tracker started. Several tasks may link one issue.
    Flushes; the caller commits with the task."""
    current = task_link(session, task.id)
    if same_issue(current, picked.connection_id, picked.issue.key):
        return current  # type: ignore[return-value]
    if current is not None:
        _refuse_intake(current)
        _drop(session, current)
    row = TrackerIssueLink(
        org_id=task.org_id, task_id=task.id, connection_id=picked.connection_id, issue_key=picked.issue.key,
        issue_url=picked.issue.url, container_id=str(picked.issue.container_id), session_id=None,
        started_by_external={}, origin=ORIGIN_MANUAL,
        writeback_scope=SCOPE_OUTCOME if task.state in (DONE, SYSTEM_FAILED) else SCOPE_FULL,
    )
    session.add(row)
    session.flush()
    return row


def drop_outside_project(session: Session, task: Task) -> None:
    """After a task moves: its hand link goes (with its write-backs) unless its project links the issue's container
    on that connection. A link the tracker made stays: it names where the task came from. Flushes."""
    current = task_link(session, task.id)
    if current is None or current.origin == ORIGIN_INTAKE:
        return
    linked = session.scalar(select(TrackerMapping.id).where(
        TrackerMapping.project_id == task.project_id, TrackerMapping.connection_id == current.connection_id,
        TrackerMapping.container_id == current.container_id,
    ).limit(1))
    if linked is None:
        _drop(session, current)


def unlink_task(session: Session, task: Task) -> None:
    """Clear the task's hand link and its write-backs; `LinkRefused` for a task the tracker started. Flushes."""
    current = task_link(session, task.id)
    if current is None:
        return
    _refuse_intake(current)
    _drop(session, current)


def task_issue_wire(session: Session, task_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Each task's linked issue as the task wire's `tracker_issue` (`{connection_id, tracker, tracker_label, key,
    url, origin}`), one query for every row; a task with none is absent."""
    ids = list(dict.fromkeys(task_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(TrackerIssueLink.task_id, TrackerIssueLink.connection_id, TrackerConnection.tracker,
               TrackerIssueLink.issue_key, TrackerIssueLink.issue_url, TrackerIssueLink.origin)
        .join(TrackerConnection, TrackerConnection.id == TrackerIssueLink.connection_id)
        .where(TrackerIssueLink.task_id.in_(ids))
    ).all()
    return {row.task_id: {
        "connection_id": str(row.connection_id), "tracker": row.tracker, "tracker_label": TRACKER_LABELS[row.tracker],
        "key": row.issue_key, "url": row.issue_url, "origin": row.origin,
    } for row in rows}
