"""Tasks an agent files outside its own pipeline: top-level drafts and edits/deletes of its filings."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import node_credentials
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import get_session
from app.models.tasks import Task
# Task transitions go through the watermark-gated helpers; the state machine single-homes in routes/tasks/.
from app.routes.tasks.policy import MAX_TASK_TEXT_LEN, MAX_TASK_TITLE_LEN
from app.persona_registry import (
    SHAPE_DELETE_FILED,
    SHAPE_EDIT_FILED,
    SHAPE_TOP_LEVEL,
    SHAPE_DRAFT_TOP_LEVEL,
)
from app.routes.tasks.agent_filing import (
    delete_agent_filed_draft,
    edit_agent_filed_draft,
    file_agent_top_level_task,
)
from app.entity_shape import StrictModel
from app.routes.agent.auth import (
    MAX_WIRE_TASK_ID,
    _CALLER_TASK_HEADER,
    _check_token,
    _guard_task_creating_caller,
    _refuse_a_node_not_holding,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ── SHAPE_TOP_LEVEL — one standalone task in the caller's own project ─────────────────────────────────
# A licence-holder with no epic (the `advisor`, and the `coder` as drafts only) files one top-level row — a sketch, a task or an epic —
# in its own row's project. Rows, dedup and the start single-home in `file_agent_top_level_task` (app/routes/tasks/agent_filing.py).
# The most attachments one filing or amend may name: a draft's inputs, not an archive.
MAX_FILED_ATTACHMENTS = 20


class AgentFiledAttachmentIn(StrictModel):
    """One file to attach to a filed draft: an artifact the CALLER'S OWN task uploaded. The daemon turns a
    runner's `{path}` entry into one of these by uploading it first (`apps/jaahd/cred_broker.py`), so the
    CP only ever sees ids. Another task's id is a 404 (`_caller_artifacts_or_404`, app/routes/tasks.py)."""

    artifact_id: int = Field(ge=1, le=MAX_WIRE_TASK_ID)


class AgentTopLevelTaskIn(StrictModel):
    """No `project` field by design: it is read off the caller's own row, and a caller-named project would be
    self-granted authority. Do not add one (see the writer's docstring). Strict: an unknown field is refused,
    not ignored — the advisor is the persona most exposed to prompt injection.
    """

    # Stored whole, only sanitized, by the writer (`authored_title`), then refined to 5-6 words out of band
    # (`app/task_title.py`). Bounded here, unlike its siblings: this persona ingests untrusted material, so the
    # stored title and downstream log line must not be unbounded.
    title: str = Field(min_length=1, max_length=MAX_TASK_TITLE_LEN)
    # Stored verbatim; capped because it reaches a runner launch as one argv element (`E2BIG` past ~128KiB).
    body: str = Field(min_length=1, max_length=MAX_TASK_TEXT_LEN)
    # Required, as on `/follow-up`: a re-run filer would otherwise stack dispatchable duplicates. Bounded and
    # charset-restricted.
    dedup_key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")

    # ── QUEUEING IS NOT STARTING, AND `False` IS THE SAFE DIRECTION ───────────────────────────────────
    # A `draft` is on the board and one press from running; a `ready` row spends a pool slot the operator did
    # not ask to spend, and a full pool fails leases rather than evicting. `True` is for an explicit "file it
    # and start it". Declared here because `model_config` is `extra='forbid'`.
    start: bool = False

    # ── WHAT KIND OF ROW: the console's Depth axis, and Involvement's one bit ─────────────────────────
    # `depth` picks the persona (sketch -> prototyper, task -> the project's default persona or coder, epic -> epic + analysis children);
    # `persona` narrows `task` alone, validated by the writer (`_top_level_persona_or_422`), which owns the
    # legal set so it is derived in one place. `guided` stores `autonomous = false`; absent is unattended.
    depth: Literal["sketch", "task", "epic"] = "task"
    persona: Optional[str] = Field(default=None, min_length=1, max_length=64)
    guided: bool = False
    # The caller's own artifacts, attached to the new row as its inputs (shared bytes, no copy). Must land
    # together with `_TOOL_CREATE_TASK`'s `attachments` (`extra='forbid'`).
    attachments: list[AgentFiledAttachmentIn] = Field(default_factory=list, max_length=MAX_FILED_ATTACHMENTS)


class AgentTopLevelTaskOut(BaseModel):
    id: int
    # False when `dedup_key` matched a live row, returned untouched (it may be leased); True on insert.
    created: bool
    # Answered, not accepted: how the caller learns which project its filing landed in.
    project_id: int
    # `"draft"`, or on an honoured `start` `"ready"` (`"active"` for an epic). On a dedup collapse (`created=false`) it is
    # the existing row's current state — which may be started even for a drafts-only filer.
    state: str
    # The persona the filing resolved to, so the caller can say what it filed.
    persona: str
    # The stored title (sanitized, or the matched row's on a collapse), so the caller reports what the board says.
    title: str


@router.post("/tasks", response_model=AgentTopLevelTaskOut)
@infra(Tenancy.ORG_SCOPED)
def create_agent_top_level_task(
    body: AgentTopLevelTaskIn,
    x_agent_task_id: str = Header(default=""),
    agent: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> AgentTopLevelTaskOut:
    """File one standalone top-level Task — sketch, task or epic — in the caller's own project, at `draft`
    unless `start=true` (`SHAPE_TOP_LEVEL`; under `SHAPE_DRAFT_TOP_LEVEL` alone `start` is ignored and the row is
    always a draft). `X-Agent-Task-Id` both authorises and resolves the project, off one row.
    Idempotent on the required `dedup_key`.

    Default `draft` (see `AgentTopLevelTaskIn.start`; transition in `file_agent_top_level_task`). No path
    scope: with no epic, the bound is the licence (own project, one row per key, licence-free personas
    only), all server-side.

    404 when the caller is no longer a live filer, 409 when the key is held by a row this shape will not
    dedup against (the writer's branches). `host` is a 422: no row to
    read a project off; its channel is `POST /tasks/system`.
    """
    caller = _require_filing_caller(
        x_agent_task_id, session, shape=(SHAPE_TOP_LEVEL, SHAPE_DRAFT_TOP_LEVEL), agent=agent
    )
    result = file_agent_top_level_task(caller, body.model_dump(), session)
    if result["created"]:
        # After the writer's commit, as `create_task` enqueues: the board's title is the filer's 5-6 word gist
        # (`app/task_title.py`), not their whole line. Deferred import: the queue module registers a workflow.
        from app.durable.task_title_queue import enqueue_title

        enqueue_title(result["id"], result["title"])
    return AgentTopLevelTaskOut(**result)


# ── SHAPE_EDIT_FILED / SHAPE_DELETE_FILED — the filing's own lifecycle, over ONE draft the caller filed ──
# Both reach only a `draft` whose `created_by_task_id` is the caller's own task
# (`_own_filed_draft_or_refuse`, app/routes/tasks/agent_filing.py); authority is the licence per shape. The target id
# travels in the body (amend) or query (delete) as `filed_task_id`, never the path or `task_id` (the
# daemon fills `{task_id}` from the peer identity); the path is the literal `/tasks/filed`.
class AgentFiledDraftEditIn(StrictModel):
    """The amend: which filed draft, its new title and body — both, always (the chat holds the current
    text; unchanged fields are no-ops) — and any attachments to add.

    `extra='forbid'` refuses `state`, `persona`, `dedup_key` and any other field, for
    `AgentTopLevelTaskIn`'s reasons: an amend edits exactly the title and body, and adds attachments.
    """

    # The `id` `task_create` answered. Bounded inside int4 (`MAX_WIRE_TASK_ID`), so a 422 rather than a DB error.
    filed_task_id: int = Field(ge=1, le=MAX_WIRE_TASK_ID)
    # Same bounds as the filing's own fields, for the same reasons (`AgentTopLevelTaskIn`).
    title: str = Field(min_length=1, max_length=MAX_TASK_TITLE_LEN)
    body: str = Field(min_length=1, max_length=MAX_TASK_TEXT_LEN)
    # ADDED to the draft's attachments, never replacing them; one already attached is skipped.
    attachments: list[AgentFiledAttachmentIn] = Field(default_factory=list, max_length=MAX_FILED_ATTACHMENTS)


class AgentFiledDraftEditOut(BaseModel):
    id: int
    project_id: int
    # `draft`, always, on success; answered for shape parity with `task_create`.
    state: str
    # The stored title (sanitized, `authored_title`), so the chat reports what the board says.
    title: str


class AgentFiledDraftDeleteOut(BaseModel):
    id: int
    deleted: bool


def _require_filing_caller(
    x_agent_task_id: str, session: Session, *, shape: str | tuple[str, ...], agent: node_credentials.AgentCaller
) -> Task:
    """`_guard_task_creating_caller` for the doors acting on the caller's own filings, where `host` is a 422:
    no Task behind the call means no project or filing. The declared task must not be held on another node
    (`_refuse_a_node_not_holding`), so another node of the org cannot borrow its licence and project.
    """
    caller = _guard_task_creating_caller(x_agent_task_id, session, shape=shape)
    if caller is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"this endpoint acts on the CALLER'S OWN filings, so {_CALLER_TASK_HEADER} must name your "
                "task id; a producer that is no Task has no project to file into and nothing filed "
                "to act on — a host producer files through POST /api/agent/tasks/system instead"
            ),
        )
    _refuse_a_node_not_holding(session, agent, caller.id)
    return caller


@router.patch("/tasks/filed", response_model=AgentFiledDraftEditOut)
@infra(Tenancy.ORG_SCOPED)
def edit_agent_filed_task(
    body: AgentFiledDraftEditIn,
    x_agent_task_id: str = Header(default=""),
    agent: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> AgentFiledDraftEditOut:
    """Rewrite the title and body of one `draft` the caller's own task filed (`SHAPE_EDIT_FILED`). 403 without
    the shape, 404 for a row that is not the caller's draft (existence undisclosed), 409 once it left
    `draft`; the writer (`edit_agent_filed_draft`) owns every branch.
    """
    caller = _require_filing_caller(x_agent_task_id, session, shape=SHAPE_EDIT_FILED, agent=agent)
    result = edit_agent_filed_draft(caller, body.filed_task_id, body.model_dump(), session)
    return AgentFiledDraftEditOut(**result)


@router.delete("/tasks/filed", response_model=AgentFiledDraftDeleteOut)
@infra(Tenancy.ORG_SCOPED)
def delete_agent_filed_task(
    filed_task_id: int = Query(
        ..., ge=1, le=MAX_WIRE_TASK_ID, description="the id a task_create call answered",
    ),
    x_agent_task_id: str = Header(default=""),
    agent: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> AgentFiledDraftDeleteOut:
    """Delete one `draft` the caller's own task filed (`SHAPE_DELETE_FILED`). The amend's refusals, then
    `delete_task_core` via `delete_agent_filed_draft`. A 200 with a body: the caller is a model reading a
    tool result.
    """
    caller = _require_filing_caller(x_agent_task_id, session, shape=SHAPE_DELETE_FILED, agent=agent)
    result = delete_agent_filed_draft(caller, filed_task_id, session)
    return AgentFiledDraftDeleteOut(**result)
