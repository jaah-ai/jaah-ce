"""Interactive Console setup — POST /api/console/{project_id}/{number}/open (spec R5 / T14).

Setup only, not a byte relay: the host daemon (`apps/jaahd/console.py`) owns the PTY and the console
relay service (`app/console_relay/`, whose docstring has the whole flow) carries the bytes. This route
authenticates and authorizes against worktree visibility, asks the node to open a PTY over the node bus
with the node leg's single-use ticket, and answers the browser with its relay leg. Every refusal goes
through `_refuse()`. An API deploy leaves open consoles alone: none of their bytes pass here.

Node routing comes from the worktree row, not this stack's `NODE_ID` (`_resolve_console_node`). Inside this
module `node_id` is the node's uuid (as a string) and `project_id` the `projects.id`: names are display only, and
travel to the node only as its own local spelling of a slot (the frame's `project`, the direct token). The
daemon resolves the tmux target and enforces detach-not-kill. Console never leases and never creates: every
open attaches to the session already running in a task-held slot, and a free slot is refused. `GET /api/console/nodes/{node_id}/windows` also lives here. Direct mode (the two
HTTP routes at the bottom) is single-homed in `app/console_direct.py`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app import (
    account_auth, agent_providers, console_direct, edition_features, console_key_leases, fleet_verdicts, hipaa,
    node_bus,
    org_limits, placement_groups, project_identity, provider_auth_policy, secret_redact, slot_allocation, slot_state,
)
from app.auth import COOKIE_NAME, read_session, require_user, session_ref, session_row_is_live
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import feature, session_only
from app.authz.principal import (
    AsyncCaller,
    Principal,
    RevokedSession,
    current_caller,
    current_principal,
    current_principal_async,
    org_admin_writer,
)
from app.authz.visibility import (
    editable_task_vm_clause,
    scoped_node_select,
    visible_project_clause,
    visible_task_vm_clause,
    worktree_may_edit,
)
from app.console_relay import tickets as relay_tickets
from app import db_async
from app.db_async import DbDep, on_session
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.nodes import Microvm, Node
from app.models.projects import Project
from app.models.tasks import Task
from app.entity_shape import get_or_404, optional_uuid, or_404
from app.models.worktrees import Worktree
from app.node_identity import (
    bus_key,
    names_by_id,
    node_id_or_none,
    node_matches,
    node_ref,
)
from app.ratelimit import SharedRateLimiter
from app.tenancy import seam

# Single home for the node-bus request envelope and the 503-not-connected / 504-no-reply split.
from app.routes.commands import _dispatch as _dispatch_to_node
from app.node_identity import cp_node_id

router = APIRouter(tags=["console"])

# Making a console's own slot is a declaration: the bar `routes/pool_slots.py::require_slot_writer` sets.
_require_slot_maker = org_admin_writer("Worktree slots")
log = logging.getLogger("cockpit-console")

# Bounds the `console_open` round trip (the daemon's tmux find).
CONSOLE_CONNECT_TIMEOUT = float(os.environ.get("CONSOLE_CONNECT_TIMEOUT", "40"))
# Where the node dials the relay, when the node cannot reach it at its control-plane URL's own host
# (a local compose stack). Unset on ECS, where the ALB routes the path.
_RELAY_NODE_URL = os.environ.get("CONSOLE_RELAY_NODE_URL", "").strip()

# WS close codes (RFC 6455 application range).
_WS_UNAUTHORIZED = 4401
_WS_NOT_FOUND = 4404
_WS_UPSTREAM_UNAVAILABLE = 4502
# The relay's own plumbing broke while the node-side session is untouched — the browser retries on this,
# unlike a plain 1000.
_WS_RELAY_FAILED = 4503
# The HTTP status a refused open answers with, beside the close code the pane actually reads. Never 502: a
# refusal is this API's verdict, not a bad gateway (a proxy's 502 is what the pane must keep retrying).
_REFUSAL_HTTP_STATUS = {
    _WS_UNAUTHORIZED: 401, _WS_NOT_FOUND: 404, _WS_UPSTREAM_UNAVAILABLE: 409, _WS_RELAY_FAILED: 503,
}
# A refusal's `detail` when its `reason` says nothing a person can read.
_REFUSAL_DETAIL = {
    _WS_UNAUTHORIZED: "Not authorized", _WS_NOT_FOUND: "Slot not found",
    _WS_UPSTREAM_UNAVAILABLE: "No session running on this slot",
}
_NODE_UNAVAILABLE_DETAIL = "Node unavailable — try again shortly"
# A free slot has no session to attach to, and a console never creates one.
_FREE_SLOT_DETAIL = "This slot is free. A console opens only on a task's slot."

# Direct mode's own bucket, keyed on the session, never `check_rate_limit` (shared with
# `/api/public/settings` and `POST /api/auth/*`, so a reconnect storm would eat the login budget). Every
# budget here is `SharedRateLimiter` — counted in the database, so it holds across the N control-plane
# tasks a session's requests may land on; a per-process count would be N× the stated limit.
_DIRECT_RATE_LIMIT_PER_MINUTE = 30
_direct_limiter = SharedRateLimiter(limit=_DIRECT_RATE_LIMIT_PER_MINUTE, bucket="console-direct")


def _check_direct_rate_limit(user: AdminUser = Depends(require_user)) -> AdminUser:
    """Bound one session's direct-console issuance, returning the user so `require_user` runs once."""
    if not _direct_limiter.allow(console_direct.session_id_for(user.id, user.session_epoch)):
        raise HTTPException(status_code=429, detail="too many direct console requests")
    return user


# Revocation's own, far larger budget: sharing issuance's would 429 the burst that abandons the most
# grants, stranding live 24h grants on the node.
_DIRECT_REVOKE_RATE_LIMIT_PER_MINUTE = 120
_direct_revoke_limiter = SharedRateLimiter(
    limit=_DIRECT_REVOKE_RATE_LIMIT_PER_MINUTE, bucket="console-direct-revoke",
)


def _check_direct_revoke_rate_limit(user: AdminUser = Depends(require_user)) -> AdminUser:
    """Bound one session's direct-console revocations, on their own bucket."""
    if not _direct_revoke_limiter.allow(console_direct.session_id_for(user.id, user.session_epoch)):
        raise HTTPException(status_code=429, detail="too many direct console revocations")
    return user

# ── Fleet window enumeration (GET /api/console/nodes/{node_id}/windows) ────────────────────────────────

# Mirrors `apps/jaahd/console.py`'s `_VIEW_ID_RE` (re-validated node-side). `\A`/`\Z`, never `^`/`$`:
# `$` also matches before a trailing newline.
_VIEW_ID_RE = re.compile(r"\A@[0-9]{1,18}\Z")
# Character-level only; the grammar and bounds are the node's, so it can grow without a CP redeploy.
_LAYOUT_SPEC_RE = re.compile(r"\A[@0-9rc(),]{1,300}\Z")

# Not `commands.COMMAND_TIMEOUT` (180s, for provisioning): a browser-facing GET.
_FLEET_WINDOWS_TIMEOUT = 15.0


class FleetWindowOut(BaseModel):
    """One window of the shared `fleet` tmux session.

    `id` (`@N`, matching `worktrees.tmux_window_id`) is the identity. `index` is a display hint only:
    `renumber-windows` renumbers successors on a kill."""

    id: str
    index: int
    name: str


class FleetWindowsOut(BaseModel):
    windows: list[FleetWindowOut]


@router.get("/api/console/nodes/{node_id}/windows", response_model=FleetWindowsOut)
@feature("system", "view")
async def list_fleet_windows(
    node_id: str,
    caller: AsyncCaller = Depends(current_caller),
    *,
    session: DbDep,
) -> FleetWindowsOut:
    """The shared `fleet` session's windows on this node, in window-index order, minus internal plumbing.

    Gated by `require_user` plus a node this caller may see via `scoped_node_select` (so "no such node"
    and "not granted" are one 404, as `GET /api/nodes/{node_id}`). The listing and `_holder` exclusion
    happen node-side (`apps/jaahd/fleet_windows.py`); this hop must not learn the excluded name."""
    # The uuid only: names repeat (`app/hosts.py` NAMING), so a name names no row.
    node = or_404(
        (await session.execute(
            scoped_node_select(caller.principal).where(node_matches(node_id)).with_only_columns(Node.id, Node.name)
        )).first(),
        detail=f"unknown node '{node_id}'",
    )
    # Read only: the connection goes back before the node bus round trip.
    await db_async.release(session)

    # Refusals and logs quote the name; the socket is addressed by the uuid.
    reply = await _dispatch_to_node(
        node.name, "fleet_windows", {}, bus_target=str(node.id), timeout=_FLEET_WINDOWS_TIMEOUT,
    )
    if reply.get("status") != "done":
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        detail = result.get("reason") or result.get("stderr") or "node could not list its windows"
        raise HTTPException(status_code=502, detail=str(detail)[:200])
    try:
        # Unreadable means a version mismatch between node and CP — a bad upstream reply, not "no windows".
        document = json.loads((reply.get("result") or {}).get("stdout") or "")
        windows = document["windows"]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        log.warning("fleet windows: node %s returned an unreadable listing: %s", node_id, exc)
        raise HTTPException(status_code=502, detail="node returned a malformed window listing") from exc
    return FleetWindowsOut(windows=[FleetWindowOut(**window) for window in windows])


async def _authenticated_admin_id(connection: HTTPConnection) -> UUID | None:
    """Validate the session cookie exactly as REST's `require_user` does (serializer, max-age,
    session_epoch, disabled), and refuse a session still owing the Terms (`auth.TERMS_PENDING_PATHS`). Returns
    the admin id, or None to reject. Never weaker than `require_user`."""
    claims = read_session(connection.cookies.get(COOKIE_NAME))
    if claims is None or claims.terms is not None:
        return None
    with seam.identity_scope():
        live = await db_async.run_in_session(
            _session_still_live, claims.admin_id, claims.epoch, claims.iepoch, identity=True,
        )
    return claims.admin_id if live else None


def _session_still_live(session: Session, admin_id: UUID, token_epoch: int, token_iepoch: int) -> bool:
    """Whether the row still holds the session `(admin_id, token_epoch)` names — asked at the open; the
    running console is re-asked by the sweep (`auth.session_ref`). The row only, not the cookie's idle window:
    an open console IS activity. Read as identity (`seam.identity_scope`, as `authz._resolve_principal`): an
    operator acting as a tenant org has their row in their own org, invisible under the one acted as — every
    acting open was refused 4401. The caller binds the scope around the session."""
    return session_row_is_live(session.get(AdminUser, admin_id), token_epoch, token_iepoch)


def _console_read_only(connection: HTTPConnection) -> bool | None:
    """Is this caller's console one-way? True = read-only, False = the full two-way terminal, None = DENY.

    The one place the tier is decided for both transports: `worktrees:view` streams output only,
    `worktrees:edit` gets two-way (`Principal.has`). `RevokedSession` is caught because an
    `HTTPException` in a WebSocket handler carries no close code. A `None` principal (the cookie-less
    agent identity) is denied: neither route declares it.
    """
    try:
        principal = current_principal(connection)
    except RevokedSession:
        return None
    if principal is None:
        return None
    return not principal.has(permission_key("worktrees", "edit"))


def _console_slot_editable(
    session: Session, connection: HTTPConnection, project_id: int, number: int, node_id: str,
) -> bool | None:
    """May this caller DRIVE this one slot? True = yes, False = visible but read-only, None = DENY.

    The per-slot companion to `_console_read_only` (`authz.worktree_may_edit` is the probe). Every denial fails
    closed. Called with the node already resolved — `(project_id, number)` collides across nodes.
    """
    try:
        principal = current_principal(connection)
    except RevokedSession:
        return None
    # `node_ref` fails closed (unresolvable → NULL → deny).
    may_edit = worktree_may_edit(
        session, principal,
        Worktree.node_id == node_ref(node_id), Worktree.project_id == project_id, Worktree.number == number,
    )
    if may_edit and _easy_mode_view_only(session, principal, project_id):
        return False
    return may_edit


def _easy_mode_view_only(session: Session, principal: Principal | None, project_id: int) -> bool:
    """An Easy Mode project's sessions are view-only to every tenant principal (D6): the client watches its app being
    built and types nothing. The operator — on its own plane, or acting as the org — keeps the two-way terminal."""
    if principal is not None and (principal.is_operator or principal.acting):
        return False
    return bool(session.scalar(select(Project.easy_mode).where(Project.id == project_id)))


# The console slot kinds, single-homed so the relay's clamp and the revoke route's wildcard cannot drift;
# `clauded` is the only kind.
CONSOLE_KINDS = ("clauded",)

def _may_name_window(connection: HTTPConnection) -> bool:
    """Whether this caller may point a console at a window other than the slot it dialled.

    `?view=@N` / `?layout=` name windows of the node's shared `fleet` session, which the node does not tie
    to `(project, number)` — a second slot claim the per-slot gate misses. Under per-slot editability
    that would escalate one driveable slot to every runner on the host, so only superadmins
    (and the agent identity), for whom `worktree_editable_clause` is `true()`, may. Not
    `sees_all_worktrees`: the wide keys widen visibility only."""
    try:
        principal = current_principal(connection)
    except RevokedSession:
        return False
    return principal is None or bool(principal.is_superadmin)


# `?view=vm:<vm_id>`: a MicroVM task's own terminal, or a console VM's for its opener. The node's spelling
# (`apps/jaahd/console.py::console_key_target`); the CP resolves it to a row before anything is sent.
_VM_VIEW_PREFIX = "vm:"


@dataclass(frozen=True)
class _TaskVmConsole:
    node: str
    may_edit: bool
    # The checked id in its canonical spelling — what the node is sent, never the caller's string.
    view: str


def _task_vm_console(
    session: Session, connection: HTTPConnection, project_id: int, number: int, view: str,
) -> _TaskVmConsole | None:
    """The running VM `?view=vm:<vm_id>` names, as this caller may see it — or None (DENY, the 4404 path).

    A VM session has no slot, so the VM row stands where the worktree row does: its node routes the open. A task
    VM's read/write decision is its TASK's (`visible_task_vm_clause`/`editable_task_vm_clause`); a console VM
    (`console_new_session`) is its opener's alone, as `console_vm` reads it, and they drive it. Not
    `_may_name_window`'s superadmin bar: that guards naming an arbitrary window of the node's tmux server, while
    this resolves to exactly one row the caller's own grant reaches. The one spelling is `(the VM's project id, 0)`;
    anything else, another person's console VM, or a VM not running is refused alike (no oracle)."""
    if not edition_features.enabled("microvm"):
        return None  # The community edition has no MicroVMs.
    from app import microvm_placement, microvm_rail

    vm_id = optional_uuid(view[len(_VM_VIEW_PREFIX):])
    if vm_id is None:
        return None
    # One spelling only: `UUID()` also takes braces, `urn:` and unhyphenated forms the node would key apart.
    if number != 0 or str(vm_id) != view[len(_VM_VIEW_PREFIX):]:
        return None
    try:
        principal = current_principal(connection)
    except RevokedSession:
        return None
    running = (
        Microvm.id == vm_id,
        Microvm.state == microvm_placement.RUNNING,
        Microvm.project_id == project_id,
        Microvm.node_id.isnot(None),
    )
    row = session.execute(
        select(Microvm.node_id, editable_task_vm_clause(principal).label("may_edit"))
        .select_from(Microvm)
        .join(Task, Task.id == Microvm.task_id)
        .where(Microvm.kind == microvm_rail.KIND_TASK, *running)
        .where(visible_task_vm_clause(principal))
    ).first()
    # Not while acting: `user_id` attributes an acting tab, it never admits one (`Principal.user_id`).
    if row is None and principal is not None and principal.user_id is not None and not principal.acting:
        node_id = session.scalar(
            select(Microvm.node_id)
            .where(Microvm.kind == microvm_rail.KIND_CONSOLE, Microvm.created_by_id == principal.user_id, *running)
        )
        if node_id is not None:
            # Driven only while the opener still clears the bar that opened it (`console_new_session`'s
            # administrator bar and the project in reach); otherwise they watch it.
            may_edit = principal.is_org_admin and session.scalar(
                select(Project.id).where(Project.id == project_id, visible_project_clause(principal))
            ) is not None and not _easy_mode_view_only(session, principal, project_id)
            return _TaskVmConsole(node=str(node_id), may_edit=may_edit, view=f"{_VM_VIEW_PREFIX}{vm_id}")
    return None if row is None else _TaskVmConsole(
        node=str(row.node_id), may_edit=bool(row.may_edit) and not _easy_mode_view_only(session, principal, project_id),
        view=f"{_VM_VIEW_PREFIX}{vm_id}")


# One detail for both direct-mode 404s: this route mints a credential, so distinguishable messages would
# be an oracle for which slots exist.
_NO_SUCH_SLOT = "no such console slot"


def _console_worktree_path(session: Session, project_id: int, number: int, node_id: str) -> str | None:
    """This slot's on-disk path, sent in `console_open` so the node need not ask the CP for it: that
    lookup (`GET /api/agent/worktrees`) was a second CP round trip on every open. None leaves the node
    to look it up itself. Read after authorization, so it describes a slot the caller may already see.
    A failed read is None too: the node's own lookup is the fallback, so it never costs the open."""
    try:
        return _read_worktree_path(session, project_id, number, node_id)
    except Exception:  # noqa: BLE001 — an optimisation; the node falls back to its own lookup
        log.exception("console: reading %s-%s's path failed; the node will look it up", project_id, number)
        return None


def _read_worktree_path(session: Session, project_id: int, number: int, node_id: str) -> str | None:
    return session.scalar(
        select(Worktree.path).where(
            Worktree.node_id == node_ref(node_id),
            Worktree.project_id == project_id,
            Worktree.number == number,
        )
    ) or None


def _console_guard(session: Session, project_id: int) -> dict | None:
    """The Architectural Guard stanza a console open carries (`lease_drain.guard_stanza`), or None where the guard
    is unavailable — and then the frame is exactly what it was. A failed read is None too: an unguarded console is
    the guard's own fail-open answer (G-1), never worth refusing the open over."""
    try:
        from app.lease_drain import guard_stanza

        return guard_stanza(session, project_id)
    except Exception:  # noqa: BLE001 — see the docstring
        log.exception("console: reading project %s's guard availability failed; opening unguarded", project_id)
        return None


def _console_names(session: Session, project_id: int, node_id: str) -> tuple[str | None, str | None]:
    """`(project name, node name)` for an open: the node's own spelling of the slot (the
    frame's `project`) and of itself (the ticket's `node_name`, the relay's second binding). None when either
    row is gone (or out of the caller's reach), which refuses the open as an unknown slot."""
    node_uuid = node_id_or_none(session, node_id)
    return (
        project_identity.names_by_id(session, [project_id]).get(project_id),
        names_by_id(session, [node_uuid]).get(node_uuid) if node_uuid is not None else None,
    )


def _console_account(session: Session, node_id: str, provider: str | None = None) -> str | None:
    """The credential account a `clauded` console create runs on — the CP's decision, sent on the frame.

    Reuses `_best_account` so a console and a fleet runner cannot pick differently (imported at call
    time to keep the scheduler off the WebSocket path): its lanes apply (an Anthropic API-key account
    only as spillover, DeepSeek as a peer), and so does Auto-assign when `provider` is None — a named
    provider keeps its Auto-assign-off accounts. `node_id` is required: an OAuth account's directory is
    per-node. Returns the wire reference, a config dir or `apikey:<slug>`; None is a verdict — the node
    refuses the create rather than use the default dir.
    """
    from app.authz.visibility import AllowedObjects
    from app.node_identity import node_org
    from app.placement_grants import org_floor, shareable_account_floor
    from app.routes.fleet import _best_account

    # The node's org is the caller's to resolve (`_best_account` takes it); one read per open.
    org = node_org(session, node_id)
    if org is None:
        # An org-less node has no floor, so refuse.
        return None
    # No runner cap: a console is a human, not counted by `app/account_runners.py`; quota gates still
    # apply. The node's org floor (`placement_grants.org_floor`) keeps a tenant's node off another
    # org's account, of every provider and auth kind, whatever the seam mode — it matters under
    # `operator_scope()` or SHADOW.
    # Never a personal account: the console pick names no user, and personal stays personal (G3).
    floor = shareable_account_floor(session, org_floor(session, "account", org))
    # A tab builds only a settings-helper key (`AgentProvider.api_key_helper`); `clauded_wt.py` refuses
    # any other API-key account, so one is never offered. Nor is any account of a provider with no terminal
    # view (`capabilities.console.terminal_view`): its CLI has no tab to open.
    no_tab = set(session.scalars(select(Account.id).where(
        Account.id.in_(floor), Account.auth_kind == account_auth.AUTH_KIND_API_KEY,
        Account.provider.notin_([p.value for p in agent_providers.PROVIDERS if p.api_key_helper]),
    )).all()) | set(session.scalars(select(Account.id).where(
        Account.id.in_(floor),
        Account.provider.in_([p.value for p in agent_providers.PROVIDERS
                              if not p.capabilities.console.terminal_view]),
    )).all())
    return _best_account(
        session, node_id, node_org_id=org,
        allowed=AllowedObjects(unrestricted=False, ids=floor - no_tab),
        provider=provider, oauth_only=False, apply_runner_cap=False,
    )


def _console_provider(raw: str | None) -> tuple[str | None, bool]:
    """`(provider, ok)` for the `provider` a console open asked for: absent is `(None, True)` (Auto-assign
    picks), a catalogue value passes, anything else is `(None, False)` — refused as "unknown provider"."""
    if not raw:
        return None, True
    return (raw, True) if agent_providers.by_value(raw) is not None else (None, False)


def _provider_off_plan(session: Session, org_id: UUID | None, provider: str | None) -> str | None:
    """The refusal for a named `provider` with no cell on the caller's org's provider grid (`provider_auth_policy.
    provider_on_plan`), else None. Asked before anything is created; Auto-assign (None) is `_best_account`'s."""
    if provider is None or provider_auth_policy.provider_on_plan(session, org_id, provider):
        return None
    return provider_auth_policy.refusal(session, org_id, provider)


@dataclass(frozen=True)
class _PickedAccount:
    """What both console frames carry about the pick: `config_dir` XOR `api_key_account` (both None when
    nothing qualified), and `provider` — the picked account's, or the requested/default one with no pick."""

    config_dir: str | None
    api_key_account: str | None
    provider: str
    account_id: UUID | None = None
    org_id: UUID | None = None


def _picked_account(session: Session, ref: str | None, requested: str | None) -> _PickedAccount:
    """Split `_console_account`'s wire reference into the frame fields, reading the account's provider."""
    fallback = requested or agent_providers.DEFAULT_PROVIDER
    if not ref:
        return _PickedAccount(config_dir=None, api_key_account=None, provider=fallback)
    is_api_key = account_auth.is_api_key_account_ref(ref)
    slug = account_auth.account_slug_from_ref(ref)
    row = None
    if slug:
        row = session.execute(
            select(Account.id, Account.provider, Account.org_id).where(Account.slug == slug)
        ).first()
    return _PickedAccount(
        config_dir=None if is_api_key else ref,
        api_key_account=(slug or None) if is_api_key else None,
        provider=row.provider if row is not None else fallback,
        account_id=row.id if row is not None else None,
        org_id=row.org_id if row is not None else None,
    )


def _pick_console_account(session: Session, node_id: str, provider: str | None) -> _PickedAccount:
    """The open's account pick: `_console_account`'s pick for `provider`."""
    return _picked_account(session, _console_account(session, node_id, provider), provider)


def _lease_console_key(session: Session, node_id: str, picked: _PickedAccount, user_id: UUID | None) -> bool:
    """Authorize `node_id` to fetch the picked API-key account's key (§3.5a) — a fresh row per create.

    A no-op True for an OAuth pick or no pick. False when the lease could not be written: the node's key
    fetch would 404, so the caller refuses the open rather than start a console that cannot log in."""
    if picked.api_key_account is None:
        return True
    if picked.account_id is None or picked.org_id is None or user_id is None:
        log.warning("console: no key lease for %s on %s — the account or user did not resolve",
                    picked.api_key_account, node_id)
        return False
    try:
        node_uuid = node_id_or_none(session, node_id)
        if node_uuid is None:
            return False
        console_key_leases.grant(
            session, node_id=node_uuid, account_id=picked.account_id, org_id=picked.org_id,
            created_by=user_id,
        )
        session.commit()
    except Exception:  # noqa: BLE001 — refused below, never a 1011 mid-handshake
        log.exception("console: writing the key lease for %s on %s failed",
                      picked.api_key_account, node_id)
        session.rollback()
        return False
    log.info("console: key lease written for account=%s node=%s user=%s",
             picked.api_key_account, node_id, user_id)
    return True


def _console_kind(raw: str | None) -> str:
    """Which session the daemon should attach: `clauded`, the only kind.

    UI input, not a security boundary — anything else clamps. Shared by the relay and direct issuance;
    not used by the revoke route, where a clamp would revoke a slot the caller did not name."""
    return raw if raw in CONSOLE_KINDS else "clauded"


def _int_query_param(connection: HTTPConnection, name: str) -> int | None:
    """A query param coerced to int, or None when absent/non-numeric (the daemon defaults the size)."""
    value = connection.query_params.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _slot_is_free(session: Session, project_id: int, number: int, node_id: str) -> bool:
    """Whether this slot is held by nobody: it has no session to attach to, so an open is refused."""
    return session.scalar(
        select(Worktree.state).where(
            Worktree.node_id == node_ref(node_id), Worktree.project_id == project_id, Worktree.number == number,
        )
    ) == slot_state.FREE


async def _discard_tickets(minted: Any) -> None:
    """Discard the tickets an open that gave no console minted. `relay_tickets` opens its own sync session: off the
    loop."""
    await asyncio.to_thread(relay_tickets.discard, minted)


# The discards a cancelled open left running, held until done (an unreferenced task is collectable).
_abandoning: set[asyncio.Task] = set()


def _discard_in_background(minted: Any) -> None:
    """`_discard_tickets` for a request being cancelled, which cannot await its cleanup: its own task."""
    task = asyncio.get_running_loop().create_task(_discard_tickets(minted))
    _abandoning.add(task)
    task.add_done_callback(_abandoning.discard)


def _resolve_console_node(
    project_id: int, number: int, requested: str | None, session: Session,
) -> str | None:
    """Authorization and node routing in one read: returns the uuid (as a string) of the node that hosts
    this worktree, or None when the caller may not open a console for it (the 4404 path).

    Authorization reduces to the worktree existing in the DB (tighten here for per-user ACLs). The node
    comes from the worktree row, never `resolve_node_id()`. `requested` (`?node=`, the node uuid) scopes the
    lookup through `node_ref`, so a name or unknown uuid matches nothing; omitted, it resolves by
    (project_id, number), lowest node name first, preferring this stack's own. Never raises: an
    `HTTPException` on a WebSocket surfaces as a bare 500. On the caller's session (last: the routes call it
    inside `run_sync`)."""
    stmt = (
        select(Worktree.node_id)
        .select_from(Worktree)
        .join(Node, Node.id == Worktree.node_id)
        .where(Worktree.project_id == project_id, Worktree.number == number)
        .order_by(Node.name, Node.id)
    )
    if requested:
        stmt = stmt.where(Worktree.node_id == node_ref(requested))
    nodes = list(session.scalars(stmt).all())
    if not nodes:
        return None
    # This stack's own row (`node_identity.cp_node_id`: a uuid, or the CP's operator-org name); none → `nodes[0]`.
    local_uuid = cp_node_id(session)
    return str(local_uuid if local_uuid in nodes else nodes[0])


class ConsoleNewSessionIn(BaseModel):
    """`node`: the node to make the slot on, by its uuid; absent takes the first assigned node with room."""

    node: str | None = None


class ConsoleNewSessionOut(BaseModel):
    """The VM a new console session runs in, on a MicroVM node: `POST .../0/open?view=vm:<vm_id>` opens it.
    `number` is 0. `node_id` is the node's name (display); `node_uuid` is what `?node=` takes."""

    project: str
    project_id: int
    number: int
    node_id: str
    node_uuid: str
    vm_id: UUID | None = None


def _microvm_console_node(session: Session, project_row: Project, org_id: UUID, named: Node | None) -> Node | None:
    """The MicroVM node a new console session runs on (D22): the named node when it is one, else the first of
    the enabled MicroVM nodes the project may start work on (`slot_allocation.placement_node_ids`) in the caller's org with room, by name. None: no MicroVM node
    serves it."""
    if not edition_features.enabled("microvm"):
        return None  # The community edition has no MicroVMs.
    from app import microvm_placement

    if named is not None:
        if not named.microvm:
            return None
        # A disabled node, or one outside the project's node groups, never runs its sessions.
        if not named.enabled:
            raise HTTPException(status_code=409, detail=f"node '{named.name}' is disabled")
        if not placement_groups.is_allocated_node(session, named.id, project_row.id):
            raise HTTPException(
                status_code=409, detail=f"node '{named.name}' is not in project '{project_row.name}''s node groups",
            )
        return named
    assigned = slot_allocation.placement_node_ids(session, project_row.id)
    candidates = sorted(
        (n for n in session.scalars(select(Node).where(Node.id.in_(assigned), Node.microvm.is_(True))).all()
         if n.org_id == org_id and n.enabled),
        key=lambda n: n.name,
    )
    # Keyed by the node uuid string (`microvm_placement.capacity`): names repeat across orgs.
    room = microvm_placement.capacity(session, project_row.id, [str(n.id) for n in candidates])
    chosen = next((n for n in candidates if room.get(str(n.id)) and room[str(n.id)].room > 0), None)
    if chosen is None and candidates:
        # The project runs on MicroVM nodes and none has room: say why.
        raise HTTPException(
            status_code=409, detail="; ".join(room[str(n.id)].why for n in candidates if str(n.id) in room),
        )
    return chosen


def _new_microvm_console(
    session: Session, node: Node, project_row: Project, *, user_id: UUID, org_id: UUID,
) -> ConsoleNewSessionOut:
    """Open a console session in its own VM on a MicroVM node (D22): no `worktrees` row. 409
    when the node admits no VM now, or no account is usable."""
    if not edition_features.enabled("microvm"):
        raise HTTPException(status_code=409, detail="no MicroVM room")  # Unreached: `_microvm_console_node`.
    from app import microvm_lifecycle, microvm_placement

    if node.org_id != org_id:
        raise HTTPException(status_code=409, detail=f"node '{node.name}' is not in your organization")
    cap = microvm_placement.capacity(session, project_row.id, [str(node.id)]).get(str(node.id))
    if cap is None or cap.room <= 0:
        raise HTTPException(status_code=409, detail=cap.why if cap is not None else "no MicroVM room")
    # On this request's session: it already holds the connection, and a second session would be a second one.
    picked = _picked_account(session, _console_account(session, str(node.id), None), None)
    account = (session.scalars(select(Account).where(Account.id == picked.account_id)).first()
               if picked.account_id is not None else None)
    if account is None:
        raise HTTPException(status_code=409, detail="no account is usable for a console on this node")
    vm, issued = microvm_placement.open_console(
        session, node, project_row, account=account, provider=picked.provider, created_by=user_id,
    )
    vm_id = vm.id
    session.commit()
    # The key lease only once the VM exists, and before the orders reach the node (it fetches the key at launch).
    if not _lease_console_key(session, str(node.id), picked, user_id):
        issued_destroy = microvm_lifecycle.destroy_console(
            session, vm.console_session_id, reason="console account key could not be authorized")
        session.commit()
        for batch in issued_destroy:
            batch.send()
        raise HTTPException(status_code=409, detail="console account key could not be authorized")
    issued.send()
    return ConsoleNewSessionOut(
        project=project_row.name, project_id=project_row.id, number=0, node_id=node.name, node_uuid=str(node.id),
        vm_id=vm_id,
    )


@router.post("/api/console/{project_id}/new-session", response_model=ConsoleNewSessionOut,
             response_model_exclude_none=True)
# `edit`, and the declare route's administrator bar: this makes a VM, then the open drives it.
@feature("worktrees", "edit")
@on_session
def console_new_session(
    session: Session, request: Request, project_id: int, body: ConsoleNewSessionIn | None = None,
    user: AdminUser = Depends(_require_slot_maker),
) -> ConsoleNewSessionOut:
    """Open a NEW console session in its own VM on a MicroVM node (named, or the project's first with room;
    `_new_microvm_console`); its end destroys the VM (`routes/agent_microvms.py`). Without a MicroVM node it is
    refused (409): a console never opens a session in a pool slot, repository or folder."""
    principal = current_principal(request)
    if principal is None or principal.user_id is None or _console_read_only(request) is not False:
        raise HTTPException(status_code=403, detail="this account cannot drive a console")
    org_id = principal.org_id
    if org_id is None:
        raise HTTPException(status_code=409, detail="this account belongs to no organization, so it holds no slot")
    project_row = project_identity.project_or_404(session, project_id)
    if _easy_mode_view_only(session, principal, project_id):
        raise HTTPException(status_code=403, detail="this account cannot drive a console")
    node = None
    if body is not None and body.node:
        node = get_or_404(session, Node, node_id_or_none(session, body.node), detail=f"no node {body.node!r}")
    # A MicroVM node runs the session in its own VM, repository or not (D22). A console VM spends the org's
    # Agents headroom.
    vm_node = _microvm_console_node(session, project_row, org_id, node)
    if vm_node is not None:
        for kind in org_limits.SESSION_KINDS:
            org_limits.require_headroom(session, org_id, kind)
        return _new_microvm_console(session, vm_node, project_row, user_id=principal.user_id, org_id=org_id)
    raise HTTPException(
        status_code=409, detail="A new console session needs a MicroVM node. Open a task's slot instead.",
    )


class ConsoleVmOut(BaseModel):
    """A new session's VM as the person who opened it sees it: its state, and when it `failed` the node's sentence
    in operator words (`microvm_lifecycle.operator_sentence`)."""

    vm_id: UUID
    state: str
    reason: str | None = None


@router.get("/api/console/{project_id}/vms/{vm_id}", response_model=ConsoleVmOut)
# The new-session route's bar: only who could open the session reads how its start went.
@feature("worktrees", "edit")
@on_session
def console_vm(
    session: Session,
    request: Request,
    project_id: int,
    vm_id: UUID,
    user: AdminUser = Depends(_require_slot_maker),
) -> ConsoleVmOut:
    """How the console VM `console_new_session` opened is doing, for its opener's Worktrees page. 404 for any VM that
    is not this caller's own console session in this project (the same answer whether or not it exists)."""
    if not edition_features.enabled("microvm"):
        raise HTTPException(status_code=404, detail="no such session")  # The community edition has no MicroVMs.
    from app import microvm_lifecycle, microvm_rail

    principal = current_principal(request)
    # A select, not `session.get`: the org scope applies (`test_tenancy_bypass_sites.py`).
    vm = get_or_404(session, Microvm, vm_id, detail="no such session")
    if (principal is None or principal.user_id is None or vm.kind != microvm_rail.KIND_CONSOLE
            or vm.project_id != project_id or vm.created_by_id != principal.user_id):
        raise HTTPException(status_code=404, detail="no such session")
    # Redacted as `microvm_lifecycle.failure_of` redacts it: node text bound for a browser.
    reason = secret_redact.redact(microvm_lifecycle.operator_sentence(vm.wait_reason))[0] if vm.wait_reason else None
    return ConsoleVmOut(vm_id=vm.id, state=vm.state, reason=reason)


@router.post("/api/console/{project_id}/{number}/open")
# `view`, not `edit`: the open's tier is decided by `_console_read_only`, narrowed per slot by
# `_console_slot_editable`, and enforced by the relay (`read_only` on the ticket row) and on the node.
# `CALLER_PLANE`: a POST only to mint its ticket, an open lets an operator reach any slot the pool lists
# (`GET /api/worktrees`, the fleet plane) — `ORG_SCOPED` bound a POST to the operator's own org and 404'd every
# other org's slot. Every open only attaches. A tenant stays on their own org.
@feature("worktrees", "view", tenancy=Tenancy.CALLER_PLANE)
async def console_open(request: Request, project_id: int, number: int) -> JSONResponse:
    """Set one console session up and hand the browser its relay leg (`app/console_relay/`).

    Everything that needs the viewer's identity happens here — auth, the tier, `console_open` over the node
    bus — and the bytes then flow through the relay, never this process. It only ever attaches: a free slot is
    refused (409), and a task-held one attaches to the session running in it. Answers `{"url": <relay path with
    the browser's ticket>}`, or a refusal carrying the WebSocket close code the pane's reconnect keys on
    (`_refuse`)."""
    # Only the console's own page may open one: the session cookie is `SameSite=lax`, so any same-SITE origin
    # (every `*.jaah.ai` dev stack) could otherwise POST here and mint a ticket. An absent header is a
    # non-browser client — direct mode's same rule.
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        return _refuse(_WS_UNAUTHORIZED, reason="open the console from the console itself")
    kind = _console_kind(request.query_params.get("kind"))
    # Passed through as-is: the daemon clamps and validates, so this is not the trust boundary.
    cols = _int_query_param(request, "cols")
    rows = _int_query_param(request, "rows")
    # Authenticate/authorize before relaying a byte (see `_reject`).
    admin_id = await _authenticated_admin_id(request)
    if admin_id is None:
        return _refuse(_WS_UNAUTHORIZED)
    # `?provider=` names the provider the frame carries; absent lets Auto-assign pick.
    provider, provider_ok = _console_provider(request.query_params.get("provider"))
    if not provider_ok:
        return _refuse(_WS_NOT_FOUND, reason="unknown provider")
    # The principal, resolved on the loop (an identity read on the async pool, `app/db_async.py`) before the sync
    # helpers below read it from the request's memo; a refusal is theirs to answer, as before.
    try:
        await current_principal_async(request)
    except RevokedSession:
        pass
    # The tier, decided once. `None` means two auth paths disagree, so deny. 4401, not a new code:
    # `ConsolePane.tsx`'s NON_RETRYABLE_CLOSE_CODES is a denylist, so a new code would retry forever.
    read_only = _console_read_only(request)
    if read_only is None:
        return _refuse(_WS_UNAUTHORIZED)
    if provider is not None:
        # A named provider off the caller's provider grid: refused before any session, 4404 as "unknown
        # provider" (non-retryable).
        principal = current_principal(request)
        off_plan = await db_async.run_in_session(
            _provider_off_plan, principal.org_id if principal is not None else None, provider,
        )
        if off_plan is not None:
            return _refuse(_WS_NOT_FOUND, reason=off_plan)
    # A MicroVM task's terminal names its VM, not a slot (`_task_vm_console`): its node and its may-edit stand in
    # for the slot's below.
    view = request.query_params.get("view")
    task_vm = None
    if view is not None and view.startswith(_VM_VIEW_PREFIX):
        task_vm = await db_async.run_in_session(_task_vm_console, request, project_id, number, view)
        if task_vm is None:
            return _refuse(_WS_NOT_FOUND, reason="console view is not available")
        view = task_vm.view
    # `?node=` (the node uuid) scopes the authorization read ((project_id, number) collides across nodes);
    # see `_resolve_console_node`.
    node_id = task_vm.node if task_vm is not None else await db_async.run_in_session(
        lambda session: _resolve_console_node(project_id, number, request.query_params.get("node"), session))
    if not node_id:
        return _refuse(_WS_NOT_FOUND)
    # The node's own spellings: the frame's `project` (its roster key, beside `project_id`) and the ticket's
    # `node_name`.
    project, node_name = await db_async.run_in_session(_console_names, project_id, node_id)
    if project is None or node_name is None:
        return _refuse(_WS_NOT_FOUND)
    # The per-slot layer, askable only once a node is resolved.
    slot_may_edit = task_vm.may_edit if task_vm is not None else await db_async.run_in_session(
        _console_slot_editable, request, project_id, number, node_id)
    if slot_may_edit is None:
        # Not visible: 4404, the same code as an unknown slot — a new code would enter the reconnect
        # ladder (denylist), and a distinguishable one would confirm the slot exists
        # (`authz.require_object_access`'s reason).
        return _refuse(_WS_NOT_FOUND)
    if not slot_may_edit:
        # Visible but not driveable: demoted by setting the same `read_only` everything below reads.
        read_only = True
    # Accepted residual: a mid-console demotion stays two-way until disconnect (`current_principal`
    # memoises).

    # `?view=@<id>` asks for a PTY on one window of the node's shared `fleet` session. Shape-checked here
    # for a renderable 4404 rather than the retryable 4503; the node re-validates.
    if view is not None and task_vm is None:
        # The same non-disclosing refusal for "malformed" and "not yours" — no window-existence oracle.
        if not _VIEW_ID_RE.match(view) or not _may_name_window(request):
            return _refuse(_WS_NOT_FOUND, reason="console view is not available")

    # `?session=` names the tmux session; absent, the daemon's default. `or None` folds a bare value into
    # absent, as `console_direct.py`'s `_send_grant`.
    session = request.query_params.get("session") or None
    if session is not None:
        validated_session, session_error = console_direct.validate_session_name(session)
        if session_error:
            return _refuse(_WS_NOT_FOUND, reason="console session name is not available")
        session = validated_session

    # `?layout=<spec>` asks for a designed grid (one tmux window, panes as tiles). The node refuses a
    # spec naming both `view` and `layout` (`console_key_target`).
    layout = request.query_params.get("layout")
    if layout is not None:
        # Every tile is a `view` by another name, so it carries the identical gate — see `_may_name_window`.
        if not _LAYOUT_SPEC_RE.match(layout) or not _may_name_window(request):
            return _refuse(_WS_NOT_FOUND, reason="console layout is not available")

    # A free slot has no session to attach to, and a console never creates one. Asked after visibility, so the
    # refusal is no oracle; a named window or layout is the node's shared session, not this slot's.
    if task_vm is None and view is None and layout is None and await db_async.run_in_session(
            _slot_is_free, project_id, number, node_id):
        return _refuse(_WS_UPSTREAM_UNAVAILABLE, reason="slot is free", detail=_FREE_SLOT_DETAIL)
    # Read before the ticket is minted, so a DB error leaves no redeemable capability.
    picked = await db_async.run_in_session(_pick_console_account, node_id, provider)
    # The ticket's node binding is resolved here, in both spellings, so the relay checks the dialling node
    # without a lookup of its own. `node_id` is already the socket's machine key (`node_identity.bus_key` of a
    # uuid is the uuid).
    bus_target = node_id
    worktree_path = None if task_vm is not None else await db_async.run_in_session(
        _console_worktree_path, project_id, number, node_id)
    guard = await db_async.run_in_session(_console_guard, project_id)
    try:
        minted = await asyncio.to_thread(
            relay_tickets.mint, node_id=bus_target, node_name=node_name, read_only=read_only,
            session_ref=session_ref(request), client_ip=request.client.host if request.client else None,
        )
    except Exception as exc:  # noqa: BLE001 — any DB fault; retryable, the row is the only thing missing
        log.warning("console: could not mint the console tickets: %s", exc)
        return _refuse(_WS_RELAY_FAILED, reason="console ticket could not be published")
    # The frame credential — `app/console_direct.py::cp_frame_proof_for_node`, in its off-loop form: the sync one
    # opens a session on the serving loop's own thread, outside any greenlet, which binds the unpooled offline
    # engine and blocks the loop for a fresh connection plus a query.
    cp_frame_proof = await console_direct.cp_frame_proof_for_node_async(node_id)
    try:
        reply = await node_bus.registry.request(
            bus_target,
            {
                "cp_frame_proof": cp_frame_proof,
                "ticket": minted.node,
                # `project_id` is the identity; `project` the name an older daemon matches its roster on.
                "project_id": project_id, "project": project, "number": number, "kind": kind,
                "cols": cols, "rows": rows, "attach_only": True,
                # The CP chooses the account: `config_dir` for OAuth, or `api_key_account`
                # (the slug, `config_dir` then None) for an API-key account; both None is a verdict the node
                # fails closed on. An old node refuses `config_dir: None` outright, so deploy nodes first.
                "config_dir": picked.config_dir,
                "api_key_account": picked.api_key_account,
                # The picked account's provider, always sent (the requested/default one with no pick).
                "provider": picked.provider,
                # Conditional, not `"view": None`: an old node ignores `view` and opens the worktree's own
                # console — a silent wrong window. Deploy such nodes first.
                **({"view": view} if view is not None else {}),
                # Conditional, same convention — omitted leaves the daemon's own default in place.
                **({"session": session} if session is not None else {}),
                # Conditional, same deploy order as `view`.
                **({"layout": layout} if layout is not None else {}),
                # Conditional: absent, the node looks the path up itself (an old node always does).
                **({"path": worktree_path} if worktree_path else {}),
                # Conditional, only ever True. Deploy order is the reverse of `view`/`layout`'s: an old
                # node would give a writable console, so the echo check below refuses until confirmed.
                **({"read_only": True} if read_only else {}),
                # Conditional: the Architectural Guard, only where it is available (`_console_guard`).
                **({"guard": guard} if guard else {}),
                # Conditional: where the node dials the relay, when this stack names it (a local stack,
                # whose node reaches the API and the relay at different hosts). Absent, the node dials
                # `NODE_PATH` on its control-plane URL, which the ALB routes to the relay.
                **({"stream_url": _RELAY_NODE_URL} if _RELAY_NODE_URL else {}),
            },
            timeout=CONSOLE_CONNECT_TIMEOUT,
            frame_type=node_bus.FRAME_CONSOLE_OPEN,
        )
        parsed = json.loads(reply.data or b"{}")
    except node_bus.NodeNotConnected as exc:
        # RELAY_FAILED, not UPSTREAM_UNAVAILABLE (which the browser treats as final): an absent node says
        # nothing about its tmux session. INFO, since the browser retries every 10s while open.
        log.info("console: node not connected: %s", exc)
        await _discard_tickets(minted)
        return _refuse(_WS_RELAY_FAILED, reason="node not connected")
    except (node_bus.NodeRequestTimeout, ValueError, OSError) as exc:
        # A lost handshake is an unknown — the node may be mid-restart with its session alive.
        log.warning("console: console_open failed: %s", exc)
        await _discard_tickets(minted)
        return _refuse(_WS_RELAY_FAILED, reason="node did not answer")
    except BaseException:
        # Anything else (a bug, a cancelled request) re-raises, but leaves no ticket behind. Its own task: a
        # cancelled task cannot await its cleanup.
        _discard_in_background(minted)
        raise

    if not isinstance(parsed, dict) or not parsed.get("ok"):
        # Every read of `parsed` is guarded: an unguarded `.get` on non-dict JSON would escape as 1011
        # and skip the discard, stranding a redeemable capability.
        error = parsed.get("error") if isinstance(parsed, dict) else None
        is_retryable = bool(parsed.get("retryable")) if isinstance(parsed, dict) else False
        # Only the definitive "no session here" gets the final code; `retryable` and unparseable do not.
        code = _WS_RELAY_FAILED if is_retryable else _WS_UPSTREAM_UNAVAILABLE
        await _discard_tickets(minted)
        # The node's words, redacted (`fleet_verdicts.redact_node_detail`): a viewer reads them.
        said = str(error or "").strip().splitlines()
        reason = (fleet_verdicts.redact_node_detail(said[0]) if said else "") or "console unavailable"
        reason = reason[:120]
        if is_retryable:
            return _refuse(code, reason=reason)
        # An attach found nothing to attach to.
        return _refuse(code, reason=reason, detail=_REFUSAL_DETAIL[_WS_UPSTREAM_UNAVAILABLE])

    # Fail closed unless the node echoes `read_only` `is True` — an old daemon answers `{"ok": true}`
    # without it. Non-retryable (a node's build does not change between reconnects). The node dialled
    # before answering; discarding the row leaves its parked leg to the relay's partner deadline.
    if read_only and parsed.get("read_only") is not True:
        log.warning(
            "console: node %s did not confirm a read-only console — refusing the open rather than "
            "relaying a writable one", node_id,
        )
        await asyncio.to_thread(relay_tickets.discard, minted)
        return _refuse(_WS_UPSTREAM_UNAVAILABLE, reason="node does not support read-only consoles")

    log.info("console open: %s %s-%s%s handed to the relay (admin=%s, read_only=%s)", node_id, project, number,
             f" {view}" if task_vm is not None else "", admin_id, read_only)
    return JSONResponse({"url": f"{relay_tickets.BROWSER_PATH}?ticket={quote(minted.browser, safe='')}"})


def _refuse(code: int, reason: str | None = None, *, detail: str | None = None, status: int | None = None) -> JSONResponse:
    """A refused open, carrying the WebSocket close code the pane would have read had the API still held
    the socket — `ConsolePane.tsx` feeds it to the same reconnect decision — and `detail`, the one line the
    pane prints: `detail`, else `reason`, else the code's own sentence. The HTTP status is for logs and
    proxies only."""
    lines = (detail or reason or "").strip().splitlines()
    line = lines[0].strip()[:120] if lines else _REFUSAL_DETAIL.get(code, _NODE_UNAVAILABLE_DETAIL)
    return JSONResponse(
        {"close_code": code, "reason": reason or "", "detail": line},
        status_code=status or _REFUSAL_HTTP_STATUS.get(code, 503),
    )


# ── Direct mode (C-5) — issuance and revocation. Every rule lives in `app/console_direct.py`. ────────


def _direct_node_facts(session: Session, node_id: str) -> console_direct.NodeFacts:
    """The `nodes` row facts issuance reads, as one query, for the node uuid `node_id`. A missing row yields
    empty facts, taking the same surfaced-reason path as a node that never reported."""
    row = session.execute(
        select(Node.name, Node.config_snapshot, Node.observed_at).where(node_matches(node_id))
    ).first()
    return console_direct.NodeFacts(
        # `NodeFacts.node_id` is the name (the tailnet handle the published host is validated against); the
        # socket is addressed by the uuid (`bus_key`).
        node_id=row.name if row is not None else node_id,
        config_snapshot=row.config_snapshot if row is not None else None,
        observed_at=row.observed_at if row is not None else None,
        bus_key=bus_key(session, node_id),
    )


@router.get("/api/console/{project_id}/{number}/direct")
# `view`, matching the relay: direct mode is a transport choice; the `edit` distinction lives in the
# grant's `read_only`.
@feature("worktrees", "view")
@session_only
async def console_direct_issue(
    project_id: int,
    number: int,
    request: Request,
    response: Response,
    kind: str | None = None,
    node: str | None = None,
    tmux_session: str | None = None,
    provider: str | None = None,
    user: AdminUser = Depends(_check_direct_rate_limit),
    *,
    session: DbDep,
) -> dict:
    """C-5 — the direct connection parameters for one (node, slot), or a surfaced reason there are none.

    Admission is the relay's: `worktrees:view` plus a triple resolving to a slot this principal can see,
    with both tier halves from `_console_read_only` and `_console_slot_editable`, so the transports
    cannot disagree about who may type.

    An unresolvable triple is a 404; everything else is 200 with the discriminated union (AC8.0 — the
    reason must reach the user). `Cache-Control: no-store` because the body carries a bearer credential
    and CloudFront fronts this. `tmux_session` is minted into the signed grant; a malformed one is 400.
    """
    response.headers["Cache-Control"] = "no-store"
    # The tier from the one function that decides it; `None` denies, as the relay does.
    read_only = _console_read_only(request)
    if read_only is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if await session.run_sync(hipaa.org_is_on, user.org_id):
        # The relay is what closes a HIPAA org's console after 15 idle minutes (`console_relay/tickets.py::
        # sweep`), and a direct dial never crosses it: the pane falls back to the relay on this answer.
        return {"status": "unavailable", "reason": "not offered in a HIPAA org"}
    validated_session, session_error = console_direct.validate_session_name(tmux_session)
    if session_error:
        raise HTTPException(status_code=400, detail=session_error)
    # The relay's `?provider=` rule, as a 400.
    provider, provider_ok = _console_provider(provider)
    if not provider_ok:
        raise HTTPException(status_code=400, detail="unknown provider")
    off_plan = await session.run_sync(_provider_off_plan, user.org_id, provider)
    if off_plan is not None:
        raise HTTPException(status_code=403, detail=off_plan)
    node_id = await session.run_sync(lambda s: _resolve_console_node(project_id, number, node, s))
    # The node's own spelling of the slot, which the grant and the direct URL carry.
    project = (await session.run_sync(project_identity.names_by_id, [project_id])).get(project_id)
    if not node_id or project is None:
        raise HTTPException(status_code=404, detail=_NO_SUCH_SLOT)
    # The relay's per-slot layer, askable once a node is resolved.
    # Not visible: the unresolvable-triple 404 verbatim, so this credential route is no oracle.
    slot_may_edit = or_404(
        await session.run_sync(_console_slot_editable, request, project_id, number, node_id), detail=_NO_SUCH_SLOT,
    )
    # The node row's facts, read with the rest; the grant is issued over the node bus below.
    node_facts = await session.run_sync(_direct_node_facts, node_id)
    # Read only so far: the connection goes back before the short sessions and the node bus below.
    await db_async.release(session)
    if not slot_may_edit:
        # Visible but not driveable: demoted via the same variable, so both tiers reach the grant as one.
        read_only = True
    # The relay's rule: a free slot has no session to attach to, and a console never creates one.
    if await db_async.run_in_session(_slot_is_free, project_id, number, node_id):
        raise HTTPException(status_code=409, detail=_FREE_SLOT_DETAIL)
    # Resolved once and held, as in the relay.
    picked = await db_async.run_in_session(_pick_console_account, node_id, provider)
    guard = await db_async.run_in_session(_console_guard, project_id)
    return await console_direct.issue(
        node_facts,
        project=project,
        number=number,
        kind=_console_kind(kind),
        session_id=console_direct.session_id_for(user.id, user.session_epoch),
        registry=node_bus.registry,
        # The caller's `view_allowed`, by the relay's `?view=` predicate — issuance is the only place the
        # CP can speak for direct mode. Kept on the frame: an older node ANDs it with its
        # `JAAH_CONSOLE_VIEW_ENABLED` backstop. Contract: `console_direct.py`.
        view_allowed=_may_name_window(request),
        tmux_session=validated_session,
        # The account pick, carried as the relay frame carries it; an attach does not read it.
        config_dir=picked.config_dir,
        api_key_account=picked.api_key_account,
        provider=picked.provider,
        # The tier, riding the grant into a node-side record the browser never receives
        # (`DirectGrantStore`); nothing of it is in the token.
        read_only=read_only,
        # A grant never creates. Accepted residual: `read_only` is snapshotted at issuance, so a demotion keeps
        # a two-way grant until expiry or revocation (`app/access_admin.py`, best-effort).
        attach_only=True,
        # The Architectural Guard, sent only where it is available.
        guard=guard,
    )


class DirectRevokeIn(BaseModel):
    """The revoke request's body. `node` is the node uuid.

    `token` is a credential, hence a POST body: a server access log records the query string."""

    kind: str | None = None
    node: str | None = None
    token: str | None = None


@router.post("/api/console/{project_id}/{number}/direct/revoke")
# `view`: this only revokes the caller's own session's grants. At `edit` a view principal's close would
# 403 and strand live 24h grants.
@feature("worktrees", "view")
async def console_direct_revoke(
    project_id: int,
    number: int,
    body: DirectRevokeIn | None = None,
    user: AdminUser = Depends(_check_direct_revoke_rate_limit),
    *,
    session: DbDep,
) -> dict:
    """Revoke this session's direct grants for one console slot — a console closing, or the toggle going
    off (AC8.4; the logout arm is in `app/routes/auth.py`).

    `token` narrows to one grant; otherwise every grant this session holds on the slot. An omitted or
    unrecognised `kind` means both kinds (revoking more is the safe direction). Reports how many nodes
    the frame reached (fire-and-forget). No per-slot layer: keyed on the caller's own `session_id`, and a
    visibility gate would strand grants.
    """
    request = body or DirectRevokeIn()
    node_id = await session.run_sync(lambda s: _resolve_console_node(project_id, number, request.node, s))
    # Grants are keyed on the node's own spelling of the slot (`console_direct.mint_token`).
    project = (await session.run_sync(project_identity.names_by_id, [project_id])).get(project_id)
    if not node_id or project is None:
        raise HTTPException(status_code=404, detail=_NO_SUCH_SLOT)
    # Read only: the connection goes back before the node bus broadcast.
    await db_async.release(session)
    delivered = await console_direct.broadcast_revoke(
        node_bus.registry,
        session_id=console_direct.session_id_for(user.id, user.session_epoch),
        project=project,
        number=number,
        kind=request.kind if request.kind in CONSOLE_KINDS else None,
        token=request.token,
    )
    return {"status": "revoked", "nodes_notified": delivered}
