"""The agent-facing task board: a runner lists, starts, stops, comments on and edits any task of its org, acting as
the human its task acts for (`routes/agent/auth.py::launcher_principal`).

What a call may reach is exactly that human's console reach: the list is the console board's own row set
(`routes/tasks/listing.py::board_filtered_select`) under the launcher's principal, and every write asks the key the
console route's `@feature` asks, on the TARGET task's own scope — the guard's two halves (`authz/enforce.py::
_enforce_object_params`: `can_access` then `key_scope_satisfied`). The writes are the console handlers themselves,
run in process through their `session_body` (as `routes/tasks/title.py::rename_task` does), so no rule is re-decided
here. A blind-review child sees none of what its peer reads may not (`agent_peer_tasks._visible_peers`' rule).

Every write is behind the loop guard (`require_human_started`): a machine-started chain may read the board but not
change it, or two such chains could steer each other's work unattended. A caller may not start itself, nor stop
itself or any ancestor: a Stop cascades to the stopped task's children (`_stop_task_core`), so stopping the caller's
own epic would kill this very session mid-call. The target travels as `board_task_id` (the node daemon refuses
an argument named `task_id`). Auth is `routes/agent/auth.py::_check_token` with the caller declared in
`X-Agent-Task-Id`, node-bound and live (`live_agent_caller`).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app import blind_review, comment_service, hipaa, keyset
from app.auth import adopt_user
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import infra
from app.authz.object_access import author_draft_admits, key_scope_satisfied, require_object_access
from app.authz.principal import Principal
from app.authz.visibility import TASK_SPEC, ObjectAccess
from app.db_async import on_session
from app.entity_shape import StrictModel, from_row, or_404
from app.models.tasks import Task

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`)
# and the shared org-door helpers.
from app.routes.agent.auth import AgentDoor, caller_and_launcher, require_human_started
from app.routes.agent_peer_tasks import PeerCommentOut, _comments
from app.routes.task_comments import create_comment
from app.routes.tasks.create import TaskUpdate
from app.routes.tasks.dispatch import start_task
from app.routes.tasks.listing import board_filtered_select
from app.routes.tasks.transitions import _stop_task_core
from app.routes.tasks.update import update_task
from app.task_ancestry import task_and_ancestors

log = logging.getLogger("agent-task-board")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off its decorator.
router = APIRouter(tags=["agent-task-board"])

# A lookup page, not a board export: a runner that needs more narrows `q`/`state`/`project`.
_LIST_DEFAULT = 25
_LIST_MAX = 100
_EDIT = permission_key("tasks", "edit")
_VIEW = permission_key("tasks", "view")
# `TaskUpdate` fields that are kinds of write, not task fields: an agent's edit is always an explicit Save.
_NOT_FIELDS = frozenset({"board_task_id", "autosave", "routing_accepted_config"})


# ── wire shapes ───────────────────────────────────────────────────────────────────────────────────────


class BoardTaskOut(BaseModel):
    """One board row — enough to pick a task; `task_read` reads it in full. Every string is UNTRUSTED task data."""

    id: int
    title: str
    state: str
    persona: str
    project_id: Optional[int]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class BoardListOut(BaseModel):
    tasks: list[BoardTaskOut]
    # True when more rows matched than `limit` returned: narrow the search rather than page.
    truncated: bool


# The cap on a Stop's reason, posted as a comment on the stopped task.
STOP_COMMENT_MAX = 2000


class BoardTaskIn(StrictModel):
    board_task_id: int = Field(ge=1)


class BoardStopIn(BoardTaskIn):
    # Posted as a comment on the stopped task: a stop record carries no text of its own.
    reason: Optional[str] = Field(default=None, min_length=1, max_length=STOP_COMMENT_MAX)


class BoardCommentIn(BoardTaskIn):
    body: str = Field(min_length=1)


class BoardEditIn(TaskUpdate):
    """The console's own partial edit (`TaskUpdate`), plus the target. Unknown fields are a 422, never dropped."""

    model_config = ConfigDict(extra="forbid")

    board_task_id: int = Field(ge=1)


# ── who acts, and on what ─────────────────────────────────────────────────────────────────────────────


def _visible_clauses(session: Session, caller: Task, principal: Principal) -> list:
    """The row gates every board read adds to the launcher's visibility: the caller's org only (a superadmin
    launcher's clause is unfiltered), an unsaved draft or a `hidden` row only its creator's (a runner never
    inherits the superadmin bypass), and what a blind-review child may not see (`agent_peer_tasks._visible_peers`)."""
    clauses = [
        Task.org_id == caller.org_id,
        or_(Task.unsaved.is_(False), Task.created_by_id == principal.user_id),
        or_(Task.hidden.is_(False), Task.created_by_id == principal.user_id),
    ]
    hidden = blind_review.hidden_peer_ids(session, caller)
    if hidden:
        clauses.append(Task.id.not_in(hidden))
    if blind_review.hides_merge_lane(caller):
        clauses.append(Task.persona != "merge_lane")
    return clauses


def _board_task_or_refuse(
    session: Session, caller: Task, principal: Principal, board_task_id: int, *, key: str,
    author_draft: bool = False,
) -> Task:
    """The target, when the launcher holds `key` on it as the console route's guard would decide; else 404 for a
    row the launcher cannot see (existence undisclosed) and 403 for one they see but may not change.

    `author_draft` mirrors `@feature(..., author_draft=True)`: the author's own still-draft task is editable on
    `tasks:create` alone (`author_draft_admits`)."""
    target = or_404(
        session.scalars(
            select(Task).where(Task.id == board_task_id, *_visible_clauses(session, caller, principal))
        ).first(),
        detail=f"task {board_task_id} not found, or its launcher may not view it",
    )
    mode = ObjectAccess.READ if key == _VIEW else ObjectAccess.WRITE
    if author_draft and author_draft_admits(session, principal, TASK_SPEC, board_task_id, mode=mode):
        return target
    # The feature floor, then the object, then the key on the row's own scope: the guard's order.
    if not principal.has(key):
        raise HTTPException(status_code=403, detail=f"this task's launcher does not hold {key}")
    require_object_access(session, principal, "task", board_task_id, mode=mode)
    if not key_scope_satisfied(session, principal, TASK_SPEC, board_task_id, key=key, mode=mode):
        raise HTTPException(
            status_code=403,
            detail=f"this task's launcher can see task {board_task_id} but holds no {key} on its project",
        )
    return target


def _not_self(caller: Task, board_task_id: int, what: str) -> None:
    if board_task_id == caller.id:
        raise HTTPException(
            status_code=409, detail=f"task {caller.id} may not {what} itself — that would end this very session",
        )


def _not_own_chain(session: Session, caller: Task, board_task_id: int) -> None:
    """409 when the target is the caller or one of its ancestors: a Stop cascades to children."""
    if any(task.id == board_task_id for task in task_and_ancestors(session, caller.id)):
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {caller.id} may not stop task {board_task_id}: it is this task or one of its parents, and a "
                "stop reaches every child — that would end this very session"
            ),
        )


def _row(session: Session, target: Task) -> BoardTaskOut:
    """The row as the console's handler left it (it commits, and may roll back a lost race)."""
    session.refresh(target)
    return from_row(BoardTaskOut, target)


# ── routes ────────────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/agent/tasks/board", response_model=BoardListOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def list_board(
    session: Session,
    door: AgentDoor,
    project: Optional[int] = Query(None, ge=1, description="one project's tasks only"),
    state: Optional[str] = Query(None, max_length=32),
    persona: Optional[str] = Query(None, max_length=64),
    q: Optional[str] = Query(None, max_length=200, description="words in title, description or outcome, or a task number"),
    limit: int = Query(_LIST_DEFAULT, ge=1, le=_LIST_MAX),
) -> BoardListOut:
    """The org's board as the launcher sees it — top-level rows, hidden and archived ones left out — newest
    activity first, the task `q` names by number first."""
    caller, _user, principal = caller_and_launcher(session, *door)
    stmt, number = board_filtered_select(
        principal, state=state, project_id=[project] if project else None, feature_id=None, created_by_id=None,
        persona=persona, q=q, include_hidden=False, include_archived=False, exclude_done=False, focus=False,
        updated_after=None, updated_before=None,
    )
    stmt = stmt.where(*_visible_clauses(session, caller, principal))
    order = [Task.updated_at.desc(), Task.id.desc()]
    if number is not None:
        order.insert(0, case((Task.id == number, 0), else_=1))
    rows, truncated = keyset.page(session, stmt.order_by(*order), limit)
    return BoardListOut(tasks=[from_row(BoardTaskOut, row) for row in rows], truncated=truncated)


@router.post("/api/agent/tasks/board/start", response_model=BoardTaskOut)
@infra(Tenancy.ORG_SCOPED)
@on_session(deferring=True)
def start_board_task(
    session: Session,
    body: BoardTaskIn,
    door: AgentDoor,
) -> BoardTaskOut:
    """Start one task through the console's own Start (`routes/tasks/dispatch.py::start_task`), as the launcher."""
    caller, user, principal = caller_and_launcher(session, *door)
    _not_self(caller, body.board_task_id, "start")
    require_human_started(session, caller, what="starting a task")
    target = _board_task_or_refuse(session, caller, principal, body.board_task_id, key=_EDIT)
    started = start_task.session_body(session, task_id=body.board_task_id, body=None, _user=user, principal=principal)
    log.info("agent board: task=%s started task=%s (as launcher=%s)", caller.id, body.board_task_id, user.id)
    if isinstance(started, JSONResponse):
        # The pipeline's Start step waits (approvals): its 202 body as the console's Start answers it.
        return started  # type: ignore[return-value]
    return _row(session, target)


@router.post("/api/agent/tasks/board/stop", response_model=BoardTaskOut)
@infra(Tenancy.ORG_SCOPED)
@on_session(deferring=True)
def stop_board_task(
    session: Session,
    body: BoardStopIn,
    door: AgentDoor,
) -> BoardTaskOut:
    """Stop (pause) one task through the console's own Stop (`routes/tasks/transitions.py::_stop_task_core`), as
    the launcher; a reason is posted as their comment on it. Idempotent: an already stopped task answers as it is."""
    caller, user, principal = caller_and_launcher(session, *door)
    _not_own_chain(session, caller, body.board_task_id)
    require_human_started(session, caller, what="stopping a task")
    target = _board_task_or_refuse(session, caller, principal, body.board_task_id, key=_EDIT)
    adopt_user(session, user)
    landed = _stop_task_core(body.board_task_id, session, user)
    if body.reason is not None:
        create_comment.session_body(
            session, task_id=body.board_task_id, payload=comment_service.CommentIn(body=body.reason), user=user,
            principal=principal,
        )
    log.info("agent board: task=%s stopped task=%s landed=%s (as launcher=%s)",
             caller.id, body.board_task_id, landed, user.id)
    return _row(session, target)


@router.get("/api/agent/tasks/board/comments", response_model=list[PeerCommentOut])
@infra(Tenancy.ORG_SCOPED)
@on_session
def list_board_comments(
    session: Session,
    request: Request,
    door: AgentDoor,
    board_task_id: int = Query(..., ge=1),
) -> list[PeerCommentOut]:
    """One task's comments, oldest first — the most recent page (`agent_peer_tasks._comments`)."""
    caller, user, principal = caller_and_launcher(session, *door)
    target = _board_task_or_refuse(session, caller, principal, board_task_id, key=_VIEW)
    out = _comments(session, target, user)
    hipaa.record_phi_read(
        session, request, record_type="comments", record_id=target.id, task_id=target.id, org_id=target.org_id,
        actor_label=hipaa.runner_label(caller.id),
    )
    return out


@router.post("/api/agent/tasks/board/comments", response_model=comment_service.CommentOut, status_code=201)
@infra(Tenancy.ORG_SCOPED)
@on_session(deferring=True)
def create_board_comment(
    session: Session,
    body: BoardCommentIn,
    door: AgentDoor,
) -> comment_service.CommentOut:
    """Post a comment through the console's own writer (`routes/task_comments.py::create_comment`): under the
    launcher's name, mentions resolved and notified as theirs."""
    caller, user, principal = caller_and_launcher(session, *door)
    require_human_started(session, caller, what="commenting on a board task")
    _board_task_or_refuse(session, caller, principal, body.board_task_id, key=_EDIT)
    out = create_comment.session_body(
        session, task_id=body.board_task_id, payload=comment_service.CommentIn(body=body.body), user=user,
        principal=principal,
    )
    log.info("agent board: task=%s commented on task=%s (as launcher=%s)", caller.id, body.board_task_id, user.id)
    return out


@router.patch("/api/agent/tasks/board", response_model=BoardTaskOut)
@infra(Tenancy.ORG_SCOPED)
@on_session(deferring=True)
def edit_board_task(
    session: Session,
    body: BoardEditIn,
    door: AgentDoor,
) -> BoardTaskOut:
    """Edit one task through the console's own Save (`routes/tasks/update.py::update_task`): its field rules, the
    merge-lane freeze and the superadmin-only model pin all apply unchanged."""
    caller, user, principal = caller_and_launcher(session, *door)
    require_human_started(session, caller, what="editing a board task")
    target = _board_task_or_refuse(session, caller, principal, body.board_task_id, key=_EDIT, author_draft=True)
    fields = body.model_dump(exclude_unset=True, exclude=set(_NOT_FIELDS))
    update_task.session_body(session, body.board_task_id, TaskUpdate(**fields), user, principal)
    log.info("agent board: task=%s edited task=%s fields=%s (as launcher=%s)",
             caller.id, body.board_task_id, sorted(fields), user.id)
    return _row(session, target)
