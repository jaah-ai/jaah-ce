"""Agent-filed top-level Tasks: filing, dedup, and editing or deleting an agent's own drafts."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import node_targets
from app.entity_shape import constraint_name, get_or_404
from app.events import emit_console_event
from app.models.projects import Project
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task
from app.persona_registry import SHAPE_TOP_LEVEL, TASK_CREATION_LICENCES
from app.short_title import authored_title, sanitize_title
from app.task_ancestry import executor_user_id
from app.task_artifacts import uploaded_clause, visible_artifact_clause
from app.task_lifecycle import ACTIVE, DRAFT, READY, _LIVE_STATES, _SYSTEM_TASK_DEDUP_EXCLUDED_STATES
from app.task_manifest import DOCUMENT_KINDS
from app import task_source
from app.task_title import _rederive_children
from app.user_activity import record_task_note
from app.routes.tasks.policy import (
    PERSONA_EPIC,
    PERSONA_PROTOTYPER,
    TOP_LEVEL_DEPTHS,
    TOP_LEVEL_DEPTH_SKETCH,
    TOP_LEVEL_DEPTH_TASK,
    TOP_LEVEL_FILED_PERSONAS,
    TOP_LEVEL_TASK_PERSONAS,
    project_default_persona,
)
from app.routes.tasks.create import _create_analysis_children, _snapshot_body_version, new_top_level_task
from app.routes.tasks.dispatch import _start, _start_epic
from app.routes.tasks.delete import delete_task_core

log = logging.getLogger(__name__)


# ── SHAPE_TOP_LEVEL — one standalone task in the caller's own project ────────────────────────────────
# Migration 0144's partial unique index over `(project_id, persona, dedup_key)` for un-settled keyed
# rows. Named so a race handler can tell its own violation from other integrity errors; used by
# `create_system_task` (routes/agent/task_reads.py) and `file_agent_top_level_task`.
_UNIQUE_DEDUP_INDEX = "uq_tasks_open_dedup_key"


def _live_agent_top_level_candidate(
    session: Session, *, project_id: int, dedup_key: str, filed_by: Optional[int] = None
) -> Optional[Task]:
    """The LIVE top-level filed row a re-file of `dedup_key` collapses onto, or None.

    Across every persona this door files (`TOP_LEVEL_FILED_PERSONAS`), so a resumed session that picks
    another depth still cannot double-file; the answer names the matched row's own persona. The index is
    per persona, so only the same-persona race is caught at flush. One exact predicate on the stored `dedup_key` column, the same index `create_system_task` probes.
    The state half is exactly `_SYSTEM_TASK_DEDUP_EXCLUDED_STATES`, which `uq_tasks_open_dedup_key`'s
    predicate mirrors (pinned by `test_dedup_key_column.py`): a narrower set would collapse a re-ask onto
    a `done`/`paused`/`awaiting_response` row nobody will work, silently losing it.

    `archived_at` stays asymmetric (the index ignores it), which is why the caller keeps a collision
    branch and its 409. Project-wide, as filing is — except `filed_by`, which narrows it to that task's own filings:
    a Nodes-project caller filing into another project must not learn that project's rows by guessing a key."""
    query = select(Task).where(
        Task.project_id == project_id,
        Task.parent_task_id.is_(None),
        Task.persona.in_(TOP_LEVEL_FILED_PERSONAS),
        Task.dedup_key == dedup_key,
        Task.archived_at.is_(None),
        Task.state.not_in(_SYSTEM_TASK_DEDUP_EXCLUDED_STATES),
    )
    if filed_by is not None:
        query = query.where(Task.created_by_task_id == filed_by)
    return session.scalars(query.order_by(Task.id)).first()


def _follow_up_project_id(session: Session, caller: Task) -> Optional[int]:
    """Where a caller in a system (Nodes) project files instead of its own project — the org's oldest own project
    (`node_targets.follow_up_project_id`) — or None for a caller in an ordinary project."""
    project = session.execute(
        select(Project.system_kind, Project.org_id).where(Project.id == caller.project_id)
    ).one_or_none()
    if project is None or project.system_kind is None:
        return None
    return node_targets.follow_up_project_id(session, project.org_id)


def _dedup_key_held_409(dedup_key: str, persona: str) -> HTTPException:
    """The one answer every "the key is taken and this filing will not collapse onto it" path gives.

    The detail lists every way here and names none as the cause: (1) an archived un-settled row; (2) a
    row of that persona with a parent (the index has no `parent_task_id IS NULL` arm). A 409 rather than
    a 500 on the race path: a real conflict, resolvable with the key the caller already knows."""
    return HTTPException(
        status_code=409,
        detail=(
            f"dedup_key {dedup_key!r} is already held by a {persona} task in this project that this "
            f"filing will not re-file onto — an archived row or a row with a parent (a child of an "
            f"epic). Choose a distinct key."
        ),
    )


def _record_top_level_collapse(
    session: Session, caller: Task, matched_id: int, persona: str, dedup_key: str,
    dropped_attachment_ids: Sequence[int] = (),
) -> None:
    """Record, on the CALLER, that a filing collapsed onto `matched_id` instead of creating a row.

    Without it `created=False` looks like the ask was never made; the attachments the collapse did NOT
    attach are named too (`task_update` adds them to a draft). A note plus a notify, neither a
    transition (`task_note:` stays out of the dwell stream; `record_transition=False`). Commits, since
    the collapse branch reaches no other commit."""
    record_task_note(
        session,
        caller.id,
        "agent_filing_deduped",
        f"re-filed dedup_key {dedup_key!r}; collapsed onto existing {persona} task {matched_id}",
        extra={
            "filed_task_id": matched_id, "dedup_key": dedup_key, "created": False,
            "dropped_attachment_ids": list(dropped_attachment_ids),
        },
    )
    emit_console_event(
        session, entity="task", id=caller.id, action="agent_filing_deduped",
        record_transition=False,
    )
    session.commit()


def file_agent_top_level_task(
    caller: Task, proposal: dict[str, Any], session: Session
) -> dict[str, Any]:
    """File ONE standalone top-level Task in the CALLER'S OWN project — a Nodes-project caller's in its org's oldest own
    project, always a draft (`node_targets.follow_up_project_id`) — (`SHAPE_TOP_LEVEL`, or
    `SHAPE_DRAFT_TOP_LEVEL`, which never starts), left `draft` unless the proposal asked for it to be STARTED. Returns `{id, created, project_id, state, persona, title}`.

    The project comes only from the caller's row: a caller-named project would be authority a runner
    grants itself. The persona comes from the proposal's `depth` (`_top_level_persona_or_422`): a sketch
    (`prototyper`), a task (`coder`, or a named licence-free persona), or an epic, built with its analysis
    children through the operator path's own recipe. None of them holds a dispatching licence except the epic's
    `plan` child, whose chain terminates (a `coder` holds only `INERT_SHAPES`), keeping the work graph a finite
    fan (`SHAPE_PRODUCES_PERSONAS`).
    `guided` stores `autonomous = false`; omitted, the row is unattended.

    Idempotent on a required `dedup_key`: a re-file writes no row and records the collapse on the caller.
    Atomicity comes from `uq_tasks_open_dedup_key` plus the probe (no epic to lock).

    `draft` by default via `new_top_level_task`; `ready` only on `start` (owner decision): a chat files
    proposals, and a started row spends a pool slot. The would-not-start 500 applies only to the start
    branch.

    Refuses: a non-live filer (404) and a key held by a row this shape will not collapse onto (409,
    `_dedup_key_held_409`)."""
    dedup_key = proposal["dedup_key"]
    project_id = caller.project_id
    named_persona = _top_level_persona_or_422(proposal)
    # ── Caller liveness — see `_require_live_filer` ──────
    _require_live_filer(caller)
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=422, detail=f"project {project_id} not found")
    # A node task's own runner files nothing into the system project (`app/node_targets.py`): its follow-up goes to
    # the org's own project instead, always as a draft, so an operator decides where and whether it runs.
    # That project is wider than the admin-only Nodes project, so the node's artifacts do not travel with it.
    redirected = project.system_kind is not None
    if redirected:
        project_id = _follow_up_project_id(session, caller)
        if project_id is None:
            raise HTTPException(status_code=422, detail=node_targets.NO_FOLLOW_UP_PROJECT)
        if _proposed_artifact_ids(proposal):
            raise HTTPException(status_code=422, detail=node_targets.NO_FOLLOW_UP_ATTACHMENTS)
        project = session.get(Project, project_id)
    # An unnamed `task` takes the landing project's default (`project_default_persona`).
    persona = named_persona or project_default_persona(
        project.default_persona if project is not None else None, TOP_LEVEL_TASK_PERSONAS,
    )
    # Resolved before any write, so a reference that is not the caller's own files nothing (404).
    attachment_sources = _caller_artifacts_or_404(caller, _proposed_artifact_ids(proposal), session)
    existing = _live_agent_top_level_candidate(session, project_id=project_id, dedup_key=dedup_key,
                                               filed_by=caller.id if redirected else None)
    if existing is not None:
        # The collapse is recorded on the caller's feed, where a human reads what their chat did; nothing is
        # written to the matched row, which may be leased. `existing.state` is read before the note commits
        # (which expires attributes).
        existing_state, existing_persona, existing_title = existing.state, existing.persona, existing.title
        _record_top_level_collapse(
            session, caller, existing.id, existing_persona, dedup_key,
            [row.id for row in attachment_sources],
        )
        # The matched row's own state and persona: a collapse writes nothing to it, so the answer describes it.
        return {
            "id": existing.id, "created": False, "project_id": project_id, "state": existing_state,
            "persona": existing_persona, "title": existing_title,
        }

    # Stored whole, only sanitized (`authored_title`), and the answer carries it; the route then enqueues the
    # 5-6 word refinement from it (`app/task_title.py`), the fallback being this title cut to the cap.
    title = authored_title(proposal["title"])
    if title != proposal["title"]:
        # A sanitized, bounded slice, never the raw value: an untrusted-ingesting persona controls it, and a raw
        # string could forge log entries (CRLF) or flood the file. `sanitize_title` collapses all whitespace.
        log.warning(
            "file_agent_top_level_task: sanitized title for task %s (%d chars submitted): %r -> %r",
            caller.id, len(proposal["title"]), sanitize_title(proposal["title"])[:120], title[:120],
        )
    executor_id = executor_user_id(session, caller.id)
    row = new_top_level_task(
        project_id=project_id,
        source=task_source.AGENT,
        title=title,
        # Verbatim: the operator and the next runner both read it.
        body=proposal["body"],
        persona=persona,
        # `.get`: absent means unattended, today's behaviour and the console's own default.
        autonomous=not bool(proposal.get("guided")),
        # The caller's executor (starter, else creator) owns it: filed work must not out-reach the human who
        # ran the chat (`task_ancestry.launcher_user_id` reads it for placement grants).
        created_by_id=executor_id,
        # And the caller's own ADDRESSEE, one hop, as every pipeline child inherits its epic's.
        assigned_to_id=caller.assigned_to_id,
    )
    # The dedup authority. No `<!-- sysdedup:KEY -->` body marker: that is the system path's shim for an old
    # body parser, never read on a visible `coder` row.
    row.dedup_key = dedup_key
    # The filer, on the row, where authorization can read it: the caller's `edit_filed`/`delete_filed`
    # shapes reach exactly these drafts. See `Task.created_by_task_id`.
    row.created_by_task_id = caller.id
    # And that executor started it, so it pushes with their credential without a human re-Start (an operator decision).
    # Stamped at filing, not at `_start`, so a draft carries it too and the start below preserves it.
    row.started_by_id = executor_id
    if persona == PERSONA_EPIC:
        # The epic's create-time default (`_create_epic_with_children`); `new_top_level_task`'s False is argued
        # for a childless row.
        row.is_plannable = True
    session.add(row)
    try:
        session.flush()   # assign row.id for the event and the note below
    except IntegrityError as exc:
        # Narrowed to this one constraint: treating any other `IntegrityError` as a lost race would swallow it.
        if _UNIQUE_DEDUP_INDEX not in (constraint_name(exc) or ""):
            raise
        # The whole transaction, not a savepoint: everything before was a read, and `session.add` ran outside
        # any savepoint (see `create_system_task`).
        session.rollback()
        winner = _live_agent_top_level_candidate(session, project_id=project_id, dedup_key=dedup_key,
                                                 filed_by=caller.id if redirected else None)
        if winner is not None:
            # A concurrent filing won between probe and flush; degrade to the answer the probe would have given,
            # note included.
            log.info(
                "file_agent_top_level_task: dedup race on %r in project %s — returning task %s",
                dedup_key, project_id, winner.id,
            )
            # Before the note commits — see the probe branch above.
            winner_state, winner_persona, winner_title = winner.state, winner.persona, winner.title
            _record_top_level_collapse(
                session, caller, winner.id, winner_persona, dedup_key,
                [row.id for row in attachment_sources],
            )
            return {
                "id": winner.id, "created": False, "project_id": project_id, "state": winner_state,
                "persona": winner_persona, "title": winner_title,
            }
        # The key is held by a row this shape will not dedup against (see `_dedup_key_held_409`).
        raise _dedup_key_held_409(dedup_key, persona) from exc

    if persona == PERSONA_EPIC:
        # The operator path's own recipe, in this transaction, so a failure leaves no partial epic. The children
        # carry the epic's owner (`executor_id`, above); they push as the epic's starter through the walk, like
        # every pipeline child.
        _create_analysis_children(row, session)

    # On the epic itself for an epic: its children read the parent's rows (`_task_attachments_meta`).
    attached_ids = _attach_caller_artifacts(caller, row, attachment_sources, session)
    # Captured before `_start` commits (expiring attributes) or a rollback discards the insert.
    row_id = row.id
    # No object grant: a Task is not grantable; the owner reaches it via `tasks:view` on its project
    # (`app/authz/visibility.py::visible_task_clause`). The row's birth event, as `create_task` emits, so its transition
    # trail does not open at `ready`.
    emit_console_event(session, entity="task", id=row_id, action="created")
    # On the caller: the advisor chat's own feed is where "and then it filed this" belongs.
    record_task_note(
        session,
        caller.id,
        "agent_filed_task",
        f"filed as {persona} task {row_id}",
        extra={
            "filed_task_id": row_id, "dedup_key": dedup_key, "persona": persona,
            "attachment_ids": attached_ids,
        },
    )
    # `.get`, defaulting to no start: an unasked start must never spend a slot, and older callers file drafts.
    # A filer licensed only for drafts (`SHAPE_DRAFT_TOP_LEVEL`) never starts: its `start` is ignored.
    if not bool(proposal.get("start")) or redirected or not _may_start_filing(caller):
        # Nothing else commits on this path (`_start` would have), so commit the insert, event and note here.
        session.commit()
        return {
            "id": row_id, "created": True, "project_id": project_id, "state": DRAFT, "persona": persona,
            "title": title,
        }
    if persona == PERSONA_EPIC:
        # An epic starts through the operator's own epic door (`draft -> active`; its children dispatch off the
        # pipeline), with no principal: the row already carries its executor's `started_by_id`, which the
        # absent-key start preserves, so its children push as that human. It commits.
        _start_epic(row_id, project, session, None)
        return {
            "id": row_id, "created": True, "project_id": project_id, "state": ACTIVE, "persona": persona,
            "title": title,
        }
    # No `started_by`, so `_start` leaves the `started_by_id` stamped at filing: the chat's executor, whose
    # GitHub credential `git_access.resolve_for_task` lends it (an operator decision, reversing the old
    # refusal; the operator chose an executor's own chat filing work as that executor over a re-Start).
    if not _start(row_id, session, sources=(DRAFT,), run_requested=False):
        # Unreachable for a fresh `draft`, asserted: a requested start that did not land would report success
        # and run nothing. `_start` commits only on success, so everything rolls back.
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"task {row_id} was created but would not start",
        )
    return {"id": row_id, "created": True, "project_id": project_id, "state": READY, "persona": persona,
            "title": title}


def _may_start_filing(caller: Task) -> bool:
    """Whether the caller's licence lets its filing start (`SHAPE_TOP_LEVEL`), not only draft it."""
    licence = TASK_CREATION_LICENCES.get(caller.persona)
    return licence is not None and SHAPE_TOP_LEVEL in licence.shapes


def _proposed_artifact_ids(proposal: dict[str, Any]) -> list[int]:
    """The `artifact_id`s a filing or amend proposal names (`AgentFiledAttachmentIn`); absent is none."""
    return [int(entry["artifact_id"]) for entry in proposal.get("attachments") or ()]


def _caller_artifacts_or_404(
    caller: Task, artifact_ids: Sequence[int], session: Session
) -> list[TaskAttachment]:
    """The CALLER'S OWN confirmed, visible document artifacts behind a filing's `attachments`, in the order
    named and de-duplicated — or a 404 naming the first that is not one.

    One 404 for missing, pending, Reset-hidden, a non-document kind and another task's row alike, so a
    filer cannot probe ids (the credential rule's posture). Resolved before anything is written, so a bad
    reference files nothing."""
    rows: list[TaskAttachment] = []
    seen: set[int] = set()
    for artifact_id in artifact_ids:
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        # Locked to the commit: a concurrent delete of the source must not reap the object between this
        # read and the copy's INSERT (`reap_unreferenced_object` checks for surviving rows).
        rows.append(get_or_404(
            session, TaskAttachment, artifact_id, lock=True,
            detail=(
                f"artifact {artifact_id} is not one of your task's own artifacts — attach only what "
                "your task uploaded with artifact_upload"
            ),
            where=(
                TaskAttachment.task_id == caller.id,
                TaskAttachment.kind.in_(DOCUMENT_KINDS),
                uploaded_clause(),
                visible_artifact_clause(TaskAttachment.created_at, caller.artifacts_hidden_before),
            ),
        ))
    return rows


def _attach_caller_artifacts(
    caller: Task, target: Task, sources: Sequence[TaskAttachment], session: Session
) -> list[int]:
    """Attach `sources` (resolved by `_caller_artifacts_or_404`) to `target` as human-input `attachment`
    rows, sharing each source's `s3_key` — metadata only, no bytes copied (`_publish_epic_final_report`'s
    precedent; objects die only behind `app/attachment_storage.py::reap_unreferenced_object`). Does not
    commit; returns the new row ids.

    `source_task_id` names the filer, which the console shows as the row's origin. A source already on
    `target` (same key) is skipped, so a re-sent amend attaches nothing twice. No quota is asked, as for the
    epic report copy: no byte is stored, and the source's upload already passed it."""
    held = set(session.scalars(
        select(TaskAttachment.s3_key).where(
            TaskAttachment.task_id == target.id, TaskAttachment.kind == ATTACHMENT_KIND_FILE,
        )
    ).all())
    fresh = [row for row in sources if row.s3_key not in held]
    if not fresh:
        return []
    copies = [
        TaskAttachment(
            task_id=target.id,
            kind=ATTACHMENT_KIND_FILE,
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            s3_key=row.s3_key,
            s3_bucket=row.s3_bucket,
            uploaded_at=row.uploaded_at,
            etag=row.etag,
            uploaded_by_id=row.uploaded_by_id,
            source_task_id=caller.id,
        )
        for row in fresh
    ]
    session.add_all(copies)
    session.flush()
    return [copy.id for copy in copies]


def _top_level_persona_or_422(proposal: dict[str, Any]) -> Optional[str]:
    """The persona a `SHAPE_TOP_LEVEL` proposal files, off its `depth` (default `task`) and, at `task` alone,
    its optional `persona` (None when unnamed: the project's default decides). A 422 names the legal values; see
    `TOP_LEVEL_TASK_PERSONAS`."""
    depth = proposal.get("depth") or TOP_LEVEL_DEPTH_TASK
    named = proposal.get("persona")
    if depth not in TOP_LEVEL_DEPTHS:
        raise HTTPException(
            status_code=422,
            detail=f"depth must be one of {', '.join(TOP_LEVEL_DEPTHS)} (got {depth!r})",
        )
    if depth != TOP_LEVEL_DEPTH_TASK:
        if named is not None:
            raise HTTPException(
                status_code=422,
                detail=f"persona is chosen only at depth 'task'; depth {depth!r} fixes its own",
            )
        return PERSONA_PROTOTYPER if depth == TOP_LEVEL_DEPTH_SKETCH else PERSONA_EPIC
    if named is None:
        return None
    if named not in TOP_LEVEL_TASK_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"persona at depth 'task' must be one of {', '.join(TOP_LEVEL_TASK_PERSONAS)} "
                f"(got {named!r})"
            ),
        )
    return named


def _require_live_filer(caller: Task) -> None:
    """Refuse (404) a caller that is not a LIVE filer — archived, or outside `_LIVE_STATES`.

    Otherwise a settled or archived advisor id would still authorize filing live work. `_LIVE_STATES`
    (`ready`/`active`/`awaiting_response`), not the epic doors' `(ACTIVE, DONE)`: a plan/finish child
    files while settling, but an advisor is only `done` when the operator ends the conversation, and
    `draft` has nothing running. Shared by the file, amend and delete doors."""
    if caller.archived_at is not None or caller.state not in _LIVE_STATES:
        raise HTTPException(
            status_code=404,
            detail=(
                f"task {caller.id} is not a live filer, so it may not file work — a filing has to come "
                "from a session an operator is actually in"
            ),
        )


def _own_filed_draft_or_refuse(caller: Task, filed_task_id: int, session: Session) -> Task:
    """The target of an amend or a delete: a row the CALLER'S OWN TASK filed, still `draft`. Else refused.

    Order is the disclosure posture: first one 404 for missing, archived, or not filed by this caller,
    so an advisor cannot probe task ids; then a 409 naming the state for its own row that left `draft`.

    Ownership is `created_by_task_id` (not the operator's `created_by_id`, nor `dedup_key`). Also
    checked: `project_id` equality (a moved draft keeps its filer, and must not drag the amend into a
    project the filing door refuses) and the persona, which must be one this door files
    (`TOP_LEVEL_FILED_PERSONAS` — the operator may convert a filed draft to an `advisor`). Deleting an
    epic draft takes its draft children with it (`delete_task_core`'s cascade).

    Locked and re-read (`FOR UPDATE`, `populate_existing`): the check-then-write races the operator's
    `_start`, and a stale `draft` could amend a started row or delete one while skipping
    `delete_task_core`'s kill/release, leaking the slot."""
    target = session.execute(
        select(Task).where(Task.id == filed_task_id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        target is None
        or target.archived_at is not None
        or target.created_by_task_id != caller.id
        or target.project_id not in (caller.project_id, _follow_up_project_id(session, caller))
        or target.persona not in TOP_LEVEL_FILED_PERSONAS
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                f"task {filed_task_id} is not a draft your task filed — this door reaches only rows "
                "your own task_create call filed, and nothing else, even with the operator's permission. To "
                "remove other drafts, point the operator at the console's Tasks board: filter State: Draft and "
                "the project, press Select all, then Delete"
            ),
        )
    if target.state != DRAFT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {filed_task_id} is '{target.state}', not a draft — a filed task that has started "
                "belongs to the operator now; tell them in the console rather than amending or deleting it"
            ),
        )
    return target


def edit_agent_filed_draft(
    caller: Task, filed_task_id: int, proposal: dict[str, Any], session: Session
) -> dict[str, Any]:
    """REWRITE the title and body of a `draft` the CALLER'S OWN TASK filed (`SHAPE_EDIT_FILED`). Returns
    `{id, project_id, state, title}` — `title` is the STORED one, sanitized (`authored_title`).

    A collapse writes nothing, so without this a correction would need a new filing per fix. Exactly
    `title` and `body`, plus optional `attachments` ADDED from the caller's own artifacts
    (`_attach_caller_artifacts`): not state, persona (`SHAPE_PRODUCES_PERSONAS` maps this shape to
    nothing), `dedup_key`, or scope.

    The title is sanitized like the filing's, the body stored verbatim; a changed body is snapshotted
    first (`_snapshot_body_version`, source `agent_filed_edit`). The note goes on the caller."""
    _require_live_filer(caller)
    target = _own_filed_draft_or_refuse(caller, filed_task_id, session)
    attachment_sources = _caller_artifacts_or_404(caller, _proposed_artifact_ids(proposal), session)
    title = authored_title(proposal["title"])
    if title != proposal["title"]:
        # A sanitized, bounded slice, never the raw value (see `file_agent_top_level_task`).
        log.warning(
            "edit_agent_filed_draft: sanitized title for task %s (%d chars submitted): %r -> %r",
            filed_task_id, len(proposal["title"]), sanitize_title(proposal["title"])[:120], title[:120],
        )
    changed: list[str] = []
    if title != target.title:
        old_title = target.title
        target.title = title
        changed.append("title")
        if target.persona == PERSONA_EPIC:
            # The children wear `[<persona>] <epic title>` (`derived_child_title`); re-stamp them in step.
            _rederive_children(session, target, old_title, title)
    if proposal["body"] != target.body:
        _snapshot_body_version(target, "agent_filed_edit", session)
        target.body = proposal["body"]
        changed.append("body")
    # Added, never replaced: an omitted list leaves the draft's attachments as they are.
    attached_ids = _attach_caller_artifacts(caller, target, attachment_sources, session)
    if changed or attached_ids:
        emit_console_event(session, entity="task", id=target.id, action="updated")
        summary = " and ".join(
            ([f"rewrote the {' and '.join(changed)}"] if changed else [])
            + ([f"added {len(attached_ids)} attachment(s)"] if attached_ids else [])
        )
        record_task_note(
            session,
            caller.id,
            "agent_edited_task",
            f"{summary} of filed task {target.id}",
            extra={
                "filed_task_id": target.id,
                "changed": changed + (["attachments"] if attached_ids else []),
                "attachment_ids": attached_ids,
            },
        )
    # Committed even when nothing changed: an idempotent re-send succeeds and the answer reads live values.
    result = {
        "id": target.id, "project_id": target.project_id, "state": target.state, "title": target.title,
    }
    session.commit()
    if "title" in changed:
        # A rewritten title is refined like the filing's (`app/task_title.py`); an unchanged re-send is not.
        from app.durable.task_title_queue import enqueue_title

        enqueue_title(result["id"], result["title"])
    return result


def delete_agent_filed_draft(caller: Task, filed_task_id: int, session: Session) -> dict[str, Any]:
    """DELETE a `draft` the CALLER'S OWN TASK filed (`SHAPE_DELETE_FILED`). Returns `{id, deleted}`.

    The amend's guards, then `delete_task_core`, the one Task deleter (its kill/release half is a no-op
    for a draft). The note is written before the core commits, so delete and record land together; it
    names the `dedup_key`, which the delete frees."""
    _require_live_filer(caller)
    target = _own_filed_draft_or_refuse(caller, filed_task_id, session)
    record_task_note(
        session,
        caller.id,
        "agent_deleted_task",
        f"deleted filed draft task {target.id}",
        extra={"filed_task_id": target.id, "dedup_key": target.dedup_key},
    )
    delete_task_core(target, session)
    return {"id": filed_task_id, "deleted": True}


# ── The system-task body marker ────────────────────────────────────────────────────────────────────
# A convention about what a Task BODY carries, not about any one route, so it lives here. Two modules
# stamp and match it, and a format string copied into one can silently stop matching the other.
def sysdedup_marker(dedup_key: str) -> str:
    """The hidden HTML-comment marker stamped in a system Task's body to carry its `dedup_key`. Invisible
    in rendered markdown; the finer per-source dedup discriminator matches on it, so distinct sources keep
    separate open rows instead of collapsing on the coarse (persona, project) key."""
    return f"<!-- sysdedup:{dedup_key} -->"


def body_with_dedup_marker(body_text: str, dedup_key: Optional[str]) -> str:
    """Append the `dedup_key` marker to a system-task body, idempotently; unchanged when no key is given."""
    if not dedup_key:
        return body_text
    marker = sysdedup_marker(dedup_key)
    if marker in body_text:
        return body_text
    return f"{body_text}\n{marker}"
