"""A step's verbs on many items at once (`act`), and archive, restore and delete-drafts (`bulk`): what the Pipeline
page's buttons and tick-all send. Each answers `{done, refused: [{key, reason}], waiting}`: a refusal is per item and
never fails the rest; `waiting` counts items whose press was recorded while their step still waits (approvals).

`subjects`: `task_ids`, `shas`, or `all_in_stage` — every item of the step's source stage (of `stage` for `bulk`)
resolved here, server-side, through the same `Board` the list pages, so a tick-all reaches rows never loaded.

Per action (the step's, `app/releases/pipeline.py::action`):

- **start** (and **start, then merge** for a draft): `go` is the console's own Start per task
  (`routes/tasks/dispatch.py::start_one`, gate included); `approve` records an approval and starts the task once its
  gate opens; `cancel` withdraws the step's presses and approvals.
- **move**: `go` passes the item into the no-branch stage when its gate opens, else records the press; `approve`
  likewise; `cancel` withdraws.
- **merge**: `go` (the Merge press) and `approve` are recorded on the item's task, then every held lane row whose gate
  opened is released (`gate.release_held`). The lane's batching and gate are untouched.
- **release**: `go` is Deploy (`service.deploy`, or `hotfix` with `hotfix_sha`; `scheduled_for` schedules it);
  `approve`, `cancel` and `retry` act on the step's open (retry: last failed) release. Subjects are not read; a
  refusal is the call's status and body, as the release routes answer it (403 for a caller not in the step's who).

Approve and reject (R1–R3) are an approver's (`gate.may_approve`: the step's `approvers`, else who may press; refused
`not_approver`), never the item's requester's while the step's `requester_cant_approve` is on (`own_item`). Each takes
an optional `comment` (stored with the approval). `reject` on an item waiting for approvals withdraws its press and
approvals (as `cancel`), writes a `step_rejected` audit row and tells whoever asked (`notices.step_rejected`); an item
not waiting is refused `not_waiting`. A release step's `reject` is `service.reject`.

Archive (D5 "Hide, can bring back") stamps `archived_at` on any task not live, drafts included; restore clears it and
the task is back where `locate` puts it. Nothing is deleted by archive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import actors, user_activity
from app.authz.catalog import permission_key
from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import editable_task_clause, has_project_key, scoped_task_select
from app.clock import utcnow
from app.entity_shape import or_404
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import Release
from app.models.tasks import Task
from app.pipeline import board as board_mod, events, gate as gate_mod, membership, notices
from app.pipeline.rules import StepRules
from app.releases import lifecycle, override as override_mod, pipeline as setup, service, stages as stages_mod, \
    waiting as waiting_mod
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DRAFT, READY

GO, APPROVE, CANCEL, RETRY, REJECT = "go", "approve", "cancel", "retry", "reject"
VERBS = (GO, APPROVE, CANCEL, RETRY, REJECT)
ARCHIVE, RESTORE, DELETE_DRAFTS = "archive", "restore", "delete_drafts"
BULK_VERBS = (ARCHIVE, RESTORE, DELETE_DRAFTS)
#: The most subjects one call names explicitly (`all_in_stage` is bounded by the project instead).
SUBJECTS_MAX = 1000
_LIVE = frozenset({READY, ACTIVE, AWAITING_RESPONSE})


@dataclass
class Outcome:
    done: int = 0
    refused: list[dict[str, str]] = field(default_factory=list)
    waiting: int = 0

    def refuse(self, key: str, reason: str) -> None:
        self.refused.append({"key": key, "reason": reason})

    def out(self) -> dict[str, Any]:
        return {"done": self.done, "refused": self.refused, "waiting": self.waiting}


@dataclass
class Subjects:
    task_ids: list[int] = field(default_factory=list)
    shas: list[str] = field(default_factory=list)
    all_in_stage: bool = False
    stage: Optional[str] = None
    group: Optional[str] = None
    #: With `all_in_stage`: False leaves out what the list folds under Automatic (`board.folded`).
    folded: bool = True
    filters: Optional[board_mod.Filters] = None
    include_hidden: bool = False


def resolve(board: board_mod.Board, subjects: Subjects, stage_id: Optional[UUID],
            bucket: Optional[str] = None) -> list[membership.Item]:
    """The items named: every item of the stage (or bucket) for `all_in_stage`, else each named task and change that
    is an item of this board. A name that is no item comes back as an `Item` with no place (refused by the caller)."""
    if subjects.all_in_stage:
        if stage_id is None and bucket is None:
            raise HTTPException(status_code=422, detail="all_in_stage needs a stage")
        return board_mod.select_items(board, stage_id, bucket, subjects.group, with_folded=subjects.folded)
    if len(subjects.task_ids) + len(subjects.shas) > SUBJECTS_MAX:
        raise HTTPException(status_code=422, detail=f"at most {SUBJECTS_MAX} subjects")
    by_key = {i.key: i for i in board.items}
    keys = [membership.task_key(t) for t in subjects.task_ids] + [membership.change_key(s) for s in subjects.shas]
    return [by_key.get(k) or membership.Item(key=k) for k in dict.fromkeys(keys)]


def _detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("code") or detail.get("message") or exc.status_code)
    return str(detail or exc.status_code)


def _subject(item: membership.Item) -> gate_mod.Subject:
    return gate_mod.Subject(task_id=item.task_id) if item.task_id is not None else \
        gate_mod.Subject(sha=item.change.merge_sha if item.change is not None else None)


def _editable(session: Session, principal: Optional[Principal], task_ids: list[int]) -> set[int]:
    """The tasks among `task_ids` the caller may change (`tasks:edit` on their project)."""
    if not task_ids:
        return set()
    return set(session.scalars(scoped_task_select(principal).where(
        Task.id.in_(task_ids), editable_task_clause(principal)).with_only_columns(Task.id)))


# ── act ───────────────────────────────────────────────────────────────────────────────────────────────

def act(session: Session, project: Project, principal: Optional[Principal], user: AdminUser, to_stage_id: Any,
        verb: str, subjects: Subjects, *, scheduled_for: Optional[datetime] = None,
        hotfix_sha: Optional[str] = None, override_reason: Optional[str] = None,
        comment: Optional[str] = None, ticket: Optional[str] = None) -> dict[str, Any]:
    """`POST …/pipeline/steps/{to_stage_id}/act` (module docstring). `override_reason`: a `go` (or a release's
    `retry`) that runs through a freeze (R7.5, `app/releases/override.py`); `comment`: an approve's or reject's."""
    stages = gate_mod.stages_of(session, project)
    step = or_404(gate_mod.step_into(stages, to_stage_id), detail="no step into that stage")
    if verb not in VERBS:
        raise HTTPException(status_code=422, detail=f"verb must be one of {', '.join(VERBS)}")
    if step.action == setup.RELEASE:
        return _act_release(session, project, principal, stages, step, verb, scheduled_for, hotfix_sha,
                            override_reason, comment, ticket).out()
    override = _merge_override(session, project, principal, step, verb, override_reason)
    board = board_mod.Board.load(session, project, principal, include_hidden=subjects.include_hidden,
                                 filters=subjects.filters)
    items = resolve(board, subjects, step.source.id)
    outcome = Outcome()
    editable = _editable(session, principal, [i.task_id for i in items if i.task_id is not None])
    if verb == RETRY:
        for item in items:
            outcome.refuse(item.key, "not_supported")
        return outcome.out()
    for item in items:
        place = board.places.get(item.key)
        if place is None:
            outcome.refuse(item.key, "not_found")
            continue
        if place.bucket == membership.ARCHIVED:
            outcome.refuse(item.key, "archived")
            continue
        try:
            _act_item(session, project, principal, user, step, verb, item, place, outcome, editable, board.stages,
                      override, comment)
            session.commit()
        except HTTPException as exc:
            session.rollback()
            outcome.refuse(item.key, _detail(exc))
    if step.action in (setup.MERGE, setup.START_MERGE) and verb in (GO, APPROVE):
        gate_mod.release_held(session, project)
    events.touch(session, project.id)
    session.commit()
    return outcome.out()


def _merge_override(session: Session, project: Project, principal: Optional[Principal], step: gate_mod.Step,
                    verb: str, reason: Optional[str]) -> Optional[tuple[str, Any]]:
    """A Merge press's override (R7.5), checked once for the call: `(reason, freeze hit)`, or None when none is
    asked or no freeze holds the integration stage (nothing to override). Only a Merge `go` takes one."""
    if reason is None:
        return None
    if verb != GO or step.action not in (setup.MERGE, setup.START_MERGE):
        raise HTTPException(status_code=422, detail={"code": "override_not_supported", "field": "override_reason"})
    try:
        text = override_mod.reason_of(principal, project.id, reason,
                                      may_press=gate_mod.may_act(principal, project.id, step))
    except override_mod.OverrideRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.body) from None
    hit = service.freeze_on(session, project, step.target, pressed=True)
    return (text, hit) if text is not None and hit is not None else None


def _act_item(session: Session, project: Project, principal: Optional[Principal], user: AdminUser,
              step: gate_mod.Step, verb: str, item: membership.Item, place: membership.StagePlace,
              outcome: Outcome, editable: set[int], stages: list[Any],
              override: Optional[tuple[str, Any]] = None, comment: Optional[str] = None) -> None:
    from app.routes.tasks.dispatch import start_one  # noqa: PLC0415 — the start route imports the gate

    subject = _subject(item)
    starting = step.action == setup.START or (step.action == setup.START_MERGE and item.state == DRAFT)
    if verb in (APPROVE, REJECT):
        # R1: an approver's, whether or not they may press the step.
        if not gate_mod.may_approve(principal, project.id, step):
            outcome.refuse(item.key, "not_approver")
            return
        if StepRules.of(step.target).requester_cant_approve and user.id in gate_mod.askers(session, step, subject):
            # R2 (as a release's requester, `access.may_approve`): while it is on, nobody who asked for the item (its
            # creator, starter or a presser, whom the count leaves out) approves or rejects it.
            outcome.refuse(item.key, "own_item")
            return
    elif not gate_mod.may_act(principal, project.id, step):
        outcome.refuse(item.key, "step_forbidden")
        return
    if verb == CANCEL:
        gate_mod.consume(session, step, subject)
        outcome.done += 1
        return
    if verb == REJECT:
        _reject(session, project, user, step, subject, item, comment)
        outcome.done += 1
        return
    if starting:
        if item.task_id is None:
            outcome.refuse(item.key, "not_a_task")
            return
        # An approver needs no right to the task: it starts as whoever pressed it, never as them.
        forbidden = verb == GO and item.task_id not in editable
        if forbidden or place.index >= step.index:
            outcome.refuse(item.key, "forbidden" if forbidden else "already_there")
            return
        starter = None
        as_user, as_principal = user, principal
        if verb == APPROVE:
            gate_mod.record(session, project, step, subject, user.id, gate_mod.APPROVE, comment)
            session.commit()
            # Started as whoever pressed it, never as the approver (whose git account it would push with, and who
            # needs no right to the task): the start runs as that person, as the tick's step sweep does.
            starter = gate_mod.first_presser(session, step, subject) or (session.scalar(
                select(Task.created_by_id).where(Task.id == item.task_id)) if step.target.auto_deploy else None)
            starter_user = session.scalar(select(AdminUser).where(AdminUser.id == starter)) \
                if starter is not None else None
            if starter_user is None or starter_user.disabled_at is not None or starter_user.is_system:
                outcome.waiting += 1
                return
            as_user, as_principal = starter_user, principal_for_user(session, starter_user)
            # Only while that person may still press the step and edit the task (as the tick's step sweep asks):
            # otherwise it waits for someone who may press it.
            if not gate_mod.may_act(as_principal, project.id, step) \
                    or not _editable(session, as_principal, [item.task_id]):
                outcome.waiting += 1
                return
        started = start_one(session, item.task_id, None, as_user, as_principal, out=False, pressing=verb == GO,
                            starter_id=starter, pipeline_hint={"stages": stages, "here": place.index})
        if isinstance(started, gate_mod.StartDecision):
            outcome.waiting += 1
        else:
            outcome.done += 1
        return
    if step.action == setup.MOVE:
        # One step at a time: only an item in the stage right before it moves through it.
        if place.bucket is not None or place.index != step.index - 1:
            outcome.refuse(item.key, "already_there" if place.index >= step.index else "not_in_source")
            return
        if verb == APPROVE:
            gate_mod.record(session, project, step, subject, user.id, gate_mod.APPROVE, comment)
        state = gate_mod.gate(session, step, subject, presser_id=user.id if verb == GO else None)
        if state.open:
            gate_mod.pass_into(session, project, step.target, subject, user.id)
            gate_mod.consume(session, step, subject)
            outcome.done += 1
        else:
            if verb == GO:
                gate_mod.record(session, project, step, subject, user.id, gate_mod.PRESS)
            outcome.waiting += 1
        return
    # Merge: the press or approval lands on the task; `act` releases every held row whose gate opened.
    if item.task_id is None:
        outcome.refuse(item.key, "not_a_task")
        return
    stored = None
    if override is not None and verb == GO and principal is not None \
            and not gate_mod.overridden(session, step, subject, override[1]):
        stored = override_mod.record(session, project, step.target, principal, override[0], override[1],
                                     task_id=item.task_id)
    if verb == GO:
        gate_mod.record(session, project, step, subject, user.id, gate_mod.PRESS, freeze_override=stored)
    else:
        gate_mod.record(session, project, step, subject, user.id, gate_mod.APPROVE, comment=comment)
    outcome.done += 1


def _reject(session: Session, project: Project, user: AdminUser, step: gate_mod.Step, subject: gate_mod.Subject,
            item: membership.Item, comment: Optional[str]) -> None:
    """R3.5 on a task step: the item's press and approvals are withdrawn (it stays where it is, waiting for a new
    press, or on a step by itself for new approvals), audited, and whoever asked is told. Only an item waiting for
    approvals with a press or an approval to withdraw."""
    if not gate_mod.has_pending(session, step, subject) or \
            not isinstance(gate_mod.gate(session, step, subject), gate_mod.WaitApprovals):
        raise HTTPException(status_code=409, detail={"code": "not_waiting"})
    asked = gate_mod.askers(session, step, subject)
    gate_mod.consume(session, step, subject)
    notices.step_ended(session, project, step, subject, asked, notices.STEP_REJECTED, by=user, comment=comment)
    target = step.target
    user_activity.record(
        session, user, "write", "step_rejected",
        f"Reject {_item_label(session, item)} · {target.name}"[:200],
        data={"project_id": project.id, "stage_id": str(target.id), "task_id": subject.task_id,
              "sha": subject.sha, "comment": comment or None})


def _item_label(session: Session, item: membership.Item) -> str:
    if item.task_id is not None:
        title = session.scalar(select(Task.title).where(Task.id == item.task_id)) or ""
        return f"task #{item.task_id} {title}".strip()
    return f"change {(item.change.merge_sha if item.change is not None else '')[:10]}"


def _act_release(session: Session, project: Project, principal: Optional[Principal], stages: list[Any],
                 step: gate_mod.Step, verb: str, scheduled_for: Optional[datetime],
                 hotfix_sha: Optional[str], override_reason: Optional[str] = None,
                 comment: Optional[str] = None, ticket: Optional[str] = None) -> Outcome:
    outcome = Outcome()
    target = step.target
    branch = stages_mod.branch_stages(stages)
    source = next((s for s in reversed(branch) if stages.index(s) < step.index), None)
    try:
        if verb == GO and hotfix_sha:
            change = next(iter(waiting_mod.merged_changes(session, project, shas=[hotfix_sha])), None)
            if change is None:
                raise service.Refused(404, {"code": "unknown_change"})
            service.hotfix(session, project, principal, change.change_id, override_reason=override_reason)
        elif verb == GO:
            if source is None:
                raise service.Refused(422, {"code": "no_source"})
            service.deploy(session, project, principal, source.id, scheduled_for, override_reason=override_reason,
                           ticket=ticket)
        else:
            release = _release_for(session, project, target.id, verb)
            if release is None:
                raise service.Refused(409, {"code": "no_release"})
            if verb == RETRY:
                service.retry(session, project, principal, release.id, override_reason=override_reason)
            elif override_reason is not None:
                raise service.Refused(422, {"code": "override_not_supported", "field": "override_reason"})
            elif verb == APPROVE:
                service.approve(session, project, principal, release.id, comment)
            elif verb == REJECT:
                service.reject(session, project, principal, release.id, comment)
            else:
                service.cancel(session, project, principal, release.id)
        events.touch(session, project.id)
        session.commit()
        outcome.done = 1
    except service.Refused as exc:
        # One subject, the release: its refusal is the call's, as the release routes answer it.
        session.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.body or None) from None
    return outcome


def _release_for(session: Session, project: Project, target_id: UUID, verb: str) -> Optional[Release]:
    if verb == RETRY:
        last = session.scalar(select(Release).where(Release.project_id == project.id, Release.to_stage_id == target_id)
                              .order_by(Release.id.desc()).limit(1))
        return last if last is not None and last.state == lifecycle.FAILED else None
    # Several may wait into one stage (R6): the step's verbs act on the newest, the one the board shows.
    opens = service.open_releases_into(session, project.id, target_id)
    return opens[-1] if opens else None


# ── resume ────────────────────────────────────────────────────────────────────────────────────────────

def resume(session: Session, project: Project, principal: Optional[Principal], user: AdminUser,
           to_stage_id: Any) -> None:
    """`POST …/pipeline/steps/{to_stage_id}/resume` (R8.3): a person who may press the step lets it run by itself
    again (`release_stages.resumed_at`, one audit row `step_resumed`). A step not paused is left as it is."""
    from app import user_activity  # noqa: PLC0415
    from app.models.releases import ReleaseStage  # noqa: PLC0415
    from app.releases import judge, tick  # noqa: PLC0415 — the tick imports this package

    stages = gate_mod.stages_of(session, project)
    step = gate_mod.step_into(stages, to_stage_id)
    if step is None or step.action != setup.RELEASE:
        raise HTTPException(status_code=404, detail="no release step into that stage")
    if not gate_mod.may_act(principal, project.id, step):
        raise HTTPException(status_code=403, detail=None)
    pause = judge.paused(session, project, stages, step.target)
    if pause is None:
        return
    stage = or_404(session.scalar(select(ReleaseStage).where(ReleaseStage.id == step.target.id).with_for_update()),
                   detail="no release step into that stage")
    stage.resumed_at = utcnow()
    user_activity.record(
        session, user, "write", "step_resumed", f"Resume by itself · Release to {stage.name} · {project.name}",
        path=f"/api/projects/{project.id}/pipeline/steps/{stage.id}/resume", method="POST",
        data={"project_id": project.id, "stage_id": str(stage.id), "release_id": pause.cause.id})
    events.touch(session, project.id)
    tick.wake(session)
    session.commit()


# ── bulk ──────────────────────────────────────────────────────────────────────────────────────────────

def bulk(session: Session, project: Project, principal: Optional[Principal], user: AdminUser, verb: str,
         subjects: Subjects) -> dict[str, Any]:
    """`POST …/pipeline/bulk`: archive, restore or delete drafts (module docstring)."""
    if verb not in BULK_VERBS:
        raise HTTPException(status_code=422, detail=f"verb must be one of {', '.join(BULK_VERBS)}")
    board = board_mod.Board.load(session, project, principal, include_hidden=subjects.include_hidden,
                                 filters=subjects.filters)
    stage_id, bucket = board_mod.stage_param(board, subjects.stage)
    items = resolve(board, subjects, stage_id, bucket)
    outcome = Outcome()
    editable = _editable(session, principal,
                         [i.task_id for i in items if i.task_id is not None and i.key in board.places])
    may_delete = has_project_key(principal, permission_key("tasks", "delete"), project.id)
    archive_ids: list[int] = []
    for item in items:
        if item.key not in board.places:
            outcome.refuse(item.key, "not_found")
        elif item.task_id is None:
            outcome.refuse(item.key, "not_a_task")
        elif item.task_id not in editable:
            outcome.refuse(item.key, "forbidden")
        elif verb == ARCHIVE:
            if item.archived:
                outcome.done += 1
            elif item.state in _LIVE:
                outcome.refuse(item.key, "live")
            else:
                archive_ids.append(item.task_id)
        elif verb == RESTORE:
            if item.archived:
                archive_ids.append(item.task_id)
            else:
                outcome.done += 1
        elif not may_delete:
            outcome.refuse(item.key, "forbidden")
        elif item.state != DRAFT:
            outcome.refuse(item.key, "not_a_draft")
        else:
            archive_ids.append(item.task_id)
    if verb == DELETE_DRAFTS:
        from app.routes.tasks.delete import delete_task_core  # noqa: PLC0415

        for task in session.scalars(select(Task).where(Task.id.in_(archive_ids), Task.state == DRAFT)):
            delete_task_core(task, session, actor=user)
            outcome.done += 1
    elif archive_ids:
        stamp = utcnow() if verb == ARCHIVE else None
        guard = Task.archived_at.is_(None) if verb == ARCHIVE else Task.archived_at.is_not(None)
        live_guard = (Task.state.not_in(sorted(_LIVE)),) if verb == ARCHIVE else ()
        result = session.execute(
            update(Task).where(Task.id.in_(archive_ids), guard, *live_guard)
            .values(archived_at=stamp, **actors.task_actor_values(session))
            .execution_options(synchronize_session=False))
        outcome.done += int(result.rowcount or 0)
    events.touch(session, project.id)
    session.commit()
    return outcome.out()
