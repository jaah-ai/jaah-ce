"""Portfolio read endpoints — all data served from DB (no live host I/O).

No cache, mirror or read-source flag in front of fleet-state reads: plain SQL against the one store.
Browser liveness comes from `LISTEN cockpit_events` (`app/events.py`)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import Select, Text, func, or_, select
from sqlalchemy.orm import Session

from app import account_fold, account_sharing, activity_rank, fleet_verdicts, merge_lane_state, project_repos, slot_allocation, slot_stack, slot_state, slot_title
# Aliased: `_wt_out` and its callers hold a LOCAL `actor_names` map.
from app.actors import actor_names as resolve_actor_names
from app.auth import require_user, require_user_or_agent
from app.command_read import command_read, withhold_host_paths
from app.authz.catalog import PROJECTS_VIEW_KEY, permission_key
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import (
    AllowedObjects,
    granted_account_slugs,
    node_reach_clause,
    readable_account_select,
    readable_account_slugs,
    scoped_node_select,
    scoped_project_select,
    sees_all_nodes,
    visible_node_ref_clause,
    visible_worktree_clause,
    worktree_editable_clause,
)
from app.db import get_session
from app.entity_shape import get_or_404
from app.models.audit import CommandAudit
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.orgs import in_live_org
from app.models.projects import Project, Runner
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.routes.projects import ProjectOut as ProjectsRouteProjectOut
from app.routes.projects import _project_out, live_cast_holders, microvm_page
from app.node_clone import account_slug_expr, account_slug_from_dir, resolve_auth_dir
from app.node_identity import names_by_id, node_ref
# The aware-or-422 date-range contract is single-homed in `routes/audit.py` (a zoneless wall clock read
# as UTC misplaces the boundary). Nothing under `app/routes/` imports this module, so no cycle.
# TODO: lift `_require_aware` to a neutral module as a public helper; two routes now depend on it.
from app.routes.audit import _require_aware
from app.tenancy import seam
from app.tenancy.copy import WITHHELD_ACTOR
from app.tenancy.ids import is_tenant_org
from app.tenancy.labels import OrgRef, org_ref_of, org_refs

router = APIRouter(tags=["portfolio"])

# Shared bound for list endpoints with no DB-enforced ceiling.
_LIST_BOUND_DEFAULT = 500
_LIST_BOUND_MAX = 2000


# ── Worktrees ────────────────────────────────────────────────────────────────

class WorktreeOut(BaseModel):
    # One pool slot as the console renders it: identity, occupancy, sensed stack state, provenance.
    id: UUID
    # The node's DISPLAY name; `node_uuid` is its identity, the `?node_id=` the console sends back.
    node_id: str
    node_uuid: str
    # `project` is the DISPLAY name (`_WT_PROJECT_JOIN`); `project_id` is the identity to join on.
    project: str
    project_id: int
    # The slot's org — its project's, held equal by the composite key (`models.OrgScoped`). Project names
    # repeat across orgs, so the console tells two same-named projects apart by it.
    org: Optional[OrgRef] = None
    number: int
    path: str
    branch: Optional[str]
    # The only occupancy fact: `free` | `task` | `user`, paired with exactly one owner column by the
    # ownership CHECK.
    state: str
    task_id: Optional[int]
    # Non-NULL exactly when `state == "user"`. `owner_user_email` is the one display form (`/api/users` is
    # superadmin-only). Never render the uuid.
    owner_user_id: Optional[UUID]
    owner_user_email: Optional[str]
    stack_state: Optional[str]
    ports: Optional[dict[str, Any]]
    urls: Optional[dict[str, Any]]
    last_active: Optional[datetime]
    idle_hours: Optional[float]
    ahead: Optional[int]
    behind: Optional[int]
    dirty: Optional[bool]
    session_status: Optional[str]
    session_started_at: Optional[datetime]
    model: Optional[str]
    account: Optional[str]
    # The account's display label (`accounts.name`), for the task status hover. A tenant reads only its own
    # org's account names: an operator account's label is not theirs to see.
    account_name: Optional[str]
    window_name: Optional[str]
    # Resolved, never null or `""` (`app/slot_title.py::resolved_title`). `window_name` stays on the wire
    # for older nodes.
    title: str
    # The RAW column an operator's rename wrote; an edit form needs it, a reader needs `title`.
    name: str
    description: str
    # Decided off the `tasks` LEFT JOIN; a dangling `task_id` (no FK on purpose) reads False.
    task_active: bool
    # `True` iff the holding task awaits a human answer (`slot_title.task_awaiting_answer_from_state`), so a
    # parked slot does not read as a dead runner. Dangling `task_id` reads `False`.
    task_awaiting_answer: bool
    # The entity provenance quartet.
    created_at: datetime
    updated_at: datetime
    # Resolved through the id (`app/actors.py::actor_names`). `str`: both columns are NOT NULL.
    created_by: str
    updated_by: str
    # `number == 0` is the main clone; keyed on `number`, not occupancy.
    protected: bool
    # When this CP last sensed the slot present; `None` for declared-but-unbuilt. The one
    # not-built vs not-probed signal.
    observed_at: Optional[datetime]
    # `Worktree.state_since`: the occupancy transition stamp, not `updated_at`.
    holder_since: Optional[datetime]
    # The stale reaper's only input; `None` until the first sighting.
    last_seen_live: Optional[datetime]
    # The slot-scope half of "may I edit", computed from `authz.worktree_editable_clause` in the same query.
    # Key-blind: the client ANDs it with its target route's key (`apps/web/src/hooks/useWorktreeAccess.ts`).
    # No default: either value would be quietly wrong for a caller that forgot the column.
    may_edit: bool
    # The assignment model's three facts: `static` held at the count or `dynamic` (destroyed once
    # freed); `retiring` once the CP has ordered its destroy (`retiring_at`); `destroy_error` the node's refusal
    # (redacted for a tenant reader, `_wt_out`).
    kind: str
    retiring: bool
    # When the destroy was ordered (`retiring_at`), so a reader sees how long a removal has run.
    retiring_since: Optional[datetime] = None
    destroy_error: Optional[str]
    # Why the node has not destroyed a retiring slot, redacted (`slot_allocation.destroy_node_error`): the refusal,
    # else the newest destroy order's failed/refused/parked reason. `None` when the node has said nothing.
    destroy_node_error: Optional[str] = None
    # Git worktree (true) or empty folder slot (false: a no-repo project) — the slot's own kind, not its
    # project's. A folder row's `branch`/`ahead`/`behind`/`dirty` are always null: there is no git to read.
    has_repo: bool
    # The node-sensed slot manifest (`fleet_observe.ObservedSlotRepos`): the slot's repos, kept and failed. `None`
    # for a single-repo slot. A tenant gets every `path` blanked, as the slot's own.
    repos: Optional[dict[str, Any]] = None


# Rendered for an id the map lacks: a word rather than a blank or a raw uuid.
_UNKNOWN_ACTOR = "unknown"

# Every argument required: they are SELECT columns, a once-per-page actor map, and an authorization
# answer, and a default would let a caller drop one and get a quiet answer that looks like data.
# The idle-hours sentinel maps to `None` here (a display shape), unlike `/api/agent/worktrees`.
def _wt_out(
    wt: Worktree,
    project_name: str,
    idle_hours: int | None,
    task_state: str | None,
    task_title: str | None,
    actor_names: dict[UUID, str],
    *,
    node_name: str,
    # The node's self-reported tailnet DNS name (its config snapshot); None when it has not reported one.
    node_tailnet_name: str | None,
    may_edit: bool,
    # `account` is a path derived from these two; dropping them would serve `null` for every slot.
    jaah_home: str | None,
    account_slug: str | None,
    account_name: str | None,
    account_org_id: UUID | None,
    # Threaded for `actor_names`' reason; `null` already means "no owner".
    owner_user_email: str | None,
    # `slot_allocation.destroy_node_errors`' entry for this slot, read once per page; None when the node said nothing.
    destroy_node_error: str | None,
    # The reader's org, so a tenant is not served the operator's host layout: `path` lives under the node's
    # `jaah_home`, which `routes/nodes/node_wire.py::_narrow_for_tenant` blanks. Blanked to `""`, never dropped (the
    # wire shape is pinned). No default, so no caller can forget it.
    reader_org: Optional[UUID],
    org: Optional[OrgRef],
) -> WorktreeOut:
    task_active = slot_title.task_active_from_state(task_state)
    return WorktreeOut(
        id=wt.id,
        # The node name for display and the host `urls_for` builds slot URLs on; the uuid is the identity.
        node_id=node_name,
        node_uuid=str(wt.node_id),
        project=project_name,
        project_id=wt.project_id,
        org=org,
        number=wt.number,
        path="" if is_tenant_org(reader_org) else wt.path,
        branch=wt.branch if wt.has_repo else None,
        state=wt.state,
        task_id=wt.task_id,
        owner_user_id=wt.owner_user_id,
        owner_user_email=owner_user_email,
        # Derived from the sensed columns (`app/slot_stack.py`); the stored columns have no writer.
        # `observed_at` bounds freshness. URLs use the node name since the browser is remote.
        stack_state=slot_stack.stack_state_for(
            stack_present=wt.stack_present,
            stack_up=wt.stack_up,
            probes=wt.probes,
            observed_at=wt.observed_at,
        ),
        ports=wt.ports,
        urls=slot_stack.urls_for(wt.ports, node_name, node_tailnet_name),
        last_active=wt.last_active,
        idle_hours=(
            None
            if idle_hours is None or idle_hours == slot_state.UNKNOWN_AGE_SENTINEL
            else float(idle_hours)
        ),
        ahead=wt.ahead if wt.has_repo else None,
        behind=wt.behind if wt.has_repo else None,
        dirty=wt.dirty if wt.has_repo else None,
        session_status=wt.session_status,
        session_started_at=wt.session_started_at,
        model=wt.model,
        # `resolve_auth_dir` fails closed to `""` -> None. Also `None` for a tenant: the dir embeds the
        # operator's `jaah_home`, and with the row's `project`/`number` would reconstruct the blanked `path`.
        account=None if is_tenant_org(reader_org) else (resolve_auth_dir(jaah_home, account_slug) or None),
        account_name=account_name if not is_tenant_org(reader_org) or account_org_id == reader_org else None,
        window_name=wt.window_name,
        title=slot_title.resolved_title(
            # Not the raw `name`: a derived label would out-rank the active task's (`slot_title.stored_opinion`).
            slot_title.stored_opinion(
                wt.name,
                default_title=wt.default_title,
                default_name=slot_title.default_name(project_name, wt.number),
            ),
            task_active=task_active, task_id=wt.task_id, task_title=task_title,
        ),
        name=wt.name,
        description=wt.description,
        task_active=task_active,
        task_awaiting_answer=slot_title.task_awaiting_answer_from_state(task_state),
        created_at=wt.created_at,
        updated_at=wt.updated_at,
        created_by=actor_names.get(wt.created_by_id, _UNKNOWN_ACTOR),
        updated_by=actor_names.get(wt.updated_by_id, _UNKNOWN_ACTOR),
        protected=wt.number == 0,
        observed_at=wt.observed_at,
        holder_since=wt.state_since,
        last_seen_live=wt.last_seen_live,
        may_edit=may_edit,
        kind=wt.kind,
        retiring=wt.retiring_at is not None,
        retiring_since=wt.retiring_at,
        # The node's raw refusal for the operator plane; a tenant reads it redacted (paths, URL credentials, tokens).
        destroy_error=(
            (fleet_verdicts.redact_node_detail(wt.destroy_error) or None) if is_tenant_org(reader_org) else wt.destroy_error
        ),
        destroy_node_error=destroy_node_error,
        has_repo=wt.has_repo,
        repos=_tenant_blanked_repos(wt.repos) if is_tenant_org(reader_org) else wt.repos,
    )


def _tenant_blanked_repos(repos: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The manifest with every `path` blanked: each lives under the node's `jaah_home`, like the slot's `path`. A
    failure's `detail` is git's own stderr, which names node paths too, so a tenant gets its reason alone."""
    if repos is None:
        return None
    blanked = {key: [{**entry, "path": ""} for entry in repos.get(key) or []] for key in ("repos", "kept")}
    blanked["failed"] = [{**entry, "detail": ""} for entry in repos.get("failed") or []]
    return {**repos, **blanked}


# Shared onclauses so the SELECT sites cannot drift. INNER: both columns are NOT NULL + FK.
_WT_PROJECT_JOIN = Project.id == Worktree.project_id
_WT_NODE_JOIN = Node.id == Worktree.node_id
# The host `slot_stack.urls_for` prefers for slot URLs.
_WT_NODE_TAILNET_NAME = Node.config_snapshot[slot_stack.TAILNET_DNS_NAME_SNAPSHOT_KEY].astext.label(
    "node_tailnet_name"
)

# OUTER: `owner_user_id` is NULL on every non-`user` slot.
_WT_OWNER_JOIN = AdminUser.id == Worktree.owner_user_id

# OUTER: `task_id` has no FK on purpose, so a slot dangling at a deleted task must still read `free`.
_WT_TASK_JOIN = Task.id == Worktree.task_id
# Labelled so unpacking cannot confuse the task's `title` with the slot's.
_WT_TASK_COLUMNS = (Task.state.label("task_state"), Task.title.label("task_title"))

# Both SELECT sites call `visible_worktree_clause` and `worktree_editable_clause` with the same
# principal, or a hidden slot is reachable by URL. Neither may leave its `select(Worktree)` (cross-join).
_WT_MAY_EDIT_LABEL = "may_edit"
# Inputs to the slot's credential-dir path. The slug is a correlated probe, since a join to `accounts`
# would change row multiplicity.
_WT_ACCOUNT_COLUMNS = (
    Node.jaah_home.label("jaah_home"),
    account_slug_expr(Worktree.account_id).label("account_slug"),
    select(Account.name).where(Account.id == Worktree.account_id).scalar_subquery().label("account_name"),
    select(Account.org_id).where(Account.id == Worktree.account_id).scalar_subquery().label("account_org_id"),
)


def _slot_rows_select(principal: Principal | None) -> Select[Any]:
    """The one SELECT both slot readers unpack (`_wt_out`'s positional order), up to its `where`: the caller adds
    `visible_worktree_clause(principal)` beside its own identity predicates."""
    return (
        select(
            Worktree,
            Project.name.label("project_name"),
            slot_state.idle_hours_expr(),
            *_WT_TASK_COLUMNS,
            Node.name.label("node_name"),
            _WT_NODE_TAILNET_NAME,
            worktree_editable_clause(principal).label(_WT_MAY_EDIT_LABEL),
            *_WT_ACCOUNT_COLUMNS,
            AdminUser.email.label("owner_user_email"),
        )
        .join(Project, _WT_PROJECT_JOIN)
        .join(Node, _WT_NODE_JOIN)
        .outerjoin(Task, _WT_TASK_JOIN)
        .outerjoin(AdminUser, _WT_OWNER_JOIN)
    )


@router.get("/api/worktrees", response_model=list[WorktreeOut])
@feature("worktrees", "view")
def list_worktrees(
    node_id: Optional[str] = Query(default=None),
    limit: int = Query(default=_LIST_BOUND_DEFAULT, ge=1, le=_LIST_BOUND_MAX),
    _user: AdminUser = Depends(require_user),
    # The authorization subject; the decision reads THIS, never the ORM row.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    stmt = (
        _slot_rows_select(principal)
        .where(visible_worktree_clause(principal))
        # (project, number) is not unique across nodes; `Node.name` makes the order stable.
        .order_by(Project.name, Worktree.number, Node.name)
        .limit(limit)
    )
    if node_id is not None:
        stmt = stmt.where(Worktree.node_id == node_ref(node_id))
    rows = session.execute(stmt).all()
    # One resolution per page, not per row.
    actor_names = resolve_actor_names(
        session,
        [actor_id for wt, *_ in rows for actor_id in (wt.created_by_id, wt.updated_by_id)],
    )
    orgs = org_refs(session, (wt.org_id for wt, *_ in rows))
    destroy_errors = slot_allocation.destroy_node_errors(session, (wt for wt, *_ in rows))
    return [
        _wt_out(
            wt, project_name, idle, task_state, task_title, actor_names,
            node_name=node_name, node_tailnet_name=node_tailnet_name, may_edit=may_edit, jaah_home=jaah_home, account_slug=account_slug,
            account_name=account_name, account_org_id=account_org_id,
            owner_user_email=owner_user_email, destroy_node_error=destroy_errors.get(wt.id),
            reader_org=_user.org_id, org=org_ref_of(orgs, wt.org_id),
        )
        for (
            wt, project_name, idle, task_state, task_title,
            node_name, node_tailnet_name, may_edit, jaah_home, account_slug, account_name, account_org_id,
            owner_user_email,
        ) in rows
    ]


@router.get("/api/worktrees/{project_id}/{number}", response_model=WorktreeOut)
@feature("worktrees", "view")
def get_worktree(
    project_id: int,
    number: int,
    node_id: Optional[str] = Query(default=None),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """One slot. `node_id` (the node uuid) names which, since (project_id, number) is not unique across nodes;
    omitted, the lowest node name wins.

    A slot this caller may not see is a 404, not a 403, so slots are not enumerable:
    `visible_worktree_clause` sits in the same `where` as the identity predicates."""
    stmt = (
        _slot_rows_select(principal)
        .where(
            Worktree.project_id == project_id,
            Worktree.number == number,
            visible_worktree_clause(principal),
        )
        .order_by(Node.name, Node.id)
    )
    if node_id is not None:
        stmt = stmt.where(Worktree.node_id == node_ref(node_id))
    row = session.execute(stmt).first()
    if row is None:
        # Both "no such slot" and "not yours" — one answer, see the docstring.
        raise HTTPException(status_code=404, detail="worktree not found")
    (
        wt, project_name, idle, task_state, task_title,
        node_name, node_tailnet_name, may_edit, jaah_home, account_slug, account_name, account_org_id,
        owner_user_email,
    ) = row
    actor_names = resolve_actor_names(session, (wt.created_by_id, wt.updated_by_id))
    return _wt_out(
        wt, project_name, idle, task_state, task_title, actor_names,
        node_name=node_name, node_tailnet_name=node_tailnet_name, may_edit=may_edit, jaah_home=jaah_home, account_slug=account_slug,
        account_name=account_name, account_org_id=account_org_id,
        owner_user_email=owner_user_email, destroy_node_error=slot_allocation.destroy_node_error(session, wt),
        reader_org=_user.org_id, org=org_ref_of(org_refs(session, (wt.org_id,)), wt.org_id),
    )


# ── Projects ─────────────────────────────────────────────────────────────────

@router.get("/api/projects", response_model=list[ProjectsRouteProjectOut])
@feature("projects", "view", collection=True, agent_allowed=True)
def list_projects(
    limit: int = Query(default=_LIST_BOUND_DEFAULT, ge=1, le=_LIST_BOUND_MAX),
    # `activity`: most tasks in the last 30 days first, then by name (`app/activity_rank.py`).
    order: Literal["name", "activity"] = "name",
    # jaahd's project resolvers read this via X-Agent-Token with no cookie. The decision reads
    # `principal`, never this ORM row.
    _user: AdminUser | None = Depends(require_user_or_agent),
    # `None` is the agent principal, allow-all by decision (`scoped_project_select(None)` is unfiltered):
    # scoping the resolvers would be a dispatch outage. It never means unauthenticated.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    # Cookie callers are scoped: `tools/control_plane_client.py::fetch_projects` logs in as `ADMIN_EMAIL`
    # and expects the full inventory, so that user must stay a superadmin.
    # `scoped_project_select`, never a hand filter: the clause only holds inside a `Project` FROM
    # (`app/authz/visibility.py::scope_key_clause`). SQL, so `limit` agrees.
    ordering = (Project.name,) if order == "name" else (activity_rank.project_task_count().desc(), Project.name)
    rows = session.scalars(
        scoped_project_select(principal, key=PROJECTS_VIEW_KEY)
        .where(in_live_org(Project.org_id))
        .order_by(*ordering)
        .limit(limit)
    ).all()
    # Shares `routes/projects._project_out`; org labels resolved once per page, not per row.
    refs = org_refs(session, [r.org_id for r in rows])
    # Likewise the merge-lane projection: one batched read for the page (`merge_lane.lane_states`).
    lanes = merge_lane_state.lane_states(session, rows)
    vm = microvm_page(session, rows)
    repos = project_repos.repos_by_project(session, [r.id for r in rows])
    # And Live Cast: each org's allowance read once (`live_cast_holders`).
    live_cast = live_cast_holders(session, [r.org_id for r in rows])
    return [
        _project_out(r, org=org_ref_of(refs, r.org_id), lane=lanes[r.id], vm=vm, repos=repos[r.id], live_cast=live_cast)
        for r in rows
    ]


# `:int`, so `/api/projects/governance-authority` and its siblings are never shadowed.
@router.get("/api/projects/{project_id:int}", response_model=ProjectsRouteProjectOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def get_project(
    project_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """One project, as a `list_projects` row: the edit page's read when the row is past the list's bound
    (an operator's All-orgs view). Same scope, so another org's project is a 404 to a tenant."""
    row = session.scalars(scoped_project_select(principal, key=PROJECTS_VIEW_KEY).where(
        Project.id == project_id, in_live_org(Project.org_id),
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown project")
    return _project_out(
        row, org=org_ref_of(org_refs(session, [row.org_id]), row.org_id), lane=merge_lane_state.lane_state(session, row),
        vm=microvm_page(session, [row]), repos=project_repos.repos_of(session, row.id),
        live_cast=live_cast_holders(session, [row.org_id]),
    )


# ── Command log ──────────────────────────────────────────────────────────────
# Sourced from `CommandAudit`.

# `models.CommandAudit` is the single home for what this table is and who writes it.

class CommandOut(BaseModel):
    id: int
    # The node's display name (`""` for none); `node_uuid` is its identity, what `?node_id=` filters on.
    node_id: str
    node_uuid: Optional[str] = None
    command_type: str
    args: dict[str, Any]
    status: str
    created_at: datetime
    completed_at: Optional[datetime]
    result: Optional[dict[str, Any]]


class CommandDetailOut(CommandOut):
    """One row, as its detail page reads it: the log's row plus who dispatched it (`command_log_entry`)."""

    actor: Optional[str] = None


class CommandPageOut(BaseModel):
    """One page of the command log, plus the filtered count of everything behind it.

    `total` counts rows this caller may see that match the filters, via the same clause as the page
    (`_command_log_filtered`), so the pager can say "Page N of M" and stop. No `total_unfiltered` (unlike
    `GET /api/audit`): the client knows whether it sent a filter."""

    items: list[CommandOut]
    total: int


#: `node_id=@none` means `command_audit_log.node_id IS NULL`: rows bound to no node (nullable with
#: `ON DELETE SET NULL`; still shown by `visible_node_ref_clause`), otherwise unselectable. A sentinel
#: string, as `GET /api/audit`'s `user_id` does; `@` cannot occur in a node name or uuid, so it cannot
#: collide.
UNBOUND_NODE = "@none"

_COMMAND_Q_MAX_LEN = 200
_COMMAND_PAGE_MAX = 10_000


def _command_log_filtered(
    stmt: Any,
    *,
    node_id: Optional[str],
    q: Optional[str],
    status: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    search_payload: bool,
) -> Any:
    """Every operator-chosen filter, AND-combined, applied to whichever statement it is handed.

    One helper for the count and the page (as `routes/audit.py::_filtered`), so `total` and the rows
    describe the same set. It carries none of the non-operator clauses (grant scoping, `_test`
    exclusion), which stay at the call site ahead of it.

    An empty string is a cleared control (`URLSearchParams` sends `status=`), normalised here for all
    three so no caller can filter on `""`.

    `search_payload` decides whether `q` may match inside `args`/`result` JSON or only the command type,
    per what `command_read` serves this reader. Required, no default."""
    node_id = node_id or None
    q = q or None
    status = status or None
    if node_id is not None:
        if node_id == UNBOUND_NODE:
            stmt = stmt.where(CommandAudit.node_id.is_(None))
        else:
            # The node uuid. `node_ref` is a scalar subquery: an unknown uuid yields NULL and matches
            # nothing (fail-closed).
            stmt = stmt.where(CommandAudit.node_id == node_ref(node_id))
    if status is not None:
        # No enum validation: `status` is whatever the node's reply carried (`routes/commands.py`), an open set;
        # an unknown value honestly matches nothing. The client's picker decides which values to offer.
        stmt = stmt.where(CommandAudit.status == status)
    if start is not None:
        stmt = stmt.where(CommandAudit.created_at >= start)
    if end is not None:
        stmt = stmt.where(CommandAudit.created_at <= end)
    if q:
        # Escaped for LIKE's metacharacters, so an underscore matches itself.
        needle = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{needle}%"
        # The command type plus the `args`/`result` JSON behind the lossy Target/Result labels. Safe to search
        # because `command_read` (`app/command_read.py`) serves these payloads whole to exactly the principals
        # this statement is scoped to, not because of write-time redaction. For a tenant it blanks host paths,
        # so the payload predicates are dropped (`search_payload`). A NUL in `q` 500s at bind, as for every
        # needle (`routes/audit.py::_filtered`).
        needles = [CommandAudit.command_type.ilike(pattern, escape="\\")]
        if search_payload:
            needles += [
                CommandAudit.args.cast(Text).ilike(pattern, escape="\\"),
                CommandAudit.result.cast(Text).ilike(pattern, escape="\\"),
            ]
        stmt = stmt.where(or_(*needles))
    return stmt


@router.get("/api/command-log", response_model=CommandPageOut)
@feature("system", "view")
def command_log(
    # Bounded: pydantic parses arbitrary-precision ints, so a huge page overflows `bigint` in the offset and
    # Postgres raises an uncaught 500; it also caps the scan one request can buy. The client derives its
    # last page from `total`.
    page: int = Query(default=1, ge=1, le=_COMMAND_PAGE_MAX),
    per: int = Query(default=50, ge=1, le=200),
    node_id: Optional[str] = Query(
        default=None,
        description=(
            f"A node uuid, or the literal '{UNBOUND_NODE}' for the rows bound to no node — the "
            "value the picker's 'No node' option carries. CLEARED MEANS OMITTED."
        ),
    ),
    q: Optional[str] = Query(
        default=None,
        max_length=_COMMAND_Q_MAX_LEN,
        description=(
            "Case-insensitive substring match across the command type, the args JSON (which is what the "
            "Target column is derived from) and the result JSON."
        ),
    ),
    status: Optional[str] = Query(
        default=None,
        description=(
            "Exact match on the recorded status (done | failed | rejected | dispatched, as written by "
            "the dispatching route). Not a closed vocabulary server-side — see `_command_log_filtered`. "
            "CLEARED MEANS OMITTED."
        ),
    ),
    start: Optional[datetime] = Query(
        default=None,
        description=(
            "Inclusive lower bound on created_at. ISO-8601 WITH AN EXPLICIT OFFSET (…+02:00 or …Z); a "
            "zoneless value is refused with 422 rather than read in a guessed zone. CLEARED MEANS "
            "OMITTED: drop the key, do not send it empty."
        ),
    ),
    end: Optional[datetime] = Query(
        default=None,
        description="Inclusive upper bound on created_at. Same requirements as start.",
    ),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CommandPageOut:
    """Node-grant scoped: the log shows only the nodes this caller was granted; `system:view` still
    required. A row bound to no node still shows (`authz.visible_node_ref_clause`) and is selectable via
    `@none`.

    Every filter is in the `WHERE`, ahead of `offset`/`limit` and inside the grant scoping, so `total` and
    the page describe the same set; filtering fetched rows client-side would return short pages
    indistinguishable from the last.

    `total` is the filtered count (`CommandPageOut`), a second query over the same statement.

    Cost: an exact `COUNT(*)`, and a `q` search is three unanchored `ILIKE`s plus two JSONB→text casts,
    bounded by the other filters and table size. A `pg_trgm` GIN index is the fix if needed (DDL)."""
    offset = (page - 1) * per
    start = _require_aware(start, "start")
    end = _require_aware(end, "end")
    # Empty strings are cleared controls (normalised in `_command_log_filtered`); the two `datetime`
    # parameters cannot be, since pydantic refuses `?start=`, so the client drops the key.
    # The two clauses that are not the operator's choice lead: what this caller may see, and the
    # test-harness rows.
    scoping = (
        CommandAudit.args["_test"].as_string().is_(None),
        visible_node_ref_clause(principal, CommandAudit.node_id),
    )
    # The reader's org decides the read model and the search surface together (`app/command_read.py`,
    # `search_payload`).
    tenant = is_tenant_org(_user.org_id)
    filters = dict(
        node_id=node_id, q=q, status=status, start=start, end=end, search_payload=not tenant,
    )

    total = session.scalar(
        _command_log_filtered(
            select(func.count()).select_from(CommandAudit).where(*scoping), **filters
        )
    ) or 0

    stmt = _command_log_filtered(
        select(CommandAudit).where(*scoping).order_by(CommandAudit.created_at.desc(), CommandAudit.id.desc()),
        **filters,
    )
    rows = session.scalars(stmt.offset(offset).limit(per)).all()
    # One query for the page's node names. `""` spells a NULL node reference (unregistered or deleted
    # node); not `"local"`, which would name a node.
    names = names_by_id(session, [r.node_id for r in rows])
    return CommandPageOut(items=[_command_out(r, names, tenant=tenant) for r in rows], total=total)


def _command_out(r: CommandAudit, names: dict[UUID, str], *, tenant: bool) -> CommandOut:
    """One row as `command_log` and `command_log_entry` serve it, through the one tenant read model."""
    args, result = command_read(r.args, r.result, tenant=tenant)
    return CommandOut(
        id=r.id, node_id=names.get(r.node_id) or "",
        node_uuid=str(r.node_id) if r.node_id is not None else None, command_type=r.command_type, args=args,
        status=r.status,
        created_at=r.created_at,
        completed_at=r.completed_at, result=result,
    )


@router.get("/api/command-log/{command_id}", response_model=CommandDetailOut)
@feature("system", "view")
def command_log_entry(
    command_id: int = Path(ge=1, le=2**31 - 1),  # `command_audit_log.id` is `integer`: past it the bind 500s.
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CommandDetailOut:
    """One row of the log, under exactly the list's scoping (`command_log`): a row this caller could not
    page to (another org's, an ungranted node's, a test-harness row) is a 404, indistinguishable from an
    unknown id.

    `actor` is an email or a `system:*` label (`routes/commands.py::_record_audit`). A tenant is served an
    email only when it names a person its own org-scoped `AdminUser` read resolves, else `WITHHELD_ACTOR`:
    an operator who dispatched onto a tenant's node is not the tenant's to name."""
    row = get_or_404(
        session, CommandAudit, command_id, detail="Command not found", where=(
            CommandAudit.args["_test"].as_string().is_(None),
            visible_node_ref_clause(principal, CommandAudit.node_id),
        ),
    )
    tenant = is_tenant_org(_user.org_id)
    actor = row.actor
    if tenant and actor and not actor.startswith("system:"):
        known = session.scalar(
            select(AdminUser.id).where(AdminUser.email == actor, AdminUser.org_id == _user.org_id).limit(1)
        )
        actor = actor if known is not None else WITHHELD_ACTOR
    out = _command_out(row, names_by_id(session, [row.node_id]), tenant=tenant)
    return CommandDetailOut(**out.model_dump(), actor=actor)


# ── Resources ─────────────────────────────────────────────────────────────────

# The join keys on the slug; the path normalization lives with `app/node_clone.py::account_slug_from_dir`.

# The account key, composed (as `routes/credentials/guards.py::_VIEW_KEY`). Not the door of either route that
# reads it; the narrowing at the bottom of `get_resources` is the single home for why, and why a missing
# principal denies. `account_provisioning` applies the same rule.
_ACCOUNTS_VIEW_KEY = permission_key("accounts", "view")


def _readable_fold_records(session: Session, principal: Principal | None) -> list[dict[str, Any]]:
    """`account_fold.account_records` narrowed to the accounts `principal` may read.

    The fold is keyed by slug with no `accounts.id`, so the slug narrows it (`readable_account_slugs`;
    `uq_accounts_slug` makes it faithful). Narrowed before the node matrix is built, so a filtered-out
    account's per-node credential dirs are withheld too. A slug naming no `accounts` row is dropped for a
    restricted reader (D2) and kept for a superadmin."""
    records = account_fold.account_records(session)
    allowed = readable_account_slugs(session, principal)
    if allowed.unrestricted:
        return records
    return [record for record in records if allowed.contains(record.get("slug"))]


def _account_rows_by_join_key(
    session: Session, principal: Principal | None,
) -> tuple[dict[str, Account], list[Account]]:
    """The `accounts` DB SSOT indexed for the resources join: (slug → row, every row by slug).

    Scoped to what `principal` may read (`app/authz/visibility.py::readable_account_select`), as `GET /api/accounts`
    is: this page serves a superset of the roster's fields. Deny by default.

    One index: a dir is derived from the slug (`node_clone.resolve_auth_dir`), so its basename is the slug
    (`account_slug_from_dir`). `slug` is unique, so `setdefault` decides nothing.

    The second return value is the complete roster: a row with an empty slug would be missing from the
    index, so the no-match pass must iterate rows."""
    rows_by_slug: dict[str, Account] = {}
    rows = list(session.scalars(readable_account_select(principal).order_by(Account.slug)).all())
    for row in rows:
        if row.slug:
            rows_by_slug.setdefault(row.slug, row)
    return rows_by_slug, rows


def _shared_account_rows(session: Session, principal: Principal | None, slugs: set[str]) -> list[Account]:
    """The `accounts` rows of another org's accounts a node in the operator's filtered orgs holds a login for —
    "shared" with the selected orgs, so the Brains page lists them beside the owned ones, marked with their own
    org. Read past the filter (`seam.operator_scope()` re-entered under `OPERATOR` only suspends it): the seam
    would drop exactly these rows. Only an operator's filtered GET reaches here; the filter is set for no one else.
    Still `readable_account_select`: the escape lifts the org filter, never the grant (personal stays personal)."""
    if not slugs:
        return []
    with seam.operator_scope():
        return list(session.scalars(
            readable_account_select(principal).where(Account.slug.in_(sorted(slugs))).order_by(Account.slug)
        ).all())


@router.get("/api/resources")
@feature("system", "view")
def get_resources(
    node_id: Optional[str] = Query(default=None),
    _user: AdminUser = Depends(require_user),
    # The authorization subject for both dimensions this page serves: the node roster and the accounts
    # (credential dir, login email, quota, uuid). Also the key-tier subject: the narrowing at the bottom
    # reads `accounts:view` off it.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """Node-grant scoped: this route answers the same "which hosts exist" question as `GET /api/nodes` (the
    account matrix is keyed by node names, and two aggregates read `runners`/`worktrees` fleet-wide), so
    unscoped it would make that route's narrowing cosmetic.

    The account dimension is narrowed separately (see the bottom of the handler); keep the two split by
    dimension, as `POST /api/commands`' 201 body is."""
    # Account records come from `account_fold.account_records`; a stale source leaves the page stale, never
    # a 500. `sessions` is seeded and then recomputed from `worktrees` below.
    data: dict[str, Any] = {
        "accounts": _readable_fold_records(session, principal),
        "sessions": {"total": 0, "busy": 0, "idle": 0},
    }
    # Per-node state beside the folded record, because the fold cannot say where: a login is per (account,
    # node). Composed here so `reconstitute`'s shape (read by the dispatch guard) stays as pinned. Every
    # node in the environment, since a node missing an account is the actionable case.

    # The operator's org filter. The fold and the recent-node roster are raw SQL
    # (`account_fold`), which the seam cannot narrow, so this handler applies it: the node dimension below,
    # the account dimension after the matrix.
    org_filter = seam.request_org_filter()
    # The roster is the leak, so the grant narrows it. Node uuids as text, the matrix's key (names repeat
    # across orgs); `None` for an unrestricted principal (no query). Under the org filter the select is
    # narrowed at the seam to the selected orgs' nodes.
    allowed_node_ids = None if sees_all_nodes(principal) and org_filter is None else {
        str(row.id) for row in session.scalars(scoped_node_select(principal)).all()
    }
    # Hoisted: an account no node has observed has no fold row and reaches the payload only via the
    # no-credentials pass, so the matrix must also be asked for roster slugs. Otherwise that account gets
    # no `nodes` key and no way to start a first login.
    rows_by_slug, all_rows = _account_rows_by_join_key(session, principal)
    # What `hidden_account_count` compares with the seam-bound count; the shared rows below are no part of it.
    owned_count = len(all_rows)
    # Fold slugs first (existing order), then roster rows without a fold record. Both are already
    # grant-narrowed, and `allowed_node_ids` still confines the node dimension.
    matrix_slugs = list(dict.fromkeys(
        [record.get("slug") for record in data["accounts"] if record.get("slug")]
        + [row.slug for row in all_rows if row.slug]
    ))
    account_nodes = account_fold.account_node_matrix(
        session, matrix_slugs, allowed_node_ids=allowed_node_ids,
        tenant_reader=is_tenant_org(_user.org_id),
    )
    if org_filter is not None:
        # Under the filter an account stays when a selected org owns it or a node of a selected org holds a login for it
        # (a present cell; the matrix is already node-narrowed). Owned is the seam-narrowed slugs, not `rows_by_slug`:
        # a colleague's personal account is no readable row, yet it is the selected org's.
        fold_slugs = [record.get("slug") or account_slug_from_dir(record.get("config_dir")) for record in data["accounts"]]
        owned = set(rows_by_slug) | set(
            session.scalars(select(Account.slug).where(Account.slug.in_([slug for slug in fold_slugs if slug])))
        )
        on_selected_nodes = {
            slug for slug, cells in account_nodes.items() if any(cell.get("present") for cell in cells)
        }
        for shared_row in _shared_account_rows(session, principal, on_selected_nodes - owned):
            rows_by_slug.setdefault(shared_row.slug, shared_row)
            all_rows.append(shared_row)
        data["accounts"] = [
            record for record, slug in zip(data["accounts"], fold_slugs, strict=True) if slug in owned or slug in on_selected_nodes
        ]
    # Each row's tenant (`tenant_org`, an `OrgRef`; `org` is the login's sensed organization): the one source for a shared
    # row, which has no `GET /api/accounts` record to carry it.
    org_labels = org_refs(session, [row.org_id for row in all_rows])
    # Shared/Local per node, Syncing… after a login, and the NodeLinks an account is shared through.
    shared_via = account_sharing.annotate(session, account_nodes)
    for record in data["accounts"]:
        record["nodes"] = account_nodes.get(record.get("slug"), [])
        record["shared_via"] = shared_via.get(record.get("slug"), [])
    # Live-computed counts are node-scoped when node_id is given, fleet-wide otherwise.
    runner_count_stmt = (
        select(func.count()).select_from(Runner)
        # `?node_id=` narrows; node reach confines. Without it the fleet-wide count is a runner census of
        # hosts this caller cannot name.
        .where(node_reach_clause(principal, Runner.node_id))
    )
    if node_id is not None:
        runner_count_stmt = runner_count_stmt.where(Runner.node_id == node_ref(node_id))
    data["runners"] = session.scalar(runner_count_stmt) or 0

    # Sessions per account from worktrees: this endpoint is their sole source (the node no longer publishes
    # per-account attribution, `apps/jaahd/collector.py::_collect_resources`). `Project.name` joined for the
    # payload's `project` label; occupancy is `Worktree.state` directly.
    live_wts_stmt = (
        select(Worktree, Project.name)
        .join(Project, _WT_PROJECT_JOIN)
        .where(
            Worktree.session_status.isnot(None), Worktree.session_status != "",
            # The whole visibility rule, not just its node conjunct: these are per-slot rows, owing the same answer
            # as `GET /api/worktrees`.
            visible_worktree_clause(principal),
        )
    )
    if node_id is not None:
        live_wts_stmt = live_wts_stmt.where(Worktree.node_id == node_ref(node_id))
    live_wts = session.execute(live_wts_stmt).all()
    # Keyed on the resolved account (`worktrees.account_id`, an FK), so counts attach to account identity.
    # NULL keeps its own bucket, read by no element but counted in the `total`/`busy` rollup.
    by_acct: dict[Optional[UUID], dict] = {}
    for wt, project_name in live_wts:
        slot = by_acct.setdefault(wt.account_id, {"total": 0, "busy": 0, "runners": 0, "worktrees": []})
        slot["total"] += 1
        if wt.session_status == "busy":
            slot["busy"] += 1
        # `state == "task"`, never `!= free`: a human's `user` console session is not a fleet runner
        # (`app/account_runners.py` narrows the same way).
        # Unlike the selection gate, in-flight launches are not counted: this page shows what is observed
        # running, while the cap must charge allocations it cannot yet see. Do not add the in-flight arm here;
        # the gate's predicate lives in `app/account_runners.fleet_assigned_runner_counts`.
        if wt.state == "task":
            slot["runners"] += 1
        slot["worktrees"].append({
            "project": project_name, "project_id": wt.project_id, "number": wt.number,
            "branch": wt.branch, "state": wt.state,
            "session_status": wt.session_status,
        })
    # Read-time join onto the `accounts` SSOT (the blob is a cache): `account_id` (writes are addressed by it;
    # `null` disables the controls), `enabled` (what dispatch gates on) and `node_id` (a pin the blob lacks).
    # `rows_by_slug`/`all_rows` are the single read above, shared with the matrix so both see one answer.
    # Pin names are resolved in one query for both loops below.
    pin_names = names_by_id(session, [row.node_id for row in all_rows])
    # Jaah homes for the pinned nodes, one query: `config_dir` is per node (`node_clone.resolve_auth_dir`).
    pinned_ids = {row.node_id for row in all_rows if row.node_id is not None}
    pin_homes: dict[UUID, Optional[str]] = {
        pinned_node_id: jaah_home
        for pinned_node_id, jaah_home in session.execute(
            # Scoped: `config_dir` is composed from `jaah_home`, so an unscoped read would republish the roster.
            scoped_node_select(principal)
            .with_only_columns(Node.id, Node.jaah_home)
            .where(Node.id.in_(pinned_ids))
        ).all()
    } if pinned_ids else {}
    # Whose personal account a sensed login is. A colleague's personal account is no row this reader may
    # read (`authz/visibility.py::visible_account_clause`), so without this the roster could only say "Provider
    # unknown" for a login its node reports. Read within the org (the seam), its owner only — never the row.
    unread_slugs = {
        acct.get("slug") for acct in data.get("accounts", [])
        if acct.get("slug") and acct.get("slug") not in rows_by_slug
    }
    owner_by_slug: dict[str, UUID] = dict(session.execute(
        select(Account.slug, Account.owner_id).where(Account.slug.in_(unread_slugs), Account.owner_id.is_not(None))
    ).tuples().all()) if unread_slugs else {}
    owner_names = resolve_actor_names(
        session, [row.owner_id for row in all_rows] + list(owner_by_slug.values())
    )
    matched_row_ids: set[UUID] = set()
    for acct in data.get("accounts", []):
        # The element's own `slug` is the key; its dir only corroborates. A node's free-text path must not pick
        # a roster row its observation key does not, since Deprovision derives its path from the matched row.
        # Disagreement is a report to distrust.

        # The `or` covers an element with a dir but no slug, matched only by the derived value.
        element_slug = acct.get("slug") or ""
        derived_slug = account_slug_from_dir(acct.get("config_dir"))
        row = (
            None
            if derived_slug and element_slug and derived_slug != element_slug
            else rows_by_slug.get(element_slug or derived_slug)
        )
        # Counts by the matched row's id; an unmatched element must not `.get` the NULL bucket, which is not its.
        counts = by_acct.get(row.id) if row is not None else None
        if counts is None:
            counts = {"total": 0, "busy": 0, "runners": 0, "worktrees": []}
        acct["sessions"] = {"total": counts["total"], "busy": counts["busy"], "idle": counts["total"] - counts["busy"]}
        acct["runners"] = counts["runners"]
        acct["worktrees"] = counts["worktrees"]
        # `accounts.id` is a uuid, carried as a string.
        acct["account_id"] = str(row.id) if row is not None else None
        # The label every surface shows (`accounts.name`); None for a fold record with no roster row.
        acct["name"] = row.name if row is not None else None
        # The owning person of a personal account, readable or not; None for an org account or an unregistered login.
        owner_id = row.owner_id if row is not None else owner_by_slug.get(element_slug)
        acct["owner"] = owner_names.get(owner_id) if owner_id is not None else None
        acct["tenant_org"] = org_ref_of(org_labels, row.org_id) if row is not None else None
        # The account's agent provider (`accounts.provider`), for the same shared row with no record to carry it.
        acct["provider"] = row.provider if row is not None else None
        if row is not None:
            matched_row_ids.add(row.id)
            acct["enabled"] = row.enabled
            # The pinned node's uuid (the address every door takes) and its display name.
            acct["node_id"] = str(row.node_id) if row.node_id is not None else None
            acct["node_name"] = pin_names.get(row.node_id)
        else:
            # No row ⇒ no recorded pin; `setdefault` so the key is present without clobbering a collector value.
            acct.setdefault("node_id", None)
            acct.setdefault("node_name", None)

    # Rows the blob has nothing for, appended as `status: "no_credentials"` (e.g. every "Add record"
    # create), since this response is the only roster the UI reads. `all_rows` is grant-scoped, so only
    # accounts `GET /api/accounts` shows this reader are appended.
    for row in all_rows:
        if row.id in matched_row_ids:
            continue
        data.setdefault("accounts", []).append({
            "slug": row.slug,
            # Derived; this pass is the only path by which a roster row's dir reaches `/api/resources`, and several
            # consumers read it. `""` for an unpinned row or undeclared home: a guessed dir would aim Deprovision
            # at an unprovisioned path.
            "config_dir": resolve_auth_dir(pin_homes.get(row.node_id), row.slug),
            # The same per-node matrix every folded record carries (see the matrix call above); every cell is
            # `present: False`, which the page renders as a first-login offer.
            "nodes": account_nodes.get(row.slug, []),
            "shared_via": shared_via.get(row.slug, []),
            "email": row.name,
            "name": row.name,
            # A distinct status: a roster row with no credentials, which only an operator can resolve.
            "status": "no_credentials",
            "quota": None,
            "sessions": {"total": 0, "busy": 0, "idle": 0},
            "runners": 0,
            "worktrees": [],
            "account_id": str(row.id),
            "tenant_org": org_ref_of(org_labels, row.org_id),
            "provider": row.provider,
            "enabled": row.enabled,
            "node_id": str(row.node_id) if row.node_id is not None else None,
            "node_name": pin_names.get(row.node_id),
        })

    total = sum(s["total"] for s in by_acct.values())
    busy = sum(s["busy"] for s in by_acct.values())
    data["sessions"] = {"total": total, "busy": busy, "idle": total - busy}

    # A tenant is not served where on the operator's host its credentials live: `config_dir` on the
    # record and in every per-node cell is blanked (as `routes/nodes/node_wire.py::_narrow_for_tenant` blanks the
    # node), to each field's own empty value (`""` / `None`) so withheld reads as unreported. One pass over
    # the finished payload, after the join loop and before the count below.
    if is_tenant_org(_user.org_id):
        for acct in data["accounts"]:
            acct["config_dir"] = ""
            for cell in acct.get("nodes") or []:
                cell["config_dir"] = None

    # ── Account facts need the account key; this dashboard is the other half of that door ────
    # Accounts are access-controlled objects: this route serves every account's dir, email, quota, uuid and matrix;
    # the roster moved to `accounts:view` while this stays `system:view` (it also serves the fleet census), so the
    # account subject is narrowed per subject, in one pass, on top of the grants (key decides whether, grants
    # which). `hidden_account_count` is always present (whole roster without the key; grant-withheld rows with it,
    # from a plain seam-bound count less the owned rows, so it cannot go negative). Absence denies.
    if not (principal is not None and principal.has(_ACCOUNTS_VIEW_KEY)):
        data["hidden_account_count"] = len(data["accounts"])
        data["accounts"] = []
    else:
        in_scope = session.scalar(select(func.count()).select_from(Account)) or 0
        data["hidden_account_count"] = in_scope - owned_count

    return data


# ── Account provisioning (Add-Account wizard progress) ─────────────────────────

class ProvisioningOut(BaseModel):
    slug: Optional[str]
    config_dir: Optional[str]
    step: Optional[str]
    # The authorize link, SEALED by the node to the wizard's per-login key: base64
    # ciphertext this control plane holds but cannot read, which only the browser tab that minted the
    # key can open. Shape- and length-bounded upstream at `durable/accounts.py::_sealed_login`, not
    # here. Never a plaintext `oauth_url`.
    sealed_url: Optional[str] = None
    # The node's own per-login public key, to which the browser seals the pasted code
    # (`*_submit_code`'s `sealed_code`). Public by construction; travels with the link it belongs to.
    seal_key: Optional[str] = None
    status: Optional[str]
    error: Optional[str]
    started_at: Optional[str]
    # Which node this flow runs on (its uuid). A login is per (account, node) and `_account_add_runtime` keys state
    # locally by slug, so the UI must match on both or it pastes one node's code to another.
    node_id: Optional[str] = None
    # The short code the operator types into the page the unsealed link opens (RFC 8628 device authorization);
    # without it the link is unusable. The user code is shown on the operator's screen by design and
    # expires in ~15 minutes; the `device_code` never travels this path. Still one live factor, so it is
    # rendered and never logged. Defaults to None: Claude's login names no code.
    user_code: Optional[str] = None


@router.get("/api/accounts/provisioning", response_model=list[ProvisioningOut])
@feature("system", "view")
def account_provisioning(
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """In-flight/last Add-Account and Re-login wizard records, derived from durable-workflow state
    (`app/durable/accounts.py::provisioning_records`) — the sole source of truth for the whole
    flow. Never exposes a token, and no longer the authorization URL either: it is served sealed
    (`sealed_url`) to the wizard tab that holds the key, ciphertext to everyone else including this CP.

    Scoped on both dimensions, ANDed, deny by default (D2): node-grant scoped on the emitted `node_id`,
    and account-grant scoped on the slug via the bare grant (`granted_account_slugs`), since
    the disclosed node is the flow's, not the account's pin. The account key gate below applies (see
    `get_resources`' closing block)."""
    # ── The whole payload is the account subject, so the gate is one line and it is first ────────────
    # Accounts are access-controlled objects: without `accounts:view`, slugs, dirs, steps and authorize URLs would
    # ride `system:view`. The key decides whether; the object filters below decide which. At the top, since every
    # field is an account fact. No `hidden_*_count`: the wire shape is a list polled every 2s, and `[]` is ordinary.
    # Absence denies (see `get_resources`).
    if principal is None or not principal.has(_ACCOUNTS_VIEW_KEY):
        return []
    # Filtered in Python: records come from workflow state, not a query.
    allowed = granted_account_slugs(session, principal)
    # Who is served the live one-time code (narrower than the record): with link and code a reader can
    # approve the pending device flow with their own account. So the floor is whoever could start the
    # login: `system:create` (`routes/commands.py::COMMAND_TYPE_KEYS`, `_SYSTEM_ONLY`; not `system:edit`,
    # the action keys are independent) and the start path's account projection (`readable_account_slugs`:
    # grant and pinned node), while the record keeps the bare one.
    sees_any_code = principal is None or principal.has_org("system:create")
    # `is_operator`, not the bare flag: a tenant-org superadmin sees only codes they could have started.
    sees_all_codes = principal is None or principal.is_operator
    startable: AllowedObjects | None = None

    def _sees_code(record: dict[str, Any]) -> bool:
        """Whether this record's live code may be served to this reader, per record.

        The unrestricted arms short-circuit here, and the slug set is resolved lazily on first use, so readers
        of code-free records on the 2s poll are not billed a second query."""
        nonlocal startable
        if sees_all_codes:
            return True
        if not sees_any_code:
            return False
        if startable is None:
            startable = readable_account_slugs(session, principal)
        return startable.contains(record.get("slug"))

    def _projected(record: dict[str, Any]) -> dict[str, Any]:
        """`record` as this reader may see it: a live code removed unless they could have started it.

        A copy whenever anything is dropped (workflow state is not ours to mutate); a record with no code is
        returned unchanged.

        Only a non-empty code is withheld. `""` passes through: it carries no secret and means "device-auth
        adapter produced no code", which the wizard renders as a warning, whereas absent/None means "this
        provider mints no code". Collapsing them showed different readers different stories.

        Withholding is spelled `""`, not `None`: without `response_model_exclude_none` both absent and None
        serialize as `null`, which tells the reader the login needs no code (false on a device flow). `""`
        yields the `deviceCodeMissing` warning, also wrong for a withheld healthy login but not about the flow
        itself. Latent today (the modal enters `polling` only from its own POST, which the reader cannot
        pass); the real fix is a fourth value and wizard arm ("a code exists; you may not see it"), deferred."""
        if not record.get("user_code") or _sees_code(record):
            return record
        return {**record, "user_code": ""}

    # The records, derived from durable-workflow state.
    from app.durable import accounts as durable_accounts

    records = [
        ProvisioningOut(**_projected(record))
        for record in durable_accounts.provisioning_records()
        if allowed.contains(record.get("slug"))
    ]
    # ── `config_dir` is the operator's host layout, withheld here as everywhere else ───────
    # Blanked to `""` for a tenant, as `get_resources`, the worktree `path` and
    # `routes/nodes/node_wire.py::_narrow_for_tenant` do; before the node filter so both return paths are covered.
    # `error` (the node's stderr, `durable/accounts.py::_failure_reason`) goes through
    # `command_read.withhold_host_paths` for tenants. An absent user blanks too (defensive default).
    if _user is None or is_tenant_org(_user.org_id):
        records = [
            record.model_copy(update={
                "config_dir": "",
                "error": withhold_host_paths(record.error) if record.error else record.error,
            })
            for record in records
        ]
    if sees_all_nodes(principal):
        return records
    # The node half, filtered too, or this route is the bypass. By the node uuid the workflow was started with
    # (names repeat across orgs); unpaginated, so no short-page hazard. A record with no node yet is kept.
    allowed_nodes = {str(row.id) for row in session.scalars(scoped_node_select(principal)).all()}
    return [r for r in records if r.node_id is None or r.node_id in allowed_nodes]
