"""The slot-row write surface: removing, releasing and re-owning pool slots.

The node never creates a slot; a slot is a `worktrees` row keyed `(node_id, project_id, number)` with a
`path`, sensed columns left to the observation. Rows come into being only through `slot_allocation.add_dynamic`
(a task's own) — never through a per-slot declare here. The index space is unbounded.

One removal verb, Delete: the row goes, the tree goes best effort, with no occupancy guards (a dead
node must not make a row undeletable); the holder is ended and the node told (`app/slot_teardown.py`).
`POST .../erase` is the console's Delete; `DELETE /api/worktrees/{project_id}/{number}` is the row-only spelling
(no node dispatch, no teardown).
Explicit-target only (BL-1): every removal names one `(node_id, project_id, number)`; bulk Delete is a
fan-out of single calls. A project is addressed by
`projects.id` and a node by its uuid (`?node_id=`): names are per org, so neither names a row.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import (
    # Module scope: `git_access` imports no `routes` module, so there is no cycle.
    fleet_verdicts,
    git_access,
    project_identity,
    slot_allocation,
    slot_stack,
    slot_state,
    slot_teardown,
)
from app.auth import require_operator_acting
from app.command_read import withhold_host_paths
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller, current_principal_async, org_admin_writer
from app.authz.visibility import worktree_may_edit
from app.db_async import DbDep, on_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import Worktree
from app.node_clone import clone_for_slot_path, legacy_clone
# `_record_audit` is `CommandAudit`'s single writer; a non-dispatch audit row (`release_slot`) has
# nowhere else to go.
from app.routes.commands import (
    _record_audit,
    default_dispatch_node, dispatch_and_audit,
)
from app.node_identity import (
    names_by_id,
    node_id_or_404,
    node_matches,
    node_org,
    node_ref,
)
from app.tenancy.copy import node_home_unrecorded
from app.tenancy.ids import is_tenant_org
from app.entity_shape import SYSTEM_ACTOR_ID, StrictModel, get_or_404, or_404

router = APIRouter(tags=["pool-slots"])
log = logging.getLogger("cockpit-pool-slots")


#: The administrator bar on a slot declared beside another write a tenant makes (`routes/projects.py`'s placement) —
#: `authz.org_admin_writer`.
require_slot_writer = org_admin_writer("Worktree slots")
# The bar on the pool's own doors (clone owner, erase, delete) is `require_operator_acting`: the operator's, as
# the console's Slots page is (Audit > Slots, `apps/web/src/pages/audit/AuditPage.tsx`) — a tenant org admin is refused in tenant
# words. An operator acting as an org is still the operator (Change clone owner's one-request act-as).


@dataclass(frozen=True)
class _SlotGone:
    """What `delete_one_slot`'s committed row phase hands its best-effort node phase."""

    project: str
    holder: Optional[str]
    issued: Any
    clone: Optional[str]
    stack_down_cmd: Optional[str]
    node_name: str
    folder_project_id: Optional[int]


class SlotDeleteOut(BaseModel):
    """What a Delete actually came to — 200 with this body, never a bare 204.

    Three successful outcomes must be told apart: tree erased; node unreached (directory orphaned); or
    something was running in the tree. None is an error and none may be hidden. `row_deleted` is always
    True on a 2xx, stated so a client reads it from the body."""

    row_deleted: bool
    # Did the node confirm the directory is gone? If not, `node_detail` says why, in the node's words.
    node_erased: bool
    node_detail: Optional[str] = None
    # What was inside (`slot_teardown.describe_holder`), or None. Present means a runner or session was
    # ended: ledger closed and a `release_slot` intent minted.
    holder_ended: Optional[str] = None


class ReleaseSlotIn(StrictModel):
    """Optional POST body for `POST .../release`: empty means `force=False`, so plain Release and the
    force retry (`{"force": true}`) share one endpoint. `extra='forbid'` as elsewhere.

    `force` waives only the fresh-live-session refusal, both CP-side and as relayed to the node
    (`apps/jaahd/commands.py::_release`) — not the per-slot authz probe (404 first). The next lease's `reset_to_base` discards the tree's uncommitted work."""

    force: bool = False


def _slot_delete_statement(row_id: UUID, node_id: str, project_id: int, number: int):
    """The row deletion, unconditional — one row, by primary key.

    A declared `worktrees` writer family (`tests/test_slot_two_writers.py::_DECLARED_WRITERS`), pinned
    on its scope. No `state != 'free'` conjunct: a hold landing between read and delete is ended by
    `slot_teardown.end_holder` in the same transaction rather than turned into a 409. The natural-key
    parameters are unused and name the slot at the call site."""
    return delete(Worktree).where(Worktree.id == row_id)


def _resolved_node_id(
    session: Session, principal: Principal | None, requested: Optional[str]
) -> str:
    """The node uuid a slot route acts on: the one the caller named, else the caller's plane's default.

    Per plane via `routes/commands.py::default_dispatch_node`, so a tenant never gets a 404 quoting the
    CP's own node. `prefer_connected=False` for every route: which row is destroyed or freed must not
    depend on connectivity. Console calls always send `?node_id=`."""
    return (requested or "").strip() or default_dispatch_node(
        session, principal, prefer_connected=False
    )


def _node_home(session: Session, node_id: str) -> Optional[str]:
    """`Node.jaah_home` as recorded, or None."""
    node = session.scalar(select(Node).where(node_matches(node_id)))
    return node.jaah_home if node else None


def _node_clone(session: Session, project: Project, node_id: str) -> str:
    """The `clone_path` an erase frame carries for `project` on `node_id`
    (`node_clone.legacy_clone`), or `""` when the node's `jaah_home` is not recorded (the only input that can
    be missing). The node erases there when a slot's own `path` cannot be read.

    No pool gate: any project's slot must be erasable."""
    return legacy_clone(_node_home(session, node_id), project.id)


def _clone_git_access_warning(
    session: Session, node_label: str, node_uuid: UUID, project_id: int, project_label: str,
    *, declarer_id: Optional[UUID] = None,
) -> Optional[str]:
    """The advisory sentence for a (node, project) clone that has no forge account behind it, or None.
    `node_label` is the node's name, the word the sentence uses.

    Asked as the node will (`git_access.resolve_for_worktree_clone`), after the caller's flush and
    before its commit, by `set_clone_owner` and `routes/projects.py`'s placement. `declarer_id`: the person
    the slots will be declared under, asked directly — a project placed at create has no slot row yet, and its
    creator's own tasks declare as the creator (`slot_allocation._declarer` reads the task's starter).
    """
    git_access_warning: Optional[str] = None
    try:
        # Inside a savepoint: a failed SELECT poisons the session, and the caller's commit would lose
        # the declaration with the advisory (`routes/agent/slot_intents.py::_note_dispatch_refusal`).
        with session.begin_nested():
            # The return value is dropped (no token reaches the response); only whether it raises
            # matters. The refusal names the declarer by email or a credential name: same-org, except an operator
            # who declared from outside the org, whom it names on purpose (`git_access._declarer_outside_org`).
            if declarer_id is None:
                git_access.resolve_for_worktree_clone(session, node_uuid, project_id)
            else:
                project = session.scalars(select(Project).where(Project.id == project_id)).first()
                repo = git_access.project_repo(project, retry=git_access.CLONE_RETRY)
                git_access.resolve_for_user(session, declarer_id, repo, retry=git_access.CLONE_RETRY)
    except git_access.GitAccessError as exc:
        # The exception's own sentence verbatim (`app/git_access.py` is its home), plus the missing
        # context: which clone on which node. It does not repeat that the declaration worked.
        git_access_warning = (
            f"This project's pool clone on node {node_label} has no git account to authenticate as, so "
            f"cloning a private repo will fail: {exc}"
        )
    except Exception:
        # A best-effort advisory never costs the write: other errors are logged with a traceback
        # and the request finishes.
        log.warning(
            "git-access advisory failed for %s on node %s — written, no warning attached",
            project_label, node_label, exc_info=True,
        )
    return git_access_warning


class CloneOwnerIn(StrictModel):
    """PUT body for `set_clone_owner`: the person the (node, project) clone should authenticate as."""

    user_id: UUID


class CloneOwnerOut(BaseModel):
    """What the reassignment came to: how many rows now name `owner_email`, and the clone advisory
    (`_clone_git_access_warning`) asked again after the write. `node_id`/`project` are display names."""

    node_id: str
    node_uuid: str
    project: str
    project_id: int
    owner_email: str
    slots: int
    git_access_warning: Optional[str] = None


def _clone_owner_statement(node_uuid: UUID, project_id: int, owner_id: UUID):
    """``UPDATE worktrees SET created_by_id = :owner WHERE node_id = :node AND project_id = :project``.

    `slot_columns.DECLARATION`'s one UPDATE (pinned by `tests/test_slot_two_writers.py`). Every row of
    the (node, project), since the clone reads its earliest human declarer. `updated_by_id` belongs to
    `MUTATED`, so the route's audit row records the act."""
    return (
        update(Worktree)
        .where(Worktree.node_id == node_uuid, Worktree.project_id == project_id)
        .values(created_by_id=owner_id)
    )


@router.put("/api/worktrees/{project_id}/clone-owner", response_model=CloneOwnerOut)
@feature("worktrees", "create")
@on_session
def set_clone_owner(
    session: Session,
    project_id: int,
    body: CloneOwnerIn,
    node_id: str = Query(default=""),
    user: AdminUser = Depends(require_operator_acting),
    principal: Principal | None = Depends(current_principal_async),
) -> CloneOwnerOut:
    """Reassign who a (node, project) pool clone authenticates to GitHub as.

    The clone's identity is its earliest human declarer (`git_access.resolve_for_worktree_clone`). Same
    bar as declaring a slot. The owner must be a real person in the node's org; unknown or out-of-scope
    is a 404."""
    resolved_node = _resolved_node_id(session, principal, node_id)
    node_uuid = node_id_or_404(session, resolved_node)
    project = project_identity.project_or_404(session, project_id).name
    # A SELECT so the read seam's org predicate applies: unseeable and nonexistent are the same 404.
    # The system actor is refused by id first: its row sits in org zero (revision 0406), out of every
    # tenant node's scope, and it is no one's clone owner anywhere.
    if body.user_id == SYSTEM_ACTOR_ID:
        raise HTTPException(
            status_code=422,
            detail="the system actor cannot own a clone: it has no Profile and no GitHub account",
        )
    owner = get_or_404(
        session, AdminUser, body.user_id, detail=f"unknown user {body.user_id}",
        where=(AdminUser.org_id == node_org(session, resolved_node),),
    )
    if owner.is_system:
        raise HTTPException(
            status_code=422,
            detail="the system actor cannot own a clone: it has no Profile and no GitHub account",
        )
    slots = session.execute(_clone_owner_statement(node_uuid, project_id, owner.id)).rowcount or 0
    if slots == 0:
        raise HTTPException(status_code=404, detail=f"{project} has no declared slot on node {resolved_node}")
    _record_audit(
        session,
        resolved_node,
        "worktree_clone_owner",
        {"project_id": project_id, "project": project, "owner_user_id": str(owner.id), "slots": slots},
        "done",
        None,
        user.email,
        commit=False,
    )
    node_name = names_by_id(session, [node_uuid]).get(node_uuid, str(node_uuid))
    warning = _clone_git_access_warning(session, node_name, node_uuid, project_id, project)
    emit_console_event(
        session, entity="worktree", id=0, action="upserted", org_id=node_org(session, resolved_node),
        commit=True,
    )
    return CloneOwnerOut(
        node_id=node_name, node_uuid=str(node_uuid),
        project=project, project_id=project_id, owner_email=owner.email or str(owner.id),
        slots=slots, git_access_warning=warning,
    )


def _delete_slot_rows(
    session: Session, node_id: str, project_id: int, project: str, number: int, row_id: UUID
) -> None:
    """The deletion's write tail, shared by the row-only DELETE and the Delete verb. Never raises, never
    refuses, does not commit (the caller commits row, event and teardown together).

    A zero rowcount is success: another writer (bulk double-fire, node-deregistration cascade) already
    achieved the caller's goal."""
    session.execute(_slot_delete_statement(row_id, node_id, project_id, number))


def _locked_slot_row(
    session: Session, node_id: str, project_id: int, project: str, number: int
) -> Worktree:
    """The one `(node_id, project_id, number)` row, taken `FOR UPDATE`. 404 when it is not a declared slot.

    `project_id` selects; `project` is only the word in the 404."""
    return or_404(
        session.scalar(
            select(Worktree)
            .where(
                Worktree.node_id == node_ref(node_id),
                Worktree.project_id == project_id,
                Worktree.number == number,
            )
            .with_for_update()
        ),
        detail=f"{project} #{number} is not a declared slot on node {node_id}",
    )


def _locked_slot(
    session: Session, principal: Principal | None, node_id: str, project_id: int, number: int, *, pair_lock: bool = True
) -> tuple[str, str, Worktree]:
    """A slot route's preamble, `(resolved node, project name, row FOR UPDATE)`: the project's 404 before any
    lock; the pair lock (`slot_allocation.lock_pair` says why before the row lock) only where the declared count changes."""
    resolved = _resolved_node_id(session, principal, node_id)
    project = project_identity.project_or_404(session, project_id).name
    if pair_lock:
        slot_allocation.lock_pair(session, node_id_or_404(session, resolved), project_id)
    return resolved, project, _locked_slot_row(session, resolved, project_id, project, number)


async def delete_one_slot(
    session: Session | AsyncSession,
    node_id: str,
    project_id: int,
    number: int,
    *,
    actor_email: str,
    # The caller's org picks whose words `node_detail` uses and whether host paths are swept. No
    # default: `is_tenant_org(None)` is True (fail-closed).
    actor_org: Optional[UUID],
) -> SlotDeleteOut:
    """Delete one slot end to end; it cannot be refused. End whatever holds it, drop the row, then ask
    the node to remove the directory — best effort, reported, never blocking.

    `node_id` is the node uuid, `project_id` the `projects.id`. Raises only for an unaddressable slot (unknown
    project or undeclared slot, 404); every other failure is an outcome in `SlotDeleteOut`.

    Row first, not tree first: tree-first makes the row's fate depend on the node, so a dead node would
    make it undeletable. An orphaned directory is a known orphan, named in the response and audit.

      1. Resolve and lock `FOR UPDATE`, so a lease cannot land between holder read and delete.
      2. End the holder in this transaction (`slot_teardown.end_holder`), off
         the pre-delete row: once the row is gone no task-side producer can find the binding.
      3. Delete and commit row, event and intents together; nothing may fail the request after this.
      4. Tell the node, best effort and forced (`slot_teardown.erase_tree_on_node`); its refusals are
         reported reasons, not 409s.

    The erase frame still carries the project's anchor clone path (`_node_clone`): a row whose stored `path` is
    missing or unshaped is erased there."""
    def rows_phase(session: Session) -> "_SlotGone":
        project_row = project_identity.project_or_404(session, project_id)
        # The node's own spelling of the slot, carried to `end_holder` and the erase frame, and the word below.
        project = project_row.name
        # Before the row lock (`slot_allocation.lock_pair` says why).
        slot_allocation.lock_pair(session, node_id_or_404(session, node_id), project_row.id)
        row = _locked_slot_row(session, node_id, project_row.id, project, number)
        row_id = row.id

        # ── STEP 2: end what is inside, in this transaction, off the pre-delete row ──
        snap = slot_teardown.snapshot(row)
        holder = slot_teardown.describe_holder(snap)
        issued = slot_teardown.end_holder(
            session, snap, project=project,
            reason=fleet_verdicts.console_delete_reason(),
        )
        # Resolved before the commit (needs `project_row`). `""` is reported as why the tree was untouched,
        # never raised.
        # The stored path first: where the node last reported the tree.
        clone = (clone_for_slot_path(_node_home(session, node_id), row.path, number)
                 or _node_clone(session, project_row, node_id))
        stack_down_cmd = project_row.stack_down_cmd
        # The word the reports below use for the node.
        node_name = names_by_id(session, [row.node_id]).get(row.node_id, node_id)
        # The slot's kind, read before the row goes: a folder is erased on the node's folder path.
        folder_project_id = None if row.has_repo else project_row.id

        # ── STEP 3: the row goes, and this commit is the point of no return ──
        _delete_slot_rows(session, node_id, project_row.id, project, number, row_id)
        # Tagged with the node's org (see `add_slot_rows`).
        emit_console_event(
            session, entity="worktree", id=0, action="deleted", org_id=node_org(session, node_id),
            commit=True,
        )
        return _SlotGone(project, holder, issued, clone, stack_down_cmd, node_name, folder_project_id)

    gone = await session.run_sync(rows_phase) if isinstance(session, AsyncSession) else rows_phase(session)
    project, holder, issued, clone = gone.project, gone.holder, gone.issued, gone.clone
    stack_down_cmd, node_name, folder_project_id = gone.stack_down_cmd, gone.node_name, gone.folder_project_id
    slot_teardown.push_intents(issued)

    # ── STEP 4: past the commit, everything is best effort and nothing may raise ──
    if not clone:
        consequence = f"where {project} #{number} lives is not known and the directory was left alone"
        if not is_tenant_org(actor_org):
            node_detail = (
                f"the control plane cannot derive where {project} #{number} lives on node {node_name} (the "
                f"node's Jaah home is not set), so the directory was left alone"
            )
        else:
            # The tenant's words (`tenancy/copy.py::node_home_unrecorded`).
            node_detail = node_home_unrecorded(node_name, consequence)
        log.warning("delete %s #%s on %s: row removed, no clone path to erase", project, number, node_name)
        return SlotDeleteOut(
            row_deleted=True, node_erased=False, holder_ended=holder,
            # No erase was dispatched, so a holder's process is certainly still running.
            node_detail=_detail_with_the_holders_fate(node_detail, holder, False),
        )
    erased = await slot_teardown.erase_tree_on_node(
        session,
        node_id=node_id,
        project=project,
        number=number,
        clone_path=clone,
        stack_down_cmd=stack_down_cmd,
        actor_email=actor_email,
        folder_project_id=folder_project_id,
    )
    # `node_id` is the uuid: the `nodes` row stands, so `bus_key` resolves it (see `erase_tree_on_node`).
    node_detail = _detail_with_the_holders_fate(erased.reason, holder, erased.confirmed)
    if is_tenant_org(actor_org):
        # The node's stderr or bus refusal can carry host paths; swept for a tenant
        # (`app/command_read.py`), verbatim for the operator.
        node_detail = withhold_host_paths(node_detail)
    return SlotDeleteOut(
        row_deleted=True,
        node_erased=erased.confirmed,
        node_detail=node_detail,
        holder_ended=holder,
    )


def _detail_with_the_holders_fate(
    reason: Optional[str], holder: Optional[str], erased: bool
) -> Optional[str]:
    """The node's own words, plus — when the erase did not land on a held slot — what that means for the
    runner that was in it.

    The ledger close always happens; the process kill rides the erase, so on an unreached node the
    runner is still executing and `holder_ended` alone would mislead. Adds nothing for a free slot or a
    confirmed erase."""
    if erased or holder is None:
        return reason
    # One wording for both consoles.
    warning = (
        f"the {holder} hold is released here, but the node never confirmed, so that runner may still "
        f"be RUNNING in the tree — an operator can stop it on the host"
    )
    return f"{reason}; {warning}" if reason else warning


@router.post(
    "/api/worktrees/{project_id}/{number}/erase",
    response_model=SlotDeleteOut,
    status_code=200,
)
@feature("worktrees", "delete")
async def erase_slot(
    project_id: int,
    number: int,
    db: DbDep,
    node_id: str = Query(default=""),
    user: AdminUser = Depends(require_operator_acting),
    # For `_resolved_node_id`'s per-plane default only.
    principal: Principal | None = Depends(current_principal_async),
) -> SlotDeleteOut:
    """Delete one pool slot — the console's Delete, single and bulk alike. 200 with a `SlotDeleteOut`
    saying what became of the row, the tree and whatever was inside it.

    Cannot be refused on occupancy (the operator's requirement): a hold buys a teardown, not a veto.
    `delete_one_slot` carries the ordering. The path keeps its `erase` spelling for external clients.
    Still superadmin on top of `worktrees:delete`, and explicit-target only (BL-1)."""
    resolved = await db.run_sync(_resolved_node_id, principal, node_id)
    return await delete_one_slot(
        db, resolved, project_id, number, actor_email=user.email, actor_org=user.org_id,
    )


@router.delete("/api/worktrees/{project_id}/{number}", status_code=204)
@feature("worktrees", "delete")
@on_session
def delete_slot_row(
    session: Session,
    project_id: int,
    number: int,
    node_id: str = Query(default=""),
    _user: AdminUser = Depends(require_operator_acting),
    principal: Principal | None = Depends(current_principal_async),
) -> Response:
    """Delete one slot's row and nothing else — no node dispatch, no holder teardown. 204 on success.

    The row-only spelling, for plumbing that handles the tree by hand; the console uses
    `POST .../erase`. The narrower tool, not the safer one: the directory stays and a holder's runner
    keeps executing with no teardown ever minted. Explicit-target only (BL-1)."""
    resolved, project, row = _locked_slot(session, principal, node_id, project_id, number)
    # Row-only means the node is never told: a still-pending destroy order goes with the row.
    slot_allocation.cancel_destroy(session, row)
    _delete_slot_rows(session, resolved, project_id, project, number, row.id)
    # `deleted`, matching `routes/accounts.py`. `id=0` as on add; tagged with the node's org.
    emit_console_event(
        session, entity="worktree", id=0, action="deleted", org_id=node_org(session, resolved),
        commit=True,
    )
    return Response(status_code=204)


@router.post("/api/worktrees/{project_id}/{number}/destroy/retry", status_code=204)
@feature("worktrees", "delete")
@on_session
def retry_slot_destroy(
    session: Session,
    project_id: int,
    number: int,
    node_id: str = Query(default=""),
    _user: AdminUser = Depends(require_operator_acting),
    principal: Principal | None = Depends(current_principal_async),
) -> Response:
    """Order a stuck or refused destroy again: clear `destroy_error` and mint a fresh `destroy_slot` intent
    (`slot_allocation.retry_destroy`). 204 on success, 409 when the slot is not being destroyed.

    The console's Retry on a slot whose destroy the node failed, refused, or never acked. Explicit-target only
    (BL-1), like the erase and the row-only delete."""
    resolved, project, row = _locked_slot(session, principal, node_id, project_id, number)
    if not slot_allocation.retry_destroy(
        session, row, reason="an operator retried this worktree's destroy — ordering it again",
    ):
        raise HTTPException(status_code=409, detail=f"{project} #{number} is not being destroyed")
    emit_console_event(
        session, entity="worktree", id=0, action="upserted", org_id=node_org(session, resolved),
        commit=True,
    )
    return Response(status_code=204)


def _require_editable_slot(
    session: Session, principal: Principal | None, row: Worktree, *, project: str, number: int, resolved_node: str,
) -> None:
    """Who may drive this slot: `@feature` is a floor; `authz.worktree_may_edit` is the per-slot half (own
    hold, or `tasks:edit` on the running task's project; D13). 404, not 403, with the undeclared-slot answer's
    exact text, so it is no id-space oracle."""
    # A guard, not a read: the row is already in hand, only its editability is asked.
    or_404(
        worktree_may_edit(session, principal, Worktree.id == row.id) or None,
        detail=f"{project} #{number} is not a declared slot on node {resolved_node}",
    )


def _fresh_live_session(row: Worktree) -> Optional[str]:
    """The `session_status` of a session running in this slot right now, or None, off this row's two
    sensed columns.

    A fresh observation with a session is real work (release refuses). A stale one means the node stopped
    observing, and its last reading is never cleared, so staleness answers None or a dead node could
    never be released. Freshness is `slot_stack.observation_is_fresh`'s rule; not `stack_state_for`,
    which conflates stale with fresh-but-empty. Liveness is presence (matching
    `apps/web/src/lib/worktree.ts::hasLiveSession`): only an exact `""` means no session.
    """
    if not slot_stack.observation_is_fresh(row.observed_at):
        return None
    return row.session_status or None


@router.post("/api/worktrees/{project_id}/{number}/release", status_code=204)
@feature("worktrees", "edit")
async def release_slot(
    project_id: int,
    number: int,
    db: DbDep,
    body: ReleaseSlotIn = ReleaseSlotIn(),
    node_id: str = Query(default=""),
    caller: AsyncCaller = Depends(current_caller),
) -> Response:
    """End one pool slot's task hold on an operator's explicit say-so (`slot_state.release` carries the
    contract). 204 on success.

    A route, not a widened `release` verb: no session or runner may free a task's slot; this is a different
    actor with a different reason.
    Not superadmin-gated: `worktrees:edit` plus the per-slot probe (`_require_editable_slot`).

    Cost: nothing is destroyed now, but the next lease's `reset_to_base` runs no liveness probe, and its
    dirty-tree refusal keys on the new task's identity branch, so uncommitted tracked work is lost
    (committed work survives on its branch ref). Hence the guard: a fresh live session on a held row is
    refused 409 with `{reason: "live_session", session_status, message}`; `{"force": true}` waives only
    that. Idempotent (204, nothing written) on a free slot the caller may drive; a scoped caller gets the
    404 for free slots, since the shared clause cannot widen.

    Ending the hold must also end the runner: off the pre-UPDATE snapshot and in the same transaction, a
    `release_slot` intent (not `kill_runner`, whose projection fence would see `free` and do nothing) and
    the session ledger's `abandoned` end. The `tasks` row is untouched (the task is re-dispatched). The
    node is told after the commit, durably by the intent and immediately by the `release` relay, with
    `force` forwarded (the node refuses outright on a live session otherwise)."""
    # The release, committed (or rolled back on a free slot: `None`), then the node told with no connection held.
    released = await db.run_sync(
        _release_slot_row, project_id=project_id, number=number, body=body, node_id=node_id, user=caller.actor,
        principal=caller.principal,
    )
    if released is None:
        return Response(status_code=204)
    issued, resolved_node, project = released

    # ── Past the commit, everything is best effort. The hold has ended; the node's side has not. ──
    # Intents are sent after the commit, never before. Deferred import (routes cycle); `.send()`
    # never raises.
    from app.routes.tasks.slots import send_minted_intents  # noqa: PLC0415

    send_minted_intents(issued)
    try:
        reply, _audit_id = await dispatch_and_audit(
            db, resolved_node, "release",
            # `force` forwarded (see the docstring); an unforced release still sends `False`.
            {"project_id": project_id, "project": project, "number": number, "force": bool(body.force)},
            caller.actor.email,
        )
    except Exception as exc:  # noqa: BLE001 — see below; nothing past the commit may fail the request
        # Everything this hop can raise (pool, registry, JSON decode, not just bus refusals): a 500
        # after the commit would look like a release that did not happen. Swallowed; the durable intent
        # outlives this hop. Bus refusals log one line; anything else keeps its traceback.
        is_bus_refusal = isinstance(exc, HTTPException)
        log.warning(
            "release %s #%s on %s: CP hold ended, node teardown not delivered: %s",
            project, number, resolved_node,
            exc.detail if is_bus_refusal else f"{type(exc).__name__}: {exc}",
            exc_info=not is_bus_refusal,
        )
    else:
        if str(reply.get("status") or "failed") != "done":
            result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
            log.warning(
                "release %s #%s on %s: CP hold ended, node refused the teardown: %s",
                project, number, resolved_node,
                str(result.get("stderr") or result.get("reason") or "").strip() or "no reason given",
            )
    return Response(status_code=204)


def _release_slot_row(
    session: Session,
    project_id: int,
    number: int,
    body: ReleaseSlotIn,
    node_id: str,
    user: AdminUser,
    principal: Principal | None,
) -> Optional[tuple[list[fleet_verdicts.IssuedIntents], str, str]]:
    """`release_slot`'s DB phase, through its commit: `(issued intents, resolved node, project)`, or None when the
    slot was already free (rolled back, nothing written)."""
    resolved_node, project, row = _locked_slot(session, principal, node_id, project_id, number, pair_lock=False)
    _require_editable_slot(session, principal, row, project=project, number=number, resolved_node=resolved_node)

    # The live-session guard. After the authz probe, so an unauthorized caller still gets the 404. Only
    # on a held row: releasing a free slot changes nothing, so refusing would just noise up bulk cleanup.
    # `row.state` comes from the `FOR UPDATE` row.
    held = row.state != slot_state.FREE
    live_status = _fresh_live_session(row) if held and not body.force else None
    if live_status is not None:
        # Structured like `erase_slot`'s: a client offers Force only on `reason == "live_session"`,
        # never on the status. `message` is rendered verbatim.
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "live_session",
                # Forwarded verbatim: it tells the operator which console to open.
                "session_status": live_status,
                # Written for the operator beside a "Force release" button (the flag is in the
                # docstring), and it names the teardown: forcing ends the session.
                "message": (
                    f"a live session ({live_status}) is running in {project} #{number} — forcing the "
                    f"release ends it, and the freed slot goes to the next dispatch, whose lease resets "
                    f"the tree onto the base branch and discards uncommitted work with no stash or "
                    f"backup. Stop the session from Console first, or force the release to take it anyway"
                ),
            },
        )

    # Read before the release clears them: the audit row names the displaced hold.
    prior_state, prior_task_id = row.state, row.task_id
    # The launch identity, off the same pre-UPDATE row: `slot_state.release` blanks it, and a fresh
    # SELECT would name the next occupant. `slot_teardown.snapshot` is shared with the delete path.
    prior = slot_teardown.snapshot(row)
    freed = slot_state.release(
        session,
        worktree_id=row.id,
        generation=row.generation,
        # Names this edge, so a reader learns which one ended the hold.
        reason=fleet_verdicts.console_release_reason(teardown=False),
        # Audit, intent and session end must share this transaction.
        commit=False,
    )
    if not freed:
        # Already free: roll back the lock and write no audit row.
        session.rollback()
        return None

    # Tagged with the node's org (see `add_slot_rows`).
    emit_console_event(
        session, entity="worktree", id=0, action="upserted", org_id=node_org(session, resolved_node),
    )
    # `_record_audit` records the CP's decision (and the displaced task) in this transaction;
    # `dispatch_and_audit` below records what the node was asked to do.
    _record_audit(
        session,
        resolved_node,
        "worktree_release",
        {
            "project_id": project_id,
            "project": project,
            "number": number,
            "state_from": prior_state,
            "task_id": prior_task_id,
        },
        "done",
        None,
        user.email,
        # `commit=False` keeps this route's transaction boundary visible here rather than inside
        # another module's helper.
        commit=False,
    )
    issued = slot_teardown.end_holder(
        session,
        prior,
        project=project,
        reason=fleet_verdicts.console_release_reason(teardown=True),
    )
    # The commit: release UPDATE, audit row, teardown intent and session end as one unit.
    session.commit()
    return issued, resolved_node, project
