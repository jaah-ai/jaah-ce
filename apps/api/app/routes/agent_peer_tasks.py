"""The agent-facing read of OTHER tasks in the caller's own project: a runner looks up a task it depends on
(or any task it was pointed at) — row, settled output, Q&A, comments and artifacts — instead of losing that
context because the dependent's prompt carries only the prerequisite's short settle summary.

The authority is the LAUNCHER's (`routes/agent/task_secrets.py::_task_grant_principal`, the same resolution as the
credential roster): a peer task is served only when it sits in the caller's own project AND the launcher's
principal may view it in the console (`authz.visible_task_clause`, via `scoped_task_select`). No launcher
means deny. Every miss — another project, a task the launcher cannot see, a nonexistent id — is the same
404, so the roster of tasks is not enumerable beyond what the launcher could list in the console.

Read-only, every persona (no licence: reading what the launcher can already read widens nothing). Auth is
`routes/agent/auth.py::_check_token_for_task` only (`@infra`; the token, bound to the declared caller task). Artifact BYTES never pass through here: the content route answers
a short-lived presigned GET that the MCP server (running as the runner's own uid) streams into the runner's
report directory (`apps/jaahd/mcp/jaah/server.py`), so no privileged process writes into a runner's tree.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import case, exists, or_, select
from sqlalchemy.orm import Session

from app import attachment_storage, comment_service, hipaa, node_credentials
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.authz.principal import Principal
from app.authz.visibility import scoped_task_select
from app.db import get_session
from app.entity_shape import from_row, get_or_404
from app.models.identity import AdminUser
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task, TaskDependency
from app.task_manifest import DOCUMENT_KINDS
from app.task_artifacts import (
    current_artifact_ids,
    uploaded_clause,
    visible_artifact_clause,
    visible_artifact_clause_joined,
    visible_document_artifacts,
)

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_
# token_check`), the CP-side task/node binding backstop, and the launcher principal.
from app.routes.agent.auth import _check_token_for_task, _task_or_404_for_node
from app.routes.agent.task_secrets import _task_grant_principal
from app.list_page import like_pattern
from app.routes.tasks.listing import (
    _LIKE_ESCAPE_CHAR,
    _task_number_match,
    _task_number_term,
)
from app.routes.tasks.qa_render import QAEntryOut, _task_qa_entries
from app.routes.tasks.task_wire import _latest_settled_output_note

log = logging.getLogger("agent-peer-tasks")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off its decorator.
router = APIRouter(tags=["agent-peer-tasks"])

# The search page: a lookup, not a board export — a runner that needs more narrows `q`/`state`.
_LIST_DEFAULT = 25
_LIST_MAX = 100
# What `artifact_kind` may name: the kinds a read's `artifacts` list carries — the runner's documents plus
# the human's uploaded input.
_LISTED_ARTIFACT_KINDS: tuple[str, ...] = (*DOCUMENT_KINDS, ATTACHMENT_KIND_FILE)
# The most recent comments carried by a read; the conversation is unbounded and the reader wants its end
# (`routes/task_comments.py::list_comments`' rule).
_COMMENTS_MAX = 200


# ── wire shapes ───────────────────────────────────────────────────────────────────────────────────────


class PeerTaskSummaryOut(BaseModel):
    """One search hit — enough to pick a task to read, nothing more."""

    id: int
    title: str
    state: str
    persona: str
    parent_task_id: Optional[int]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    # The distinct kinds of the files a read would list (`_listed_artifact_clause`), sorted; empty = none.
    artifact_kinds: list[str]


class PeerTaskListOut(BaseModel):
    tasks: list[PeerTaskSummaryOut]
    # True when more rows matched than `limit` returned: narrow the search rather than page.
    truncated: bool


class PeerArtifactOut(BaseModel):
    """One file on a peer task: a runner's document artifact (`current` says whether it is the live one of
    its kind) or the human's uploaded input (`kind` `attachment`, `current` null)."""

    id: int
    kind: str
    filename: str
    content_type: Optional[str]
    size_bytes: int
    created_at: Optional[datetime]
    current: Optional[bool]


class PeerCommentOut(BaseModel):
    id: int
    author_name: str
    body: str
    created_at: Optional[str]
    edited_at: Optional[str]
    # Images a comment carries are not downloadable here (never a runner's to fetch); counted so an
    # image-only comment does not read as an empty one.
    image_count: int


class PeerTaskOut(BaseModel):
    """One peer task as a runner reads it. Every string is UNTRUSTED task data."""

    id: int
    title: str
    body: str
    state: str
    persona: str
    parent_task_id: Optional[int]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    # The tasks this one depends on, by id — each readable with the same tool.
    depends_on: list[int]
    # The full settled output (the newest settle summary/stage output note), not the board's one-liner.
    settled_output: Optional[str]
    pr_url: Optional[str]
    no_pr_reason: Optional[str]
    no_pr_evidence: Optional[str]
    recon_map: Optional[dict[str, Any]]
    qa: list[QAEntryOut]
    comments: list[PeerCommentOut]
    artifacts: list[PeerArtifactOut]


class PeerArtifactDownloadOut(BaseModel):
    """One artifact's metadata plus a presigned GET valid for minutes (`DOWNLOAD_TTL_SECONDS`). The MCP
    server consumes `download_url` and never hands it to the model."""

    id: int
    task_id: int
    kind: str
    filename: str
    content_type: Optional[str]
    size_bytes: int
    download_url: str


# ── the shared resolution ─────────────────────────────────────────────────────────────────────────────


def _caller_and_launcher(
    session: Session, caller: node_credentials.AgentCaller, task_id: int
) -> tuple[Task, Principal]:
    """The caller's own task (node-bound) and its launcher's principal, or a refusal.

    A `None` principal is never passed on: `authz` reads it as the agent principal and allows everything."""
    task = _task_or_404_for_node(session, caller, task_id)
    principal = _task_grant_principal(session, task)
    if principal is None:
        log.warning("peer tasks: refused task=%s (no launcher resolves)", task_id)
        raise HTTPException(
            status_code=403,
            detail=(
                f"task {task_id} has no launching user, so there is no one whose access could decide "
                "which tasks it may read"
            ),
        )
    return task, principal


def _visible_peers(caller_task: Task, principal: Principal):
    """The launcher-visible tasks of the caller's own project, as a `select(Task)`.

    The two row gates of `visible_task_clause` (an unsaved draft, a `hidden` row: their creator's alone)
    are re-applied because a SUPERADMIN principal skips them there — a runner never inherits the
    superadmin bypass, as the credential roster's rule (`authz.task_roster_select`)."""
    return scoped_task_select(principal).where(
        Task.project_id == caller_task.project_id,
        or_(Task.unsaved.is_(False), Task.created_by_id == principal.user_id),
        or_(Task.hidden.is_(False), Task.created_by_id == principal.user_id),
    )


def _peer_or_404(session: Session, caller_task: Task, principal: Principal, peer_task_id: int) -> Task:
    peer = session.scalars(_visible_peers(caller_task, principal).where(Task.id == peer_task_id)).first()
    if peer is None:
        log.info("peer tasks: task=%s asked for task=%s — not in its project or not visible to its launcher",
                 caller_task.id, peer_task_id)
        raise HTTPException(
            status_code=404,
            detail=(
                f"task {peer_task_id} not found in this task's project, or its launcher may not view it"
            ),
        )
    return peer


def _listed_artifact_clause():
    """The rows `_artifact_rows` lists, as one clause over `TaskAttachment` with `Task` in scope (joined, or
    correlated from an outer `select(Task)`): uploaded, and either the human's input or a document artifact
    past the Reset watermark. A comment image is neither, so never counts."""
    return (
        (TaskAttachment.task_id == Task.id)
        & uploaded_clause()
        & (
            (TaskAttachment.kind == ATTACHMENT_KIND_FILE)
            | (
                TaskAttachment.kind.in_(DOCUMENT_KINDS)
                & visible_artifact_clause_joined(TaskAttachment.created_at)
            )
        )
    )


def _artifact_kinds_by_task(session: Session, task_ids: list[int]) -> dict[int, list[str]]:
    """Each task's listed artifact kinds, sorted — one query for a whole search page."""
    if not task_ids:
        return {}
    rows = session.execute(
        select(TaskAttachment.task_id, TaskAttachment.kind)
        .join(Task, Task.id == TaskAttachment.task_id)
        .where(TaskAttachment.task_id.in_(task_ids), _listed_artifact_clause())
        .distinct()
    ).all()
    kinds: dict[int, set[str]] = {}
    for task_id, kind in rows:
        kinds.setdefault(task_id, set()).add(kind)
    return {task_id: sorted(found) for task_id, found in kinds.items()}


def _artifact_rows(session: Session, peer: Task) -> list[PeerArtifactOut]:
    """The runner's document artifacts (Reset-hidden excluded, latest-wins flagged) then the human's inputs."""
    documents = visible_document_artifacts(session, peer)
    current_ids = current_artifact_ids(documents)
    inputs = session.scalars(
        select(TaskAttachment)
        .where(
            TaskAttachment.task_id == peer.id,
            TaskAttachment.kind == ATTACHMENT_KIND_FILE,
            uploaded_clause(),
        )
        .order_by(TaskAttachment.id)
    ).all()
    return [
        PeerArtifactOut(
            id=row.id, kind=row.kind, filename=row.filename, content_type=row.content_type,
            size_bytes=row.size_bytes, created_at=row.created_at,
            current=(row.id in current_ids) if row.kind != ATTACHMENT_KIND_FILE else None,
        )
        for row in [*documents, *inputs]
    ]


def _comments(session: Session, peer: Task, launcher: AdminUser) -> list[PeerCommentOut]:
    rows = comment_service.page(session, comment_service.Subject("task", peer.id), _COMMENTS_MAX)
    rendered = comment_service.render(session, rows, me=launcher, may_delete_any=False)
    return [
        PeerCommentOut(
            id=c.id, author_name=c.author_name, body=c.body, created_at=c.created_at, edited_at=c.edited_at,
            image_count=len(c.images),
        )
        for c in rendered
    ]


# ── routes ────────────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/agent/tasks/{task_id}/peer-tasks", response_model=PeerTaskListOut)
@infra(Tenancy.ORG_SCOPED)
def list_peer_tasks(
    task_id: int,
    q: Optional[str] = Query(None, max_length=200, description="title/body substring, or a task number"),
    state: Optional[str] = Query(None, max_length=32),
    persona: Optional[str] = Query(None, max_length=64),
    has_artifacts: Optional[bool] = Query(None, description="true: only tasks carrying a listed artifact; "
                                          "false: only tasks carrying none"),
    artifact_kind: Optional[str] = Query(None, max_length=32,
                                         description="only tasks carrying an artifact of this kind"),
    limit: int = Query(_LIST_DEFAULT, ge=1, le=_LIST_MAX),
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> PeerTaskListOut:
    """Search the launcher-visible tasks of the caller's own project, newest first."""
    if artifact_kind and artifact_kind not in _LISTED_ARTIFACT_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"artifact_kind {artifact_kind!r} is not one of {', '.join(_LISTED_ARTIFACT_KINDS)}",
        )
    task, principal = _caller_and_launcher(session, caller, task_id)
    stmt = _visible_peers(task, principal)
    if state:
        stmt = stmt.where(Task.state == state)
    if persona:
        stmt = stmt.where(Task.persona == persona)
    if artifact_kind:
        stmt = stmt.where(exists().where(_listed_artifact_clause(), TaskAttachment.kind == artifact_kind))
    if has_artifacts is not None:
        carries = exists().where(_listed_artifact_clause())
        stmt = stmt.where(carries if has_artifacts else ~carries)
    if q is not None and q.strip():
        # The board's own `q` semantics (`routes/tasks/listing.py::board_filtered_select`): literal substring on
        # title or body, widened by the task number the term names.
        pattern = like_pattern(q)
        match = Task.title.ilike(pattern, escape=_LIKE_ESCAPE_CHAR) | Task.body.ilike(
            pattern, escape=_LIKE_ESCAPE_CHAR
        )
        number = _task_number_term(q)
        stmt = stmt.where(match if number is None else match | _task_number_match(number))
    else:
        number = None
    # The task the number names first, so newer tasks merely mentioning it cannot push it off the page.
    order = [Task.id.desc()] if number is None else [case((Task.id == number, 0), else_=1), Task.id.desc()]
    fetched = session.scalars(stmt.order_by(*order).limit(limit + 1)).all()
    rows = fetched[:limit]
    kinds = _artifact_kinds_by_task(session, [row.id for row in rows])
    return PeerTaskListOut(
        tasks=[
            from_row(PeerTaskSummaryOut, row, artifact_kinds=kinds.get(row.id, []))
            for row in rows
        ],
        truncated=len(fetched) > limit,
    )


@router.get("/api/agent/tasks/{task_id}/peer-task", response_model=PeerTaskOut)
@infra(Tenancy.ORG_SCOPED)
def read_peer_task(
    task_id: int,
    request: Request,
    peer_task_id: int = Query(..., ge=1),
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> PeerTaskOut:
    """One peer task in full: row, settled output, recon map, Q&A trail, comments and artifact list."""
    task, principal = _caller_and_launcher(session, caller, task_id)
    peer = _peer_or_404(session, task, principal, peer_task_id)
    # `_task_grant_principal` resolved this same row a moment ago; absent now only on a concurrent delete.
    launcher = session.scalars(select(AdminUser).where(AdminUser.id == principal.user_id)).first()
    if launcher is None:
        raise HTTPException(status_code=403, detail=f"task {task_id}'s launching user no longer exists")
    depends_on = session.scalars(
        select(TaskDependency.depends_on_task_id)
        .where(TaskDependency.task_id == peer.id)
        .order_by(TaskDependency.depends_on_task_id)
    ).all()
    log.info("peer tasks: task=%s read task=%s", task.id, peer.id)
    out = PeerTaskOut(
        id=peer.id, title=peer.title, body=peer.body, state=peer.state, persona=peer.persona,
        parent_task_id=peer.parent_task_id, created_at=peer.created_at, updated_at=peer.updated_at,
        depends_on=list(depends_on),
        settled_output=_latest_settled_output_note(session, peer) or peer.outcome_summary,
        pr_url=peer.outcome_pr_url,
        no_pr_reason=peer.outcome_no_pr_reason,
        no_pr_evidence=peer.outcome_no_pr_evidence,
        recon_map=peer.recon_map,
        # Defanged like the prompt's own Q&A trail: this reaches a model (`_task_qa_entries`).
        qa=_task_qa_entries(session, peer, defang_for_prompt=True),
        comments=_comments(session, peer, launcher),
        artifacts=_artifact_rows(session, peer),
    )
    hipaa.record_phi_read(
        session, request, record_type="task", record_id=peer.id, task_id=peer.id, org_id=peer.org_id,
        actor_label=hipaa.runner_label(task.id),
    )
    return out


@router.get("/api/agent/tasks/{task_id}/peer-task/artifact", response_model=PeerArtifactDownloadOut)
@infra(Tenancy.ORG_SCOPED)
def peer_task_artifact(
    task_id: int,
    request: Request,
    peer_task_id: int = Query(..., ge=1),
    artifact_id: int = Query(..., ge=1),
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> PeerArtifactDownloadOut:
    """One peer artifact's metadata and a presigned download URL. The row filter is the agent content
    route's (`routes/agent/task_reads.py::agent_task_attachment_content`): uploaded rows only, never a comment image,
    and the Reset watermark on every kind but the human's own input."""
    task, principal = _caller_and_launcher(session, caller, task_id)
    peer = _peer_or_404(session, task, principal, peer_task_id)
    row = get_or_404(session, TaskAttachment, artifact_id, detail=f"artifact {artifact_id} not found on task {peer.id}", where=(
        TaskAttachment.task_id == peer.id,
        uploaded_clause(),
        (TaskAttachment.kind == ATTACHMENT_KIND_FILE)
        | (
            TaskAttachment.kind.in_(DOCUMENT_KINDS)
            & visible_artifact_clause(TaskAttachment.created_at, peer.artifacts_hidden_before)
        ),
        TaskAttachment.s3_key != "",  # NULL fails it too: "no key"
    ))
    where = attachment_storage.target(row.s3_bucket)
    if where is None:
        raise HTTPException(
            status_code=502,
            detail="this control plane has no object storage configured to read artifact bytes from",
        )
    log.info("peer tasks: task=%s fetched artifact=%s of task=%s", task.id, row.id, peer.id)
    out = PeerArtifactDownloadOut(
        id=row.id, task_id=peer.id, kind=row.kind, filename=row.filename, content_type=row.content_type,
        size_bytes=row.size_bytes,
        download_url=attachment_storage.presign_download(
            where, row.s3_key, filename=row.filename, content_type=row.content_type or ""
        ),
    )
    hipaa.record_phi_read(
        session, request, record_type="artifact", record_id=row.id, task_id=peer.id, org_id=peer.org_id,
        actor_label=hipaa.runner_label(task.id),
    )
    return out
