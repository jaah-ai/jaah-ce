"""Node roster reads: `GET /api/nodes`, `/api/nodes/page`, `/api/nodes/{node_id}`, and their presence/count lookups."""
from __future__ import annotations

from datetime import timedelta
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app import account_fold, activity_rank, jaah_discovery, node_host_metrics, node_liveness, node_allocation
from app.actors import actor_names_for
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.object_access import can_access
from app.authz.principal import Principal, current_principal
from app.authz.visibility import ObjectAccess, scoped_node_select, sees_all_nodes
from app.db import get_session
from app.entity_shape import count_by
from app.models.credentials import Account, AccountObservation
from app.models.identity import AdminUser
from app.models.node_groups import NodeGroupCapacity
from app.models.nodes import Node
from app.models.orgs import in_live_org
from app.models.projects import Runner
from app.models.worktrees import Worktree
from app.node_identity import is_uuid_shaped
from app.tenancy import seam
from app.list_page import Page, PageQuery, list_page, page_query
from app.tenancy.ids import is_tenant_org
from app.tenancy.labels import org_refs
from app.routes.nodes.node_wire import (
    NodeCounts,
    NodeDetailOut,
    NodeOut,
    _accounts_nodelink_nodes,
    _hosting_facts,
    _microvm_editable,
    _narrow_for_tenant,
    _node_out,
    _reported_generation,
    _session_kvm_editable,
    _status_and_last_seen,
)
from app.routes.nodes.intents import _INTENT_FILTER_MAX_LEN

router = APIRouter(tags=["nodes"])


def _counts_map(session: Session, node_ids: list[UUID]) -> dict[UUID, NodeCounts]:
    """Rollup counts per node via five grouped queries (no N+1). worktrees/task_slots/runners are per-node row counts;
    `accounts` is the enabled accounts this node still attests, inside the dispatch gate's window.

    It counts logins, not work: counting running accounts would read `0` for an idle logged-in node,
    indistinguishable from a lost login. The source is `account_observations` (the only per-node answer;
    `accounts.node_id` is a repointable pin), pruned each pass, joined to the roster for `enabled`; an
    unrostered slug is dropped. The window is `account_fold.RECENT_REPORT_SECONDS`, imported so the count
    never promises what the gate refuses.

    It tests three of the gate's eight exclusions (`jaah_scheduler.EXCLUSION_REASONS`): `enabled`,
    `account_unobserved`, `node_reports_no_credentials`, the ones about a usable login here. It is not a
    dispatchability oracle. API-key accounts materialize nothing, so they count 0 everywhere. An offline
    node counts 0 (the gate would refuse too).

    Known narrowing: the seam scopes both entities by org, and `account_observations.account_id` has no
    same-org key, so a cross-org observation reads 0 (fail-closed; fixing needs a schema change).

    Keyed by the row's uuid in and out; a node with no rows counts zero."""
    if not node_ids:
        return {}
    wanted = list(node_ids)
    wt_counts = count_by(session, Worktree.node_id, wanted)
    retiring_counts = count_by(session, Worktree.node_id, wanted, where=(Worktree.retiring_at.isnot(None),))
    task_slot_counts = count_by(
        session, Worktree.node_id, wanted, where=(Worktree.number >= 1, Worktree.retiring_at.is_(None))
    )
    runner_counts = count_by(session, Runner.node_id, wanted)
    account_counts = dict(
        session.execute(
            # `count(distinct ...)` equals `count(*)` here (`uq_account_observations_node_slug`); it names what is
            # counted. The inner join is also the `account_id IS NOT NULL` guard.
            select(AccountObservation.node_id, func.count(distinct(AccountObservation.account_id)))
            .join(Account, Account.id == AccountObservation.account_id)
            .where(
                AccountObservation.node_id.in_(wanted),
                Account.enabled.is_(True),
                # Compared in Postgres against the CP-stamped `attested_at`, never a Python clock
                # (`models.AccountObservation.attested_at`).
                AccountObservation.attested_at
                > func.now() - timedelta(seconds=account_fold.RECENT_REPORT_SECONDS),
                # The node's own word that it holds no usable credential for this slug (an exclusion in
                # `jaah_scheduler.EXCLUSION_REASONS`); without it the count would claim a login the host denies (a
                # `false_healthy` cell: reported usable, no credential on disk). `is_distinct_from`, never `!=`:
                # a NULL status must pass (`account_fold.NODE_STATUS_NO_CREDENTIALS`).
                AccountObservation.extra["status"].astext.is_distinct_from(
                    account_fold.NODE_STATUS_NO_CREDENTIALS
                ),
            )
            .group_by(AccountObservation.node_id)
        ).all()
    )
    return {
        node_id: NodeCounts(
            worktrees=wt_counts.get(node_id, 0),
            task_slots=task_slot_counts.get(node_id, 0),
            retiring_slots=retiring_counts.get(node_id, 0),
            runners=runner_counts.get(node_id, 0),
            accounts=account_counts.get(node_id, 0),
        )
        for node_id in node_ids
    }


def _published_release(session: Optional[Session], rows: list[Optional[Node]]) -> Optional[str]:
    """The fleet's desired release, read once per request (never once per node), or None.

    What `GET /api/agent/release-desired` serves (the version this CP runs, which node updaters install),
    read through `app.routes.agent.releases.cp_release_desired` as `app/node_liveness_job.py` does, so page and
    alarm agree. Not the inert `release_desired` table, which would misplace nodes.

    Skipped when no row in this request reports a generation (as `node_liveness.any_generation_reported`),
    before any DB access, so `session` is Optional. Scoped to the request's row set: the list may carry
    `release_desired` where a node's own detail read has `null`. Read it as "what this payload compared
    against" (`test_the_guard_is_scoped_to_the_requests_row_set`). `create_node` always short-circuits
    but keeps the call for uniformity."""
    if not any(_reported_generation(row) for row in rows):
        return None
    from app.routes.agent import releases as agent_releases  # deferred: avoids a routes<->routes import cycle

    desired = agent_releases.cp_release_desired()
    return getattr(desired, "version", None) or None


@router.get("/api/nodes", response_model=list[NodeOut])
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def list_nodes(
    # `activity`: most tasks run there in the last 30 days first, then the roster's order (`app/activity_rank.py`).
    order: Literal["name", "activity"] = "name",
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[NodeOut]:
    """The node roster, node-grant scoped (`authz.scoped_node_select`).

    `nodes:view` is still required: the key says whether, the grant says which. Deny by default (D2): no
    node grants means an empty list, which `pages/Nodes.tsx` renders as a grant-shaped empty state.

    The presence union is superadmin-only: an unregistered host has no uuid, so no grant can name it
    (`authz.visible_node_clause`), and a synthetic identity would be a second node identity. Registering
    the node makes it grantable and visible. An operator's org filter (`X-Jaah-Org-Scope`) drops the union
    too: a presence-only host belongs to no org, and a presence key can name a filtered-out org's node."""
    rows, presence, node_ids = _roster(session, principal)
    if order == "activity":
        counts = activity_rank.node_task_counts(session, (row.id for row in rows.values()))
        # Stable: equal counts keep the roster's order. A presence-only host has no row, so no count.
        node_ids.sort(key=lambda key: -(counts.get(rows[key].id, 0) if key in rows else 0))
    return _node_outs(session, user, principal, node_ids, rows, presence)


def _roster(
    session: Session, principal: Principal | None
) -> tuple[dict[str, Node], dict[str, dict[str, Optional[str]]], list[str]]:
    """The roster `list_nodes` and `list_nodes_page` share: the grant-scoped rows keyed by uuid, the presence
    snapshot, and the ordered entry keys (a row's uuid, or a presence-only host's presence key). The
    presence union's rule is `list_nodes`'s docstring."""
    presence = jaah_discovery.presence_map(session)
    # Keyed by uuid, as `jaah_discovery.presence_map` is: a name is unique per org only.
    # Never a soft-deleted org's node: its row is kept for Restore (`in_live_org`).
    rows = {
        str(row.id): row
        for row in session.scalars(scoped_node_select(principal).where(in_live_org(Node.org_id))).all()
    }
    # The union is conditional (see `list_nodes`): a scoped or org-filtered caller sees exactly the rows.
    with_presence = sees_all_nodes(principal) and seam.request_org_filter() is None
    node_ids = sorted(
        set(rows) | set(presence) if with_presence else set(rows),
        # Listed by name, as before; the key breaks a tie between two orgs' same-named nodes.
        key=lambda key: _roster_sort_key(key, rows),
    )
    return rows, presence, node_ids


def _roster_sort_key(key: str, rows: dict[str, Node]) -> tuple[str, str]:
    """`(name, key)`: the roster's order, and `GET /api/nodes/page`'s default."""
    return (rows[key].name if key in rows else key, key)


def _node_outs(
    session: Session,
    user: AdminUser,
    principal: Principal | None,
    node_ids: list[str],
    rows: dict[str, Node],
    presence: dict[str, dict[str, Optional[str]]],
) -> list[NodeOut]:
    """The wire rows for `node_ids`, in that order, tenant-narrowed. Every bulk read (`_node_out`'s
    pre-resolved maps) covers THE IDS PASSED and no more, so a page pays only for its rows."""
    listed = [rows[node_id] for node_id in node_ids if node_id in rows]
    counts = _counts_map(session, [row.id for row in listed])
    empty = NodeCounts(worktrees=0, runners=0, accounts=0)
    # One read for the whole list.
    published = _published_release(session, [rows.get(node_id) for node_id in node_ids])
    # One actor resolution for the whole page (see `_node_out`).
    actor_names = actor_names_for(session, listed)
    # One statement for the whole page (see `_node_out`): `DISTINCT ON (node_id)` walks
    # `uq_node_host_metrics_node_bucket` backwards, one index seek per node
    # (`app/node_host_metrics.py::latest_map`). Registered rows only; a presence-only entry has no uuid.
    host_metrics = node_host_metrics.latest_map(session, [row.id for row in listed])
    # One statement each for the whole page (see `_node_out`).
    hosting = _hosting_facts(session, [row.id for row in listed])
    orgs = org_refs(session, [row.org_id for row in listed])
    jaah_cloud = node_allocation.jaah_cloud(row.id for row in listed)
    placement = node_allocation.placement(row.id for row in listed)
    accounts_nodelink = _accounts_nodelink_nodes(session, listed)
    return [
        _narrow_for_tenant(
            _node_out(
                session,
                node_id,
                rows.get(node_id),
                presence,
                counts.get(rows[node_id].id, empty) if node_id in rows else empty,
                detail=False,
                published=published,
                actor_names=actor_names,
                host_metrics=host_metrics,
                hosting=hosting,
                orgs=orgs,
                jaah_cloud=jaah_cloud,
                placement=placement,
                accounts_nodelink=accounts_nodelink,
            ),
            user,
            principal,
        )
        for node_id in node_ids
    ]


def _node_row(session: Session, node_id: str) -> Optional[Node]:
    """The row a `{node_id}` path segment addresses: by uuid only, never by name (a name is unique per org
    only, so it cannot address a row). A non-uuid addresses no row. A seam-narrowed select, not
    `session.get`, whose identity-map hit would skip the tenant narrowing."""
    if not is_uuid_shaped(node_id):
        return None
    return session.scalar(select(Node).where(Node.id == UUID(node_id), in_live_org(Node.org_id)))


def _readable_node_or_404(
    session: Session, principal: Principal | None, node_id: str
) -> tuple[dict[str, dict[str, Optional[str]]], Optional[Node]]:
    """The presence snapshot plus this node's row, or a 404: the one door every per-node read goes through
    (`get_node`, `get_node_health`, and any future per-node read), so no route answers for a node another
    hides (D13).

    `node_id` is the row's uuid (`_node_row`), or a presence-only host's presence key; then probed by
    `can_access`, since the 404 must match the unknown answer exactly.

    All three refusals (unknown id, no grant, presence-only for a non-superadmin) are identical, or diffing
    bodies would recover an existence oracle."""
    presence = jaah_discovery.presence_map(session)
    row = _node_row(session, node_id)
    unknown = HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    if row is None and node_id not in presence:
        raise unknown
    if row is not None and not can_access(
        session, principal, "node", row.id, mode=ObjectAccess.READ
    ):
        # `can_access`, not `require_object_access`: the latter's generic `"not found"` body differs from the
        # unknown-node body above, which is the oracle this denies.
        raise unknown
    if row is None and not sees_all_nodes(principal):
        # Presence-only is superadmin-only (see `list_nodes`).
        raise unknown
    return presence, row


# ── The roster, searched, filtered, sorted and paged (`GET /api/nodes/page`) ──────────────────────
# Declared above `GET /api/nodes/{node_id}` for the same reason as `intents`: `page` would otherwise be
# read as a node id. Numbered pages (`app/list_page.py`), by the operator's choice: one paging style for
# every roster, Hosts and Users alike, over a keyset's guarantee against a row skipped or repeated while
# the roster moves between two reads.
# The vocabularies `?status=` and `?enabled=` take. Status is `jaah_discovery.status_and_last_seen`'s
# tri-state; the enabled triple reads the `enabled`/`draining` pair as `NodeOut.draining` documents it.
_ROSTER_STATUSES = ("online", "offline", "unknown")
_ROSTER_ENABLED_STATES = ("enabled", "draining", "disabled")
# `?nodevault=`: whether the node holds NodeVault keys now (`Node.nodevault_capable`). `not_capable` takes every
# node that does not, a never-reported or presence-only one included — none of them is granted a key.
_ROSTER_NODEVAULT_STATES = ("capable", "not_capable")
# A needle shorter than this never matches a fingerprint prefix: `nodevault_identity_fp` is hex, so a
# short needle such as `ab` would land on most of the fleet and drown the name matches.
_FINGERPRINT_PREFIX_MIN = 4
# `?sort=`'s order for the presence tri-state: reachable first, never-seen last.
_ROSTER_STATUS_RANK = {"online": 0, "offline": 1, "unknown": 2}
_ROSTER_SORTS = ("name", "status", "seen", "version")


def _roster_entry_matches(
    node_id: str,
    row: Optional[Node],
    presence: dict[str, dict[str, Optional[str]]],
    *,
    needle: Optional[str],
    status: Optional[str],
    enabled: Optional[str],
    capacity_ids: Optional[set[UUID]],
    host_ids: set[UUID],
    nodevault: Optional[str] = None,
) -> bool:
    """Whether one roster entry survives every filter of `list_nodes_page`. A presence-only entry (no row)
    reads as `NodeOut`'s defaults: enabled, not draining, in no pool. A Multi-Node Host's own record
    (`host_ids`, from `vnodes.hosting_facts`) is never a node row — it is the Hosts tab's — so it is left out
    BEFORE the page and the total are cut, or a page could fill with rows the Nodes tab never draws."""
    if row is not None and row.id in host_ids:
        return False
    if needle is not None:
        # A presence-only entry has only its key to be found by; a row's uuid matches by PREFIX, at the same
        # floor as the fingerprint (a two-character hex needle would otherwise land on most of the fleet).
        haystack = [row.name, row.description, *(row.tags or [])] if row is not None else [node_id]
        prefixed = [row.nodevault_identity_fp, node_id] if row is not None else []
        by_prefix = len(needle) >= _FINGERPRINT_PREFIX_MIN and any(
            value is not None and value.lower().startswith(needle) for value in prefixed
        )
        if not by_prefix and not any(needle in value.lower() for value in haystack if value):
            return False
    if status is not None and _status_and_last_seen(node_id, row, presence)[0] != status:
        return False
    if enabled is not None:
        is_enabled = row.enabled if row is not None else True
        is_draining = bool(row.draining) if row is not None else False
        state = "disabled" if not is_enabled else "draining" if is_draining else "enabled"
        if state != enabled:
            return False
    if capacity_ids is not None and (row is None or row.node_group_capacity_id not in capacity_ids):
        return False
    if nodevault is not None and (row is not None and bool(row.nodevault_capable)) != (nodevault == "capable"):
        return False
    return True


@router.get("/api/nodes/page", response_model=Page[NodeOut])
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def list_nodes_page(
    q: Optional[str] = Query(default=None, max_length=_INTENT_FILTER_MAX_LEN),
    status: Optional[str] = Query(default=None, max_length=_INTENT_FILTER_MAX_LEN),
    enabled: Optional[str] = Query(default=None, max_length=_INTENT_FILTER_MAX_LEN),
    # A node group's uuid (`routes/node_groups.py`), never its name.
    group: Optional[str] = Query(default=None, max_length=_INTENT_FILTER_MAX_LEN),
    nodevault: Optional[str] = Query(default=None, max_length=_INTENT_FILTER_MAX_LEN),
    paging: PageQuery = Depends(page_query(_ROSTER_SORTS)),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Page[NodeOut]:
    """One page of the node roster: the Nodes page's read once a fleet runs to thousands of nodes, where
    `list_nodes`'s full serialization is the cost. `list_nodes` stays as it is: many consumers read the
    whole list to resolve a name, and its shape is pinned.

    The same roster as `list_nodes` (`_roster`: grant scope, presence union, name-then-key order), then
    narrowed in Python, not SQL: `status` is presence-derived (`_status_and_last_seen`), so no column
    holds it, and the other filters ride along on the same walk rather than splitting the predicate across
    two homes. Only the page's entries are serialized (`_node_outs`), so the bulk reads are per page.

    `q`: a case-insensitive substring of the name, description, any tag or a presence-only host's key; a
    needle of `_FINGERPRINT_PREFIX_MIN`+ characters also matches a prefix of the uuid or of
    `nodevault_identity_fp`, the fingerprint the list no longer shows in full. `status`/`enabled` are
    vocabularies (422 otherwise). A Multi-Node Host's own record is left out (see `_roster_entry_matches`).
    `nodevault`: `capable` or `not_capable` (`_ROSTER_NODEVAULT_STATES`), so a NodeVault sweep of the fleet is
    one screen. `group`: a node group's uuid, matching the nodes on any of its capacity lines
    (`Node.node_group_capacity_id`); a non-uuid matches nothing, as `list_intents`'s `?node=`.

    `sort`: `name`, `status` (online, offline, unknown), `seen` (the last-seen instant) or `version` (the
    reported `generation`, compared as a release number); a node without the value (never seen, unreported
    or unreadable version) sits last either way. Without `sort`, `list_nodes`'s name-then-key order, which
    also breaks every tie. `total` is the filtered set's size."""
    if status is not None and status not in _ROSTER_STATUSES:
        raise HTTPException(status_code=422, detail=f"unknown status '{status}'")
    if enabled is not None and enabled not in _ROSTER_ENABLED_STATES:
        raise HTTPException(status_code=422, detail=f"unknown enabled '{enabled}'")
    if nodevault is not None and nodevault not in _ROSTER_NODEVAULT_STATES:
        raise HTTPException(status_code=422, detail=f"unknown nodevault '{nodevault}'")
    # Trimmed; blank is no filter.
    needle = (q or "").strip().lower() or None
    capacity_ids: Optional[set[UUID]] = None
    if group is not None:
        # One query for the group's lines; a non-uuid is the empty set, so it matches nothing (not a 404:
        # a filter addresses nothing, and no existence oracle).
        capacity_ids = (
            set(session.scalars(
                select(NodeGroupCapacity.id).where(NodeGroupCapacity.group_id == UUID(group))
            ).all())
            if is_uuid_shaped(group) else set()
        )

    rows, presence, node_ids = _roster(session, principal)
    # One statement for the whole roster: which rows are hosts (`_node_outs` reads the page's hosting again,
    # for the wire; this read decides membership).
    host_ids = {
        node_id for node_id, fact in _hosting_facts(session, [row.id for row in rows.values()]).items()
        if fact.is_host
    }
    matched = [
        node_id for node_id in node_ids
        if _roster_entry_matches(
            node_id, rows.get(node_id), presence,
            needle=needle, status=status, enabled=enabled, capacity_ids=capacity_ids, host_ids=host_ids,
            nodevault=nodevault,
        )
    ]
    # A tenant is served no reported version (`_narrow_for_tenant`), so it may not order by one either: the order
    # would answer what the blank withholds. Refused as any sort this reader is not offered.
    if paging.sort == "version" and is_tenant_org(user.org_id):
        raise HTTPException(status_code=422, detail="unknown sort 'version'")
    # Sorted and cut over keys, so only the page's entries are serialized.
    def seen(node_id: str) -> tuple[str, Optional[str]]:
        return _status_and_last_seen(node_id, rows.get(node_id), presence)

    page_ids, total = list_page(matched, paging, sorts={
        "name": lambda node_id: _roster_sort_key(node_id, rows),
        "status": lambda node_id: _ROSTER_STATUS_RANK[seen(node_id)[0]],
        # ISO-8601 UTC, so lexicographic order is chronological (as `jaah_discovery._is_fresher`).
        "seen": lambda node_id: seen(node_id)[1],
        "version": lambda node_id: node_liveness.parse_version(_reported_generation(rows.get(node_id))),
    })
    return Page[NodeOut](
        items=_node_outs(session, user, principal, page_ids, rows, presence),
        total=total,
        page=paging.page,
        per=paging.per,
    )


@router.get("/api/nodes/{node_id}", response_model=NodeDetailOut)
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def get_node(
    node_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> NodeDetailOut:
    """One node's detail, node-grant scoped, so a node filtered out of `list_nodes` is not readable by URL
    (D13). Addressed by `NodeOut.id`, the row's uuid (see `_readable_node_or_404`)."""
    presence, row = _readable_node_or_404(session, principal, node_id)
    empty = NodeCounts(worktrees=0, runners=0, accounts=0)
    counts = _counts_map(session, [row.id]).get(row.id, empty) if row is not None else empty
    out = _node_out(
        session,
        node_id,
        row,
        presence,
        counts,
        detail=True,
        published=_published_release(session, [row]),
    )
    if row is not None and isinstance(out, NodeDetailOut):
        out.microvm_editable = _microvm_editable(session, row, user, principal)
        out.session_kvm_editable = _session_kvm_editable(session, row, user, principal)
    return _narrow_for_tenant(out, user, principal)
