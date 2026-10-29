"""Agent reads of the node's accounts and pool worktrees (`GET /api/agent/accounts`, `/worktrees`)."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    account_auth,
    agent_providers,
    node_credentials,
    slot_stack,
    slot_state,
    slot_title,
    tenant_defaults,
)
from app.account_runners import fleet_assigned_runner_counts
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import get_session
from app.node_clone import account_slug_expr, resolve_auth_dir
from app.shared_storage import resolve_shared_auth_dir
from app.node_identity import (
    is_registered_node,
    jaah_home_expr,
    name_expr,
    names_by_id,
    node_matches,
    node_ref,
    warn_unregistered_node,
)
from app.models.credentials import Account
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.entity_shape import iso_or_none
from app.routes.agent.auth import _CALLER_TASK_ID_RE, _check_token, _wire_project, caller_node_ref

router = APIRouter(prefix="/api/agent", tags=["agent"])


# No `GET /api/agent/stream` command-wake channel: commands ride the node-dialled WebSocket
# (`app/node_bus.py`). The browser's command log is `GET /api/stream` (routes/stream.py).


# ── Dispatcher account read (DB SSOT) ──────────────────

class AgentAccountOut(BaseModel):
    """One credential account the jaahd dispatcher may auth a runner with (DB SSOT). `runners` is the live
    count of fleet-held sessions under this account's config_dir, the fewest-runners tiebreak.
    """

    slug: str
    config_dir: str
    # For OLDER jaahd packages only (a newer one resolves the token itself, config dir then `.shared/<slug>`,
    # and ignores both fields): where a shared account's credential file lives, set only when the asking
    # node reports a mounted token share (`nodes.shared_accounts_root`); exported as
    # `CLAUDE_SECURESTORAGE_CONFIG_DIR`. `None`, never `""`: an empty value means `~/.claude`.
    securestorage_dir: Optional[str] = None
    # The `accounts.shared` column, independent of the asking node (older packages refuse a shared account
    # with no `securestorage_dir` rather than fall back to a local token).
    shared: bool = False
    enabled: bool
    # `oauth`/`api_key` (`app/account_auth.py`). An API-key account serves `config_dir=""`, identical to an
    # OAuth account on a home-less node, so the node cannot infer it. Defaults to OAuth for older nodes.
    auth_kind: str = account_auth.AUTH_KIND_OAUTH
    # Whose CLI this credential logs into (`app/agent_providers.py`); the node's health probe picks its
    # signal by it (`apps/jaahd/provider_health.py`). Necessary, not sufficient
    # (`fleet_dispatch.account_dir_usable`). Defaulted for older nodes.
    provider: str = agent_providers.DEFAULT_PROVIDER
    # The NAME, not `accounts.node_id`'s uuid: the node package compares it to the value in node.env.
    node_id: Optional[str] = None
    runners: int = 0


# ORG_SCOPED, not OPERATOR: every caller is a node asking for its own roster
# (`apps/jaahd/control_plane_api.py::agent_api_accounts`); the dispatcher reads the DB in-process.
# Fleet-wide arms are widened by `_read_scope_for`.
@router.get("/accounts", response_model=list[AgentAccountOut])
@infra(Tenancy.ORG_SCOPED)
def agent_accounts(
    node_id: str = Query(..., min_length=1,
                         description="the asking node — config_dir is a PER-NODE answer"),
    include_disabled: bool = Query(default=False),
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> list[AgentAccountOut]:
    """The enabled account roster as the asking node sees it; `include_disabled=1` returns the whole
    roster. gh-free.

    No `quota`: dispatchability is CP-side (`jaah_scheduler.gate_and_tier`). `node_id` is required
    because `config_dir` is per node (`node_clone.resolve_auth_dir`); no declared home resolves to `""`.
    `min_length=1` refuses an identity-less daemon; an unregistered id only warns.

    `securestorage_dir` is served only when the account is `shared`, the node reports a mounted token share
    (a non-empty `shared_accounts_root`) and its org may use NodeLink; `shared` rides unconditioned beside it.
    """
    if not is_registered_node(node_id, session):
        warn_unregistered_node(node_id, "the account roster read")
    stmt = select(Account).order_by(Account.slug)
    if not include_disabled:
        stmt = stmt.where(Account.enabled.is_(True))
    rows = session.scalars(stmt).all()

    # Fleet runners per account (`app/account_runners.py`), which selection caps and tiebreaks on. Live
    # defect: `Worktree` is tenant-owned and this route is `ORG_SCOPED`, so counts are org-local and
    # under-count a saturated account. Fix owed: wrap this call in `seam.operator_scope()`.
    runner_counts = fleet_assigned_runner_counts(session)
    # The asking node's row, read once: home and the token share it reports (`nodelinks.ingest_snapshot` keeps
    # only a permitted root). No row → `""` for every `config_dir` and `None` for every `securestorage_dir`.
    # An org whose NodeLink plan term is off is served no `securestorage_dir`, as before the switch was retired.
    node_row = session.execute(
        select(Node.jaah_home, Node.shared_accounts_root, Node.org_id).where(node_matches(node_id))
    ).first()
    jaah_home = node_row.jaah_home if node_row else None
    share_root = (
        str(node_row.shared_accounts_root)
        if node_row and node_row.shared_accounts_root
        and tenant_defaults.shared_store_allowed(session, node_row.org_id)
        else ""
    )

    # `accounts.node_id` is a uuid and the LABEL is the name: one batched read, not a subquery per row.
    # A NULL reference ("fleet-wide") misses the dict and stays None.
    account_node_names = names_by_id(session, [row.node_id for row in rows])
    return [
        AgentAccountOut(
            slug=row.slug,
            # `""` is true for an API-key account (no directory anywhere); `auth_kind` disambiguates.
            config_dir=(
                "" if account_auth.is_api_key(row.auth_kind)
                else resolve_auth_dir(jaah_home, row.slug)
            ),
            # `or None` keeps the contract (an empty value means `~/.claude`). The API-key conjunct is explicit
            # because `accounts.shared` is writable on any row.
            securestorage_dir=(
                (resolve_shared_auth_dir(row.slug, share_root) or None)
                if (row.shared and share_root and not account_auth.is_api_key(row.auth_kind))
                else None
            ),
            shared=row.shared,
            enabled=row.enabled,
            # Straight off the row; no secret rides with it. The key itself lives in a `credentials` row
            # served slug-in-path (`GET /api/agent/accounts/{slug}/api-key` → `get_account_api_key`).
            auth_kind=row.auth_kind,
            # Straight off the row: a property of the account, so no node-scoped conjunct belongs on it.
            provider=row.provider,
            node_id=account_node_names.get(row.node_id),
            # Keyed on the account itself: `fleet_assigned_runner_counts` groups `worktrees.account_id`,
            # the FK the ingest resolves from whatever dir the node reported.
            runners=runner_counts.get(row.id, 0),
        )
        for row in rows
    ]


# ── Dispatcher worktree-pool roster read (DB SSOT) ──

class AgentWorktreeOut(BaseModel):
    """One pool-worktree slot in a project's roster, as the jaahd dispatcher reads it
    (`worktree_mechanics.lifecycle.roster_from_db` → `select_slot`). gh-free. `branch` feeds branch reuse;
    `last_active` the .env-seed MRU pick.
    """

    # The NAME, resolved through `worktrees.node_id`'s uuid FK: consumers compare it against their own
    # node.env, and `apps/jaahd` ships independently of the CP.
    node_id: str
    # The identity (`worktrees.project_id`). `project` still carries the display NAME, read through a
    # JOIN, because `apps/jaahd` speaks it and ships independently — flipping it breaks under skew.
    project_id: int
    project: str
    number: int
    path: str
    branch: Optional[str] = None
    # Occupancy (`free`/`task`/`user`), straight off `worktrees.state`; `apps/jaahd` reads
    # this vocabulary.
    state: str
    last_active: Optional[str] = None
    # The slot's own kind (`worktrees.has_repo`): false for an empty folder slot, which the console
    # launcher (`clauded_wt.py`) makes and opens as a folder rather than refusing it as no git worktree.
    has_repo: bool = True
    # ── State-plane compatibility fields ──────────────────────────────────────
    # `str` on purpose: consumers compare `rec.get("_number") == str(number)`. `serialization_alias` because
    # Pydantic v2 drops leading-underscore attributes.
    project_compat: str = Field(serialization_alias="_project")
    number_compat: str = Field(serialization_alias="_number")
    # No `lease_held` field: nothing reads it (`app/slot_state.py::read` answers it directly).
    # The rest of the sensed record, so readers need no raw KV payload.
    dirty: Optional[bool] = None
    merged: Optional[bool] = None
    session: Optional[bool] = None
    session_id: Optional[str] = None
    session_status: Optional[str] = None
    console: Optional[str] = None
    account: Optional[str] = None
    window_name: Optional[str] = None
    # Resolved CP-side, never null nor `""` — the same `slot_title.resolved_title` the console's
    # `WorktreeOut` applies, so the two payloads cannot disagree. REQUIRED (`str`, no default).
    # `window_name` stays on the wire and UNREAD.
    title: str
    task_id: Optional[int] = None
    stack_state: Optional[str] = None
    observed_at: Optional[str] = None
    last_used: Optional[str] = None
    idle_hours: Optional[int] = None
    probes: Optional[dict[str, Any]] = None
    ports: Optional[dict[str, Any]] = None
    host_env: Optional[dict[str, Any]] = None
    # `ahead` IS LOAD-BEARING: self-heal's `classify_stuck` discriminates its only live class (C2) on
    # `not dirty and ahead > 0 and not merged`; unserialized it reads `None` and self-heal silently stops
    # killing stuck runners. `behind`, `session_started_at`, `tmux_target` feed the other predicates.
    ahead: Optional[int] = None
    behind: Optional[int] = None
    session_started_at: Optional[str] = None
    tmux_target: Optional[str] = None
    # The hold's launch generation (bumped per lease and redispatch): the `kill_runner` executor's fence against
    # an intent minted for a predecessor launch (`jaahd/reconcile/enact_signal.py::_enact_kill_runner`).
    generation: Optional[int] = None


@router.get("/worktrees", response_model=list[AgentWorktreeOut])
@infra(Tenancy.OPERATOR)
def agent_worktrees(
    project: str = Query(default=""),
    project_id: Optional[int] = Query(default=None),
    node_id: str = Query(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    x_agent_task_id: str = Header(default=""),
    session: Session = Depends(get_session),
) -> list[AgentWorktreeOut]:
    """The pool roster the dispatcher and node readers pick from, narrowed to what the caller is.

    `project_id` (or, from an older daemon, `project` — a name inside the caller's org, `_wire_project`) is
    optional; unresolvable → 404, not `[]` (Console reads `[]` as "no live session"). `node_id` is the node
    uuid (`app/node_identity.py`).

    The node is derived, not filtered: a node credential confines the roster to that node, and naming
    another is `caller_node_ref`'s 403. CP-self and insecure arms get an ordinary filter, so the route stays
    `OPERATOR` (`test_authz_policy._INFRA_OPERATOR_PLANE_ALLOWLIST`). A numeric `X-Agent-Task-Id` (stamped
    by the daemon, `cred_broker._perform_call`) confines it further to that task's slots; defence in depth.
    """
    idle_expr = slot_state.idle_hours_expr()
    # The node's NAME for the served field AND the tie-break — a correlated probe, not a join, so the
    # existing outer joins keep their multiplicity.
    node_name = name_expr(Worktree.node_id).label("node_name")
    query = (
        select(
            Worktree,
            idle_expr.label("idle_joined"),
            # Title-resolution join, identical to `routes/portfolio.py`'s. LEFT: `worktrees.task_id`
            # carries no FK, and an inner join would drop a slot dangling at a deleted task.
            Task.state.label("task_state"),
            Task.title.label("task_title"),
            # Display name through the FK, not a stored copy. INNER: `worktrees.project_id` is NOT NULL
            # with `ON DELETE CASCADE`, so nothing is dropped and the wire cannot carry a NULL `project`.
            Project.name.label("project_name"),
            node_name,
            # The credential dir is DERIVED from `account_id` (FK). Two correlated probes, not joins: the
            # statement already carries three outer joins. `resolve_auth_dir` composes them below.
            account_slug_expr(Worktree.account_id).label("account_slug"),
            jaah_home_expr(Worktree.node_id).label("slot_home"),
        )
        .join(Project, Project.id == Worktree.project_id)
        .outerjoin(Task, Task.id == Worktree.task_id)
        # `node_id` completes the sort key: (project, number) is NOT unique, and an unfiltered
        # single-slot reader would otherwise pick a different node's row per call. Ordered by NAMES:
        # humans read this through `ctl.py worktrees`, and a uuidv7 sorts by registration time.
        .order_by(Project.name, Worktree.number, node_name)
    )
    if project_id is not None or project:
        project_row = _wire_project(session, caller, project_id, project)
        if project_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown project {project_id if project_id is not None else repr(project)}",
            )
        query = query.where(Worktree.project_id == project_row.id)
    # 403 on a `node_id` naming a node other than the credential's; the PROVEN value is `caller.node_id`.
    caller_node_ref(session, caller, node_id)
    if caller.node_id is not None:
        query = query.where(Worktree.node_id == caller.node_id)
    elif node_id:
        # `node_ref` resolves INSIDE the statement: a value naming no node's uuid yields NULL and
        # `col = NULL` matches nothing (fail-CLOSED). A Python `None` would render `IS NULL` and answer the WHOLE fleet.
        query = query.where(Worktree.node_id == node_ref(node_id))
    declared_task = x_agent_task_id.strip()
    if _CALLER_TASK_ID_RE.fullmatch(declared_task):
        # `task_id` is NULL on `free` and `user` rows (`ck_worktrees_state_ownership`), so those never match.
        query = query.where(Worktree.task_id == int(declared_task))
    out: list[AgentWorktreeOut] = []
    for row in session.execute(query).all():
        wt = row[0]
        out.append(
            AgentWorktreeOut(
                node_id=row.node_name,
                project_id=wt.project_id,
                project=row.project_name,
                number=wt.number,
                path=wt.path,
                branch=wt.branch,
                state=wt.state,
                last_active=iso_or_none(wt.last_active),
                has_repo=wt.has_repo,
                # String compatibility pair — see the model. Still the NAME.
                project_compat=str(row.project_name),
                number_compat=str(wt.number),
                dirty=wt.dirty,
                merged=wt.merged,
                session=wt.session,
                session_id=wt.session_id,
                session_status=wt.session_status,
                console=wt.console,
                # The answer the CP would give that node for that account, not the node's last spelling.
                # `""` (unresolved account, or no declared home) maps to None.
                account=resolve_auth_dir(row.slot_home, row.account_slug) or None,
                window_name=wt.window_name,
                # `resolved_title`'s OUTPUT. The INPUT is the OPINION, not the raw `name`, whose derived
                # value would out-rank the task branch; `routes/portfolio.py::_wt_out` must match.
                title=slot_title.resolved_title(
                    slot_title.stored_opinion(
                        wt.name,
                        default_title=wt.default_title,
                        default_name=slot_title.default_name(row.project_name, wt.number),
                    ),
                    task_active=slot_title.task_active_from_state(row.task_state),
                    task_id=wt.task_id,
                    task_title=row.task_title,
                ),
                task_id=wt.task_id,
                # DERIVED from this row's SENSED columns; the stored `stack_state` column has no writer
                # (`app/slot_stack.py`). `observed_at` bounds freshness: without it a dead daemon keeps
                # reporting `"up"`.
                stack_state=slot_stack.stack_state_for(
                    stack_present=wt.stack_present,
                    stack_up=wt.stack_up,
                    probes=wt.probes,
                    observed_at=wt.observed_at,
                ),
                observed_at=iso_or_none(wt.observed_at),
                last_used=iso_or_none(wt.last_used),
                idle_hours=row.idle_joined,
                probes=wt.probes,
                ports=wt.ports,
                host_env=wt.host_env,
                ahead=wt.ahead,
                behind=wt.behind,
                session_started_at=iso_or_none(wt.session_started_at),
                tmux_target=wt.tmux_target,
                generation=wt.generation,
            )
        )
    return out
