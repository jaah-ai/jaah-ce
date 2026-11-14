"""Whether a step lets an item through: `gate`, the ONE rule for every step (architecture §2.5), and the seams that
enforce it — Start (`start_decision`), Merge (`held_for`, `release_held`), Move (the `act` route) and the release
sign-off (`signoff_missing`, D16 "Deploy waits for all").

`gate(step, subject)`:

1. the step is by hand (`auto` false) and nobody allowed has pressed its verb → `WaitPress`;
2. fewer approvals than `approvals_required` → `WaitApprovals`. Every stored approval counts, even when the approver
   set changed since (D-12: it was authorised when given); while `requester_cant_approve` is on (R2, the default) a
   presser's or requester's never does;
3. the item has been in the step's source stage for less than its soak (R4) → `WaitTime('soak', until)`;
4. now is outside the step's allowed hours (R5) → `WaitTime('hours', next)`;
5. otherwise `Open`.

Soak and hours wait, never refuse: a press during either is recorded, and the release tick's step sweep opens the
step when they lapse (`app/releases/tick.py::steps`). Approvals are taken outside allowed hours (R5.3).

A release step's press and approvals are the release's own (`app/releases/service.py`, on the release's settings
snapshot); its gate here is only the sign-off: a release out of a no-branch stage waits until every waiting change
has passed into that stage (`WaitSignOff`).

Presses and approvals of per-item steps are `pipeline_approvals` rows (by task, or by merged sha); a step that opens
for a task on Start or Move consumes them. Passes into no-branch stages are `pipeline_passes` rows. Neither is
written for a stage that is not stored yet (Ideas and Building of a project the runtime upgrade has not reached,
`app/releases/stages.py`): such a stage has today's settings, whose gate is always open.

Who may act on a step: `may_act` — the step's `who` (`release_stages.deployers`), or with none set the key its
default holds (`app/releases/pipeline.py::default_key`). Who may approve or reject it: `may_approve` — its
`approvers` when set, else the same (R1, `access.may_approve_step`). The agent principal (`None`: an automation, a schedule, an
agent's own filing) acts as the control plane always has, allow-all, so a machine start is gated by the step's
settings (by hand, approvals) and never by `who`.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Union
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app import edition_features
from app.actors import actor_names
from app.authz.principal import Principal
from app.clock import utcnow
from app.models.merge_lane import MergeLaneRow
from app.models.projects import Project
from app.models.releases import PipelineApproval, PipelinePass, ReleaseStage
from app.models.outcomes import TaskOutcome
from app.models.tasks import Task
from app.merge_lane_state import OPEN_STATES as LANE_OPEN
from app.pipeline import events, membership, reasons as reasons_mod
from app.pipeline.reasons import Reason
from app.pipeline.rules import StepRules
from app.releases import access, hours as hours_mod, pipeline as setup, stages as stages_mod, waiting as waiting_mod

PRESS, APPROVE = "press", "approve"


@dataclass(frozen=True)
class Open:
    kind: str = "open"
    open: bool = True


@dataclass(frozen=True)
class WaitPress:
    kind: str = "press"
    open: bool = False


@dataclass(frozen=True)
class WaitApprovals:
    have: int
    need: int
    kind: str = "approvals"
    open: bool = False


@dataclass(frozen=True)
class WaitSignOff:
    count: int
    kind: str = "sign_off"
    open: bool = False


@dataclass(frozen=True)
class WaitTime:
    """A timed wait: `soak` until the item has soaked in the source stage, `hours` until the next allowed hour."""

    kind: str
    until: datetime
    open: bool = False


GateState = Union[Open, WaitPress, WaitApprovals, WaitSignOff, WaitTime]
OPEN = Open()


def gate_out(state: GateState) -> dict[str, Any]:
    """The wire shape of a closed gate: `{kind, have, need}` or `{kind, count}`."""
    if isinstance(state, WaitApprovals):
        return {"kind": state.kind, "have": state.have, "need": state.need}
    if isinstance(state, WaitSignOff):
        return {"kind": state.kind, "count": state.count}
    if isinstance(state, WaitTime):
        return {"kind": state.kind, "until": state.until.isoformat()}
    return {"kind": state.kind}


@dataclass(frozen=True)
class Step:
    """The step into `stages[index]` (its settings are that stage's columns) and what it does."""

    index: int
    source: ReleaseStage
    target: ReleaseStage
    action: str


@dataclass(frozen=True)
class Subject:
    """What a per-item step lets through: a task (its top-level row) or a merged change."""

    task_id: Optional[int] = None
    sha: Optional[str] = None


def stages_of(session: Session, project: Project) -> list[ReleaseStage]:
    """The pipeline a step is judged on: `setup.stages_for`, with the Merge step as it runs today while the runtime
    upgrade has not reached the project (by itself, no approvals, default who — as `setup.read` shows it), since the
    stored integration row still carries its old, never-acted-on release settings."""
    stages = setup.stages_for(session, project)
    if not setup.upgrade_pending(project, stages):
        return stages
    integration = stages_mod.integration_stage(stages)
    return [ReleaseStage(
        id=s.id, org_id=s.org_id, project_id=s.project_id, position=s.position, name=s.name, branch=s.branch,
        level=s.level, deploy_environment=s.deploy_environment, url=s.url, url_source=s.url_source,
        auto_deploy=True, approvals_required=0, deployers=None,
    ) if integration is not None and s.id == integration.id else s for s in stages]


def steps_of(stages: Sequence[ReleaseStage]) -> list[Step]:
    return [Step(i, stages[i - 1], stages[i], setup.action(stages[:i], stages[i])) for i in range(1, len(stages))]


def step_into(stages: Sequence[ReleaseStage], stage_id: Any) -> Optional[Step]:
    return next((s for s in steps_of(stages) if str(s.target.id) == str(stage_id)), None)


def may_act(principal: Optional[Principal], project_id: int, step: Step) -> bool:
    """May `principal` press the step's verb? The agent principal (`None`) acts as the control plane: allow-all."""
    if principal is None:
        return True
    return access.may_deploy(principal, project_id, step.target, default_key=setup.default_key(step.action))


def may_approve(principal: Optional[Principal], project_id: int, step: Step) -> bool:
    """May `principal` approve (or reject) the step (R1)? Its `approvers`, else who may press it. The agent
    principal (`None`) approves nothing: an approval is a person's."""
    if principal is None:
        return False
    return access.may_approve_step(principal, project_id, step.target, default_key=setup.default_key(step.action))


def _subject_clause(subject: Subject) -> tuple[Any, ...]:
    if subject.task_id is not None:
        return (PipelineApproval.task_id == subject.task_id,)
    return (PipelineApproval.task_id.is_(None), PipelineApproval.merge_sha == subject.sha)


@dataclass(frozen=True)
class _Facts:
    """What the step's evaluators judge: whether it was pressed (or is being pressed), and approvals had/needed."""

    pressed: bool
    have: int
    need: int


def _facts(session: Session, step: Step, subject: Subject, presser_id: Optional[UUID], pressing: bool) -> _Facts:
    rows = list(session.execute(
        select(PipelineApproval.kind, PipelineApproval.user_id)
        .where(PipelineApproval.stage_id == step.target.id, *_subject_clause(subject))
    ))
    pressers = {user for kind, user in rows if kind == PRESS}
    if presser_id is not None:
        pressers.add(presser_id)
    rules = StepRules.of(step.target)
    need = rules.approvals_required
    have = 0
    if need:
        approvers = {user for kind, user in rows if kind == APPROVE}
        if rules.requester_cant_approve:
            approvers -= pressers | requesters(session, subject)
        have = len(approvers)
    return _Facts(bool(pressers) or pressing, have, need)


def _waits_press(step: Step, facts: _Facts) -> bool:
    return not step.target.auto_deploy and not facts.pressed


def _waits_approvals(facts: _Facts) -> bool:
    return facts.have < facts.need


def started_at(session: Session, task_id: int) -> Optional[datetime]:
    """When `task_id` started: its first dispatch, or its children's for an epic (a task carries no start stamp of
    its own; `task_outcomes.dispatched_at` records each dispatch)."""
    return session.scalar(select(func.min(TaskOutcome.dispatched_at)).join(Task, Task.id == TaskOutcome.task_id)
                          .where(or_(Task.id == task_id, Task.parent_task_id == task_id)))


def arrival(session: Session, step: Step, subject: Subject) -> Optional[datetime]:
    """When `subject` arrived in the step's source stage, for soak (R4.2): the task's start for the work stage, the
    change's merge for the integration stage, else its pass into the source (`pipeline_passes`), else, for a task,
    when it was created (an idea is in the first stage from its creation)."""
    created = session.scalar(select(Task.created_at).where(Task.id == subject.task_id)) \
        if subject.task_id is not None else None
    if step.source.level == stages_mod.WORK and subject.task_id is not None:
        return started_at(session, subject.task_id) or created
    if stages_mod.is_integration(step.source):
        # Its merge: the change's, or a task's newest one (an epic's children merge one by one).
        merged = select(func.max(TaskOutcome.merged_at))
        return session.scalar(merged.where(TaskOutcome.merge_sha == subject.sha) if subject.sha is not None else
                              merged.join(Task, Task.id == TaskOutcome.task_id).where(
                                  or_(Task.id == subject.task_id, Task.parent_task_id == subject.task_id))) \
            or created
    clause = (PipelinePass.task_id == subject.task_id if subject.task_id is not None
              else PipelinePass.merge_sha == subject.sha)
    passed = session.scalar(select(func.max(PipelinePass.created_at)).where(
        PipelinePass.stage_id == step.source.id, clause))
    return passed or created


def soak_until(session: Session, step: Step, subject: Subject, now: datetime) -> Optional[datetime]:
    """When the step's soak (R4) lets `subject` through, while it still holds it; else None."""
    minutes = StepRules.of(step.target).soak_minutes
    if not minutes:
        return None
    arrived = arrival(session, step, subject)
    if arrived is None:
        return None
    until = arrived + timedelta(minutes=minutes)
    return until if until > now else None


def hours_next(step: Step, now: datetime) -> Optional[datetime]:
    """The next allowed hour (R5) while the step is outside its allowed hours; else None."""
    return hours_mod.next_allowed(StepRules.of(step.target).hours, now)


def gate(session: Session, step: Step, subject: Subject, *, presser_id: Optional[UUID] = None,
         pressing: bool = False, now: Optional[datetime] = None) -> GateState:
    """The step's gate for `subject` (module docstring): the first blocking reason `reasons` lists, through the same
    evaluators. `presser_id`: a press being made now by an allowed user; `pressing`: a press being made now with no
    person behind it (a machine start)."""
    facts = _facts(session, step, subject, presser_id, pressing)
    if _waits_press(step, facts):
        return WaitPress()
    if _waits_approvals(facts):
        return WaitApprovals(facts.have, facts.need)
    now = now or utcnow()
    until = soak_until(session, step, subject, now)
    if until is not None:
        return WaitTime("soak", until)
    upcoming = hours_next(step, now)
    if upcoming is not None:
        return WaitTime("hours", upcoming)
    return OPEN


def _named(session: Session, who: dict[str, Any]) -> dict[str, Any]:
    """`who` as a reason names it: `{roles, users: [{id, name}]}`."""
    users = [UUID(str(u)) for u in who.get("users") or []]
    names = actor_names(session, set(users)) if users else {}
    return {"roles": [str(r) for r in who.get("roles") or []],
            "users": [{"id": str(u), "name": names.get(u, "")} for u in users]}


def press_who(stage: ReleaseStage, action: str) -> dict[str, Any]:
    return stage.deployers if stage.deployers is not None else setup.default_who(action)


def who_of(session: Session, step: Step) -> dict[str, Any]:
    """Who may press the step, as a reason names them: `{roles, users: [{id, name}]}`."""
    return _named(session, press_who(step.target, step.action))


def press_reason(session: Session, step: Step) -> Reason:
    return reasons_mod.of("press", **who_of(session, step))


def approvers_of(stage: ReleaseStage, action: str) -> dict[str, Any]:
    """Who may approve the step into `stage` (R1): its `approvers`, else who may press it."""
    approvers = StepRules.of(stage).approvers
    return approvers if approvers is not None else press_who(stage, action)


def nobody_approves(session: Session, project: Project, stage: ReleaseStage, action: str) -> bool:
    """Whether no current member of the project's org may approve the step (R1.4: a listed role with no members is
    still allowed, and the step then says "No one can approve"). The default approvers (a built-in key) always have
    the org's Owner, so only a set list is looked at."""
    if StepRules.of(stage).approvers is None and stage.deployers is None:
        return False
    from app.authz.principal import principal_for_user  # noqa: PLC0415 — as `app/releases/notify.py`

    from app.models.identity import AdminUser  # noqa: PLC0415

    named = [UUID(u) for u in approvers_of(stage, action).get("users") or []]
    if named and session.scalar(select(AdminUser.id).where(
            AdminUser.id.in_(named), AdminUser.org_id == project.org_id, AdminUser.disabled_at.is_(None)).limit(1)):
        return False  # a named member approves: no need to resolve everyone
    key = setup.default_key(action)
    return not any(access.may_approve_step(principal_for_user(session, m), project.id, stage, default_key=key)
                   for m in approver_candidates(session, project, stage, action))


def approver_candidates(session: Session, project: Project, stage: ReleaseStage, action: str) -> list[Any]:
    """The members of the project's org who could be the step's approvers: enabled people, an operator only when
    named (an operator's bypass makes no one an approver, nor reminds them)."""
    from app.models.identity import AdminUser  # noqa: PLC0415

    named = [UUID(u) for u in approvers_of(stage, action).get("users") or []]
    return list(session.scalars(select(AdminUser).where(
        AdminUser.org_id == project.org_id, AdminUser.disabled_at.is_(None), AdminUser.is_system.is_(False),
        or_(AdminUser.is_superadmin.is_(False), AdminUser.id.in_(named))).order_by(AdminUser.id)))


@dataclass(frozen=True)
class Approval:
    """One approval as a reason lists it: who, their optional comment, when."""

    user_id: UUID
    comment: Optional[str]
    at: Optional[datetime]


def approvals_reason(session: Session, project: Project, stage: ReleaseStage, action: str, have: int, need: int,
                     approved: Sequence[Approval] = (), viewer: Optional[str] = None) -> Reason:
    """`approvals`: the counts, who may approve (`approvers_of`), who approved and what they said, the viewer's own
    standing (`viewer_state`) and whether nobody can approve (`nobody_approves`)."""
    names = actor_names(session, {a.user_id for a in approved}) if approved else {}
    return reasons_mod.of(
        "approvals", have=have, need=need, **_named(session, approvers_of(stage, action)),
        approved=[{"name": names.get(a.user_id, ""), "comment": a.comment,
                   "at": a.at.isoformat() if a.at is not None else None} for a in approved],
        viewer=viewer, nobody=nobody_approves(session, project, stage, action))


def viewer_state(principal: Optional[Principal], project_id: int, stage: ReleaseStage, action: str, *,
                 approved: bool, requester: bool) -> Optional[str]:
    """The viewer's standing on a step waiting for approvals: `approved` (already did), `not_approver`, `requester`
    (asked for it, and R2 keeps them out), else `counts`; None with no viewer."""
    if principal is None:
        return None
    if approved:
        return "approved"
    if not access.may_approve_step(principal, project_id, stage, default_key=setup.default_key(action)):
        return "not_approver"
    if requester and StepRules.of(stage).requester_cant_approve:
        return "requester"
    return "counts"


def instructions_reason(stage: ReleaseStage) -> Optional[Reason]:
    """The step's instructions for approvers (R3.4), shown beside a wait for approvals."""
    text = StepRules.of(stage).instructions
    return reasons_mod.of("instructions", text=text) if text else None


def timeout_at(stage: ReleaseStage, since: Optional[datetime]) -> Optional[datetime]:
    """When a wait for approvals that began at `since` times out (R3.3: never reset by a partial approval)."""
    minutes = StepRules.of(stage).approval_timeout_minutes
    if minutes is None or since is None:
        return None
    return since + timedelta(minutes=minutes)


def timeout_reason(stage: ReleaseStage, since: Optional[datetime], now: Optional[datetime] = None) -> Optional[Reason]:
    """When the wait times out; under `remind`, past the first timeout, when the next reminder goes."""
    at = timeout_at(stage, since)
    if at is None or since is None:
        return None
    rules = StepRules.of(stage)
    now = now or utcnow()
    if rules.on_timeout == "remind" and at <= now and rules.approval_timeout_minutes:
        period = timedelta(minutes=rules.approval_timeout_minutes)
        at = since + period * ((now - since) // period + 1)
    return reasons_mod.of("timeout", at=at.isoformat(), action=rules.on_timeout)


def approvals_of(session: Session, step: Step, subject: Subject) -> list[Approval]:
    """The approvals stored for `subject` on the step, oldest first."""
    return [Approval(user, comment, at) for user, comment, at in session.execute(
        select(PipelineApproval.user_id, PipelineApproval.comment, PipelineApproval.created_at)
        .where(PipelineApproval.stage_id == step.target.id, PipelineApproval.kind == APPROVE,
               *_subject_clause(subject)).order_by(PipelineApproval.created_at))]


def pressed_at(session: Session, step: Step, subject: Subject) -> Optional[datetime]:
    """When `subject` was first pressed on the step: when it began waiting for approvals (R3.3)."""
    return session.scalar(select(PipelineApproval.created_at).where(
        PipelineApproval.stage_id == step.target.id, PipelineApproval.kind == PRESS, *_subject_clause(subject))
        .order_by(PipelineApproval.created_at).limit(1))


def subject_reasons(session: Session, project: Project, step: Step, subject: Subject,
                    viewer: Optional[Principal] = None) -> list[Reason]:
    """The approval reasons of a pressed `subject` waiting for approvals: `approvals` (with the viewer's standing),
    the step's `instructions` and its `timeout`."""
    facts = _facts(session, step, subject, None, False)
    if not _waits_approvals(facts):
        return []
    approved = approvals_of(session, step, subject)
    user_id = getattr(viewer, "user_id", None)
    asked = askers(session, step, subject)
    state = viewer_state(viewer, project.id, step.target, step.action,
                         approved=any(str(a.user_id) == str(user_id) for a in approved),
                         requester=any(str(u) == str(user_id) for u in asked))
    return reasons_mod.ordered([
        approvals_reason(session, project, step.target, step.action, facts.have, facts.need, approved, state),
        instructions_reason(step.target), timeout_reason(step.target, pressed_at(session, step, subject))])


def reasons(session: Session, project: Project, step: Step, subject: Subject, now: Optional[datetime] = None, *,
            viewer: Optional[Principal] = None, presser_id: Optional[UUID] = None,
            pressing: bool = False) -> list[Reason]:
    """Every reason the step holds `subject` for, in display order (`app/pipeline/reasons.py`). `gate` is the first
    blocking one; `now` and `viewer` are for the timed and per-viewer evaluators."""
    facts = _facts(session, step, subject, presser_id, pressing)
    now = now or utcnow()
    return reasons_mod.ordered([
        press_reason(session, step) if _waits_press(step, facts) else None,
        *(subject_reasons(session, project, step, subject, viewer) if _waits_approvals(facts) else ()),
        soak_reason(session, step, subject, now),
        hours_reason(step, now),
    ])


def soak_reason(session: Session, step: Step, subject: Subject, now: datetime) -> Optional[Reason]:
    until = soak_until(session, step, subject, now)
    return reasons_mod.of("soak", until=until.isoformat(), source=step.source.name) if until is not None else None


def hours_reason(step: Step, now: datetime) -> Optional[Reason]:
    from app.releases import judge  # noqa: PLC0415 — the judge's service imports this module

    return judge.hours_reason(StepRules.of(step.target), now)


def requesters(session: Session, subject: Subject) -> set[UUID]:
    """Who asks for the item, so never counts as its approver (as a release's requester, `access.may_approve`): the
    task's creator and starter, or the merged change's author."""
    if subject.task_id is not None:
        row = session.execute(select(Task.created_by_id, Task.started_by_id).where(Task.id == subject.task_id)).first()
        return {u for u in (row or ()) if u is not None}
    found = session.execute(select(Task.created_by_id, Task.started_by_id).join(
        TaskOutcome, TaskOutcome.task_id == Task.id).where(TaskOutcome.merge_sha == subject.sha).limit(1)).first()
    return {u for u in (found or ()) if u is not None}


def askers(session: Session, step: Step, subject: Subject) -> set[UUID]:
    """Who asked for `subject` on the step: its requesters and whoever pressed it (told when the wait ends)."""
    return requesters(session, subject) | set(session.scalars(select(PipelineApproval.user_id).where(
        PipelineApproval.stage_id == step.target.id, PipelineApproval.kind == PRESS, *_subject_clause(subject))))


def askers_of(session: Session, step: Step, subjects: Sequence[Subject]) -> dict[Subject, set[UUID]]:
    """`askers` for many subjects of one step in two queries (the board reads it for every waiting item)."""
    out: dict[Subject, set[UUID]] = {s: set() for s in subjects}
    if not subjects:
        return out
    by_task = {s.task_id: s for s in subjects if s.task_id is not None}
    by_sha = {s.sha: s for s in subjects if s.task_id is None and s.sha is not None}
    for task_id, sha, user_id in session.execute(
            select(PipelineApproval.task_id, PipelineApproval.merge_sha, PipelineApproval.user_id).where(
                PipelineApproval.stage_id == step.target.id, PipelineApproval.kind == PRESS)):
        subject = by_task.get(task_id) if task_id is not None else by_sha.get(sha)
        if subject is not None:
            out[subject].add(user_id)
    if by_task:
        for task_id, created, started in session.execute(select(Task.id, Task.created_by_id, Task.started_by_id)
                                                         .where(Task.id.in_(list(by_task)))):
            out[by_task[task_id]] |= {u for u in (created, started) if u is not None}
    for subject in by_sha.values():
        out[subject] |= requesters(session, subject)
    return out


def has_pending(session: Session, step: Step, subject: Subject) -> bool:
    """Whether `subject` holds a press or an approval on the step (something a reject withdraws)."""
    return session.scalar(select(PipelineApproval.id).where(
        PipelineApproval.stage_id == step.target.id, *_subject_clause(subject)).limit(1)) is not None


def _stored(session: Session, stage: ReleaseStage) -> bool:
    return session.scalar(select(ReleaseStage.id).where(ReleaseStage.id == stage.id)) is not None


def record(session: Session, project: Project, step: Step, subject: Subject, user_id: Optional[UUID],
           kind: str, comment: Optional[str] = None, *, freeze_override: Optional[dict[str, Any]] = None) -> bool:
    """Record `user_id`'s press or approval (with its optional `comment`) of the step for `subject` (once); False when
    nothing was written. `freeze_override`: a press that runs through a freeze (`app/releases/override.py`), set on
    an earlier press of the same person too (replacing an override of another freeze occasion)."""
    if user_id is None or not _stored(session, step.target):
        return False
    exists = session.scalar(select(PipelineApproval).where(
        PipelineApproval.stage_id == step.target.id, PipelineApproval.user_id == user_id,
        PipelineApproval.kind == kind, *_subject_clause(subject)))
    if exists is not None:
        if freeze_override is not None:
            exists.freeze_override = freeze_override
            session.flush()
        return False
    session.add(PipelineApproval(
        org_id=project.org_id, project_id=project.id, stage_id=step.target.id, task_id=subject.task_id,
        merge_sha=subject.sha if subject.task_id is None else None, kind=kind, user_id=user_id,
        comment=comment or None,
        # Set only when present: a JSONB None would be stored as JSON null, which reads as an override.
        **({"freeze_override": freeze_override} if freeze_override is not None else {})))
    session.flush()
    return True


def consume(session: Session, step: Step, subject: Subject) -> None:
    """The step opened for `subject`: its presses and approvals are spent."""
    session.execute(delete(PipelineApproval).where(
        PipelineApproval.stage_id == step.target.id, *_subject_clause(subject)))
    events.touch(session, step.target.project_id)


def pass_into(session: Session, project: Project, stage: ReleaseStage, subject: Subject,
              user_id: Optional[UUID]) -> None:
    """Record that `subject` moved into the no-branch `stage` (once)."""
    if not _stored(session, stage):
        return
    clause = (PipelinePass.task_id == subject.task_id if subject.task_id is not None
              else PipelinePass.merge_sha == subject.sha)
    if session.scalar(select(PipelinePass.id).where(PipelinePass.stage_id == stage.id, clause)) is not None:
        return
    session.add(PipelinePass(org_id=project.org_id, project_id=project.id, stage_id=stage.id,
                             task_id=subject.task_id, merge_sha=subject.sha if subject.task_id is None else None,
                             created_by_id=user_id))
    session.flush()


# ── Start ─────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StartDecision:
    """`start_decision`'s answer. `open`: start now (the passes are recorded, uncommitted). Else the item waits on
    `step` for `state`, or the caller may not press it (`forbidden`)."""

    open: bool
    step: Optional[Step] = None
    state: Optional[GateState] = None
    forbidden: bool = False

    def waiting_body(self) -> dict[str, Any]:
        assert self.step is not None and self.state is not None
        return {"state": "waiting", "stage_id": str(self.step.target.id), "gate": gate_out(self.state)}


def start_target(stages: Sequence[ReleaseStage]) -> Optional[int]:
    """The index of the stage a Start lands a task in: the work stage, else the integration stage (start, then
    merge); None for a pipeline with neither."""
    layout = membership.Layout.of(stages)
    return layout.work if layout.work is not None else layout.integration


def gated(task: Task) -> bool:
    """Whether a Start of `task` is gated: a top-level task never started. Epic children belong to their epic's
    start; a restart belongs to the task's own earlier one."""
    return task.parent_task_id is None and task.state == membership.DRAFT


def _place(session: Session, project: Project, stages: Sequence[ReleaseStage], task: Task) -> int:
    item = membership.Item(key=membership.task_key(task.id), task_id=task.id, state=task.state)
    facts = membership.Facts.load(session, project, stages, changes=[])
    return membership.locate(project, stages, [item], facts)[item.key].index


def start_decision(session: Session, project: Project, task: Task, principal: Optional[Principal],
                   presser_id: Optional[UUID], *, pressing: bool = True,
                   stages: Optional[Sequence[ReleaseStage]] = None, here: Optional[int] = None) -> StartDecision:
    """Walk the steps from where `task` is to its start stage: each no-branch step must be open by itself (a pass
    is recorded); the start step needs the caller's press (`pressing`) and its approvals. A closed start step
    records the press; nothing commits here."""
    if not gated(task):
        return StartDecision(True)
    stages = stages_of(session, project) if stages is None else stages
    target = start_target(stages)
    if target is None or target == 0:
        return StartDecision(True)
    here = _place(session, project, stages, task) if here is None else here
    if here >= target:
        return StartDecision(True)
    steps = steps_of(stages)
    subject = Subject(task_id=task.id)
    passes: list[Step] = []
    for index in range(here + 1, target + 1):
        step = steps[index - 1]
        if index < target:
            state = gate(session, step, subject)
            if not state.open:
                if pressing and isinstance(state, WaitTime):
                    # It lapses by itself: the press on the start step is what the step sweep starts it from.
                    record(session, project, steps[target - 1], subject, presser_id, PRESS)
                return StartDecision(False, step, state)
            passes.append(step)
            continue
        if pressing and not may_act(principal, project.id, step):
            return StartDecision(False, step, None, forbidden=True)
        state = gate(session, step, subject, presser_id=presser_id if pressing else None, pressing=pressing)
        if not state.open:
            if pressing:
                record(session, project, step, subject, presser_id, PRESS)
            return StartDecision(False, step, state)
    for step in passes:
        pass_into(session, project, step.target, subject, presser_id)
        consume(session, step, subject)
    consume(session, steps[target - 1], subject)
    events.touch(session, project.id)
    return StartDecision(True)


def first_presser(session: Session, step: Step, subject: Subject) -> Optional[UUID]:
    """Who pressed the step first for `subject`: the starter a gate that opens later starts the task as."""
    return session.scalar(select(PipelineApproval.user_id).where(
        PipelineApproval.stage_id == step.target.id, PipelineApproval.kind == PRESS, *_subject_clause(subject))
        .order_by(PipelineApproval.created_at).limit(1))


def starts_on_create(session: Session, project: Project) -> bool:
    """Whether the step out of the first stage runs by itself with no approvals, so a new idea starts on create."""
    stages = stages_of(session, project)
    steps = steps_of(stages)
    return bool(steps) and bool(steps[0].target.auto_deploy) and not steps[0].target.approvals_required \
        and start_target(stages) not in (None, 0)


# ── Merge ─────────────────────────────────────────────────────────────────────────────────────────────

def merge_step(stages: Sequence[ReleaseStage]) -> Optional[Step]:
    """The step into the integration stage when it merges (`merge`, or `start_merge` with no work stage)."""
    integration = stages_mod.integration_stage(stages)
    if integration is None:
        return None
    step = step_into(stages, integration.id)
    return step if step is not None and step.action in (setup.MERGE, setup.START_MERGE) else None


def overridden(session: Session, step: Step, subject: Subject, hit: Any = None) -> bool:
    """Whether a press of the step for `subject` runs through the freeze `hit` (R7.5): its stored override was given
    for that freeze occasion (`app/releases/override.py::covers`). Without `hit`: whether any press carries one."""
    from app.releases import override as override_mod  # noqa: PLC0415 — the release package imports this module

    stored = session.scalars(select(PipelineApproval.freeze_override).where(
        PipelineApproval.stage_id == step.target.id, PipelineApproval.kind == PRESS,
        PipelineApproval.freeze_override.is_not(None), *_subject_clause(subject)))
    return any(hit is None or override_mod.covers(o, hit) for o in stored)


def merge_freeze(session: Session, project: Project, step: Step, subject: Subject) -> Any:
    """The freeze holding a Merge of `subject` into the integration stage (a freeze scoped to it, R7.1), or None:
    `service.freeze_on` as a press faces it, passed by an override on the subject's press for that occasion."""
    from app.releases import service  # noqa: PLC0415 — the release service imports this module

    # A by-hand step, or a press made: a freeze letting presses through lets it pass; a Merge by itself nobody pressed
    # is nobody's press (R7.3).
    pressed = not step.target.auto_deploy or first_presser(session, step, subject) is not None
    hit = service.freeze_on(session, project, step.target, pressed=pressed)
    return None if hit is not None and overridden(session, step, subject, hit) else hit


def _merge_open(session: Session, project: Project, step: Step, subject: Subject,
                now: Optional[datetime] = None) -> bool:
    return gate(session, step, subject, now=now).open and merge_freeze(session, project, step, subject) is None


def held_for(session: Session, project: Project, task_id: int) -> bool:
    """Whether a PR of `task_id` into the integration branch waits on the Merge step (by hand and not pressed,
    approvals owed, or a freeze on the integration stage): the lane row is `held` and never batched."""
    step = merge_step(stages_of(session, project))
    if step is None:
        return False
    root = membership.roots_of(session, [task_id]).get(task_id, task_id)
    return not _merge_open(session, project, step, Subject(task_id=root))


def release_held(session: Session, project: Project, now: Optional[datetime] = None) -> list[MergeLaneRow]:
    """Clear `held` on every open lane row of the project whose Merge gate is open and no freeze holds now (a press,
    an approval, a pipeline save, a soak or allowed hours lapsed, a freeze that lifted: the release tick's sweep);
    returns the rows released. The lane run is filed if none is open (`merge_lane.ensure_lane`)."""
    rows = list(session.scalars(select(MergeLaneRow).where(
        MergeLaneRow.project_id == project.id, MergeLaneRow.held.is_(True), MergeLaneRow.state.in_(LANE_OPEN))))
    if not rows:
        return []
    step = merge_step(stages_of(session, project))
    roots = membership.roots_of(session, [r.task_id for r in rows])
    now = now or utcnow()
    released = [row for row in rows if step is None
                or _merge_open(session, project, step, Subject(task_id=roots.get(row.task_id, row.task_id)), now)]
    for row in released:
        row.held = False
        # Queued from now: the lane's one-hour send-back (`merge_lane.send_back_stale_rows`) counts from here.
        row.created_at = now
        if step is not None:
            consume(session, step, Subject(task_id=roots.get(row.task_id, row.task_id)))
    if not released:
        return released
    session.flush()
    if edition_features.enabled("merge_lane"):
        from app import merge_lane  # noqa: PLC0415 — the lane imports this module

        merge_lane.ensure_lane(session, project.id)
    return released


# ── Release sign-off (D16) ────────────────────────────────────────────────────────────────────────────

def signoff_missing(session: Session, project: Project, target_id: Any) -> int:
    """How many changes waiting on the release into `target_id` have not passed into the no-branch stage right
    before it (0 when that stage has a branch, or the step is no release)."""
    stages = stages_of(session, project)
    step = step_into(stages, target_id)
    if step is None or step.action != setup.RELEASE or step.source.level != stages_mod.NONE:
        return 0
    branch = stages_mod.branch_stages(stages)
    source = next((s for s in reversed(branch) if stages.index(s) < step.index), None)
    if source is None:
        return 0
    pending = waiting_mod.waiting(session, project, branch).get(source.id, [])
    if not pending:
        return 0
    facts = membership.Facts.load(session, project, stages, changes=pending)
    by_task = facts.passes_task.get(step.source.id, set())
    by_sha = facts.passes_sha.get(step.source.id, set())
    return sum(1 for c in pending if c.merge_sha not in by_sha
               and (c.task_id is None or facts.roots.get(c.task_id, c.task_id) not in by_task))
