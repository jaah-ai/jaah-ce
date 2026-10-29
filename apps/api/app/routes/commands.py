"""Command dispatch — request/reply over the WebSocket node bus.

`POST /api/commands` CP-validates, resolves DB-derived args (`path`/`clone_path`), then dispatches
synchronously to the target node's daemon (`apps/jaahd`), which executes via its command allowlist
and its own origin check (`apps/jaahd/commands.py`'s `_serve_one`). Occupancy is `worktrees.state`;
there is no holder vocabulary to guard on. The reply is returned directly (no queued state, no polling),
and a completed dispatch INSERTs one `command_audit_log` row in the caller's session (`_record_audit`),
read by `GET /api/command-log`.

Transport: the node's persistent WebSocket (`app/node_bus.py`), as a correlated `cmd` frame."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import (
    account_auth, agent_providers, console_direct, cp_peers, edition, forges, identities, ce_caps, node_bus,
    project_identity, provider_auth_policy,
)
from app.account_slug import new_account_slug
from app.auth import require_user
from app.authz.declarations import feature_any
from app.authz.object_access import (
    OWNERSHIP_PERSONAL,
    can_access,
    require_object_access,
    stamp_owner_on_create,
    validated_ownership,
)
from app.authz.principal import Principal, creates_org_rows, current_principal
from app.authz.visibility import ObjectAccess, sees_all_nodes, worktree_editable_clause
from sqlalchemy.exc import TimeoutError as SQLTimeoutError

from app.db import SessionLocal, get_session, release_connection
from app.events import emit_console_event
from app.login_seal_wire import (
    CHUNK_CIPHERTEXT_BYTES,
    SEAL_KEY_MAX_CHARS,
    SEALED_MAX_CHARS,
    is_wire_key,
    is_wire_sealed,
)
from app.models.audit import CommandAudit
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import Project
from app.models.worktrees import Worktree
from app.node_clone import clone_for_slot_path, resolve_auth_dir, resolve_clone
from app.node_identity import (
    bus_key, cp_node_id, is_uuid_shaped, node_id_or_none, node_matches, node_ref, resolve_node_id,
)
from app.shared_storage import resolve_shared_auth_dir
from app.org_gate import require_org_active
from app.tenancy import seam
from app.tenancy.copy import not_available
from app.command_read import command_read
from app.tenancy.ids import ORG_ZERO_ID, is_tenant_org

router = APIRouter(prefix="/api/commands", tags=["commands"])
log = logging.getLogger("cockpit-commands")

# Bounds the round trip so a hung node never hangs the request. Generous: some types (stack_up,
# account_add) do real provisioning (`apps/jaahd/commands.py` records the tradeoff).
COMMAND_TIMEOUT = float(os.environ.get("COMMAND_TIMEOUT", "180"))

# Types that name a slot and so need `project_id` + `number` — input validation, not policy. They are also
# checked by `_require_editable_slot`.
SLOT_ARG_TYPES = {"release", "retire", "set_title"}
VALID_TYPES = SLOT_ARG_TYPES | {
    # `create` materialises a new slot's tree (names no existing slot); `reconcile` re-observes one slot
    # or, with neither arg, the whole pool.
    "create", "reconcile",
    "check",
    "stack_up", "stack_down", "stack_restart", "stack_rebuild",
    "logs", "sync_develop", "open_finder", "open_cursor", "run",
    "rename_tmux",
    # Account lifecycle and re-auth: host-local account registry and credential dir, not a slot.
    "account_add", "account_add_submit_code", "account_delete",
    "account_reauth", "account_reauth_submit_code",
    # A shared account's token leaf on the EFS share, which `account_delete` never reaches; the node resolves
    # the path from the slug itself. Sent by tenant offboarding (`app/tenancy/offboard.py`).
    "account_shared_delete",
    # The managed layer for one account on this node: a shared account authenticates everywhere once its
    # token lands on the share, but only the login node has converged its settings, so the CP fans this
    # out (`_converge_shared_account_fleet_wide`; node: `apps/jaahd/accounts.py::handle_account_command`).
    "account_converge",
    # The `await_credentials` twin of `*_submit_code`: a device-auth provider's CLI completes on disk
    # once the operator approves, and these verbs wait for the credentials and finalize. Re-callable: a
    # closed poll window answers `awaiting_redirect` (exit 0).
    "account_add_await_login", "account_reauth_await_login",
    # Re-install a clone's pre-push guard at the DB's branch prefix and push posture settings; the guard
    # reads markers in the clone's git config, so a DB edit is inert until this lands.
    "sync_access_guard",
    # "Update this node now". No args and no `_resolve_command_args` arm: the node reads the target
    # version itself (`GET /api/agent/release-desired`), so no version string reaches a root `apt-get`.
    "node_update",
    # "Converge this node's self-hosted GitHub Actions runners now". No args, as `node_update`: the node
    # reads its desired state (`GET /api/agent/gha-runners/desired`, including a live registration token)
    # itself, so no operator input reaches the root `install.py` and no secret sits in a bus frame or the
    # audit log.
    "gha_runners_apply",
    # "Converge this node's NodeLink mount now". No args, as `gha_runners_apply`: the node reads its desired
    # store (`GET /api/agent/nodelink`) itself. Sent by `app/nodelink.py::request_converge`.
    "nodelink_converge",
}

# ── WHO MAY RUN WHICH COMMAND — the per-`command_type` permission map ────────────────────────────────

# `@feature_any("worktrees:edit", "system:create")` is the floor the kernel enforces; this table is the
# narrowing it cannot express. A disjunction because the two keys reach different verbs of one POST.

# Both halves are load-bearing: without `_require_command_key` a `worktrees:edit` holder reaches `retire`;
# widen the declaration and this table is the only barrier. The authz registry test pins both.

# One route (settled decision 5); `worktrees:view` grants none of these (settled decision 7). Fails closed:
# an unmapped `command_type` resolves to `_SYSTEM_ONLY`.
_WORKTREE_OR_SYSTEM = frozenset({"worktrees:edit", "system:create"})
_SYSTEM_ONLY = frozenset({"system:create"})

# A per-op narrowing on `run`: `ports_reband` rewrites every slot's `.env` and restarts stacks, and its
# dedicated route (`POST /api/port-grid/reband`) is superadmin, so this generic verb must not be weaker.
_RUN_OP_SUPERADMIN_ONLY = frozenset({"ports_reband"})

# A per-org narrowing on the pool verbs: `retire`/`create`/`reconcile` shape the operator's pool,
# never anything a tenant owns — yet `system:create` is grantable to a tenant admin, and a tenant-born
# superadmin holds every key. Keyed on the org (`is_tenant_org`), not `is_operator`, so the operator's
# non-superadmin staff keep their fleet. `node_update` and `gha_runners_apply` are not here: they converge
# the target node only, and the node WRITE probe below confines a tenant to their own org's nodes.
_OPERATOR_ORG_ONLY = frozenset({"retire", "create", "reconcile"})

COMMAND_TYPE_KEYS: dict[str, frozenset[str]] = {
    # ── the worktree tier: `worktrees:edit` OR `system:create` ──────────────────────────────────────
    # Ending a hold.
    "release": _WORKTREE_OR_SYSTEM,
    # The slot's docker stack. Mutates the worktree's own environment and nothing outside it.
    "stack_up": _WORKTREE_OR_SYSTEM,
    "stack_down": _WORKTREE_OR_SYSTEM,
    "stack_restart": _WORKTREE_OR_SYSTEM,
    "stack_rebuild": _WORKTREE_OR_SYSTEM,
    # Git + shell conveniences scoped to the one worktree the caller is looking at.
    "sync_develop": _WORKTREE_OR_SYSTEM,
    "logs": _WORKTREE_OR_SYSTEM,
    "open_finder": _WORKTREE_OR_SYSTEM,
    "open_cursor": _WORKTREE_OR_SYSTEM,
    "rename_tmux": _WORKTREE_OR_SYSTEM,

    # ── everything else: `system:create` ────────────────────────────────────────────────────────────
    # `set_title` relabels one driveable slot, so it rides the worktree tier (as
    # `PUT /api/worktrees/{project}/{number}/title`). `retire`, `create` and `reconcile` shape what the
    # fleet may dispatch onto, so they stay system-only (releasing only ends a hold).
    "set_title": _WORKTREE_OR_SYSTEM,
    "retire": _SYSTEM_ONLY,
    "create": _SYSTEM_ONLY,
    "reconcile": _SYSTEM_ONLY,
    # Repo-wide operations against a project's clone, not against any one worktree.
    "check": _SYSTEM_ONLY,
    # `run` executes an arbitrary declared op, so it stays admin-only.
    "run": _SYSTEM_ONLY,
    # Account lifecycle: host-local registry and OAuth credentials; unreachable by `worktrees:edit`.
    "account_add": _SYSTEM_ONLY,
    "account_add_submit_code": _SYSTEM_ONLY,
    # The redirect-mode continuation of the same two flows, so the same key.
    "account_add_await_login": _SYSTEM_ONLY,
    "account_reauth_await_login": _SYSTEM_ONLY,
    "account_converge": _SYSTEM_ONLY,
    "account_delete": _SYSTEM_ONLY,
    "account_shared_delete": _SYSTEM_ONLY,
    "account_reauth": _SYSTEM_ONLY,
    "account_reauth_submit_code": _SYSTEM_ONLY,
    # Portfolio-level: re-installing a clone's push guard.
    "sync_access_guard": _SYSTEM_ONLY,
    # Converges the node's own package — the most system-shaped verb.
    "node_update": _SYSTEM_ONLY,
    # Converges the node's runner pool (root-side). Its own route is `nodes:edit` plus a WRITE grant; this
    # covers the generic POST.
    "gha_runners_apply": _SYSTEM_ONLY,
    # Converges the node's NodeLink mount (root-side). Its own trigger is `PUT /api/nodes/{id}`'s switch; this
    # covers the generic POST.
    "nodelink_converge": _SYSTEM_ONLY,
}


# The account tier. CONTRACT: every `account_*` verb names its account under `args.slug` — a new one that
# names it elsewhere would skip `_require_account_access` silently. Keyed on `"account_"`.
#: Verbs only an in-process caller sends (`app/tenancy/offboard.py`); the raw bus refuses them outright.
_INTERNAL_ONLY = frozenset({"account_shared_delete"})

_ACCOUNT_TIER_COMMANDS = frozenset(key for key in COMMAND_TYPE_KEYS if key.startswith("account_"))


# Derived from the table, never typed twice. Membership, not identity: `keys is _WORKTREE_OR_SYSTEM` would
# miss an equal fresh frozenset, granting reach without the slot requirement.
WORKTREE_TIER_TYPES = frozenset(
    command_type
    for command_type, keys in COMMAND_TYPE_KEYS.items()
    if "worktrees:edit" in keys
)

# The `args` a worktree-tier command may never supply: `path` comes from `(project_id, number)`, the two
# stack commands from the project row — CP decisions; `holder` and `config_dir` are node inputs no caller
# may name.

# The stack commands run under `bash -lc` and only a superadmin may author them (`routes/projects.py`).
# Defence in depth: `_resolve_command_args` already pops both on every path.

# Kept even where the marginal escalation is small, because reviewers treat these verbs as scoped.

# Stripped, not rejected (a field the caller cannot influence is not an error), and in the handler rather
# than `_resolve_command_args`, which internal dispatchers also reach.
_CALLER_MUST_NOT_SUPPLY = ("path", "holder", "config_dir", "stack_up_cmd", "stack_down_cmd")

# The only args a session may supply on either phase-1 login verb — an allowlist. `shared` is absent: the
# CP re-adds it for a new `account_add` on a node that reports a mounted token share (an old `jaahd`'s
# override; a current one ignores it). The two arms stay separate because only `account_add` re-adds `shared`. `_test` is live (the node and
# `routes/portfolio.py` read it). `provider` is validated and re-injected by `_with_resolved_provider`,
# so its membership is currently redundant — kept as the declaration of what a session may send.
# `peer_key` is the wizard's per-login public key. `with_code` is retired: a caller still
# sending it is neither refused nor relayed.
_ACCOUNT_PHASE1_CALLER_ARGS = frozenset({"slug", "peer_key", "_test", "provider"})

# The Add-Account wizard's three verbs, which a non-administrator may also run to bring their OWN account
# (G2): `accounts:create` held anywhere — a Developer or Maintainer — admits them on the PERSONAL path
# (`_personal_account_path`), which makes the account theirs, confines an enrolment and a continuation to
# their own login, and leaves the org's accounts untouched. Every other account verb stays system-only.
_PERSONAL_ACCOUNT_VERBS = frozenset({"account_add", "account_add_submit_code", "account_add_await_login"})
_ACCOUNTS_CREATE_KEY = "accounts:create"
# The verbs' own key (`_SYSTEM_ONLY`), named for the one question the personal path asks of it.
_SYSTEM_CREATE_KEY = "system:create"


def _personal_account_path(
    session: Session, principal: Principal | None, command_type: str, args: dict[str, Any]
) -> bool:
    """Admit this caller on the Add-Account wizard's PERSONAL path? One of `_PERSONAL_ACCOUNT_VERBS`, held
    `accounts:create` (anywhere — a Developer or Maintainer, G2) and not the verb's own org-wide
    `system:create`. Admitting conjoins the account object floor (`_require_account_access`) and confines
    an existing slug to the caller's own account (`_require_own_account_slug`); the caller's login is
    confined later (`_require_own_login`) and the node is probed like every verb's."""
    if command_type not in _PERSONAL_ACCOUNT_VERBS or principal is None:
        return False
    if principal.has_org(_SYSTEM_CREATE_KEY) or not principal.has(_ACCOUNTS_CREATE_KEY):
        return False
    _require_account_access(session, principal, command_type, args)
    _require_own_account_slug(session, principal, args)
    return True


def _require_command_key(
    command_type: str, principal: Principal | None, *, personal_path: bool = False
) -> None:
    """The per-`command_type` half of this route's gate — the narrowing the declaration cannot express.

    Any mapped key passes; both `Principal.has` and `has_org` are True for a superadmin. A worktree-tier
    verb asks the floor (`has`): `_require_editable_slot` confines it to one slot afterwards. Every other
    verb shapes the pool, a node, an account or a project's clone beyond any one slot, so it WIDENS and
    asks `has_org` — a key held on one project never opens them. `principal is None` (allow-all in
    `app/authz/`) is explicitly denied, so a future agent-token path cannot open the bus. An unmapped
    `command_type` resolves to `_SYSTEM_ONLY`."""
    required = COMMAND_TYPE_KEYS.get(command_type, _SYSTEM_ONLY)
    if principal is not None:
        holds = principal.has if command_type in WORKTREE_TIER_TYPES else principal.has_org
        if any(holds(key) for key in required):
            return
        if personal_path and command_type in _PERSONAL_ACCOUNT_VERBS:
            # Admitted and confined by `_personal_account_path`.
            return
    raise HTTPException(
        status_code=403,
        detail=f"forbidden: {command_type} requires one of {sorted(required)}",
    )


# One constant detail for all three refusals below (unknown project, unknown slot, not drivable), so the
# route is no slot-existence oracle.
_NO_SUCH_SLOT = "no such worktree slot"


def _require_editable_slot(
    session: Session,
    principal: Principal | None,
    node_id: str,
    project_id: int,
    number: int,
) -> None:
    """The per-slot half of a worktree verb's gate: this caller may drive this slot, or 404.

    Applies to the derived `WORKTREE_TIER_TYPES`. No feature key bypasses it (`worktree_editable_clause`
    reads grants and slot ownership, not `system:create` or `WIDE_WORKTREE_KEYS`); a superadmin does.
    Node-scoped, since `(project_id, number)` is not unique across nodes. Every miss (unknown project —
    `_require_project` answers it with this same detail — unknown slot, not drivable) is 404
    `_NO_SUCH_SLOT`: fail-closed and non-disclosing. Runs after the 422s.

    The clause needs `Worktree` in the enclosing `select_from` (as `authz.can_access`); a bare select
    cross-joins and is a silent allow-all. Selected as a column so both refusals share one round trip."""
    row = session.execute(
        select(worktree_editable_clause(principal).label("may_edit"))
        .select_from(Worktree)
        .where(
            # Resolved in-statement: `node_id` is the node uuid and the column a uuid FK. Fails closed
            # (unresolvable → NULL → no row → 404).
            Worktree.node_id == node_ref(node_id),
            Worktree.project_id == project_id,
            Worktree.number == number,
        )
    ).first()
    if row is not None and row.may_edit:
        return
    raise HTTPException(status_code=404, detail=_NO_SUCH_SLOT)


# Account-add slug format — validated here (fast fail) AND authoritatively in the agent (T7).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
#: `_SLUG_RE` as the refusal a form shows (`routes/accounts.py`, `routes/orgs.py`).
SLUG_RULE = "slug must be lowercase letters/digits/-/_, start alphanumeric, max 32 chars"
# The sealed-login wire shapes are `app/login_seal_wire.py`'s: the CP never sees a pasted
# code any more (the browser seals it to the node's per-login key), so shape and bound are the only
# checks this route can make. The node applies its own `_OAUTH_CODE_RE` to what it unseals.
# Commands needing a worktree `path` resolved from the CP's Worktree cache.
_PATH_COMMANDS = {
    "stack_up", "stack_down", "stack_restart", "stack_rebuild", "run",
    "logs", "sync_develop", "open_finder", "open_cursor",
    "rename_tmux",
}


class CommandRequest(BaseModel):
    command_type: str
    args: dict[str, Any]
    node_id: Optional[str] = None


class CommandOut(BaseModel):
    id: int
    node_id: str
    command_type: str
    args: dict[str, Any]
    status: str
    result: Optional[dict[str, Any]]


def _as_int(value: Any) -> Optional[int]:
    """`value` as an int, or None if it isn't one — so a non-numeric caller field resolves to "no
    project", never a 500."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_command_args(
    command_type: str, args: dict[str, Any], session: Session, node_id: str,
) -> dict[str, Any]:
    """Inject the CP-decided fields, per command family: the project's name beside its id, worktree
    `path`, the stack command pair for the four stack verbs, `base` + `clone_url` for `sync_develop`, `clone_path` +
    `gate_cmd` for `check`, `config_dir` for `account_delete` (from the `accounts` row by `slug`), and
    `repo_root` + `branch_prefix` + `push_enabled` for `sync_access_guard`.

    A project is named by `args["project_id"]` only; a name never resolves to a row (names are unique
    per org, not fleet-wide). The resolved row's name is forwarded as `project` for daemons that still
    match their roster by name; an id naming no row strips any caller `project`/`repo` so a stale name
    cannot steer the node, and every injection below then misses (the node fails closed).

    Clone paths are computed for `node_id` from `Node.jaah_home` plus the project's id
    (`app/node_clone.py`)."""

    def _clone_for(project_id: Optional[int]) -> str:
        # The main clone's STORED path first — where the node last reported it — else `resolve_clone`'s.
        node = session.scalar(select(Node).where(node_matches(node_id)))
        jaah_home = node.jaah_home if node else None
        stored = session.scalar(
            select(Worktree.path).where(
                Worktree.node_id == (node.id if node else None),
                Worktree.project_id == project_id,
                Worktree.number == 0,
            )
        ) if node is not None and project_id is not None else None
        return clone_for_slot_path(jaah_home, stored, 0) or resolve_clone(jaah_home, project_id)

    out = dict(args)
    project: Optional[Project] = None
    if out.get("project_id") is not None:
        project_id = _as_int(out.get("project_id"))
        if project_id is not None:
            project = session.scalar(select(Project).where(Project.id == project_id))
        out.pop("project", None)
        out.pop("repo", None)
        if project is not None:
            out["project_id"] = project.id
            out["project"] = project.name
    if command_type in _PATH_COMMANDS:
        number = out.get("number")
        if project is not None and number is not None and "path" not in out:
            # `node_id` is part of the filter: `(project_id, number)` alone is not unique. A miss injects
            # nothing and the node's own refusal answers — this step is injection, not validation.
            path = session.scalar(
                select(Worktree.path)
                .where(
                    Worktree.node_id == node_ref(node_id),
                    Worktree.project_id == project.id,
                    Worktree.number == int(number),
                )
            )
            if path:
                out.setdefault("path", path)
        if command_type == "sync_develop":
            # The integration branch is a Project column, always overwritten: a surviving caller `base`
            # would be a rebase target the caller chose. A miss leaves it absent; the node fails closed.

            # The pop is outside the `project is not None` test, or omitting the project would keep the
            # caller's `base`.
            out.pop("base", None)
            # The remote likewise: the node fetches from this URL, never the tree's `origin`. Absent when the
            # project has no repository reference; the node fails closed.
            out.pop("clone_url", None)
            if project is not None:
                out["base"] = project.integration_branch
                ref = forges.repo_ref_or_none(project)
                if ref is not None:
                    out["clone_url"] = ref.clone_url()
        if command_type in ("stack_up", "stack_down", "stack_restart", "stack_rebuild"):
            # The stack command lines are Project columns, and the node has no default of its own. Both
            # ride all four verbs (`stack_restart`/`stack_rebuild` need down and up).

            # Pops outside the `project` test: these strings are executed, so a surviving caller value
            # would be remote code execution. A miss leaves both absent; `stack_ops.handle_stack` fails
            # closed.
            out.pop("stack_up_cmd", None)
            out.pop("stack_down_cmd", None)
            if project is not None:
                out["stack_up_cmd"] = project.stack_up_cmd
                out["stack_down_cmd"] = project.stack_down_cmd
    elif command_type == "check":
        # The clone path comes from the Project row, always overwritten. `repo_root` is popped too (the node
        # handler reads it as its source), and `gate_cmd` is a Project column.
        out.pop("clone_path", None)
        out.pop("repo_root", None)
        out.pop("gate_cmd", None)
        if project is not None:
            clone = _clone_for(project.id)
            if clone:
                out["clone_path"] = clone
            out["gate_cmd"] = project.gate_quick_cmd
    elif command_type in ("account_delete", "account_converge"):
        # The node removes credentials at this path — the highest-consequence injection. Derived for the
        # target node (`resolve_auth_dir(<that node's jaah_home>, slug)`).

        # Not always overwritten: an explicit `config_dir` with no `slug` is a supported shape (the
        # deprovision routes). Naming a slug forfeits naming a path, and every miss (no row, pinned
        # elsewhere, unregistered target, no Jaah home) leaves the key absent so the node refuses.

        # Removes the on-disk dir only; `POST /api/accounts/{id}/deprovision` removes both.
        slug = out.get("slug")
        if slug is not None:
            out.pop("config_dir", None)
            if isinstance(slug, str) and slug:
                # Compared in uuid space: the wire `node_id` is the node uuid, as `Account.node_id`.
                # Unresolvable → None → matches no pinned row (fail closed).
                row = session.execute(
                    select(Account.node_id, Account.shared).where(Account.slug == slug)
                ).first()
                requested_node = node_id_or_none(session, node_id)
                # A shared account's pin does not confine `account_converge` (its purpose is converging
                # other nodes' per-node dirs, `app/shared_storage.py`); `account_delete` keeps the
                # confinement.
                waive_pin = command_type == "account_converge" and row is not None and bool(row.shared)
                if row is not None and (
                    waive_pin or row.node_id is None or row.node_id == requested_node
                ):
                    # The target node's own home, never the pin's.
                    config_dir = resolve_auth_dir(
                        session.scalar(select(Node.jaah_home).where(node_matches(node_id))), slug,
                    )
                    if config_dir:
                        out["config_dir"] = config_dir
    elif command_type == "sync_access_guard":
        # Same injection and allowlist as `check`. `branch_prefix`/`push_enabled` decide what the clone may
        # push, so a caller pair is never kept; the node fails closed on either absent.
        out.pop("repo_root", None)
        out.pop("branch_prefix", None)
        out.pop("push_enabled", None)
        # Popped for the same reason; the clone's refusal messages name it as the PR base.
        out.pop("integration_branch", None)
        if project is not None:
            clone = _clone_for(project.id)
            if clone:
                out["repo_root"] = clone
            out["branch_prefix"] = project.branch_prefix
            out["push_enabled"] = project.push_enabled
            out["integration_branch"] = project.integration_branch
    return out


async def _dispatch(
    node_id: str, command_type: str, args: dict[str, Any], *, bus_target: str,
    timeout: float = COMMAND_TIMEOUT,
) -> dict[str, Any]:
    """Request/reply dispatch to the target node over the WebSocket node bus (`app/node_bus.py`).
    Returns the daemon's parsed reply `{"status":..., "result":...}`.

    `bus_target` is the machine key the socket is registered under (resolved by the caller);
    `node_id` is the value every log line and refusal quotes. 503 (not connected, retryable) vs 504
    (connected but silent, maybe mid-execution) — so a timeout is never retried."""
    # The third CP→node frame family (with `console_open` and the console-direct grant/revoke), all
    # re-checked by `apps/jaahd/node_frame_auth.py`. The proof is the only credential on the frame; an
    # empty one refuses every command.
    envelope = {
        "v": 1,
        # Keyed on the recipient the frame is sent to (`bus_target`), as `_send_grant`/`_notify_revoke`,
        # so the proof and the address cannot resolve under different scopes.
        "cp_frame_proof": await console_direct.cp_frame_proof_for_node_async(bus_target),
        "command_type": command_type,
        "args": args,
    }
    try:
        reply = await node_bus.registry.request(bus_target, envelope, timeout=timeout)
    except node_bus.NodeNotConnected as exc:
        log.warning("command dispatch: %s is not on the node bus", node_id)
        raise HTTPException(
            status_code=503, detail=f"node '{node_id}' is not connected (no command responder)",
        ) from exc
    except node_bus.NodeRequestTimeout as exc:
        log.warning("command dispatch: node %s did not reply within %ss", node_id, timeout)
        raise HTTPException(
            status_code=504, detail=f"node '{node_id}' did not respond in time",
        ) from exc
    return _parse_reply(reply)


def _parse_reply(reply: Any) -> dict[str, Any]:
    """Parse and shape-check a node reply, identically for both transports."""
    try:
        parsed = json.loads(reply.data or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="node returned a malformed reply") from exc
    if not isinstance(parsed, dict) or "status" not in parsed:
        raise HTTPException(status_code=502, detail="node returned a malformed reply")
    return parsed


def _audit_org(session: Session, resolved_node_id: Optional[UUID], actor: str) -> Optional[UUID]:
    """The org a `command_audit_log` row belongs to: its node's, else the org bound to this context, else the
    operator's who asked.

    `_record_audit`'s `org_id=` comment says why. Both reads go through `__table__` so the read seam does
    not filter the derivation (as `fleet_verdicts._persist_intents`)."""
    if resolved_node_id is not None:
        node_org = session.execute(
            select(Node.__table__.c.org_id).where(Node.__table__.c.id == resolved_node_id)
        ).scalar()
        if node_org is not None:
            return node_org
    # A bound org is the only one the write seam accepts: an operator switched into a tenant is a member of
    # several orgs, and their oldest membership is not the one acting.
    bound = seam.bound_org()
    if bound is not None:
        return bound
    # `actor` is an email: the oldest membership of that person (`identities.first_membership`).
    member = identities.first_membership(session, actor)
    return member.org_id if member is not None else None


def _record_audit(
    session: Session, node_id: str, command_type: str, args: dict[str, Any], status: str,
    result: Optional[dict[str, Any]], actor: str, *, commit: bool = True,
) -> int:
    """INSERT one `command_audit_log` row, in the caller's session, and return its id.

    `CommandAudit`'s only writer; `GET /api/command-log` reads it. Not best-effort: exceptions propagate,
    so no mutation is reported successful unaudited.

    Commits by default, because `get_session` does not commit on teardown and `_ensure_account_row`
    rolls back on two branches. `commit=False` is for a caller with its own open mutation —
    `routes/pool_slots.py::release_slot` passes it so this helper does not own its transaction. Either
    way the row is flushed and its event emitted before return."""
    resolved_node_id = node_id_or_none(session, node_id or "local")
    row = CommandAudit(
        # A nullable uuid FK; fails open (unresolvable value → NULL) so the audit row is never lost over a
        # lookup. `or "local"` resolves only if a `local` node is registered.
        node_id=resolved_node_id,
        # The node's org first, never overridden by the caller (as `fleet_verdicts._persist_intents`):
        # under `Tenancy.OPERATOR` nothing stamps this `Stored` row, and a refused create would lose the
        # audit of a destructive command. With no node, the bound org, then the actor's (`org_id` is NOT
        # NULL and the dispatch already ran) — attribution, not an ambient default. All empty → `None`.
        # Resolved in Python, not a `coalesce` of subqueries: `write_seam._apply` compares `org_id` on this
        # mapped instance, and a `ScalarSelect` there raises.
        org_id=_audit_org(session, resolved_node_id, actor),
        command_type=command_type,
        args=args or {},
        status=status,
        actor=actor,
        result=result,
    )
    session.add(row)
    # Flush, emit, commit: `pg_notify` is transactional, so the event fires exactly when the row becomes
    # visible — on the caller's commit under `commit=False`. One event per command.
    session.flush()
    emit_console_event(session, entity="command", id=row.id, action=status)
    if commit:
        session.commit()
    return row.id


# Bounds internal dispatches with no human waiting (`dispatch_and_audit_sync`). Short: a slow node is
# logged and moved past.
INTERNAL_DISPATCH_TIMEOUT = 5.0
# One node converging one account (`account_converge`): `provision_accounts.py --offline` takes seconds,
# beyond the 5s internal bound.
_CONVERGE_TIMEOUT = 120.0


# The family the two scrubbers below key on — a prefix, not a verb tuple, so a verb of the retired node-CLI
# credential wizard (no route sends one now) is still redacted, and an exemption is a visible edit.
_CREDENTIAL_OAUTH_PREFIX = "credential_oauth_"

# The code half's ceiling, single-homed here.
_ERROR_CODE_MAX_LEN = 64


def _credential_safe_args(command_type: str, args: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-bearing arg fields from every surface that outlives or leaves the request: the
    `command_audit_log` row and the 201 response body. The live dispatch still carries the real args.

    One field: the pasted code (`code`/`sealed_code`) of every paste-code verb — the two account
    `*_submit_code` verbs and the `credential_oauth_*` family, so a new OAuth verb is redacted by default.
    Pairs with `_credential_safe_result`, which scrubs the node's reply.

    The stack command pair is deliberately kept in the audit row (`_echo_safe_args` drops it from the
    201): it is the only record of what the fleet executed, non-secret by policy, and `system:view` is
    already trusted with every node's history. `gate_cmd` likewise. Never inline a credential in those
    columns."""
    if (
        command_type in ("account_add_submit_code", "account_reauth_submit_code")
        or command_type.startswith(_CREDENTIAL_OAUTH_PREFIX)
    ) and ("code" in args or "sealed_code" in args):
        # Nulled and marked, never dropped: in the audit row an absent key would hide that a code was
        # carried. `sealed_code` (the account verbs' live shape, ciphertext the CP cannot read) is
        # redacted exactly as the plaintext `code` was: the audit row is permanent and a later key leak
        # must not turn it into a code store. One marker for both spellings.
        redacted = {**args, "code_redacted": True}
        for key in ("code", "sealed_code"):
            if key in redacted:
                redacted[key] = None
        return redacted
    if command_type.startswith(_NODEVAULT_PREFIX):
        return _nodevault_redacted(args)
    if command_type == "trigger_preflight" and "input" in args:
        # The delivered event (up to 1 MiB of a sender's payload) and the state document: the deliveries log
        # is their record, not this permanent row. Marked, never dropped, as the paste-code arm.
        return {**args, "input": None, "input_redacted": True}
    return args


# The NodeVault verbs (`apps/jaahd/nodevault_commands.py`): their args and replies carry key blobs, secret
# envelopes and rewrapped data keys — ciphertext, but the epic's floor is that no wrapped-key blob reaches a
# log or an audit row. Hashes, ids, `held` and error words stay: they are the diagnostic.
_NODEVAULT_PREFIX = "nodevault."
_NODEVAULT_CIPHERTEXT_FIELDS = ("blob", "blobs", "items", "envelope")


def _nodevault_redacted(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with every ciphertext-bearing field nulled and `ciphertext_redacted: true` marked. Pure."""
    present = [field for field in _NODEVAULT_CIPHERTEXT_FIELDS if field in document]
    if not present:
        return document
    return {**document, **{field: None for field in present}, "ciphertext_redacted": True}


# A node's reply is node-chosen JSON, so the audit copy keeps only the fields a NodeVault verb answers with,
# each bounded — never a denylist over a shape the node controls.
_NODEVAULT_REPLY_FIELDS = ("exit_code", "error", "reason", "held", "identity_fp", "capable")
_NODEVAULT_WORD_CAP = 64


def _nodevault_safe_result(result: dict[str, Any]) -> dict[str, Any]:
    """The audit copy of a `nodevault.*` reply: the allowlisted fields, strings capped, `held` bounded; anything
    else dropped and marked `ciphertext_redacted: true`. Returns `result` itself when nothing changes. Pure."""
    kept: dict[str, Any] = {}
    for field in _NODEVAULT_REPLY_FIELDS:
        if field not in result:
            continue
        value = result[field]
        if isinstance(value, str):
            value = value[:_NODEVAULT_WORD_CAP]
        elif field == "held" and isinstance(value, list):
            value = [str(item)[:_NODEVAULT_WORD_CAP] for item in value[:_NODEVAULT_WORD_CAP]]
        elif not isinstance(value, (bool, int)) and value is not None:
            value = None
        kept[field] = value
    if set(result) - set(kept):
        kept["ciphertext_redacted"] = True
    return result if kept == result else kept


def _error_code_only(value: Any) -> Any:
    """A node failure string cut down to its code half — the text before its first colon, bounded.

    Readers already split `"<code>: <detail>"`, and the node's catch-all arm can put an arbitrary
    exception string in the detail, so this closes that fail-open at the seam. Non-strings pass through."""
    if not isinstance(value, str):
        return value
    return value.split(":", 1)[0].strip()[:_ERROR_CODE_MAX_LEN]


def _credential_safe_result(
    command_type: str, result: Optional[dict[str, Any]], *, echo: bool = False,
    provider: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """`_credential_safe_args`' missing symmetry — redact a credential-bearing node reply from every
    surface that outlives or leaves the request.

    `credential_oauth_*`: the reply's `stdout` is a vendor OAuth document (refresh/access/id tokens), so
    the whole `stdout` goes on both sinks — no key denylist over a shape we do not control, and every verb
    in the family. `exit_code` survives, `stderr` is cut to its code (`_error_code_only`).
    `account_add`/`account_reauth`: only the device-auth `user_code` goes (`_without_user_code`), from the
    audit row alone. The authorize URL rides sealed (`sealed_url`), ciphertext this CP cannot
    read, so neither sink has anything to anchor or redact there.

    `echo=True` is the 201 body, read only by the principal who pressed the button, who holds
    `system:create` by construction (`_SYSTEM_ONLY`; not `system:edit`) — the
    same reader `account_provisioning`'s `sees_code` serves; scrubbing it there made
    `Accounts.tsx::seedRecordFromReply` paint a false "nothing to paste" screen. `echo` cannot reach the
    credential-document arm. The requesting route gets the unscrubbed reply. Pure: returns `result`
    itself when nothing changes, never mutates it.
    """
    if not isinstance(result, dict):
        return result
    if command_type.startswith(_NODEVAULT_PREFIX):
        return _nodevault_safe_result(result)
    if command_type == "trigger_preflight" and "stdout" in result:
        # The script's answer is derived from the event `_credential_safe_args` keeps off this row; the
        # delivery's outcome is its record. Its length stays, for diagnosis.
        return {**result, "stdout": None, "stdout_bytes": len(str(result["stdout"] or "").encode())}
    if command_type in _ACCOUNT_LOGIN_VERBS:
        # The echo keeps the code, the audit row does not. No anchoring of the echo: the URL is sealed,
        # and the browser applies the vendor anchor (`AgentProviderOut.authorize_url_prefix`) to what it
        # unseals; the audit row records what the node said, unedited.
        return result if echo else _without_user_code(result)
    if not command_type.startswith(_CREDENTIAL_OAUTH_PREFIX):
        return result
    scrubbed = dict(result)
    if "stdout" in scrubbed:
        scrubbed["stdout"] = None
        scrubbed["stdout_redacted"] = True
    if "stderr" in scrubbed:
        scrubbed["stderr"] = _error_code_only(scrubbed["stderr"])
    return scrubbed if scrubbed != result else result


# The two login verbs whose reply carries a live login factor. A tuple, not an `account_` prefix, which
# would also reshape verbs that return no code.
_ACCOUNT_LOGIN_VERBS = ("account_add", "account_reauth")


def _stdout_payload(result: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The JSON object a command reply's `stdout` carries, or None when there is no stdout, it is not
    JSON, or it is not an object."""
    stdout = (result or {}).get("stdout")
    if not isinstance(stdout, str) or not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _without_user_code(result: dict[str, Any]) -> dict[str, Any]:
    """`result` with the device-auth `user_code` taken out of its JSON `stdout` — everything else intact.

    The caller still receives the unscrubbed reply; this guards `command_audit_log.result`, which is
    permanent, searchable and read by `system:view`. With both link and code there, any `system:view`
    holder could complete the pending device flow with their own account inside the window. A surgical
    removal, since the rest of the reply is the diagnostic. Pure (returns `result` itself when nothing
    is removed); a non-JSON or non-object `stdout` is left as is."""
    payload = _stdout_payload(result)
    if payload is None or "user_code" not in payload:
        return result
    payload.pop("user_code")
    scrubbed = dict(result)
    scrubbed["stdout"] = json.dumps(payload)
    scrubbed["user_code_redacted"] = True
    return scrubbed


def _echo_safe_args(command_type: str, args: dict[str, Any]) -> dict[str, Any]:
    """Drop `stack_up_cmd`/`stack_down_cmd` — and a CP-resolved `config_dir` — from the args echoed back
    in the 201 body. Echo-only: the audit row keeps both (`_credential_safe_args`), and the dispatch
    obviously must.

    The asymmetry is about who reaches each sink: the echo is reachable through the slot-edit clause,
    whose task arm (`editable_task_clause`) has no project arm, so a task-scoped `worktrees:edit`
    principal would read project command lines that `GET /api/projects` refuses them. Keys are dropped,
    not nulled or marked (matching an unresolved project). `config_dir` likewise names the account slug;
    this does not close `account_delete`'s existence oracle (still in `status`/`result`)."""
    if command_type in ("account_delete", "account_converge"):
        return {k: v for k, v in args.items() if k != "config_dir"}
    if command_type not in ("stack_up", "stack_down", "stack_restart", "stack_rebuild"):
        return args
    return {k: v for k, v in args.items() if k not in ("stack_up_cmd", "stack_down_cmd")}


def _config_dir_from_reply(result: Optional[dict[str, Any]]) -> Optional[str]:
    """The host's config dir out of an account command's own JSON stdout, or None if unusable.

    The CP (in a container with a different `$HOME`) cannot derive it. Untrusted stdout: anything
    malformed returns None, and no row is created."""
    payload = _stdout_payload(result)
    if payload is None or not isinstance(payload.get("config_dir"), str):
        return None
    return payload["config_dir"].strip() or None


def _securestorage_dir_from_reply(result: Optional[dict[str, Any]]) -> Optional[str]:
    """The credential store the node says this login was actually pointed at, out of a phase-1 reply's own
    JSON stdout — `""` for an ordinary local mint, None when the node said nothing usable.

    Asked because deciding and observing differ under deploy skew: an old `jaahd` ignores `shared: true`
    and mints locally, exit 0, so `accounts.shared` records where the token went (`_ensure_account_row`;
    cf. `apps/jaahd/accounts.py::_login_store_refusal`). `""` (a current node reporting local) and None
    (too old, or unparseable — worth a log line) both record local."""
    payload = _stdout_payload(result)
    if payload is None or not isinstance(payload.get("securestorage_dir"), str):
        return None
    return payload["securestorage_dir"].strip()


# The authorize URL's ceiling: node stdout is untrusted, and this string reaches `command_audit_log`, the
# durable `workflow_events` projection and a DOM href. 2048, not ~512, because a Google consent URL measures
# ~550 chars (pinned by `tests/test_commands_login_reply.py::_GOOGLE_CONSENT_URL`).
_OAUTH_URL_MAX_LEN = 2048


def _oauth_url_from_reply(
    result: Optional[dict[str, Any]], *, provider: Optional[str] = None,
) -> Optional[str]:
    """The plaintext authorization URL out of a login reply's own JSON stdout. Untrusted subprocess
    stdout: anything missing or malformed returns None.

    No route reads it today (its last reader, the node-CLI credential wizard, now retired). The account
    verbs (`account_add`/`account_reauth`) never read it: their login reports `sealed_url`
    (`durable/accounts.py::_sealed_login`), and a plaintext answer is refused upstream (`login_seal_unsupported`).

    Over-long fails closed to None rather than truncating (a truncated authorize URL loses its `state`
    and `code_challenge`). With a `provider`, the host is also anchored: the node's own scrape guards
    against its CLI, this guards against the node (rolled back, mis-provisioned, owned), since `safeHref`
    admits any http(s). A refusal is the ordinary `oauth_url_scrape_failed` path. `provider=None` and a
    provider declaring no prefix (Claude) skip the anchor — the roster decides.
    """
    payload = _stdout_payload(result)
    if payload is None or not isinstance(payload.get("oauth_url"), str):
        return None
    url = payload["oauth_url"].strip()
    if len(url) > _OAUTH_URL_MAX_LEN:
        return None
    # `authorize_url_admits`, not `startswith`: the match is the roster's fact, and for a constant
    # verification page it is equality — the only spelling that refuses a prefilled `?user_code=` tail or
    # a suffix-glued host.
    admits = agent_providers.authorize_url_admits(provider, url) if provider is not None else None
    if admits is False:
        # Never logs the URL (attacker-chosen text), only its length.
        log.warning(
            "a node reported a %d-character authorize URL that provider %r's declared anchor (%r) does "
            "not admit; publishing no URL, which surfaces as the ordinary scrape failure",
            len(url), provider, agent_providers.authorize_url_prefix(provider),
        )
        return None
    return url or None


# The publishable shape of a device-auth user code — the CP twin of
# `apps/jaahd/accounts.py::_USER_CODE_RE`, same bounds.
_USER_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")


def _user_code_from_reply(result: Optional[dict[str, Any]]) -> Optional[str]:
    """The device-auth user code out of a phase-1 account_add/account_reauth reply's own JSON stdout —
    the short string (shaped `ABCD-EFGHI`) an operator has to type into the page the authorize URL opens —
    or None when the reply names no such key at all.

    An RFC 8628 page approves nothing without it, so it travels with the URL. Absent is Claude and must
    stay byte-identical (no `or ""`). `""` means a device-auth adapter produced no usable code — a defect
    to show. Only the user code, never the `device_code`; never log it. Lives here because this module
    parses node replies; its consumer is `app/durable/accounts.py::_reported_user_code`."""
    payload = _stdout_payload(result)
    if payload is None or not isinstance(payload.get("user_code"), str):
        return None
    code = payload["user_code"].strip()
    # The CP's own bound, twin of the node's `_USER_CODE_RE`: an untrusted reply could otherwise render a
    # spoofing sentence under "Enter this one-time code" and write unbounded text to the event store.
    # Fails closed to `""`, not None: a malformed code comes from a device-auth adapter that ran, and None
    # would make the wizard falsely say "nothing to paste"; `""` renders `deviceCodeMissing`. The
    # malformed string itself is never published. `_keep_user_code_off_the_record` reads through this.
    if code and not _USER_CODE_RE.match(code):
        return ""
    return code


def _payload_status_from_reply(result: Optional[dict[str, Any]]) -> Optional[str]:
    """The node's own `status` out of an account command's JSON stdout, or None if unusable.

    `*_await_login` needs it: `awaiting_redirect` (still alive) and `done` (finalized) are both exit 0
    and dispatch status `done`, and confusing them either ends a live wizard or strands a finished one.
    Parsed by `_stdout_payload` like every reply reader."""
    payload = _stdout_payload(result)
    if payload is None or not isinstance(payload.get("status"), str):
        return None
    return payload["status"].strip() or None


#: The verbs that may name a project, always by `args.project_id` (an int, `projects.id`). A name is never
#: accepted: names are unique per org, not fleet-wide. Pinned by `tests/test_commands_args.py`.
_PROJECT_COMMANDS = WORKTREE_TIER_TYPES | SLOT_ARG_TYPES | {"create", "reconcile", "check", "sync_access_guard"}

_PG_INT4 = (-2_147_483_648, 2_147_483_647)


def _require_project(session: Session, command_type: str, args: dict[str, Any]) -> None:
    """The project half of the object floor (alongside the node check and `_require_account_access`): a
    named project must be one this caller's org can read, else 404.

    Read through the session, so the read seam narrows a tenant to its own org's rows: a foreign id and a
    nonexistent one answer alike (`project_identity.project_or_404`). Worktree-tier verbs answer with
    `_NO_SUCH_SLOT`, so the route stays no slot-existence oracle. A retired `project`/`repo` name without
    `project_id` is a 422 rather than "no project", which for `reconcile` would widen to the whole pool.
    A verb outside `_PROJECT_COMMANDS`, or a body naming no project, is not refused here; the verb's own
    arm decides whether a project is required."""
    if command_type not in _PROJECT_COMMANDS:
        return
    value = args.get("project_id")
    if value is None:
        if args.get("project") is not None or args.get("repo") is not None:
            raise HTTPException(
                status_code=422,
                detail="args.project and args.repo are retired; name the project by args.project_id",
            )
        return
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.isdigit())):
        raise HTTPException(status_code=422, detail="args.project_id must be an integer project id")
    detail = _NO_SUCH_SLOT if command_type in WORKTREE_TIER_TYPES else None
    project_id = int(value)
    # No value the column cannot hold reaches Postgres — that is "not found", not a 500.
    if not _PG_INT4[0] <= project_id <= _PG_INT4[1]:
        raise HTTPException(status_code=404, detail=detail or f"unknown project {project_id}")
    project_identity.project_or_404(session, project_id, detail)
    args["project_id"] = project_id


def _require_account_access(
    session: Session,
    principal: Principal | None,
    command_type: str,
    args: dict[str, Any],
) -> None:
    """The account half of the object floor — the command bus otherwise checked only the node.

    Without it `system:create` plus one node grant reached `account_delete` for any account on it. A
    missing row is not refused here: `account_add` on a new slug is a create (`stamp_owner_on_create`),
    and other verbs' handlers answer a miss (an add on an existing slug is an enrolment and is
    checked).

    Known residual: missing vs ungranted is distinguishable, so a `system:create` holder can enumerate
    slugs; 404-on-both was reverted (it breaks new-slug adds and
    `test_system_create_is_unchanged_for_every_command_type`). Shape errors are the later 422 arms'."""
    if command_type not in _ACCOUNT_TIER_COMMANDS:
        return
    slug = args.get("slug")
    if not isinstance(slug, str) or not slug:
        return
    account_id = session.scalar(select(Account.id).where(Account.slug == slug))
    if account_id is None:
        _refuse_slug_held_by_another_org(principal, slug)
        return
    # 404, never 403 — see `require_object_access`.
    require_object_access(session, principal, "account", account_id, mode=ObjectAccess.WRITE)


def _refuse_slug_held_by_another_org(principal: Principal | None, slug: str) -> None:
    """Refuse an account verb naming a slug that the caller's org cannot see but another org holds.

    The seam makes a foreign row look missing, which is the permissive branch for every account verb; but
    `accounts.slug` is fleet-unique (`uq_accounts_slug`) and observations fold by slug
    (`app/account_fold.py`), so a tenant's node could write the operator's account health.

    A fleet-wide read under `seam.detached()` on its own `SessionLocal()`, one scalar out
    (listed in the tenancy-scope census); it reads only whether the slug is taken, already a
    fleet-wide fact. Skipped for the operator plane. 409, as `POST /api/accounts` gives for the same
    collision."""
    if principal is None or principal.is_operator:
        return
    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        taken = fleet.scalar(select(Account.id).where(Account.slug == slug)) is not None
    if taken:
        raise HTTPException(status_code=409, detail=account_slug_not_available(slug))


def _require_own_account_slug(
    session: Session, principal: Principal | None, args: dict[str, Any]
) -> None:
    """On the personal path an existing slug must be the caller's OWN account: an enrolment re-materializes
    that account on another node, which for an org account (reachable by a binding) is the
    administrator's. 404 like `require_object_access` — the caller may not be able to see the row."""
    slug = args.get("slug")
    if principal is None or not isinstance(slug, str) or not slug:
        return
    owner = session.execute(select(Account.owner_id).where(Account.slug == slug)).first()
    if owner is not None and owner.owner_id != principal.user_id:
        raise HTTPException(status_code=404, detail="not found")


async def _require_own_login(
    durable_accounts: Any, principal: Principal | None, node_id: str, slug: Any
) -> None:
    """The live login for `(node_id, slug)` must have been started by `principal` — the personal path's
    bound on continuing, cancelling or resuming one. No live login passes (the verb answers it). 404."""
    if principal is None or not isinstance(slug, str):
        return
    creator = await run_in_threadpool(durable_accounts.login_creator, node_id, slug)
    if creator is not durable_accounts.NO_LIVE_LOGIN and creator != str(principal.user_id):
        raise HTTPException(status_code=404, detail="not found")


def account_slug_not_available(slug: str) -> str:
    """The one sentence a tenant reads for a slug the fleet holds and the seam hides from them.

    Shared by this bus (`_refuse_slug_held_by_another_org`) and `routes/accounts.py::_slug_collision`.
    "Not available" states the fleet-wide uniqueness without confirming the row exists; the operator keeps
    "already exists".
    """
    return not_available(f"account slug '{slug}'")


def _provider_mints_on_shared_store(provider: str) -> bool:
    """True iff a login for `provider` writes its credential to the shared fleet store rather than to the
    node's own disk — which is the question both shared-account behaviours on this route turn on.

    Read off the catalogue's `node_pinned` (`app/agent_providers.py`), never a provider-name comparison. A
    `shared_token_link` provider mints on the share too (its login runs in the store). For a
    `node_pinned` provider the injected `shared: true` stands down (its credentials are node-local). An
    unknown value answers True (keeps the guard); unreachable, since unknown providers are 422d first."""
    row = agent_providers.by_value(provider)
    return row is None or not row.node_pinned


def _with_resolved_provider(args: dict[str, Any], provider: str) -> dict[str, Any]:
    """`args` carrying the provider the control plane resolved — injected, exactly as `shared` is.

    An existing account's provider comes off its roster row (`_account_row_provider`), so a request that
    omitted it still reaches the node with the right one. The default provider is removed, not just not
    added, so a Claude dispatch stays byte-identical and the audit copy matches the wire (the workflow's
    `start_args` applies the same rule)."""
    if agent_providers.implied_on_wire(provider):
        return {k: v for k, v in args.items() if k != "provider"}
    return args | {"provider": provider}


def _account_row_provider(session: Session, slug: str) -> str | None:
    """The `provider` of the existing `accounts` row for `slug`, or None when the slug has no row.

    The roster is the authority (`accounts.provider` is fixed; `PUT /api/accounts/{id}` refuses to change
    it), so a request naming a different provider is refused with 422. Trusting the claim would switch the
    workflow off the URL-shape clamp (`_declared_completion`) — the CP's only defence against a
    paste-mode node on a redirect-mode request. `None` means a new slug, where the caller's value stands."""
    return session.scalar(select(Account.provider).where(Account.slug == slug))


def _account_slug_is_new(session: Session, slug: str) -> bool:
    """True iff the `accounts` roster has no row for `slug` — the one discriminator between `account_add`'s
    two jobs, derived in the request and persisted into the workflow so a replay cannot re-ask.

    Enrolment (`apps/web/src/pages/credentials/Accounts.tsx::onEnrol`) reuses `mode="add"` with a fixed
    slug. The row, not a UI-supplied mode, is the authority: a slug with a row has its own `accounts.shared`
    answer, so the injected `shared: true` applies only to a new slug
    (cf. `_login_start`'s `shared_override`). Racy but harmless: `_ensure_account_row`'s
    `ON CONFLICT DO NOTHING` keeps an existing row."""
    return session.scalar(select(Account.id).where(Account.slug == slug)) is None


def _ownership_matches_row(session: Session, principal: Principal | None, slug: str, personal: bool) -> bool:
    """Whether an `account_add` naming `slug`'s existing row asks for the owner that row already has.

    The owner the request would stamp is derived by the one rule that stamped the row
    (`stamp_owner_on_create`, as `_ensure_account_row` calls it), never re-spelled here."""
    probe = Account()
    stamp_owner_on_create(principal, probe, OWNERSHIP_PERSONAL if personal else None)
    return probe.owner_id == session.scalar(select(Account.owner_id).where(Account.slug == slug))


def _node_share_root(session: Session, node_id: Optional[str]) -> Optional[str]:
    """Where `node_id` mounts the shared credential store, or `None` when there is no row to ask.

    `None`, not `""`: `resolve_shared_auth_dir` reads `None` as the fleet default and `""` as a refusal to
    compose. `node_id` is the node uuid (`node_matches`)."""
    if not node_id:
        return None
    return session.scalar(select(Node.shared_accounts_root).where(node_matches(node_id)))


def _operator_node_in_another_org(principal: Principal | None, node_id: Optional[str]) -> Optional[Any]:
    """The operator's target node `(id, org_id, runner_clis, shared_accounts_root, name, org_name)` when the
    request's own read could not see it.

    An operator's POST stays bound to its own org (`authz/enforce.py::_principal_scope`, R5.7), so another
    org's node resolves to no row there while `sees_all_nodes` still lets the command through:
    the org it lands in and the CLIs it attests are read here, fleet-wide, on a session of its own. Never
    for a tenant — `is_operator`, not the superadmin flag."""
    if principal is None or not principal.is_operator or not node_id:
        return None
    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        return fleet.execute(
            select(
                Node.id, Node.org_id, Node.runner_clis, Node.shared_accounts_root, Node.name,
                Org.name.label("org_name"),
            ).outerjoin(Org, Org.id == Node.org_id).where(node_matches(node_id))
        ).first()


def _refuse_operator_add_in_another_org(
    session: Session, foreign_node: Any, slug: Any, personal: bool
) -> None:
    """The two `account_add`s an operator may not start on another org's node.

    Personal: the row would be the operator's own inside that org, where nobody there can use it (422).
    A slug that org already holds: the request's org-bound read cannot see it, so the add would otherwise
    proceed as a create and the insert silently lose (409)."""
    if personal:
        raise HTTPException(
            status_code=422,
            detail=(
                "A personal account on another organization's node would be yours inside that "
                "organization, where nobody can use it. Add it as the organization's account."
            ),
        )
    if not isinstance(slug, str) or not slug:
        return
    with _account_row_session(session, foreign_node) as row_session:
        taken = row_session.scalar(select(Account.id).where(Account.slug == slug)) is not None
    if taken:
        raise HTTPException(
            status_code=409, detail=f"account '{slug}' already exists in that node's organization"
        )


@contextmanager
def _account_row_session(session: Session, foreign_node: Optional[Any]) -> Iterator[Session]:
    """The session phase 1's roster row is written on: the request's own, or, for the operator's node in
    another org (`_operator_node_in_another_org`), one bound to that node's org, as
    `durable/accounts.py::ensure_row` binds phase 2's. On the request's, the node would resolve to no row
    and the org-less insert would be refused."""
    if foreign_node is None:
        yield session
        return
    with seam.detached(), SessionLocal() as row_session, seam.bind_from(row_session, Node, foreign_node.id):
        yield row_session


#: The audit `actor` for a converge fired with no operator in hand (the durable workflow) — the `system:*`
#: shape other internal writers use.
_CONVERGE_SYSTEM_ACTOR = "system:account-converge"


def _actor_label(session: Session, principal: Principal | None) -> str:
    """Who an internally-fired command is audited as: the wizard operator's email when there is one (so
    `_audit_org`'s fallback can match it), else the `system:*` label."""
    user_id = getattr(principal, "user_id", None) if principal is not None else None
    if user_id is None:
        return _CONVERGE_SYSTEM_ACTOR
    email = session.scalar(select(AdminUser.email).where(AdminUser.id == user_id))
    return str(email) if email else _CONVERGE_SYSTEM_ACTOR


def _converge_shared_account_fleet_wide(
    slug: str, *, origin_node_id: Optional[str], actor: str,
) -> None:
    """Command every other node of the origin's org that reports a mounted token share
    (`nodes.shared_accounts_root` non-empty) to converge its managed layer for a shared account that
    just finished logging in, as one durable workflow per node.

    The token is usable fleet-wide at once, but only the login node has converged the per-node layer
    (settings, consent, plugins); `apps/jaahd/reconcile/accounts_probe.py::AgentsProbeCache.converge_unprovisioned` catches up only in
    minutes, and a runner placed first blocks on "Managed settings require approval"
    (`apps/jaahd/plugin_repair.py`). This function only enumerates which nodes owe a converge and
    enqueues (`app/durable/account_converge.py` owns the durability); the unit is one (account, node).

    Its own session, resolving the origin node in `seam.identity_scope()` and `bind_from` its org (the
    account belongs to its minting node's org; idiom: `durable/accounts.py::ensure_row`). Both callers
    already bind that org, so a foreign origin is refused and logged. Never raises — the node's own
    trigger and the launch-time backstop still cover a failure. The origin node is skipped
    (`apps/jaahd/accounts.py::_finalize_account_add` did its work)."""
    if not origin_node_id:
        log.warning("account %r: shared, but its login named no node — nothing to bind a fleet-wide "
                    "converge under, so the other nodes converge at their next runner launch", slug)
        return
    # Deferred: importing an `app/durable/` module registers its workflow with DBOS, and this module loads
    # before the runtime; call-time lookup also lets tests stand in for the enqueue.
    from app.durable.account_converge import enqueue_converge

    try:
        with SessionLocal() as session:
            with seam.identity_scope():
                origin = node_id_or_none(session, origin_node_id)
            if origin is None:
                log.warning("account %r: origin node %r resolves to no row, so the fleet-wide converge "
                            "has no org to run under — skipped", slug, origin_node_id)
                return
            with seam.bind_from(session, Node, origin):
                targets = session.execute(
                    select(Node.id, Node.name)
                    .where(Node.enabled.is_(True), func.coalesce(Node.shared_accounts_root, "") != "")
                    .order_by(Node.name)
                ).all()
    except Exception as exc:  # noqa: BLE001 — best-effort by contract; the login has already succeeded
        log.warning("account %r: could not read the share-enabled roster for the fleet-wide converge "
                    "(%s: %s) — the nodes' own roster-driven trigger converges it",
                    slug, type(exc).__name__, exc)
        return
    # Enqueued outside the session block, so no pooled connection sits idle-in-transaction across N
    # system-database writes. The origin is dropped here (it may not be in the set) so the summary counts
    # what was owed.
    owed = [row for row in targets if row.id != origin]
    # `bus_target` and the workflow's node argument are both `str(row.id)` (`node_identity.bus_key`), a
    # rename-proof identifier. "Handed to the queue": `enqueue_converge` may attach to an existing run
    # under the same key, so the summary does not claim a run began.
    queued = sum(1 for row in owed if enqueue_converge(slug, str(row.id), origin_node_id, actor))
    log.info("account %r: handed the managed-layer converge to the durable queue for %d of %d "
             "share-enabled node(s)", slug, queued, len(owed))


def _converge_one_node(slug: str, node_id: str, actor: str) -> None:
    """Converge one node's managed layer for one shared account, audit what the node actually answered,
    and raise if it did not converge.

    The body of `app/durable/account_converge.py::converge_on_node`, kept here because it needs this
    module's arg derivation, scrubbers and `_record_audit`; the durable module owns retry. Runs under the
    caller's bound scope (the origin node's org) with its own session. Dispatches through
    `node_bus.run_on_serving_loop`, never `asyncio.run` or `dispatch_and_audit_sync` (whose throwaway loop
    discards every reply).

    Raises on every outcome a retry could fix, after writing the audit row. A missing `config_dir` is
    split: a target with no Jaah home yet raises (transient); a gone or unshared account row is
    permanent and returns. Both leave an audit row."""
    # Seeded before the `try` so the `except` arms can always name the node.
    node_name = node_id
    with SessionLocal() as session:
        try:
            # The log-facing name, not the uuid the queue carries (the uuid addresses; the name is for
            # operators). `or node_id` if the row vanished.
            node_name = session.scalar(select(Node.name).where(Node.id == node_id)) or node_id
            # `_resolve_command_args` derives the target's own `config_dir`; its `account_converge` arm
            # waives the pin test for a shared account.
            args = _resolve_command_args("account_converge", {"slug": slug}, session, node_id)
            if not args.get("config_dir"):
                # Which miss — through the same `node_matches` spelling the derivation used.
                if not session.scalar(select(Node.jaah_home).where(node_matches(node_id))):
                    # Raised (the `except` below audits it): a node not yet described is
                    # indistinguishable from a late one, which the ladder exists for.
                    raise RuntimeError(
                        f"account {slug!r}: node {node_name!r} has not declared a Jaah home, so no "
                        f"config dir could be derived — not converged"
                    )
                # Permanent, so not raised, but audited as a converge owed and refused.
                log.warning(
                    "account %r: not converging on node %r — the account row is gone or no longer "
                    "shared, so no config dir could be derived", slug, node_name,
                )
                _record_audit(
                    session, node_id, "account_converge", {"slug": slug}, "failed",
                    {"reason": "no config dir: the account row is gone or is not shared"}, actor,
                )
                return
            # Release the pool connection before the slow `provision_accounts.py --offline` leg; the
            # session begins a new transaction for the audit.
            session.rollback()
            reply = node_bus.run_on_serving_loop(
                node_id,
                _dispatch(node_name, "account_converge", args,
                          bus_target=node_id, timeout=_CONVERGE_TIMEOUT),
                timeout=_CONVERGE_TIMEOUT + 5.0,
            )
        except Exception as exc:
            # Audit the node that was never reached (`NodeNotConnected`, timeout). Roll back first: the
            # session may be in an aborted transaction.
            session.rollback()
            try:
                _record_audit(session, node_id, "account_converge", {"slug": slug}, "failed",
                              {"reason": f"{type(exc).__name__}: {exc}"}, actor)
            except Exception as audit_exc:  # noqa: BLE001 — the raise below is what must survive
                session.rollback()
                log.warning("account %r: could not audit the failed converge on node %r: %s",
                            slug, node_name, audit_exc)
            log.warning("account %r: converge on node %r did not complete (%s: %s) — the durable "
                        "workflow retries it, and a runner launch there converges the same layer",
                        slug, node_name, type(exc).__name__, exc)
            # Re-raised so the step's retry ladder runs.
            raise
        status = str(reply.get("status") or "failed")
        result = reply.get("result") if isinstance(reply.get("result"), dict) else None
        _record_audit(
            session, node_id, "account_converge",
            _credential_safe_args("account_converge", args), status,
            _credential_safe_result("account_converge", result), actor,
        )
        if status == "done":
            log.info("account %r: converged on node %r", slug, node_name)
            return
        # The node's own reason (a half-converged account, in `stderr`), then raise for the ladder.
        reason = ((result or {}).get("stderr") or (result or {}).get("reason") or "").strip()[-400:]
        log.warning("account %r: converge on node %r answered %s — %s", slug, node_name, status, reason)
        raise RuntimeError(
            f"account {slug!r}: node {node_name!r} answered {status} to account_converge — {reason}"
        )


def _fan_out_best_effort(
    session: Session, slug: str, node_id: Optional[str], principal: Principal | None,
) -> None:
    """Start the fleet-wide converge for a shared row that just became dispatchable, and never raise.

    Sits in `_ensure_account_row` after the row's commit, so a best-effort trigger cannot preempt a
    higher-ranked write or land in the handler claiming the row was not created."""
    try:
        _converge_shared_account_fleet_wide(slug, origin_node_id=node_id,
                                            actor=_actor_label(session, principal))
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        log.warning("account %r: could not start the fleet-wide converge (%s: %s) — the nodes' own "
                    "roster-driven trigger converges it", slug, type(exc).__name__, exc)


def _ensure_account_row(
    session: Session, slug: str, result: Optional[dict[str, Any]], *,
    enabled: bool, node_id: Optional[str] = None, principal: Principal | None = None,
    provider: str = agent_providers.DEFAULT_PROVIDER, born_shared: bool = False,
    personal: bool = False, name: Optional[str] = None,
) -> None:
    """Give an Add-Account wizard slug its `accounts` roster row — the DB SSOT the dispatcher reads from,
    and the only thing that makes the account visible in the console. Called on a `done` reply to both
    phases: phase 1 (`account_add`) inserts it disabled, phase 2 (`account_add_submit_code`) enables it
    (creating it if phase 1's reply was unusable) — phase 2 can outlive `COMMAND_TIMEOUT`, and nothing
    else would create the row.

    `enabled=False` keeps a credential-less phase-1 dir out of selection; the collector still probes it.
    `node_id` pins the row to the login's node. ON CONFLICT DO NOTHING on `slug`; phase 2 only ever sets
    `enabled` true, only on a row pinned to this node. Never raises.

    `shared` is true iff the node reported minting into exactly the store the CP composes for it
    (`_securestorage_dir_from_reply`) — observed, not assumed. The column is no console concept any more:
    it is served only to old `jaahd` packages, which read the token from the share iff it is set; a current
    node resolves the token locally and ignores it. An enrolment loses the insert, leaving the row's own answer.
    A genuinely new row is stamped with the delegating `principal`'s owner (`stamp_owner_on_create`); phase
    2's enable does not change it. `personal` is the creator's explicit choice (or the personal path's
    rule, G2): the row is theirs whoever they are. `name` is the label the creator typed; absent, the slug.
    """
    if not slug:
        return
    # The phase-1 refusal: no reported credential dir means no row — the "credentials landed" signal.
    config_dir = _config_dir_from_reply(result)
    if not config_dir:
        log.warning(
            "account wizard reply for slug %r was done without a usable config_dir — no accounts row "
            "created; the account needs a manual record-create to become dispatchable", slug,
        )
        return
    # Where the token went, decided once. It counts only when it equals the store this side composes for
    # the slug (the node-side twin is `_login_store_refusal`). `expected_store` can be `""`
    # (unresolvable slug), so it is tested for truth first.
    reported_store = _securestorage_dir_from_reply(result)
    # That node's own share root (0185); an unknown node falls back to the fleet default.
    expected_store = resolve_shared_auth_dir(slug, _node_share_root(session, node_id))
    shared_observed = bool(expected_store) and reported_store == expected_store
    try:
        # `name`: the person's label, else the slug (NOT NULL, no default), as the create route.
        # `shared` is the node's report of exactly the store the CP composes for it: an old node mints there
        # only when asked (`born_shared`), a current one whenever its share is mounted. Phase 1 decides it;
        # phase 2 only updates `enabled`. `provider` is written on the insert only (immutable via PUT); a
        # winning phase-2 insert takes the default, having observed no choice.
        shared = shared_observed
        values: dict[str, Any] = {
            "slug": slug, "name": name or slug, "enabled": enabled, "shared": shared,
            "provider": provider,
            # No `org_id` key here; the `if node_id:` branch adds one only when the node answers.
        }
        if not enabled:
            # Phase 1: no credential yet. NULL marks the row an unfinished sign-in may drop (`signed_in_at`);
            # a phase-2 insert takes the column's default, "signed in now".
            values["signed_in_at"] = None
        # Personal or org account, decided by the rule's one home. This Core insert takes no ORM row, so
        # the rule is asked on a transient `Account` (never added to the session) and its answer copied;
        # only the insert that wins writes it.
        ownership = Account()
        stamp_owner_on_create(principal, ownership, OWNERSHIP_PERSONAL if personal else None)
        if ownership.owner_id is not None:
            values["owner_id"] = ownership.owner_id
        # Declared before the branch: the phase-2 guard reads it on every path, and a NameError would be
        # swallowed as "could not create the row".
        resolved_pin = None
        if node_id:
            # A nullable uuid FK (NULL = unpinned), failing open: an unresolvable node costs the pin, never
            # the row.
            resolved_pin = node_id_or_none(session, node_id)
            if resolved_pin is None:
                # Logged: NULL means usable fleet-wide, wider than asked (bounded by `available_dirs`).
                log.warning(
                    "account %r kept its roster row but LOST its node pin: node %r is not registered, "
                    "so accounts.node_id is NULL — the account reads as usable fleet-wide", slug, node_id,
                )
            values["node_id"] = resolved_pin
            # The tenant, by hand: this Core `pg_insert` bypasses the write seam's stamping, and a NULL
            # `org_id` is in no org's `placement_grants.org_floor`. The node's org (the durable workflow
            # has no bound scope). No fallback org: if the node does not answer, the key is
            # omitted and the insert refuses (`test_no_create_path_falls_back_to_a_well_known_org`). A
            # `session.scalar` so the value is visible in Python
            # (`test_the_org_stamp_reads_the_node_and_is_not_a_constant`).
            node_org = session.scalar(select(Node.org_id).where(Node.id == resolved_pin))
            if node_org is not None:
                values["org_id"] = node_org
        new_id = session.scalar(
            pg_insert(Account)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["slug"])
            .returning(Account.id)
        )
        if new_id is None:
            # A row already exists for this slug. Phase 1 leaves it exactly as the console has it.
            if not enabled:
                session.rollback()
                return
            # The phase-2 enable guard: a login on node A must not enable a row pinned to node B.

            # Strict equality with no `node_id IS NULL` arm, which would widen a credential path. An
            # unresolvable node therefore enables nothing (`== None` compiles to `IS NULL`).
            if resolved_pin is None:
                log.warning(
                    "account wizard phase 2 for slug %r enabled nothing: node %r is not registered, so "
                    "there is no pin to match. Enabling on a slug alone would reach a row pinned to "
                    "another host, which is what this guard exists to refuse.", slug, node_id,
                )
                session.rollback()
                return
            # Re-running Add Account on a disabled slug on the same node re-enables it (explicit operator
            # action plus a completed OAuth login).
            existing_id = session.scalar(
                update(Account)
                .where(
                    Account.slug == slug,
                    Account.node_id == resolved_pin,
                    Account.enabled.is_(False),
                )
                # The first completed sign-in stamps `signed_in_at`; a re-enable keeps the original stamp.
                .values(enabled=True, signed_in_at=func.coalesce(Account.signed_in_at, func.now()))
                .returning(Account.id)
            )
            if existing_id is None:
                session.rollback()   # already enabled, or points elsewhere — nothing to change
                return
            emit_console_event(session, entity="account", id=existing_id, action="upserted")
            # Read before the commit: the row's own `shared` decides whether other nodes owe a converge.
            is_shared = bool(session.scalar(select(Account.shared).where(Account.id == existing_id)))
            session.commit()
            if is_shared:
                _fan_out_best_effort(session, slug, node_id, principal)
            return
        if born_shared and enabled and not shared_observed:
            # Phase 2 won the insert for a login the CP asked to share: the token is on the share but the
            # row says pinned, so an old node reads its own config dir. Fail-closed; a re-auth fixes it.
            log.error(
                "account %r: phase 2 created the roster row for a login the control plane asked to mint "
                "on the SHARED store, but no phase-1 reply observed where the token went — the row is "
                "PINNED to node %r as the fail-closed answer. If an old node cannot find the token, "
                "re-authenticate the account.",
                slug, node_id,
            )
        # A winning insert already enabled (phase 2 won); if shared, the converge is still owed.
        fan_out_shared = enabled and shared
        emit_console_event(session, entity="account", id=new_id, action="upserted")
        session.commit()
        if fan_out_shared:
            _fan_out_best_effort(session, slug, node_id, principal)
    except Exception:
        session.rollback()
        log.exception("could not create the accounts row for wizard slug %r", slug)


def _audit_after_dispatch(
    session: Session, node_id: str, command_type: str, audit_args: dict[str, Any],
    status: str, result: Optional[dict[str, Any]], actor: str, *, commit: bool = True,
) -> int:
    """`_record_audit` for the post-dispatch write, where a failure means something different.

    The connection was released during the wait, so this checkout can fail after the node already
    executed — losing the record of a command that ran (fail-open), silently for callers that swallow
    every exception (`slot_teardown.py`, `pool_slots.py`). So the scrubbed `audit_args` are logged first,
    then it still raises. No retry: another `pool_timeout` wait during saturation is what starved
    `/api/health` (`app/db._pool_kwargs`)."""
    try:
        return _record_audit(session, node_id, command_type, audit_args, status, result, actor, commit=commit)
    except SQLTimeoutError:
        log.error(
            "AUDIT ROW LOST: %s ran on node %s (status=%s, actor=%s) but command_audit_log could not "
            "be written — no connection was available. args=%r result=%r",
            command_type, node_id, status, actor, audit_args, result,
        )
        raise


async def dispatch_and_audit(
    session: Session, node_id: str, command_type: str, args: dict[str, Any], actor: str,
    *, timeout: float = COMMAND_TIMEOUT,
) -> tuple[dict[str, Any], int]:
    """Dispatch one command and record its audit row — the combined flow `enqueue_command` and
    the internal (fire-and-forget) call sites below both need. Returns (reply, audit_row_id).

    `session` is the caller's (it owns the audit row; `_record_audit` propagates). A timed-out wait still
    audits `"dispatched"` before re-raising — the node is executing regardless.

    The caller's connection returns to the pool for the node round trip (after `bus_key`, the last read),
    so the pool bounds concurrent statements, not concurrent commands (`db.release_connection`). The
    price: the bus-key read and the audit write are separate transactions; a session that has written
    keeps its connection. `_dispatch`'s frame-proof lookup opens its own session."""
    audit_args = _credential_safe_args(command_type, args)
    bus_target = bus_key(session, node_id)
    release_connection(session)
    try:
        reply = await _dispatch(
            node_id, command_type, args, bus_target=bus_target, timeout=timeout,
        )
    except HTTPException as exc:
        # Through the same sink as the success path: this runs after the release too, and a
        # `dispatched` row records a command the node is executing while the CP stopped listening.
        _audit_after_dispatch(
            session, node_id, command_type, audit_args, "dispatched", {"reason": exc.detail}, actor,
        )
        raise
    status = str(reply.get("status") or "failed")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else None
    # The reply returns to the caller unscrubbed (it needs the real document); only the audit copy is
    # redacted, composed at the call so the scrub is visible at every sink.
    audit_id = _audit_after_dispatch(
        session, node_id, command_type, audit_args, status,
        _credential_safe_result(command_type, result), actor,
    )
    return reply, audit_id


def dispatch_and_audit_blocking(
    session: Session, node_id: str, command_type: str, args: dict[str, Any], actor: str, *, timeout: float,
) -> dict[str, Any]:
    """`dispatch_and_audit` for a threadpool caller that needs the REPLY — the trigger processor's script
    preflight (`app/trigger_preflight_script.py`). Blocks this thread for it.

    Only the dispatch hops onto the socket's serving loop (`node_bus.run_on_serving_loop`; `asyncio.run` is
    broken here). The bus-key read and the audit row stay on THIS thread, in the caller's session, where its
    org binding lives — context variables do not cross to the loop. Raises what `dispatch_and_audit`
    raises, plus `node_bus.NodeNotConnected` when no loop serves the target (nothing was sent: unaudited)."""
    audit_args = _credential_safe_args(command_type, args)
    bus_target = bus_key(session, node_id)
    release_connection(session)
    coro = _dispatch(node_id, command_type, args, bus_target=bus_target, timeout=timeout)
    try:
        try:
            reply = node_bus.run_on_serving_loop(bus_target, coro, timeout=timeout + 5)
        except concurrent.futures.TimeoutError:
            # Only a wedged serving loop gets here (`_dispatch`'s own deadline is 5s tighter): `_dispatch`'s 504.
            raise HTTPException(
                status_code=504, detail=f"node '{node_id}' did not reply within {timeout}s",
            ) from None
    except HTTPException as exc:
        _audit_after_dispatch(
            session, node_id, command_type, audit_args, "dispatched", {"reason": exc.detail}, actor,
        )
        raise
    status = str(reply.get("status") or "failed")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else None
    _audit_after_dispatch(
        session, node_id, command_type, audit_args, status, _credential_safe_result(command_type, result), actor,
    )
    return reply


async def _dispatch_on_a_throwaway_loop(dispatch):
    """`dispatch`, then the loop's own peer-forwarding client closed before `asyncio.run` ends the loop
    (`app/cp_peers.py::aclose_running_loop_client`): a forward to a peer-held socket opens keep-alive
    connections on THIS loop, and nothing else would close them."""
    try:
        return await dispatch
    finally:
        await cp_peers.aclose_running_loop_client()


def dispatch_and_audit_sync(
    session: Session, node_id: str, command_type: str, args: dict[str, Any], actor: str, *, commit: bool = True,
) -> None:
    """Sync entry point for non-request call sites that fire a command without waiting on its result —
    the mechanical compliance auto-merge and the mechanical orphan reap, both `app/routes/agent/`.

    Called from a sync route (no loop: `asyncio.run()` starts one) and from inside the main loop (the
    coroutine runs on a dedicated thread). Not a wrapper over `dispatch_and_audit`: a `Session` is not
    thread-safe, so only the dispatch crosses and the audit is written on the caller's thread. A timeout
    is logged and swallowed (a `"dispatched"` row is still written); the audit write propagates.
    `commit=False` leaves the audit row in the caller's transaction (an outbox handler inside its SAVEPOINT).
    """
    audit_args = _credential_safe_args(command_type, args)
    # The key is resolved on this thread, before the hop — the session does not cross.
    bus_target = bus_key(session, node_id)
    # The same release as `dispatch_and_audit`, on this thread (the session's). Before the coroutine is
    # built: `release_connection` can raise, and an unawaited `_dispatch(...)` would add a RuntimeWarning.
    release_connection(session)
    coro = _dispatch_on_a_throwaway_loop(_dispatch(
        node_id, command_type, args, bus_target=bus_target,
        timeout=INTERNAL_DISPATCH_TIMEOUT,
    ))
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            reply = asyncio.run(coro)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                reply = pool.submit(asyncio.run, coro).result()
    except HTTPException as exc:
        log.warning(
            "internal dispatch %s (node=%s) did not complete: %s", command_type, node_id, exc.detail,
        )
        _record_audit(
            session, node_id, command_type, audit_args, "dispatched", {"reason": exc.detail}, actor, commit=commit,
        )
        return
    status = str(reply.get("status") or "failed")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else None
    # Scrubbed for `dispatch_and_audit`'s reason, composed here so a reader sees it.
    _audit_after_dispatch(
        session, node_id, command_type, audit_args, status,
        _credential_safe_result(command_type, result), actor, commit=commit,
    )

# The sentence a tenant reads when their org owns no node to run on. 409: the org is in a state the
# command cannot run in, and only an operator changes it.
_NO_NODE_IN_ORG = "Your organization has no enabled node to run this on. Ask an operator to add one."
# The sentence when the org has nodes but none this caller may write to. It discloses one own-org fact,
# accepted on `GET /api/resources`' `hidden_account_count` terms: actionable, and nothing about another org.
_NO_GRANTED_NODE_IN_ORG = (
    "No node in your organization is shared with you to run this on. Ask an operator for access."
)


def _default_node_in_callers_org(
    session: Session, principal: Principal, *, prefer_connected: bool = True
) -> str:
    """The uuid of the node an unpinned command runs on for a caller inside a tenant org.

    Falling back to the CP's own `NODE_ID` sent a tenant onto the operator's node (and a bare 404); the
    fallback must stay inside the caller's org. A plain ORM read under this `ORG_SCOPED` route's bound
    seam is the guarantee. Prefers a writable node on the bus (`node_identity.bus_key`); otherwise the
    first enabled writable node by name, so the normal "not connected" sentence applies. No enabled node
    is a 409. Returns the uuid (a name is not an identifier); `require_object_access(..., WRITE)` still runs
    on it. Draining nodes stay
    eligible (these verbs are not leases)."""
    nodes = session.scalars(
        select(Node).where(Node.enabled.is_(True)).order_by(Node.name)
    ).all()
    if not nodes:
        raise HTTPException(status_code=409, detail=_NO_NODE_IN_ORG)
    # Only writable nodes influence the pick: an ungranted node's liveness is withheld from this caller,
    # and letting it change the answer class would leak it. No writable node → `_NO_GRANTED_NODE_IN_ORG`,
    # not the bare 404.
    writable = [
        node for node in nodes
        if can_access(session, principal, "node", node.id, mode=ObjectAccess.WRITE)
    ]
    if not writable:
        raise HTTPException(status_code=409, detail=_NO_GRANTED_NODE_IN_ORG)
    # `prefer_connected=False` drops the liveness tiebreak for a caller addressing an existing row
    # (`routes/pool_slots.py::_resolved_node_id`): deterministic, first writable node by name.
    if prefer_connected:
        for node in writable:
            if node_bus.registry.is_connected(str(node.id)):
                return str(node.id)
    return str(writable[0].id)


def default_dispatch_node(
    session: Session, principal: Principal | None, *, prefer_connected: bool = True
) -> str:
    """The uuid of the node an unpinned dispatch runs on for this caller — one answer per plane.

    A tenant-org caller gets a node of their own org (`_default_node_in_callers_org`); everyone else —
    operator, operator staff, and the loopback `principal is None` — gets the CP's own node (`cp_node_id`),
    else the raw `resolve_node_id() or "local"`, which resolves no row and so 404s/503s downstream. The
    community edition has no CP node of its own: there it is the edition's one node (`ce_bootstrap.node_id`).
    Keyed on plane membership (`is_tenant_org`), not `sees_all_nodes`.

    The one home for the verdict: every dispatch site that names no node calls this, never
    `resolve_node_id()` directly. Raises only that helper's 409; a fire-and-forget caller catches exactly
    that status. `prefer_connected` passes through (the operator arm ignores it)."""
    if principal is not None and is_tenant_org(principal.org_id):
        return _default_node_in_callers_org(session, principal, prefer_connected=prefer_connected)
    own = cp_node_id(session)
    if own is not None:
        return str(own)
    if edition.IS_CE:
        from app import ce_bootstrap  # deferred: the cloud never reads it

        ce_node = ce_bootstrap.node_id(session)
        if ce_node is not None:
            return ce_node
    return resolve_node_id() or "local"


def _echo(
    audit_id: int, node_id: str, command_type: str, args: dict[str, Any], status: str,
    result: Optional[dict[str, Any]], *, user: AdminUser,
) -> CommandOut:
    """The 201 body, composed once for `enqueue_command`'s three return sites, and the fourth scrubber:
    the tenant read model (`app/command_read.py::command_read`).

    The echo is the document `GET /api/command-log` serves, so it narrows the same way — otherwise a
    tenant could read back the injected host `path` and the node's host output. Composed after
    `_echo_safe_args` (permission) and `_credential_safe_result` (credentials); this one asks whose host,
    keyed on the reader's org (`tenancy/ids.py::is_tenant_org`)."""
    echo_args, echo_result = command_read(args, result, tenant=is_tenant_org(user.org_id))
    return CommandOut(
        id=audit_id, node_id=node_id, command_type=command_type,
        args=echo_args, status=status, result=echo_result,
    )


@router.post("", response_model=CommandOut, status_code=201)
# The declaration is a floor, not the whole answer: `authz_guard` denies holders of neither key, then
# `_require_command_key` decides which one this `command_type` needs (see `COMMAND_TYPE_KEYS`).
@feature_any("worktrees:edit", "system:create")
async def enqueue_command(
    body: CommandRequest,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    # The personal path (G2): decided first, because it is the one narrower door onto three system verbs.
    personal_only = _personal_account_path(session, principal, body.command_type, body.args)
    # Before the `VALID_TYPES` 422, so a `worktrees:edit`-only caller gets a 403 on a system verb rather
    # than an inventory of verbs.
    _require_command_key(body.command_type, principal, personal_path=personal_only)
    # The org half, before anything else resolves, so a tenant learns nothing from later errors
    # (`_OPERATOR_ORG_ONLY`). Decided once for every plane-dependent sentence below; `None` reads as a
    # tenant (fail-closed).
    tenant_seat = principal is None or is_tenant_org(principal.org_id)
    if body.command_type in _OPERATOR_ORG_ONLY and tenant_seat:
        # Worded for a tenant: names the operator, never the operator's deployment (`app/tenancy/copy.py`).
        raise HTTPException(
            status_code=403,
            detail=(
                f"command '{body.command_type}' administers the host and is the operator's — your "
                "organization cannot run it, whatever key it holds"
            ),
        )

    if body.command_type in _INTERNAL_ONLY:
        # Dispatched by its one caller in process (`dispatch_and_audit`), never through this door: removing
        # a shared token cuts the account off on every node at once, which no `system:create` grant covers.
        raise HTTPException(
            status_code=403,
            detail=f"command '{body.command_type}' is sent only by tenant offboarding, never directly",
        )
    if body.command_type not in VALID_TYPES:
        # The inventory is the reader's: a tenant is not shown the host verbs it can never run.
        offered = VALID_TYPES - _OPERATOR_ORG_ONLY if tenant_seat else VALID_TYPES
        raise HTTPException(
            status_code=422,
            detail=f"unknown command_type '{body.command_type}'; valid: {sorted(offered)}",
        )
    # A new account's slug is the server's to draw (`app/account_slug.py`): a person names the account with
    # `args.name`, which only the roster row reads, so it is taken off the args the node and audit see.
    # An `account_add` naming a slug is an enrolment (or an API caller's own pick) and keeps it.
    account_name: Optional[str] = None
    if body.command_type == "account_add":
        raw_name = body.args.pop("name", None)
        account_name = (raw_name.strip() or None) if isinstance(raw_name, str) else None
        if not body.args.get("slug"):
            body.args["slug"] = await run_in_threadpool(new_account_slug)

    # An unpinned body defaults per plane via `default_dispatch_node`. `principal is None` takes the
    # operator arm here (it names the caller's own host), though the `_OPERATOR_ORG_ONLY` check reads it
    # as a tenant (that one refuses, so it fails closed).
    # Off the loop: `default_dispatch_node` reads the DB (`node_bus.registry.is_connected` — a shared
    # placement-table read), never on the event loop.
    node_id = body.node_id or await run_in_threadpool(default_dispatch_node, session, principal)

    # The node must be one this caller may see (D13). It widens nothing, and stops a zero-grant caller
    # enumerating hosts via the echoed `node_id`. Probed by uuid only when the value resolves
    # (`routes/pool_slots.py::reserve_worktree`); the agent plane is exempt.
    target_node_uuid = node_id_or_none(session, node_id)
    foreign_node = (
        _operator_node_in_another_org(principal, node_id) if target_node_uuid is None else None
    )
    if foreign_node is not None:
        # The audit row stays where it is; this line names the org the operator reached into.
        log.info("operator %s: %s on node %s in org %s, outside the operator's org",
                 user.email, body.command_type, node_id, foreign_node.org_id)
    if target_node_uuid is not None:
        require_object_access(session, principal, "node", target_node_uuid, mode=ObjectAccess.WRITE)
    elif not sees_all_nodes(principal):
        # Presence-only (unregistered but connected) is superadmin-only, matching
        # `routes/nodes/reads.py::list_nodes`: `bus_key` falls back to the raw value, so otherwise a zero-grant
        # caller could reach it by name and read 503-vs-reply as a liveness oracle.
        raise HTTPException(status_code=404, detail="not found")
    if body.command_type == "account_add":
        # Adding an AI account (the console's sign-in wizard lands here): a pending org adds none
        # (`app/org_gate.py`). The org the row lands in is the answering node's (`_ensure_account_row`),
        # else the caller's; refused before anything is dispatched.
        node_org = (
            session.scalar(select(Node.org_id).where(Node.id == target_node_uuid))
            if target_node_uuid is not None
            else foreign_node.org_id if foreign_node is not None else None
        )
        require_org_active(session, node_org or user.org_id)
        # The community edition's account cap, before a login runs (`app/ce_caps.py`); re-signing-in an account
        # already on the roster adds no row.
        slug = body.args.get("slug")
        if edition.IS_CE and not (slug and session.scalar(select(Account.id).where(Account.slug == slug))):
            ce_caps.require_account_headroom(session, node_org or user.org_id)

    # The account must be one this caller may write — a node grant is not an account grant. Here, not per
    # verb, so no verb can join the tier and miss the floor.
    _require_account_access(session, principal, body.command_type, body.args)
    # Personal or the org's, for a new `account_add` (`authz.validated_ownership`); relayed to the login.
    account_personal = personal_only
    if body.command_type == "account_add":
        # One administrator question for every create door (`authz.creates_org_rows`), and never on the
        # personal path.
        ownership = validated_ownership(
            body.args.get("ownership"), may_create_org=not personal_only and creates_org_rows(principal)
        )
        account_personal = personal_only or ownership == OWNERSHIP_PERSONAL
        if foreign_node is not None:
            _refuse_operator_add_in_another_org(session, foreign_node, body.args.get("slug"), account_personal)
    # The project must be one this caller's org can read — the third object floor.
    _require_project(session, body.command_type, body.args)

    if body.command_type == "run":
        op = body.args.get("op")
        if not isinstance(op, str) or not op:
            raise HTTPException(
                status_code=422,
                detail="run commands require a non-empty string args.op",
            )
        # `is_operator`, not the bare flag: `POST /api/port-grid/reband` is gated by `require_operator`, and
        # this door must not be weaker for a tenant-born superadmin.
        if op in _RUN_OP_SUPERADMIN_ONLY and not (principal is not None and principal.is_operator):
            # Two readers, two sentences: operator staff read the bar; a tenant reads no "deployment" or
            # "superadmin" (`app/tenancy/copy.py`).
            detail = (
                f"run op '{op}' is the operator's — your organization cannot run it"
                if tenant_seat
                else f"run op '{op}' requires the deployment's operator — a superadmin in an operator-plane org"
            )
            raise HTTPException(status_code=403, detail=detail)

    # Declared before the branch: two phase-1 consumers below (the `shared` injection, `start_login`,
    # shared with `account_reauth`) read it. True is safe only because each consumer is gated on
    # `account_add`, whose arm always sets it.
    account_add_is_new = True
    # Whether an old `jaahd` is told to mint on its share: False except for a new `account_add` on a node
    # reporting a mounted token share — fail-closed.
    born_shared = False
    # The operator's agent-CLI choice, declared early for the same reason. `DEFAULT_PROVIDER` is what an
    # absent `args.provider` means end to end.
    account_provider = agent_providers.DEFAULT_PROVIDER
    if body.command_type in ("account_add", "account_reauth"):
        # Phase 1 of the add-with-code / re-auth wizard. `account_add` creates or enrols
        # (`_account_slug_is_new`); `account_reauth` refreshes (the agent enforces existence).
        slug = body.args.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=422,
                detail=f"{body.command_type} requires args.slug matching ^[a-z0-9][a-z0-9_-]{{0,31}}$",
            )
        # `args.peer_key` is required: the wizard's per-login public key, to which the node seals the
        # authorize URL. Without one the CP would have to hold the URL in the clear, which
        # it no longer does for any caller. Shape and bound only (`login_seal_wire.is_wire_key`);
        # whether it is a usable RSA key is the node's `openssl` verdict (`login_seal_invalid`). A
        # retired `with_code` is ignored, not refused: the provider's declared `login_completion` decides
        # the mode (`app/durable/accounts.py::_declared_completion`); this route only validates and relays.
        peer_key = body.args.get("peer_key")
        if not is_wire_key(peer_key):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.command_type} requires args.peer_key: the wizard's base64 public key "
                    f"(max {SEAL_KEY_MAX_CHARS} chars)"
                ),
            )
        #
        # `args.provider` is optional and absence means Claude. A membership test: the node selects a
        # login adapter from it, so an unknown value is refused here with a diagnosable message. An
        # explicit `null` is refused.
        if "provider" in body.args:
            if body.args["provider"] not in agent_providers.PROVIDER_VALUES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{body.command_type} requires args.provider to be one of "
                        f"{sorted(agent_providers.PROVIDER_VALUES)} when present"
                    ),
                )
            account_provider = str(body.args["provider"])
        # An existing account answers for itself (as `PUT /api/accounts/{id}`): an equal echo passes, a
        # disagreeing value is 422, an absent one resolves to the row's (`_account_row_provider`). Covers
        # `account_reauth` and enrolments; only a new add lets the caller's value stand.
        existing_provider = _account_row_provider(session, slug)
        if existing_provider is not None:
            if "provider" in body.args and account_provider != existing_provider:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"account '{slug}' is registered against provider '{existing_provider}', which "
                        f"cannot be changed; {body.command_type} accepts args.provider only as an equal "
                        f"echo of it (or omitted). Retire the account and register a new one to move it "
                        f"to another provider."
                    ),
                )
            account_provider = existing_provider
        # The login runs the vendor's CLI on the login node, so a provider outside the fleet baseline
        # (`needs_attested_binary`) must be attested there — shared or not.
        row = agent_providers.by_value(account_provider)
        # A provider whose accounts are only an API key has no login to run: refused before any node is
        # reached, rather than started with no completion mode to wait on. Its accounts are created on
        # `POST /api/accounts` with the key.
        if row is not None and account_auth.AUTH_KIND_OAUTH not in row.auth_kinds:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{row.label} accounts sign in with an API key, not a login: add the account "
                    "with its key instead"
                ),
            )
        if body.command_type == "account_add" and existing_provider is None:
            # A new subscription account: the provider grid of the org the row lands in
            # (`app/provider_auth_policy.py`), as `POST /api/accounts` checks a key account's.
            provider_auth_policy.require_allowed(
                session, node_org or user.org_id, account_provider, account_auth.AUTH_KIND_OAUTH
            )
        else:
            # An existing account's enrolment or re-login: its pair was allowed once, but its org's provider
            # grid decides whether its provider may sign in now (any cell of it on).
            account_org = (
                session.scalar(select(Account.org_id).where(Account.slug == slug))
                if existing_provider is not None else user.org_id
            )
            if not provider_auth_policy.provider_on_plan(session, account_org, account_provider):
                raise HTTPException(
                    status_code=422,
                    detail=f"{provider_auth_policy.refusal(session, account_org, account_provider)} Nothing was started.",
                )
        if row is not None and row.needs_attested_binary:
            # The provider's only server-side gate (the dialog's picker is a courtesy, and enrolments have
            # none): the node must attest the CLI in `runner_clis` (`app/models/nodes.py::Node.runner_clis`). NULL
            # and `[]` both refuse (fail closed), with the detail saying which. 409: the request is fine,
            # the fleet's state refuses it.
            login_node = session.get(Node, target_node_uuid) if target_node_uuid is not None else foreign_node
            # An unregistered node has no attestation — the same refusal. `target_node_uuid` is resolved.
            attested = login_node.runner_clis if login_node is not None else None
            if attested is None or row.binary not in attested:
                if attested is None:
                    what_the_node_said = (
                        "has not reported which agent CLIs it has, so this control plane cannot tell "
                        f"whether it can run '{row.binary}'"
                    )
                else:
                    what_the_node_said = (
                        f"reports the agent CLIs {sorted(attested)}, which do not include '{row.binary}'"
                    )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"node '{node_id}' {what_the_node_said} — and a {row.label} login runs "
                        f"'{row.binary}' on the node it is started on, so it can only run on a host that "
                        f"has that binary. That "
                        f"list is SENSED off the node's own PATH and never granted in the console: "
                        f"install {row.binary} for user 'jaah' on that host and let it report in, or "
                        f"register the account on a node that already reports it."
                    ),
                )
        if body.command_type == "account_add":
            # The discriminator, read once, before any dispatch. Its own call rather than
            # `existing_provider is None`: "does a row exist" must not depend on the provider column.
            account_add_is_new = _account_slug_is_new(session, slug)
            if (not account_add_is_new and body.args.get("ownership") is not None
                    and not _ownership_matches_row(session, principal, slug, account_personal)):
                # An existing account already answers who owns it; decided once, at create. The wizard's own
                # restart repeats the owner it started with, which is not a change.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "This account's owner was set when its sign-in was first started and cannot be "
                        "changed. Add a new account to choose a different owner."
                    ),
                )
            # Old-node compatibility only (a current `jaahd` mints into `{JAAH_HOME}/auth/.shared` on its
            # own and ignores the flag): an old node mints on its share when told `shared: true`
            # (`_login_start`'s `shared_override`) and REFUSES that when no share is mounted
            # (`_login_store_refusal`), so the flag goes only to a node reporting a mounted token share
            # (`nodes.shared_accounts_root`). A personal-path account and a `node_pinned` provider are
            # never told, exactly as before.
            if account_add_is_new and not personal_only and _provider_mints_on_shared_store(account_provider):
                # Another org's node is invisible to this request's read: its own row answers.
                born_shared = bool(
                    foreign_node.shared_accounts_root if foreign_node is not None
                    else _node_share_root(session, node_id)
                )
        # No shared-node 409: the row records what the node observed.
        if body.command_type == "account_add":
            # The per-node ceiling (`max_accounts_per_node`, `app/agent_providers.py`), never shown on the
            # dialog. Non-positive = unlimited; only for `node_pinned` providers; the request's own slug
            # is excluded (enrolment); disabled accounts still count (only retirement frees the host);
            # `account_add` only. Advisory, not serialized — in-flight logins can all pass. Inert today
            # (every provider publishes unlimited); the canary
            # `test_every_shipping_provider_publishes_the_unlimited_sentinel_today` fails when that changes.
            row = agent_providers.by_value(account_provider)
            # Unknown values and unregistered nodes were already refused above. Another org's node is counted
            # in that org (`_account_row_session`), where its accounts are.
            limit_node = target_node_uuid if target_node_uuid is not None else getattr(foreign_node, "id", None)
            if (
                row is not None
                and row.node_pinned
                and row.max_accounts_per_node > 0
                and limit_node is not None
            ):
                with _account_row_session(session, foreign_node) as count_session:
                    pinned_here = count_session.execute(
                        select(func.count()).select_from(Account).where(
                            Account.provider == account_provider,
                            Account.node_id == limit_node,
                            Account.slug != slug,
                        )
                    ).scalar_one()
                if pinned_here >= row.max_accounts_per_node:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"node '{node_id}' already holds {pinned_here} {row.label} account(s), and "
                            f"{row.label} allows only {row.max_accounts_per_node} per node — so this one "
                            f"cannot be added there. That ceiling is a property of the vendor's own "
                            f"credential design, not a policy this route chose: it is recorded per "
                            f"provider in the catalogue (app/agent_providers.py, "
                            f"`max_accounts_per_node`), which is the single home for the number. "
                            f"Register the account on a node holding fewer than "
                            f"{row.max_accounts_per_node} of them."
                        ),
                    )
        # 409 if a fresh phase-1 for this slug on this node is in flight — a fast fail mirroring the
        # agent's per-slug flock (authoritative; add and re-auth share it). Decided from durable-workflow
        # state, which `CODE_TIMEOUT_SECONDS` ends, so no freshness window.
        from app.durable import accounts as durable_accounts

        # It refuses a duplicate, not a replacement: the wizard reads any 409 as "resume the running
        # attempt", which re-attached an operator who changed the login settings to the dead attempt. So
        # identical parameters → 409; different ones (`login_request` lists them) → cancel the live
        # attempt and proceed. A double-click still sends identical parameters and is refused.
        live_login = await run_in_threadpool(durable_accounts.login_request, node_id, slug)
        if personal_only and live_login is not None:
            # Never cancel or resume someone else's login from the personal path.
            await _require_own_login(durable_accounts, principal, node_id, slug)
        requested_login = {
            "reauth": body.command_type == "account_reauth",
            "peer_key": body.args["peer_key"],
            "provider": account_provider,
        }
        if live_login is None:
            in_flight = False
        elif live_login == requested_login:
            in_flight = True
        else:
            # `False`: it ended between the read and the cancel — nothing in flight either way.
            await run_in_threadpool(durable_accounts.cancel_login, node_id, slug)
            in_flight = False
        if in_flight:
            raise HTTPException(
                status_code=409,
                detail=f"{body.command_type} already in progress for slug '{slug}'",
            )

    if body.command_type in ("account_add_submit_code", "account_reauth_submit_code"):
        # Phase 2: the pasted authorization code, SEALED by the browser to the node's per-login key.
        # Wire-shape checks only (the node unseals and exchanges); no 409 guard on a
        # continuation. `_credential_safe_args` keeps the ciphertext out of the audit and response.
        slug = body.args.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=422,
                detail=f"{body.command_type} requires args.slug matching ^[a-z0-9][a-z0-9_-]{{0,31}}$",
            )
        if "code" in body.args:
            # A plaintext code is refused outright, not silently ignored beside a sealed one: the whole
            # point of the seal is that the code never crosses this control plane readable.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.command_type} does not accept a plaintext args.code; send args.sealed_code "
                    f"(the code sealed to the login's seal_key)"
                ),
            )
        sealed_code = body.args.get("sealed_code")
        if not is_wire_sealed(sealed_code):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.command_type} requires args.sealed_code: base64 of whole "
                    f"{CHUNK_CIPHERTEXT_BYTES}-byte chunks (max {SEALED_MAX_CHARS} chars)"
                ),
            )

    if body.command_type in ("account_add_await_login", "account_reauth_await_login"):
        # Phase 2 in redirect mode: only the slug. No 409 guard (a continuation, and deliberately
        # re-callable); the node's per-slug flock answers `in_progress_elsewhere`.
        slug = body.args.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=422,
                detail=f"{body.command_type} requires args.slug matching ^[a-z0-9][a-z0-9_-]{{0,31}}$",
            )

    if body.command_type in SLOT_ARG_TYPES:
        # Input validation for both slot verbs: naming the slot is required. The node's origin check is
        # the authoritative gate.
        project_id = body.args.get("project_id")
        number = body.args.get("number")
        if project_id is None or number is None:
            raise HTTPException(
                status_code=422,
                detail="args must include 'project_id' and 'number' for worktree commands",
            )
        try:
            int(number)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail="args.number must be a valid integer",
            )

    is_account_phase1 = body.command_type in ("account_add", "account_reauth")
    is_account_phase2 = body.command_type in ("account_add_submit_code", "account_reauth_submit_code")
    is_account_await = body.command_type in ("account_add_await_login", "account_reauth_await_login")
    # Verbatim by default for verbs with no arm below. The phase-1 login verbs' args come only from
    # `_ACCOUNT_PHASE1_CALLER_ARGS`; a new node arg must be added there on purpose.
    command_args = body.args
    if body.command_type in WORKTREE_TIER_TYPES:
        # A worktree verb must name a slot; `path` is then CP-decided (`_CALLER_MUST_NOT_SUPPLY`). This is the one place the requirement is stated.
        if body.args.get("project_id") is None or body.args.get("number") is None:
            raise HTTPException(
                status_code=422,
                detail="args must include 'project_id' and 'number' for worktree commands",
            )
        # Int-ness validated here: `_resolve_command_args` casts `int(number)` unguarded, so a bad value
        # would 500.
        try:
            slot_number = int(body.args["number"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail="args.number must be a valid integer",
            )
        # And the slot must be one the caller may drive — a per-row answer no feature key can express.
        _require_editable_slot(session, principal, node_id, int(body.args["project_id"]), slot_number)
        command_args = {k: v for k, v in body.args.items() if k not in _CALLER_MUST_NOT_SUPPLY}
    elif body.command_type == "account_add":
        # Where the node mints the token, CP-decided: a caller's `shared` is stripped and never kept. An
        # enrolment sends no flag (the row has its own `shared`; a True would strand the token on the
        # share). A boolean, never a path (`resolve_shared_auth_dir`). This is the audit copy; the workflow
        # injects its own (`authenticate_account`). An allowlist: this verb decides where a token lands.
        command_args = _with_resolved_provider(
            {k: v for k, v in body.args.items() if k in _ACCOUNT_PHASE1_CALLER_ARGS},
            account_provider,
        )
        # `born_shared` re-injects `shared` for exactly one case — a new account, of a provider whose CLI
        # can use the share, on a node reporting a mounted token share. The node's override is one-way
        # (`_login_start`, `shared_override`), so the value is `true` or absent, never `false`. Enrolments
        # and re-auths send nothing (`_account_slug_is_new`).
        if born_shared:
            command_args["shared"] = True
    elif body.command_type == "account_reauth":
        # The same allowlist on `account_reauth`, so a caller's `shared` never reaches the audit copy (inert
        # today, but not a value a caller may send). A separate arm, so the `shared` injection above stays
        # unreachable from a re-auth.
        command_args = _with_resolved_provider(
            {k: v for k, v in body.args.items() if k in _ACCOUNT_PHASE1_CALLER_ARGS},
            account_provider,
        )
    elif body.command_type in ("account_delete", "account_converge"):
        # `account_converge` requires a slug: `_require_account_access` keys on `args["slug"]`, so a
        # slugless converge would skip the floor.
        if body.command_type == "account_converge":
            slug_arg = body.args.get("slug")
            # The same type test the floor uses: a non-`str` slug would pass both otherwise.
            if not isinstance(slug_arg, str) or not slug_arg:
                raise HTTPException(
                    status_code=422, detail="account_converge requires a non-empty string args.slug",
                )
        # The same strip: `config_dir` is the path a host removes, CP-derived from (slug, target node). The
        # sanctioned deprovision routes (superadmin) call `dispatch_and_audit` directly.
        command_args = {k: v for k, v in body.args.items() if k not in _CALLER_MUST_NOT_SUPPLY}

    if is_account_phase1 or is_account_phase2 or is_account_await:
        # The durable engine owns waiting, retries and redeploys. `run_in_threadpool` because
        # the steps are synchronous and `app/durable/accounts.py::dispatch` needs a free serving loop.
        from app.durable import accounts as durable_accounts

        if personal_only and (is_account_phase2 or is_account_await):
            # A continuation reaches only the caller's own login: a code pasted into a colleague's
            # login would bind their new account to this caller's vendor identity.
            await _require_own_login(durable_accounts, principal, node_id, slug)

        # `command_args`, not `body.args`: the audit row records what the fleet executed (for `account_add`
        # they differ by the CP-decided `shared`).
        audit_args = _credential_safe_args(body.command_type, command_args)
        if is_account_phase1:
            status, result = await run_in_threadpool(
                durable_accounts.start_login, body.command_type, node_id, slug,
                # Retired and unread by the workflow (the declared mode decides); kept in position because
                # recorded inputs are a replay contract.
                True,
                # The creator as an id (workflow inputs are persisted and replayed); `None` stays a no-op.
                str(principal.user_id) if principal is not None else None,
                # The discriminator, carried rather than re-derived: after a replay the workflow's own
                # `ensure_row` would have inserted the row, and a retry would misread itself as an enrolment.
                # Meaningless for `account_reauth`.
                account_add_is_new,
                # The agent CLI must cross here too, or the operator gets a Claude login under their label.
                account_provider,
                # The share decision, carried: a persisted input cannot drift on replay.
                born_shared,
                # The wizard's per-login public key, validated above; the node seals the URL to it.
                peer_key,
                # Personal (the caller's own) or the org's; decided above, carried like the rest.
                account_personal,
            )
        elif is_account_await:
            # Not a second dispatch: the workflow is already polling the await verb; a second
            # `*_await_login` would race it on the node's per-slug flock.
            status, result = await run_in_threadpool(
                durable_accounts.await_login, node_id, slug
            )
        else:
            status, result = await run_in_threadpool(
                durable_accounts.submit_code, node_id, slug, sealed_code
            )
        # No audit row for a non-terminal await ping: on this engine it dispatches nothing, and the log
        # records executions. Terminal replies keep their row.
        if is_account_await and _payload_status_from_reply(result) == "awaiting_redirect":
            # `id=0` means "no row" here only; the wizard ignores this body, and making `CommandOut.id`
            # Optional would change every client's shape.
            return _echo(
                0, node_id, body.command_type, audit_args, status,
                _credential_safe_result(body.command_type, result, echo=True, provider=account_provider),
                user=user,
            )
        audit_id = _record_audit(
            session, node_id, body.command_type, audit_args, status,
            _credential_safe_result(body.command_type, result), user.email,
        )
        if body.command_type == "account_add" and status == "done":
            # Phase 1's disabled roster row is written at the route so a config dir is never stranded;
            # phase 2's enabled row is written in the workflow.
            with _account_row_session(session, foreign_node) as row_session:
                _ensure_account_row(
                    row_session, slug, result, enabled=False, node_id=node_id, principal=principal,
                    provider=account_provider, born_shared=born_shared,
                    # This insert is the one that wins, so the ownership choice and the name must ride it.
                    personal=account_personal, name=account_name,
                )
        return _echo(
            audit_id, node_id, body.command_type, audit_args, status,
            _credential_safe_result(body.command_type, result, echo=True, provider=account_provider),
            user=user,
        )

    resolved_args = _resolve_command_args(body.command_type, command_args, session, node_id)
    reply, audit_id = await dispatch_and_audit(
        session, node_id, body.command_type, resolved_args, user.email,
    )
    status = str(reply.get("status") or "failed")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else None
    return _echo(
        # The `command_audit_log` row id; never 0 (`_record_audit` raises).
        audit_id, node_id, body.command_type,
        # Both args scrubbers: `_credential_safe_args` (shared with the audit row) and `_echo_safe_args`
        # (echo-only).
        _echo_safe_args(body.command_type, _credential_safe_args(body.command_type, resolved_args)),
        status,
        # The reply scrubber too: a reply can be a live credential document.
        _credential_safe_result(body.command_type, result, echo=True, provider=account_provider),
        user=user,
    )
