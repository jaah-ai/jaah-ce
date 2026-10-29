"""The one home for writing a request's actor onto a row's provenance columns.

Writers: `stamp_actor` (per-route, entity tables), `stamp_dirty_task_actors` (a `before_flush`
listener for `tasks`), and `task_actor_values` (splatted into a Core `update(Task)`). Readers:
`actor_names` and `actor_names_for`. Every entity carries the same provenance columns, each with one job.

A helper rather than the columns' `server_default = SYSTEM_ACTOR_ID`: the default alone would pass
the schema test while every operator edit claimed the system as author.

Provenance only, never an access decision (`app/authz/`) — with one exception: a staging org removes only the role
bindings the system copied from its parent (`app/staging_org.py::sync_admins`).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import act_as
from app.entity_shape import SYSTEM_ACTOR_ID, SYSTEM_ACTOR_NAME
from app.identities import person_label
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.models.tasks import Task
from app.tenancy import seam
from app.tenancy.copy import WITHHELD_ACTOR
from app.tenancy.ids import is_tenant_org


def actor_names(
    session: Session, ids: Iterable[Optional[UUID]], *, withhold_staff: bool = False, name_acting: bool = False,
    home_org: Optional[UUID] = None,
) -> dict[UUID, str]:
    """Resolve actor ids to the names a provenance cell renders, in one query.

    A read of the name pair rather than a stored label (`identities.person_label`), so a rename
    applies everywhere. Takes an iterable to avoid N+1 on list routes. NULLs are dropped; a missing id is
    absent from the result — except for a tenant reader.

    For a caller bound to a tenant org, `AdminUser` is seam-filtered, so operator-provisioned rows
    resolve to nothing; those ids are filled with `app/tenancy/copy.py::WITHHELD_ACTOR`. Every other scope
    leaves unresolved ids absent. `SYSTEM_ACTOR_ID` keeps its own name in EVERY scope: its row sits in org zero
    (revision 0406), which a tenant's scope does not read.

    The `isinstance` check is load-bearing: `is_tenant_org` returns True for a seam sentinel or `None`,
    which would fill every cell on the operator's own console. Branch on the scope kind explicitly and
    fail closed, as `tenancy/seam.py::criteria_for` does.

    `name_acting=True` — a RESPONSE rendered for this request's reader, never text stored or sent on — gives an
    operator ACTING as that org (`act_as.reader_acting`) an operator-plane id as "<name>, acting as <org>" instead
    (`_acting_labels`): the person is no secret to them, and only an operator acting as the org writes into it. Opt-in,
    because the flag is ambient: a change row or an assignment card written in the same request is the tenant's to read.

    `withhold_staff=True` gives an operator-plane account that same label in EVERY scope, for a tenant's own
    record that both sides read (its IP allowlist): the operator then sees what the tenant sees, not a
    colleague's bare email.

    `home_org` — the org owning the record the names annotate — gives a reader who sees names plainly (the operator's
    own console) an actor from ANY OTHER org as "<name> (operator)" for an operator-plane account, else
    "<name> (<their org>)", so a cross-org press on a tenant's task is visible at a glance.
    """
    wanted = {actor_id for actor_id in ids if actor_id is not None}
    if not wanted:
        return {}
    rows = session.execute(
        select(AdminUser.id, AdminUser.first_name, AdminUser.last_name, AdminUser.email, AdminUser.org_id).where(
            AdminUser.id.in_(wanted)
        )
    ).all()
    resolved: dict[UUID, str] = {
        actor_id: (
            WITHHELD_ACTOR
            if withhold_staff and actor_id != SYSTEM_ACTOR_ID and not is_tenant_org(org_id)
            else person_label(first_name, last_name, email)
        )
        for actor_id, first_name, last_name, email, org_id in rows
    }
    if SYSTEM_ACTOR_ID in wanted:
        resolved.setdefault(SYSTEM_ACTOR_ID, SYSTEM_ACTOR_NAME)
    scope = seam.current_scope()
    if not isinstance(scope, UUID) or not is_tenant_org(scope):
        if home_org is not None and not withhold_staff:
            _name_foreign_orgs(session, resolved, {row.id: row.org_id for row in rows}, home_org)
        return resolved
    missing = wanted - set(resolved)
    if missing and name_acting and not withhold_staff and act_as.reader_acting():
        resolved.update(_acting_labels(session, missing, scope))
    for actor_id in wanted - set(resolved):
        resolved[actor_id] = WITHHELD_ACTOR
    return resolved


def _name_foreign_orgs(
    session: Session, resolved: dict[UUID, str], org_of: dict[UUID, Optional[UUID]], home_org: UUID
) -> None:
    """`actor_names`'s `home_org` suffix, IN PLACE: every resolved person outside `home_org` names where they came
    from. The system actor is nobody's colleague and keeps its bare name."""
    foreign = {
        actor_id: org_id for actor_id, org_id in org_of.items()
        if actor_id != SYSTEM_ACTOR_ID and actor_id in resolved and org_id != home_org
    }
    tenant_orgs = {org_id for org_id in foreign.values() if org_id is not None and is_tenant_org(org_id)}
    org_names: dict[UUID, str] = {}
    if tenant_orgs:
        org_names = dict(session.execute(select(Org.id, Org.name).where(Org.id.in_(tenant_orgs))).tuples().all())
    for actor_id, org_id in foreign.items():
        where = "operator" if not is_tenant_org(org_id) else org_id and org_names.get(org_id) or "another org"
        resolved[actor_id] = f"{resolved[actor_id]} ({where})"


def _acting_labels(session: Session, ids: set[UUID], org_id: UUID) -> dict[UUID, str]:
    """The operator-plane ids among `ids` as "<name>, acting as <org>", for an acting operator reading `org_id`.

    Read on the identity lane (`act_as.own_row`'s reason: those rows live in the operator's org, which the bound
    scope does not read). A tenant-org id is not relabelled: it stays withheld, as before."""
    with seam.identity_scope():
        rows = session.execute(
            select(AdminUser.id, AdminUser.first_name, AdminUser.last_name, AdminUser.email, AdminUser.org_id).where(
                AdminUser.id.in_(ids)
            )
        ).all()
    staff = [row for row in rows if not is_tenant_org(row.org_id)]
    org_name = session.scalar(select(Org.name).where(Org.id == org_id)) if staff else None
    if org_name is None:
        return {}
    return {
        actor_id: f"{person_label(first_name, last_name, email)}, acting as {org_name}"
        for actor_id, first_name, last_name, email, _org in staff
    }


def actor_names_for(session: Session, rows: Iterable[Any]) -> dict[UUID, str]:
    """`actor_names` over a page of entity rows: the provenance pair of every row, in one
    query."""
    ids: list[Optional[UUID]] = []
    for row in rows:
        ids.extend((row.created_by_id, row.updated_by_id))
    return actor_names(session, ids)


def stamp_actor(obj: Any, user: Any, *, created: bool = False) -> None:
    """Attribute one entity row to `user`.

    `created=True` (INSERT) sets both columns; the default (UPDATE) sets only `updated_by_id`, so
    authorship survives later edits. A `None` user raises rather than falling back to the system
    actor.
    """
    if user is None:
        raise ValueError(
            "stamp_actor requires the request's AdminUser; a None actor would silently leave the row "
            "attributed to the system actor by the column default"
        )
    if created:
        obj.created_by_id = user.id
    obj.updated_by_id = user.id


# ── The `tasks` writer: a flush listener, not a per-route call ─────────────────────────────────────

#: Where a request records its actor for the listener. On `Session.info` (per-session), not a module
#: global shared across concurrent requests.
ACTOR_SESSION_KEY = "actor_id"


def set_request_actor(session: Session, actor_id: Optional[UUID]) -> None:
    """Record who this session's writes belong to, for `stamp_dirty_task_actors`.

    Called once per authenticated request from `app/auth.py::_session_user`. A `None` actor is left
    absent, so the listener falls back to the system actor."""
    if actor_id is None:
        session.info.pop(ACTOR_SESSION_KEY, None)
        return
    session.info[ACTOR_SESSION_KEY] = actor_id


#: Set while a session acts AS a person without that person acting (a cron firing as its owner): rows keep
#: the owner's provenance (`updated_by_id`), but the Activity feed names no one (`transition_actor`).
MACHINE_ACT_SESSION_KEY = "machine_act"


def transition_actor(session: Session) -> Optional[UUID]:
    """The person a task transition written on this session is attributed to in the Activity feed, or None
    for a machine act: the system actor, an unattributed session, or a `MACHINE_ACT_SESSION_KEY` session."""
    if session.info.get(MACHINE_ACT_SESSION_KEY):
        return None
    actor_id = session.info.get(ACTOR_SESSION_KEY)
    return None if actor_id == SYSTEM_ACTOR_ID else actor_id


def _session_actor(session: Session) -> UUID:
    """Who this session's task writes belong to, with the system-actor fallback. The one home for that
    fallback, shared by the listener and `task_actor_values`."""
    return session.info.get(ACTOR_SESSION_KEY) or SYSTEM_ACTOR_ID


def task_actor_values(session: Session) -> dict[str, Any]:
    """The actor a Core `update(Task)` must carry: `.values(…, **task_actor_values(session))`.

    The flush listener cannot see a Core UPDATE (no ORM object is dirty) while `onupdate` still moves
    `updated_at`, which would pair a fresh clock with the previous writer. Same fallback as the
    listener. A repo test enforces every site uses it."""
    return {"updated_by_id": _session_actor(session)}


def stamp_dirty_task_actors(session: Session, _flush_context: Any, _instances: Any) -> None:
    """`before_flush` listener: attribute every Task this flush writes to the session's actor.

    A listener rather than per-route `stamp_actor` because `tasks` has no choke point — many routes,
    the agent API and scheduler jobs mutate it. No recorded actor stamps the system actor
    (coding-standards.md §Entity shape).

    `session.is_modified` skips unchanged objects so no UPDATE is manufactured. Covers ORM flushes only:
    a Core `update(Task)` must splat `task_actor_values(...)`; raw SQL escapes both."""
    actor_id = _session_actor(session)
    for obj in session.new:
        if isinstance(obj, Task):
            obj.updated_by_id = actor_id
    for obj in session.dirty:
        if isinstance(obj, Task) and session.is_modified(obj, include_collections=False):
            obj.updated_by_id = actor_id
