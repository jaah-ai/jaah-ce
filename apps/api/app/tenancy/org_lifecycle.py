"""Deleting an org: stop everything it runs, then purge every row it owns. Never refused.

Any org may be deleted — an enabled one, the caller's own, the one holding the last superadmin, the operator
plane itself. The delete does not wait for work to drain; it ends it (`wind_down`): the org is disabled, every
live task is failed and its runner killed, every hosting's machine is ended, every node is deleted (its slots
and their holders with it, a control-plane-launched instance terminated), then `purge_org_rows` empties every
tenant-owned table in one transaction. Rows OUTSIDE the org that still point at its rows are detached first
(`detach_outside_references`), so nothing elsewhere in the fleet refuses the purge.

`app/tenancy/org_delete.py`'s trigger is the floor: if the purge misses a table, the whole transaction is
refused with a 409 naming it, so the purge does not verify itself.

Purged: every table `app/tenancy/manifest.py` grades tenant-owned. Kept: `tenancy_exclusions`
(`Shared` — the trail outlives the org) and host directories (`app/tenancy/node_org.py`). Attachment S3
keys are read before the purge and reaped after commit; transcript objects, worktrees, shared-store
leaves and vendor installs are not this module's: tenant offboarding (`app/tenancy/offboard.py`) wraps it.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence
from uuid import UUID

from sqlalchemy import Table, delete, func, select, text, update
from sqlalchemy.orm import Session

from app import edition_features, task_lifecycle
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.base import Base
from app.models.identity import AdminUser, UserIdentity
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.task_extras import TaskAttachment
from app.models.tasks import Task
from app.models.worktrees import VirtualNode, VnodeHost
from app.tenancy.manifest import TENANT_OWNED

log = logging.getLogger(__name__)

#: SQLSTATEs a refused purge arrives as: `23001` from `org_delete.py`'s trigger (a table the purge missed), and
#: an FK from a row `detach_outside_references` could not detach (`23503` NO ACTION, `23001` RESTRICT). The
#: codes do not partition the cases: discriminate on `constraint_name`, which referential integrity sets and a
#: plpgsql `RAISE` does not. Read `diag` defensively (psycopg-specific).
ORG_DELETE_REFUSED_SQLSTATES = ("23001", "23503")

#: The reason every task the delete ends carries (`awaiting_response_body`), and every holder's kill.
ORG_DELETED_REASON = "its org was deleted"



async def wind_down(session: Session, org: Org, actor: AdminUser, *, keep_node_rows: bool = False) -> dict[str, Any]:
    """Stop everything this org runs, before its rows are purged. Returns what was ended, for the audit row.

    Each step commits as it goes: it is a wind-down, not the purge, and every step is safe to repeat (a second
    delete of a half-wound-down org picks up where this one stopped). Order matters:
      1. Disable — no new dispatch reaches the org while the rest runs.
      2. Fail every live task — `end_attempt(final=True)` lands `system_failed` and mints the runner's kill.
      3. End every hosting's machine (`hosting_provisioning.terminate`): its instance is in the operator's AWS
         account, which the node delete does not sign for.
      4. Delete every remaining node — co-located and carried nodes before the host carrying them (a host's
         nodes may be another org's when the org is the operator plane) — through the console's own node delete
         (`routes/nodes/delete.py::delete_node_row`, forced past its allocated/hosted refusals): holders ended, trees
         erased, a control-plane-launched instance terminated. A host's teardowns still pending go with it: the
         host that would finish them is the machine being removed. `keep_node_rows` is the soft delete's
         switch: the org's own nodes are torn down the same but their rows stay for Restore
         (`delete_node_row(keep_row=True)`); a node of another org carried on one of its hosts still goes.
    The database steps run off the event loop (`run_in_threadpool`); only the node delete is awaited here."""
    # Deferred: both modules import routes, and routes import this one.
    from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

    from app.routes.nodes.delete import delete_node_row  # noqa: PLC0415

    org_id = org.id
    failed, hostings = await run_in_threadpool(_stop_work, session, org)

    deleted: list[str] = []
    failures: list[str] = []
    hosts = await run_in_threadpool(_host_ids, session, org_id)
    own = set(await run_in_threadpool(_own_node_ids, session, org_id))
    for node_id, name in await run_in_threadpool(_nodes_in_delete_order, session, org_id):
        if node_id in hosts:
            await run_in_threadpool(_drop_pending_teardowns, session, node_id)
        keep_row = keep_node_rows and node_id in own
        out = await delete_node_row(
            session, node_id, actor, reason=f"{ORG_DELETED_REASON} — end the runner", force=True, keep_row=keep_row,
        )
        deleted.append(name)
        failures += [f"{name}: {failure}" for failure in out.node_erase_failures]
        if out.instance_detail and not out.instance_terminated:
            failures.append(f"{name}: {out.instance_detail}")
    if failures:
        log.warning("org %s delete: %s node step(s) left work behind: %s", org_id, len(failures), "; ".join(failures))
    return {
        "tasks_failed": failed,
        "hostings_ended": hostings,
        "nodes_deleted": deleted,
        "node_failures": failures,
    }


def _stop_work(session: Session, org: Org) -> tuple[list[int], list[str]]:
    """Steps 1-3 of `wind_down`: disable, fail every live task, end every hosting's machine."""
    from app.routes.tasks.runner_settle import end_attempt  # noqa: PLC0415

    org_id = org.id
    org.enabled = False
    session.commit()

    live = sorted(task_lifecycle._ORG_DELETE_BLOCKING_TASK_STATES)
    task_ids = session.scalars(
        select(Task.id).where(Task.org_id == org_id, Task.state.in_(live)).order_by(Task.id)
    ).all()
    failed = [
        task_id for task_id in task_ids
        if end_attempt(task_id, session, reason=ORG_DELETED_REASON, final=True, sources=tuple(live))
    ]

    ended: list[str] = []
    if edition_features.enabled("hostings"):  # The community edition has no hostings.
        from app import hosted_machines  # noqa: PLC0415

        ended = hosted_machines.end_org_hostings(session, org_id)
    session.commit()
    return failed, ended


def _host_ids(session: Session, org_id: UUID) -> set[UUID]:
    """The org's Multi-Node Hosts (a `vnode_hosts` marker on one of its nodes)."""
    return set(
        session.scalars(
            select(VnodeHost.node_id).join(Node, Node.id == VnodeHost.node_id).where(Node.org_id == org_id)
        ).all()
    )


def _drop_pending_teardowns(session: Session, host_id: UUID) -> None:
    """Remove a host's `virtual_nodes` rows before the host itself is deleted. By now each is a teardown the
    host was to finish (its node deleted just before), and the host is going: nothing would ever finish them,
    and `virtual_nodes.host_node_id` RESTRICTs the host's delete while they exist."""
    session.execute(delete(VirtualNode).where(VirtualNode.host_node_id == host_id))
    session.commit()


def _own_node_ids(session: Session, org_id: UUID) -> list[UUID]:
    return list(session.scalars(select(Node.id).where(Node.org_id == org_id)))


def _nodes_in_delete_order(session: Session, org_id: UUID) -> list[tuple[UUID, str]]:
    """The org's nodes, plus every node carried on one of its hosts (any org's), carried nodes first.

    A host is refused while it carries a node (`virtual_nodes.host_node_id` RESTRICT), so each host comes after
    everything on it."""
    hosts = _host_ids(session, org_id)
    carried = session.execute(
        select(Node.id, Node.name)
        .join(VirtualNode, VirtualNode.node_id == Node.id)
        .where(VirtualNode.host_node_id.in_(hosts))
    ).all() if hosts else []
    own = session.execute(select(Node.id, Node.name).where(Node.org_id == org_id).order_by(Node.name)).all()
    seen: set[UUID] = set()
    ordered: list[tuple[UUID, str]] = []
    # Carried nodes, then the org's non-host nodes, then its hosts.
    for node_id, name in [*carried, *(row for row in own if row.id not in hosts), *(row for row in own if row.id in hosts)]:
        if node_id not in seen:
            seen.add(node_id)
            ordered.append((node_id, name))
    return ordered


def _outside_references(session: Session, org_id: UUID) -> Iterable[tuple[Table, Any, list[Any], Any, bool]]:
    """Every single-column blocking FK from a row OUTSIDE this org into a row the purge deletes, as
    `(child table, its column, the WHERE picking the referencing rows, the value to point them at, detachable)`.

    A row of another org (or of an org-less table) may reference one of this org's rows — most often a user of
    this org as some row's `created_by_id`/`updated_by_id`. A nullable column is cleared; a NOT NULL reference to
    a user is handed to the system actor (whose rows they are once the author is gone); anything else cannot be
    detached. Single-column FKs only: the composite `(id, org_id)` keys (`app/tenancy/keys.py`) force a child
    into its parent's org, so their children are purged with it."""
    owned = {cls.__tablename__ for cls in TENANT_OWNED}
    system_actor_survives = session.scalar(
        select(AdminUser.org_id).where(AdminUser.id == SYSTEM_ACTOR_ID)
    ) != org_id
    # Unsorted on purpose: each UPDATE only repoints a reference, so order is irrelevant.
    for child in Base.metadata.tables.values():
        for constraint in child.foreign_key_constraints:
            if len(constraint.elements) != 1:
                continue
            element = constraint.elements[0]
            parent = element.column.table
            if parent.name not in owned or "org_id" not in parent.c:
                continue
            if (element.ondelete or "").upper() in _NON_BLOCKING_ON_DELETE:
                continue
            column = element.parent
            where = [column.in_(select(element.column).where(parent.c.org_id == org_id))]
            if "org_id" in child.c:
                where.append(child.c.org_id != org_id)
            if column.nullable:
                yield child, column, where, None, True
            elif parent.name == AdminUser.__tablename__ and system_actor_survives:
                yield child, column, where, SYSTEM_ACTOR_ID, True
            else:
                yield child, column, where, None, False


def undetachable_references(session: Session, org_id: UUID) -> dict[str, int]:
    """`{child.column: rows}` outside this org that point into it and that `detach_outside_references` cannot
    repoint — read BEFORE the wind-down, so a delete the purge would refuse stops before it destroys anything.
    In practice only the operator plane has any: other orgs' rows are authored by its system actor."""
    found: dict[str, int] = {}
    for child, column, where, _value, detachable in _outside_references(session, org_id):
        if detachable:
            continue
        total = session.scalar(select(func.count()).select_from(child).where(*where))
        if total:
            found[f"{child.name}.{column.name}"] = int(total)
    # A staging org names its parent (`orgs.parent_org_id`, `app/staging_org.py`); `orgs` is the tenancy axis, so the
    # sweep above never sees it. Purge the staging org first.
    staging = session.scalar(select(func.count()).select_from(Org).where(Org.parent_org_id == org_id))
    if staging:
        found["orgs.parent_org_id"] = int(staging)
    return found


def detach_outside_references(session: Session, org_id: UUID) -> dict[str, int]:
    """Point every row OUTSIDE this org away from the rows the purge is about to delete (`_outside_references`).
    Returns `{child.column: rows}`. No commit: it rides the purge's transaction."""
    detached: dict[str, int] = {}
    for child, column, where, value, detachable in _outside_references(session, org_id):
        if not detachable:
            continue
        result = session.execute(update(child).where(*where).values({column.name: value}))
        if result.rowcount:
            detached[f"{child.name}.{column.name}"] = int(result.rowcount)
    # `virtual_nodes.tenant_org_id` names the ORG itself; a teardown still pending on another org's host keeps it as a
    # plain id (its FK is gone, revision 202610130300) and is finished by that host as usual.
    return detached


#: `ON DELETE` actions that do not constrain the purge's order. Everything else blocks; an unspecified
#: action (`NO ACTION`) arrives as `""` and blocks by falling through this set.
_NON_BLOCKING_ON_DELETE = frozenset({"CASCADE", "SET NULL", "SET DEFAULT"})


def _delete_order(tables: Sequence[Table], owned: Iterable[str]) -> tuple[Table, ...]:
    """Topologically sort `tables` so a child is emptied before the parent it blockingly references.

    Pure, so `tests/test_org_delete_order.py` can prove the order on synthetic metadata (today's schema
    cannot tell a guaranteed order from SQLAlchemy's lucky one). Sorting on blocking edges only is what
    makes an order exist: e.g. `credentials.created_by_id -> admin_users` is RESTRICT while
    `admin_users.git_credential_id -> credentials` is SET NULL, and dropping non-blocking edges opens every
    such cycle. Ties break by table name so a failing purge is reproducible. Raises on a blocking cycle."""
    owned_names = set(owned)
    by_name = {table.name: table for table in tables if table.name in owned_names}
    # `blocks[parent]` = children to empty first. References outside `owned` order nothing; they yield a
    # legitimate refusal instead (`ORG_DELETE_REFUSED_SQLSTATES`).
    blocks: dict[str, set[str]] = {name: set() for name in by_name}
    for name, table in by_name.items():
        for key in table.foreign_keys:
            target = key.column.table.name
            if target == name or target not in by_name:
                continue
            if (key.ondelete or "").upper() in _NON_BLOCKING_ON_DELETE:
                continue
            blocks[target].add(name)

    ordered: list[Table] = []
    placed: set[str] = set()
    remaining = sorted(by_name)
    while remaining:
        ready = [name for name in remaining if blocks[name] <= placed]
        if not ready:
            # A blocking cycle: fail loudly rather than emit a partial order that would orphan rows.
            raise RuntimeError(
                "the tables' blocking foreign keys form a cycle, so no front-to-back delete order "
                f"exists: {sorted(set(remaining))}"
            )
        for name in ready:
            ordered.append(by_name[name])
            placed.add(name)
        remaining = [name for name in remaining if name not in placed]
    return tuple(ordered)


def org_owned_tables() -> tuple[Table, ...]:
    """Every tenant-owned table, in an order safe to DELETE from front to back.

    The set is `app/tenancy/manifest.py::TENANT_OWNED`; the order is derived by `_delete_order`. A new
    census table is purged automatically; a table outside the census is caught by `org_delete.py`'s
    catalogue scan. `orgs` itself (`Axis`) is deleted by the caller, last."""
    return _delete_order(
        Base.metadata.sorted_tables, (cls.__tablename__ for cls in TENANT_OWNED)
    )


def org_owned_attachment_keys(session: Session, org_id: UUID) -> list[tuple[Optional[str], str]]:
    """Every distinct `(bucket, key)` this org's attachment rows name — read before the purge, or never.

    The CP holds no `s3:ListBucket`, so an object whose row is gone is unreclaimable residue
    (`app/models/task_extras.py::TaskAttachment`). Distinct because rows can share a key (an epic's report copy);
    `app/attachment_storage.py::reap_unreferenced_object` deletes only once no surviving row names it."""
    return [
        (bucket, key)
        for bucket, key in session.execute(
            select(TaskAttachment.s3_bucket, TaskAttachment.s3_key)
            .where(TaskAttachment.org_id == org_id, TaskAttachment.s3_key.is_not(None))
            .distinct()
        )
    ]


def purge_org_rows(session: Session, org_id: UUID) -> dict[str, int]:
    """Delete every row this org owns, children first. Returns `{table: rows deleted}`, omitting zeroes.

    Core `delete()`: one statement per table, invisible to the read/write seams. Self-references within one
    statement are fine (FKs are checked at statement end). The return is the forensic record staged on the
    audit row by `routes/orgs.py`, since the tenant's own history goes with it. No commit: the caller owns
    the transaction so the purge and the `orgs` row leave together."""
    deleted: dict[str, int] = {}
    # The ONE door through `token_ledger_entries`' and `gov_ledger`'s immutability triggers (revisions 0336,
    # 0378): erasing the org erases its ledgers. Transaction-local, and it names this org only.
    session.execute(
        text("SELECT set_config('token_billing.purging_org', :org, true)"), {"org": str(org_id)}
    )
    # The people whose memberships go with the org; any left with no membership anywhere go too below.
    members = AdminUser.__table__
    people = [
        row[0] for row in session.execute(
            select(members.c.identity_id).where(members.c.org_id == org_id, members.c.identity_id.is_not(None))
        )
    ]
    for table in org_owned_tables():
        result = session.execute(delete(table).where(table.c.org_id == org_id))
        if result.rowcount:
            deleted[table.name] = int(result.rowcount)
    if people:
        # A person whose only memberships were this org's is this org's to erase; one still a member
        # elsewhere stays (`app/models/identity.py::UserIdentity`).
        identities = UserIdentity.__table__
        result = session.execute(
            delete(identities).where(
                identities.c.id.in_(people),
                ~select(members.c.id).where(members.c.identity_id == identities.c.id).exists(),
            )
        )
        if result.rowcount:
            deleted[identities.name] = int(result.rowcount)
    return deleted


def org_owned_row_counts(session: Session, org_id: UUID) -> dict[str, int]:
    """`{table: rows}` for every tenant-owned table still holding this org's id — zeroes omitted.

    The tests' oracle, kept beside the purge so both read the same census. No route serves it."""
    counts: dict[str, int] = {}
    for table in org_owned_tables():
        total = session.scalar(
            select(func.count()).select_from(table).where(table.c.org_id == org_id)
        )
        if total:
            counts[table.name] = int(total)
    return counts


def purge_org(session: Session, org: Org) -> dict[str, int]:
    """Delete an org's rows and the org row itself, for a caller that is not the route (test fixtures).

    Skips everything the route adds: no wind-down, no `FOR UPDATE`, no audit row, no object reap, no console
    event. No commit: the caller owns the transaction."""
    deleted = purge_org_rows(session, org.id)
    session.execute(delete(Org.__table__).where(Org.__table__.c.id == org.id))
    return deleted
