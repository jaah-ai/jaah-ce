"""Agent reads and claims of tasks: by id, attachments, bundle, claim, system tasks and needs-attention."""
from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import (
    agent_bundles,
    attachment_storage,
    entity_shape,
    hipaa,
    merge_lane_state,
    node_credentials,
    runner_terminations,
)
from app.actors import task_actor_values
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import get_session
from app.events import emit_console_event
from app.task_ancestry import executor_user_id
# The Reset soft-delete watermark's ONE predicate — imported, never re-spelled here.
from app.task_artifacts import uploaded_clause, visible_artifact_clause
from app.task_lookup import get_task_or_404
# Every PERSONA fact comes from the one registry record per persona (`app/persona_registry.py`).
from app.persona_registry import DONE_CRITERION_OUTCOME, PERSONA_DONE_CRITERION
from app.models.agent_sessions import Agent, AgentEnablement
from app.models.projects import Project
from app.models.task_extras import ATTACHMENT_KIND_COMMENT_IMAGE, ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task, TaskQuestion
# Task transitions go through the watermark-gated helpers; the state machine single-homes in routes/tasks/.
from app.routes.tasks.dispatch import (
    _RUNNER_BINDING_CLEARED,
    _terminates_a_running_runner,
    claim_task_active_ready_by_id,
    task_dispatch_out,
    _resolve_open_task_by_id,
)
from app.routes.tasks.agent_filing import _UNIQUE_DEDUP_INDEX, body_with_dedup_marker
from app.routes.tasks.policy import PERSONA_ADHOC, PERSONA_TRIAGE, OPERATOR_CRITERION_PERSONAS
from app.persona_registry import SHAPE_SYSTEM_TASK, SYSTEM_PERSONAS
from app.routes.tasks.epics import _maybe_roll_up_parent
from app.routes.tasks.create import _snapshot_body_version
# State vocabulary and guard sets single-home in `app/task_lifecycle.py`.
from app.task_lifecycle import (
    AWAITING_RESPONSE,
    DONE,
    READY,
    _NEEDS_ATTENTION_STATES,
    _RESOLVABLE_SYSTEM_TASK_STATES,
    _SYSTEM_TASK_DEDUP_EXCLUDED_STATES,
    _SYSTEM_TASK_INITIAL_STATES,
    _SYSTEM_TASK_TERMINAL_STATES,
)
from app.entity_shape import constraint_name, get_or_404
from app.routes.agent.auth import (
    _check_token,
    _check_token_for_declared_task,
    _check_token_for_live_task,
    _check_token_for_task,
    _guard_task_creating_caller,
    _wire_project,
    caller_node_ref,
    require_caller_task,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])
log = logging.getLogger("agent-routes")


# ── By-id resolver (the composer's DB-SSOT dispatch source) ──────────────────────────────────────────
@router.get("/tasks/by-id")
@infra(Tenancy.ORG_SCOPED)
def agent_task_by_id(
    task_id: int,
    request: Request,
    _: None = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> Response:
    """Agent-token source of a Task's dispatch content: the single DB input `app/dispatch_compose.py` builds
    a runner's prompt from (`TaskDispatchOut`). 200 + body on an open-dispatch-state match, 200 + empty
    body otherwise. No 409: a primary key is unique.
    """
    task = _resolve_open_task_by_id(task_id, session)
    if task is None:
        return Response(status_code=200)
    # `None` is the infrastructure principal, explicitly: the agent surface must see the projection
    # unfiltered (every prerequisite output, recon map, attachment and out-of-scope item).
    content = task_dispatch_out(task, session, None).model_dump()
    hipaa.record_phi_read(
        session, request, record_type="task", record_id=task.id, task_id=task.id, org_id=task.org_id,
        actor_label=hipaa.runner_label(task.id),
    )
    return JSONResponse(content=content)


@router.get("/tasks/{task_id}/attachments/{attachment_id}/content")
@infra(Tenancy.ORG_SCOPED)
def agent_task_attachment_content(
    task_id: int,
    attachment_id: int,
    request: Request,
    _: None = Depends(_check_token_for_declared_task),
    session: Session = Depends(get_session),
) -> Response:
    """Agent-token content fetch for one attachment's bytes — always a 307 to a presigned GET.

    The read path `apps/jaahd/fleet_dispatch.py` uses to materialize files into a runner's workspace
    (jaahd has no DB credentials). 404 unless `attachment_id` belongs to `task_id`. Never carries a byte
    (`app/attachment_storage.py`).

    The Reset watermark applies to every kind except the human's own file, as in
    `app/routes/attachments.py::user_task_attachment_content`: otherwise the agent token could fetch a
    deliverable the console has hidden. `ATTACHMENT_KIND_FILE` is named positively because Reset abandons
    the runner's attempt, not the human's input. A comment image is never a runner's to fetch
    (`app/routes/attachments.py`'s module docstring).
    """
    task = session.get(Task, task_id)
    # No loader option needed: the row is metadata and an S3 key.
    attachment = get_or_404(session, TaskAttachment, attachment_id, detail="attachment not found", where=(
        TaskAttachment.task_id == task_id,
        # A pending row is not a row yet (`app/task_artifacts.py::uploaded_clause`).
        uploaded_clause(),
        TaskAttachment.kind != ATTACHMENT_KIND_COMMENT_IMAGE,
        or_(
            TaskAttachment.kind == ATTACHMENT_KIND_FILE,
            visible_artifact_clause(
                TaskAttachment.created_at,
                task.artifacts_hidden_before if task is not None else None,
            ),
        ),
    ))
    if not attachment.s3_key:
        # A keyless row's bytes never arrived; there is nowhere else to look, so 404.
        raise HTTPException(status_code=404, detail="attachment not found")
    # The node fetches from S3 directly (D4/D8), after the 404 guards. Filename and type are signed into the
    # URL (`attachment_storage.presign_download`). Not `browser_url`: nodes dial the endpoint directly. The row's
    # own bucket, in its own region (`TaskAttachment.s3_bucket`).
    where = attachment_storage.target(attachment.s3_bucket)
    if where is None:
        raise HTTPException(
            status_code=502,
            detail=(
                f"attachment {attachment_id} has its bytes in object storage, but this control "
                f"plane has no bucket or credentials configured to read them"
            ),
        )
    url = attachment_storage.presign_download(
        where,
        attachment.s3_key,
        filename=attachment.filename,
        content_type=attachment.content_type or "",
    )
    if task is not None:
        hipaa.record_phi_read(
            session, request, record_type="attachment", record_id=attachment.id, task_id=task_id,
            org_id=task.org_id, actor_label=hipaa.runner_label(task_id),
        )
    return RedirectResponse(
        url=url,
        status_code=307,
        # The signature expires in minutes; a cached redirect would fail at S3.
        headers={"Cache-Control": "no-store"},
    )


@router.get("/tasks/{task_id}/agent-bundle")
@infra(Tenancy.ORG_SCOPED)
def agent_task_bundle(
    task_id: int,
    _: node_credentials.AgentCaller = Depends(_check_token_for_live_task),
    session: Session = Depends(get_session),
) -> Response:
    """The SDK agent code a task's run uses — always a 307 to a presigned GET of its bundle (DD3).

    Keyed on the task's OWN claim stamps (`run_agent_id`, `run_agent_commit`) and nothing the caller names,
    so a node holding this task gets exactly the commit its dispatch recorded, never another task's or a
    newer refresh's. A node not holding the task NOW gets 409 (`_require_live_hold`), whoever else holds it. The
    node checks the bytes against `bundle_sha256` itself (K4)."""
    task = get_or_404(session, Task, task_id, detail=f"task {task_id} has no agent run")
    if task.run_agent_id is None or not task.run_agent_commit:
        raise HTTPException(status_code=404, detail=f"task {task_id} has no agent run")
    # The stamp must name an agent the task's org may run: its own, or a connector it enabled. A stamp writer
    # that missed that check must not turn this route into a read of another org's code.
    runnable = session.scalar(
        select(Agent.id).where(Agent.id == task.run_agent_id, Agent.org_id == task.org_id)
    ) or session.scalar(
        select(AgentEnablement.agent_id).where(
            AgentEnablement.agent_id == task.run_agent_id, AgentEnablement.org_id == task.org_id
        )
    )
    if runnable is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} has no agent run")
    where = attachment_storage.target()
    if where is None:
        raise HTTPException(
            status_code=502,
            detail="agent code is in object storage, but this control plane has no bucket or credentials to read it",
        )
    return RedirectResponse(
        url=attachment_storage.presign_download(
            where,
            agent_bundles.bundle_key(task.run_agent_id, task.run_agent_commit),
            filename=f"{task.run_agent_commit}.tar.gz",
            content_type="application/gzip",
        ),
        status_code=307,
        headers={"Cache-Control": "no-store"},
    )


def _content_disposition(filename: str, disposition: str = "attachment") -> str:
    """A safe `Content-Disposition` value for a user-supplied filename.

    `filename*=UTF-8''...` (RFC 6266/5987) carries the real name, percent-encoded; browsers prefer it.
    `filename="..."` is ASCII-only and deliberately lossy: a non-latin-1 byte would raise
    `UnicodeEncodeError` when the header is built (a permanent 500), so everything outside printable ASCII,
    plus double quote and backslash (header injection), becomes `_`.

    `disposition` defaults to download; the report render route (`app/routes/attachments.py`) passes `inline`.
    """
    safe = re.sub(r'[^\x20-\x7e]|["\\]', "_", filename)
    encoded = urllib.parse.quote(filename, safe="")
    return f"{disposition}; filename=\"{safe}\"; filename*=UTF-8''{encoded}"


# ── Dispatcher-owned ready→active claim ───────────────────────────────────────
class ClaimActiveIn(BaseModel):
    # The claim key: the Task's own row id (`claim_task_active_ready_by_id`). Stamped by the daemon from
    # the calling session's identity on the socket path, like `node_id` below.
    task_id: int
    # Which node is claiming (name or uuid). Absent ⇒ a node-restricted task's claim is refused; unrestricted
    # ones are unaffected. Stamped by the daemon from the caller's identity (`Verb.identity_body_keys`).
    node_id: Optional[str] = None


class ClaimActiveResult(BaseModel):
    # True iff this call won `ready → active`; False when already active/blocked/… or absent (idempotent).
    claimed: bool


@router.post("/tasks/claim-active", response_model=ClaimActiveResult)
@infra(Tenancy.ORG_SCOPED)
def claim_active_task(
    body: ClaimActiveIn,
    x_agent_task_id: str = Header(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> ClaimActiveResult:
    """Dispatcher-owned `ready → active` claim, written after the dispatcher wins the KV lease; the DB `tasks`
    row is the sole source of `ready ↔ active`.

    Funnels through `claim_task_active_ready_by_id` (app/routes/tasks.py), an atomic guarded UPDATE. No
    gh/git here. `node_id` carries the node floor and is derived from the credential
    (`caller_node_ref`), so a node cannot claim a floor on another node.
    """
    require_caller_task(session, caller, x_agent_task_id, body.task_id)
    # Derived where the credential can say so.
    node_ref = caller_node_ref(session, caller, body.node_id)
    claimed = claim_task_active_ready_by_id(body.task_id, session, node_id=node_ref)
    return ClaimActiveResult(claimed=claimed)


# Post-merge gate verification is retired; its routes must not return.


# ── System-task creation ──────────────────────────────────────────────────────
# The producer route that materializes a hidden system Task (janitor/compliance_fix/maturity/
# portfolio_drift/harvest/triage/adhoc). System Tasks report through this same API.
class SystemTaskIn(BaseModel):
    # One of SYSTEM_PERSONAS (422 otherwise) — the enforcing home is `create_system_task` below.
    persona: str
    # The project's id. `project` (a name inside the calling node's org) is read only when this is absent —
    # an older daemon's shape (`_wire_project`).
    project_id: Optional[int] = None
    project: str = ""
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    # Operator dispatch-order lever (Task.priority); omitted → the column server_default (0 = normal).
    priority: Optional[int] = None
    # Initial state: draft|ready|active (default ready), never terminal. `draft` is a notice nothing should
    # dispatch (an operator alarm, `app/node_liveness_job.py`); remediation stays `ready`.
    state: str = READY
    # One-open-per-(persona, project) dedup: when true, an existing NON-TERMINAL row of the same
    # (persona, project_id) is returned (created=false) instead of inserting a duplicate.
    dedup_open: bool = False
    # Optional finer dedup discriminator, required when persona == 'adhoc' (422 without it): per-(persona,
    # project) alone is too coarse for per-source escalations and would clobber unrelated `adhoc` Tasks.
    #
    # Bounded and charset-restricted (migration 0144), like `FinishFollowUpIn.dedup_key`: a stored, indexed
    # column, and past the btree tuple limit Postgres raises `program_limit_exceeded`, which is not an
    # `IntegrityError` and would 500 the alarm path.
    dedup_key: Optional[str] = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"
    )


class SystemTaskOut(BaseModel):
    id: int
    # False when `dedup_open` matched an existing non-terminal row and returned it unchanged; True when
    # this call inserted a new Task.
    created: bool


# Advisory-lock namespace for the keyless half of `create_system_task`'s dedup; prefixed because
# `hashtext` is 32 bits. The keyed half relies on `uq_tasks_open_dedup_key`.
_SYSTEM_TASK_DEDUP_LOCK_PREFIX = "system_task_dedup:"


def _open_dedup_candidate(
    session: Session, *, project_id: int, persona: str, dedup_key: Optional[str]
) -> Optional[Task]:
    """The one open system Task a `dedup_open` call would collapse onto, or None.

    A `dedup_key` narrows the match to that key; without one it is one-open-per-(persona, project), valid
    only for a per-producer persona. Matched on the column `uq_tasks_open_dedup_key` indexes so probe and
    insert agree. Ordered by id for determinism.
    """
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.persona == persona,
            Task.state.not_in(_SYSTEM_TASK_DEDUP_EXCLUDED_STATES),
        )
        .order_by(Task.id)
    )
    if dedup_key:
        stmt = stmt.where(Task.dedup_key == dedup_key)
    return session.scalars(stmt).first()


def _refresh_dedup_hit(existing: Task, body: "SystemTaskIn", session: Session) -> None:
    """Refresh the title/body of the row a `dedup_open` call collapsed onto, so a producer re-detecting the
    same condition keeps the latest gap note.

    Not a transition: state/hidden/persona/parent/org untouched, no lifecycle event. The outgoing title/body
    is snapshotted into `task_body_versions` first. Marker and column are re-asserted for both body parsers.
    """
    _snapshot_body_version(existing, "system_dedup_refresh", session)
    existing.title = body.title
    existing.body = body_with_dedup_marker(body.body, body.dedup_key)
    existing.dedup_key = body.dedup_key


# `ORG_SCOPED`, like its find/resolve siblings: "the row this key dedups onto" and "the row this key
# finds" must not drift (`find_open_system_task`). It writes one tenant's Task into one tenant's Project.
# Fleet-wide producers arrive on `ARM_CP_SELF`, which `_read_scope_for` binds `OPERATOR`. The free
# `body.project_id` reach below is not blessed as fleet-wide; the write seam's composite keys contain it.
@router.post("/tasks/system", response_model=SystemTaskOut)
@infra(Tenancy.ORG_SCOPED)
def create_system_task(
    body: SystemTaskIn,
    x_agent_task_id: str = Header(default=""),
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> SystemTaskOut:
    """Create a hidden system Task (self_heal janitor remediation, maturity check-lacking, portfolio drift,
    lessons harvest, plain fleet adhoc).

    The row is `hidden = true` (the sole UI-filter authority) with `parent_task_id NULL`. `persona` must be
    in SYSTEM_PERSONAS and `state` in `_SYSTEM_TASK_INITIAL_STATES` (422); 404 on an unknown project
    (`body.project_id`, else the legacy name inside the calling node's org — `_wire_project`).

    The dedup guard is single-homed here: a keyed call relies on `uq_tasks_open_dedup_key` (migration
    0144); a coarse call (`dedup_open`, no key) takes a transaction-scoped advisory lock, since that index
    covers only `dedup_key IS NOT NULL`.

    `dedup_open`: a same-(persona, project) row outside `_SYSTEM_TASK_DEDUP_EXCLUDED_STATES` (`done`,
    `system_failed`, `paused`, `awaiting_response` — the last because it awaits a human decision on its
    current text; `test_dedup_key_column.py` pins the migration's copy) is refreshed in place, its old
    title/body snapshotted to `task_body_versions`, and returned with created=false. `dedup_key` is
    required for `adhoc`. A caller declaring itself a Task needs a `SHAPE_SYSTEM_TASK` licence (403); the
    two epic fan-out roles hold one, `advisor` does not.
    """
    caller = _guard_task_creating_caller(x_agent_task_id, session, shape=SHAPE_SYSTEM_TASK)
    if body.persona not in SYSTEM_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=f"persona {body.persona!r} is not a system persona — must be one of "
                   f"{list(SYSTEM_PERSONAS)}",
        )
    if merge_lane_state.is_lane_persona(body.persona):
        # A script persona (the merge lane) is filed by the control plane alone (`merge_lane.ensure_lane`):
        # a caller-filed one would dispatch outside the lane's own k and dedup keys.
        raise HTTPException(
            status_code=422,
            detail=f"persona {body.persona!r} is filed by the control plane only, never by a caller",
        )
    if body.state not in _SYSTEM_TASK_INITIAL_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state {body.state!r} not allowed for a system task — must be one of "
                   f"{list(_SYSTEM_TASK_INITIAL_STATES)}",
        )
    if body.dedup_open and body.persona == PERSONA_ADHOC and not body.dedup_key:
        # `adhoc` is the shared catch-all for deferred follow-ups; a coarse match would clobber an unrelated Task,
        # so `adhoc` callers must narrow with a `dedup_key`.
        raise HTTPException(
            status_code=422,
            detail="dedup_open with persona='adhoc' requires dedup_key — a coarse (persona, project) "
                   "match would dedup against ANY open adhoc Task, not just ones from this same source",
        )
    project_row = _wire_project(session, _, body.project_id, body.project)
    if project_row is None:
        named = body.project_id if body.project_id is not None else repr(body.project)
        raise HTTPException(status_code=404, detail=f"project {named} not found")
    if caller is not None and project_row.id != caller.project_id:
        # A task-backed caller files into its own project only: the licence answers which shape, not where, and
        # `body.project_id` is a free value. This makes the `plan`/`finish` "its own epic's" constraint
        # mechanical. The `host` producer (caller is None) is exempt: portfolio audit and drift crons file
        # across repos.
        log.warning(
            "system-task refused: task %s (project=%s) named project %s",
            caller.id, caller.project_id, project_row.id,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"task {caller.id} may only file system tasks in its own project; "
                f"{project_row.name!r} is another"
            ),
        )
    if body.dedup_open:
        # The coarse path needs the advisory lock: `uq_tasks_open_dedup_key` covers only keyed rows, and without
        # the lock concurrent keyless callers land duplicates. Not a second unique index: `(project_id, persona)
        # WHERE dedup_key IS NULL` would also constrain callers that never asked to dedup.
        if body.dedup_key is None:
            lock_key = f"{_SYSTEM_TASK_DEDUP_LOCK_PREFIX}{project_row.id}:{body.persona}:"
            session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))
        existing = _open_dedup_candidate(
            session, project_id=project_row.id, persona=body.persona, dedup_key=body.dedup_key
        )
        if existing is not None:
            _refresh_dedup_hit(existing, body, session)
            session.commit()
            return SystemTaskOut(id=existing.id, created=False)
    executor_id = executor_user_id(session, caller.id) if caller is not None else None
    task = Task(
        project_id=project_row.id,
        title=body.title,
        body=body_with_dedup_marker(body.body, body.dedup_key),
        # The column is the dedup authority (migration 0144); the body marker is a rollout compatibility shim.
        dedup_key=body.dedup_key,
        persona=body.persona,
        state=body.state,
        hidden=True,
        parent_task_id=None,
        # Provenance and therefore confinement: the actor listener stamps only `updated_by_id` (`app/actors.py`),
        # so without this the row has no creator and no person's reach narrows its accounts or credentials
        # (only the org floor in `placement_grants`). The caller's original executor via the ancestry walk
        # (starter, else creator), not `caller.created_by_id`, so a null-stamped caller cannot file
        # null-stamped rows; it also starts the row, so it pushes as that human. A `host` producer stays
        # NULL (D5).
        created_by_id=executor_id,
        started_by_id=executor_id,
        # The project's org (§4.6): `tasks.project_id` is a same-org key, so an alarm about a tenant's node or
        # cron files into the operator's Alarm project as the operator's row. Stamped here because every producer
        # runs on the operator plane, where the write seam stamps nothing (`app/tenancy/write_seam.py`) and
        # `placement_grants` would refuse the row.
        org_id=project_row.org_id,
    )
    if body.priority is not None:
        task.priority = body.priority
    session.add(task)
    try:
        session.flush()   # assign task.id before the event emit
    except IntegrityError as exc:
        # Only this constraint means "lost the race"; any other `IntegrityError` re-raises.
        if _UNIQUE_DEDUP_INDEX not in (constraint_name(exc) or ""):
            raise
        # Roll back the whole transaction, not a savepoint: for an HTTP caller everything before was a read, and a
        # savepoint leaves the added `task` unusable (`PendingRollbackError`). Contract: an in-process caller
        # passing its own session (`app/node_liveness_job.py::_Seams.create_task`) must hold no uncommitted work.
        session.rollback()
        # `uq_tasks_open_dedup_key` refused the insert: a concurrent caller won the race, so degrade to the
        # refresh this call would have done. A raced alarm must never 500.
        # A key without `dedup_open` gets a 409 naming the key: the index ignores `dedup_open`, so a refresh would
        # defy the caller and a re-raise would crash.
        if body.dedup_key is not None and not body.dedup_open:
            raise HTTPException(
                status_code=409,
                detail=f"dedup_key {body.dedup_key!r} is already held by an open {body.persona} task in "
                       f"project '{project_row.name}'. Pass dedup_open=true to refresh that row instead, or "
                       f"choose a distinct key.",
            )
        if body.dedup_key is None:
            raise
        winner = _open_dedup_candidate(
            session, project_id=project_row.id, persona=body.persona, dedup_key=body.dedup_key
        )
        if winner is None:
            # The conflicting row settled meanwhile; surface the original error rather than retrying the insert.
            raise
        _refresh_dedup_hit(winner, body, session)
        session.commit()
        log.info(
            "system task dedup race on %s (persona=%s, project=%s) — refreshed task %s instead",
            body.dedup_key, body.persona, project_row.id, winner.id,
        )
        return SystemTaskOut(id=winner.id, created=False)
    emit_console_event(session, entity="task", id=task.id, action=body.state)
    session.commit()
    log.info(
        "created hidden system task %s (persona=%s, project=%s, state=%s)",
        task.id, body.persona, project_row.id, body.state,
    )
    return SystemTaskOut(id=task.id, created=True)


# ── System-task lookup + resolve ──────────────────────────────────────────────
# Stateless producers do not keep the id they opened; this returns the one open Task for (persona,
# project), the same row `create_system_task`'s dedup matches.
class OpenSystemTaskOut(BaseModel):
    # id of the one open (non-terminal) system Task for the queried (persona, project); None → nothing
    # open (a clean no-op for the caller — a cleared condition with no Task left to resolve).
    id: Optional[int] = None


@router.get("/tasks/system/open", response_model=OpenSystemTaskOut)
@infra(Tenancy.ORG_SCOPED)
def find_open_system_task(
    persona: str,
    project: str = "",
    project_id: Optional[int] = None,
    dedup_key: Optional[str] = None,
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> OpenSystemTaskOut:
    """Return the id of the single open system Task for (persona, project), or None — the lookup stateless
    producers use to drive the by-id resolve/settle routes.

    Same excluded states and ordering as `create_system_task`'s dedup, via the shared
    `_open_dedup_candidate`. `dedup_key` narrows to that key's row, so a coarse `adhoc` match cannot close
    an unrelated Task. `persona` must be in SYSTEM_PERSONAS (422); 404 on an unknown project (`project_id`,
    else the legacy name inside the calling node's org — `_wire_project`).
    """
    if persona not in SYSTEM_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=f"persona {persona!r} is not a system persona — must be one of {list(SYSTEM_PERSONAS)}",
        )
    project_row = _wire_project(session, _, project_id, project)
    if project_row is None:
        named = project_id if project_id is not None else repr(project)
        raise HTTPException(status_code=404, detail=f"project {named} not found")
    # The same helper as the dedup, so the two cannot drift.
    existing = _open_dedup_candidate(
        session, project_id=project_row.id, persona=persona, dedup_key=dedup_key
    )
    return OpenSystemTaskOut(id=existing.id if existing is not None else None)


def recently_settled_system_task(
    session: Session,
    *,
    persona: str,
    project_id: int,
    dedup_key: str,
    within_seconds: float,
    cleared_prefix: str,
) -> Optional[dict]:
    """The most recently settled system Task carrying `dedup_key`, if it settled inside `within_seconds`.

    The complement of `_open_dedup_candidate`: once an alarm is settled its key is free and the next pass
    re-files, so a producer that wants a cooldown needs to see the closure (`app/node_liveness.py`'s
    `_rearm_suppressed`; `app/lease_drain.py` answers it from its own scan). In-process only.

    The closure clock is `github_synced_at` (stamped only by `resolve_system_task`), falling back to
    `updated_at`, which any write bumps and would extend a mute. Residual error is a late re-file, never a
    loop. Requires `hidden`, a system persona, and `closed_by_human`: only a person dismissing the card
    earns the cooldown (a node token could otherwise mute the alarm about itself).

    Returns `id`, `age_seconds` and `cleared_by_sweep` (a boolean, never the tenant's summary text). None
    when nothing matches (a gone project matches nothing). Raises on a non-system persona (a caller bug).
    """
    if persona not in SYSTEM_PERSONAS:
        raise ValueError(f"persona {persona!r} is not a system persona — must be one of {list(SYSTEM_PERSONAS)}")
    settled_at = func.coalesce(Task.github_synced_at, Task.updated_at)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    row = session.execute(
        select(Task.id, settled_at.label("settled_at"), Task.outcome_summary, Task.updated_by_id)
        .where(
            Task.project_id == project_id,
            Task.persona == persona,
            Task.dedup_key == dedup_key,
            Task.hidden.is_(True),
            Task.state.in_(_SYSTEM_TASK_TERMINAL_STATES),
            settled_at >= cutoff,
        )
        .order_by(settled_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "age_seconds": (datetime.now(timezone.utc) - row.settled_at).total_seconds(),
        "cleared_by_sweep": (row.outcome_summary or "").startswith(cleared_prefix),
        "closed_by_human": (
            row.updated_by_id is not None
            and str(row.updated_by_id) != str(entity_shape.SYSTEM_ACTOR_ID)
        ),
    }


class ResolveSystemTaskIn(BaseModel):
    # The close line stored on the resolved Task, bounded 1..280; defaulted so an idempotent re-POST can send
    # `{}`.
    outcome_summary: str = Field(default="resolved", min_length=1, max_length=280)


class ResolveSystemTaskResult(BaseModel):
    # True iff THIS call moved a `_RESOLVABLE_SYSTEM_TASK_STATES` system Task → done; False when already done
    # (idempotent no-op).
    resolved: bool


@router.post("/tasks/system/{task_id}/resolve", response_model=ResolveSystemTaskResult)
@infra(Tenancy.ORG_SCOPED)
def resolve_system_task(
    task_id: int,
    body: ResolveSystemTaskIn,
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> ResolveSystemTaskResult:
    """Resolve a hidden outcome-criterion system Task (`_RESOLVABLE_SYSTEM_TASK_STATES` → done) on a
    producer's "condition cleared" signal. No PR, no runner: it settles directly, like the settle route's
    outcome-criterion branch (`_settle_outcome_criterion_task`).

    Guards:
      * 404 — no such Task.  422 — not a system persona.
      * 409 — a `merged_pr`-criterion persona (`compliance_fix`/`janitor`): they settle via `/settle`.
      * 409 — a terminal-but-not-done state (`failed`/`triaged`).
      * 409 — a `triage` Task with an `open` TaskQuestion.
      * already `done` → idempotent no-op (`resolved=False`, 200).

    Stamps `outcome_summary`, resets `dispatch_attempts`, stamps `github_synced_at`, emits `done`, and
    rolls up any parent. An atomic guarded UPDATE, so a race is a clean `resolved=False`.
    """
    task = get_task_or_404(task_id, session)
    if task.persona not in SYSTEM_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=f"task {task_id} persona {task.persona!r} is not a system persona",
        )
    if merge_lane_state.is_lane_persona(task.persona):
        # A lane run settles on its own `/settle`; a producer closing it would strand its `testing` rows.
        raise HTTPException(
            status_code=422,
            detail=f"task {task_id} is a merge-lane run; it settles itself and is never resolved by a caller",
        )
    if PERSONA_DONE_CRITERION.get(task.persona) != DONE_CRITERION_OUTCOME:
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} persona {task.persona!r} is not an outcome-criterion system "
                   "persona — it settles on its runner's own /complete call declaring a merged_pr or "
                   "no_pr terminal, not /resolve",
        )
    if task.state == DONE:
        return ResolveSystemTaskResult(resolved=False)   # idempotent — already resolved
    if task.state not in _RESOLVABLE_SYSTEM_TASK_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} state {task.state!r} is not resolvable — must be "
                f"{'|'.join(_RESOLVABLE_SYSTEM_TASK_STATES)}"
            ),
        )
    # The settle route's answered-trail gate, on every settle door: a `triage` row with an `open` question
    # never settles. Resolved (incl. `dismissed`), not answered. A round a Reset soft-deleted is not
    # outstanding (`app/task_artifacts.py`), or the re-run could never settle.
    if task.persona == PERSONA_TRIAGE:
        open_question = session.scalars(
            select(TaskQuestion.id).where(
                TaskQuestion.task_id == task_id,
                TaskQuestion.status == "open",
                visible_artifact_clause(
                    TaskQuestion.created_at, task.artifacts_hidden_before),
            )
        ).first()
        if open_question is not None:
            raise HTTPException(
                status_code=409,
                detail="triage task has open question(s) — resolve the Q&A trail before settling",
            )
    now = datetime.now(timezone.utc)
    # Read under a row lock before the UPDATE (`_terminates_a_running_runner`): the resolvable states include
    # `draft`/`ready`, so the UPDATE guard proves nothing about a runner, and `_RUNNER_BINDING_CLEARED` erases
    # the evidence. Callers are CP producers (`cron_alarms.resolve_alarm`, `node_liveness_job`,
    # `lease_drain`); ending an operator-Started session is `complete_task`'s situation and owes the same row.
    resolve_terminated_runner = _terminates_a_running_runner(task_id, session)
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state.in_(_RESOLVABLE_SYSTEM_TASK_STATES))
        .values(
            state=DONE,
            **_RUNNER_BINDING_CLEARED,
            dispatch_attempts=0,
            github_synced_at=now,
            outcome_summary=body.outcome_summary,
            **task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if row is None:
        return ResolveSystemTaskResult(resolved=False)   # lost the race — another actor settled it
    # Above the `done` transition, for `end_attempt`'s ordering reason.
    if resolve_terminated_runner:
        runner_terminations.record(
            session, task_id, runner_terminations.CAUSE_CONDITION_CLEARED,
        )
    emit_console_event(session, entity="task", id=row[0], action=DONE)
    session.commit()
    log.info(
        "system task %s (persona=%s) resolved to done (%s; github_synced_at=%s)",
        task_id, task.persona, body.outcome_summary, now,
    )
    _maybe_roll_up_parent(row[0], session)
    return ResolveSystemTaskResult(resolved=True)


# ── Needs-attention read for the jaahd entry-notify sweep ───────────────────
# jaahd's `sweep_needs_attention_notify` reads which rows are currently actionable to fire the
# entry-notify once per entry and re-arm on exit. A light projection plus the row's age, so the sweep
# can leave just-landed rows to the transition notifier.

# The personas left out, derived from the criterion map so new live-human personas are covered.
# An interactive chat parks `awaiting_response` at every turn, with its human at the console; an
# entry-notify would ping per turn. The transition notifier suppresses the same rows
# (`app/task_notify.py`). Imported from `routes/tasks/`, not re-derived, so sweep and route agree.
_OPERATOR_CRITERION_PERSONAS = OPERATOR_CRITERION_PERSONAS


class NeedsAttentionRowOut(BaseModel):
    id: int
    # NOT Optional: `tasks.persona` is NOT NULL (migration 0113).
    persona: str
    state: str
    # The identity, and the NAME (joined) the notify names as the affected repo.
    project_id: int
    project: str
    title: str
    hidden: bool
    # Age on the database's clock, for the sweep's grace gate (`task_pipeline.NOTIFY_GRACE_SECONDS`): an age,
    # so the reader never compares its clock to ours.
    age_seconds: float


@router.get("/tasks/needs-attention", response_model=list[NeedsAttentionRowOut])
@infra(Tenancy.OPERATOR)
def agent_list_needs_attention(
    _: None = Depends(_check_token),
    session: Session = Depends(get_session),
) -> list[NeedsAttentionRowOut]:
    """Every Task in an actionable `blocked`/`failed`/`triaged` state — every persona except the interactive
    ones, including hidden rows. The data source for jaahd's entry-notify sweep; the console board shows
    the same set via "Show hidden". Newest first; the host watermark keys on id.

    The one exclusion (`_OPERATOR_CRITERION_PERSONAS`) changes only what is announced: the board still shows
    the `?` on a parked chat.
    """
    rows = session.execute(
        select(
            Task.id, Task.persona, Task.state, Project.name, Task.title, Task.hidden,
            # `updated_at` is the entry instant for a parked row; a later edit only delays this backstop.
            func.extract("epoch", func.now() - Task.updated_at).label("age_seconds"),
            Task.project_id,
        )
        .join(Project, Project.id == Task.project_id)
        # This route's own filter (`app/task_lifecycle.py`); a state missing here is never announced.
        .where(Task.state.in_(_NEEDS_ATTENTION_STATES))
        # Filtered in SQL to avoid shipping per-turn chat rows. Scoped to the park, not the persona, matching
        # `task_notify.py`: a chat's `system_failed` (runner died, nobody guaranteed watching) must still be
        # announced. The OR form is equivalent to `not_(and_(…))` since both columns are NOT NULL.
        .where(or_(
            Task.persona.notin_(_OPERATOR_CRITERION_PERSONAS),
            Task.state != AWAITING_RESPONSE,
        ))
        .order_by(Task.id.desc())
    ).all()
    return [
        NeedsAttentionRowOut(
            id=r[0], persona=r[1], state=r[2], project_id=r[7], project=r[3], title=r[4], hidden=r[5],
            age_seconds=float(r[6] or 0.0),
        )
        for r in rows
    ]
