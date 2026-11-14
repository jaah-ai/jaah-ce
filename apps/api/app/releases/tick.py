"""The `release-tick` system activity (architecture §3.7): every 60 s, and early (`wake`) after a task finishes (a
merge into the integration branch, a lane merge), a ledger anchor, a release request or an approval.

Per project, in that project's own org (`seam.bind_from`), one commit each so one project's fault never stops the rest:

0. approval timeout — a release waiting for approvals past its step's approval timeout (counted from its request,
   never reset by a partial approval; R3.3) under `cancel` is canceled, `cancellation{timeout}` (R3.2); under
   `remind` it keeps waiting and step 4 reminds its approvers once per period;
1. schedule — a `scheduled` release that is due goes `queued`, unless a freeze covers its target (it stays
   `scheduled`, failure NULL; the tab reads "Frozen until …" off the freeze);
2. executor — every `queued` release has a live executor task (`releases.ensure_executor`), unless every one targets a
   frozen stage (no run would take it);
3. auto-deploy — a stage with `auto_deploy` whose previous stage has changes waiting files a promote as the system
   once the gate is green (`gate_green`), unless frozen, paused after an off-path release (`judge.paused`, R8), one
   is open, the last one it filed failed carrying the same newest change (a new change or a person's Retry moves
   it on), its source has not verified the change (`arrivals.step_open`), or a roll back took that change off the
   target (`arrivals.holds_auto`);
4. notices — "Approve release", "Live", "Release failed", "Rolled back", the approval reminder, and "Approval timed out" /
   "Rejected" to the requester of a release that ended waiting (`app/releases/notify.py`), last and in a
   commit of their own (`notices`);
5. no deploy — a `deploying` release no deploy anchored `NO_DEPLOY_AFTER` after it merged fails
   `{no_deploy, open_run}` (before that, `anchoring.sweep` links a deploy reconciled before it got there); an arrival
   deploying or verifying past its time fails, and one still owed a node gets its executor (`arrivals.sweep`);
6. survey — a `survey` job while changes wait and the last survey is missing or older than `SURVEY_EVERY`, or once a
   release went terminal since; an open one keeps an executor only while a run may take it (`jobs.claimable`);
7. unclaimed — a `queued` release unclaimed `UNCLAIMED_FOR` fails `{node, retry}` when no node of the org is
   online, else `{not_started, retry}` carrying the executor's wait reason as its message when a queued executor
   has one (an executor still cloning or launching has none, and its release keeps waiting), or the reason its last
   executor failed while `_cooling` files no new one;

then, after the pass committed, the step sweep (`steps`): a pressed task step whose wait ended opens without another
press, and one still waiting for approvals past its timeout has its press withdrawn (`cancel`, the askers told) or
its approvers reminded once per period (`remind`), each item in a commit of its own, and a held Merge whose freeze
lifted goes to the lane;

and after an executor task failed, the tick files none for `EXECUTOR_BACKOFF` (`_cooling`);

(A merged hotfix's back-merge is the release executor's: it files it when it reports the hotfix merged.)

The projects a pass visits: an open release, an auto-deploy stage, a merge or a release that moved in `RECENT`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select, union
from sqlalchemy.orm import Session

from app import edition_features
from app.clock import utcnow
from app.merge_lane_state import OPEN_STATES as LANE_OPEN
from app.models.merge_lane import MergeLaneCertificate, MergeLaneRow
from app.models.outcomes import TaskOutcome, TaskOutcomeHead
from app.models.projects import Project
from app.models.releases import PipelineApproval, Release, ReleaseChange, ReleaseFreeze, ReleaseJob, ReleaseStage
from app.models.tasks import Task
from app.releases import (
    anchoring, arrivals, executor, jobs, judge, lifecycle, notify, runs, service, snapshot, stages as stages_mod, waiting as waiting_mod,
)
from app.releases.runs import MESSAGE_MAX

log = logging.getLogger(__name__)

ACTIVITY = "release-tick"
INTERVAL_SECONDS = 60
TIMEOUT_SECONDS = 50
MIN_GAP_SECONDS = 5

NO_DEPLOY_AFTER = timedelta(hours=2)
UNCLAIMED_FOR = timedelta(minutes=5)
#: After an executor task failed, the tick files none for this long (`_cooling`).
EXECUTOR_BACKOFF = timedelta(hours=1)
SURVEY_EVERY = timedelta(hours=1)
#: How far back a merge or a moved release brings a project into a pass.
RECENT = timedelta(hours=24)
#: Outcome states that read the merged change red (lane off).
_RED_OUTCOMES = ("reverted", "failed")


def wake(session: Session) -> None:
    """Run the tick once `session` commits (loss-tolerant: the interval is the backstop)."""
    from app.cron_runtime import wake_after_commit

    wake_after_commit(session, ACTIVITY)


@dataclass
class Report:
    projects: int = 0
    acted: list[str] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    def detail(self) -> str:
        text = f"{self.projects} project(s)"
        if self.acted:
            text += "; " + ", ".join(self.acted[:20])
        if self.failed:
            text += f"; failed: {', '.join(str(p) for p in self.failed)}"
        return text


# ── Which projects a pass visits ─────────────────────────────────────────────────────────────────────

def candidates(session: Session, now: datetime) -> list[int]:
    since = now - RECENT
    ids = session.scalars(union(
        select(Release.project_id).where(Release.state.in_(lifecycle.OPEN)),
        select(Release.project_id).where(Release.updated_at >= since),
        # A release target that deploys by itself: branch-level and not the integration stage (which stores no
        # branch, `stages.is_integration`), whose `auto_deploy` is the Merge step's, not a release's.
        select(ReleaseStage.project_id).where(
            ReleaseStage.auto_deploy.is_(True), ReleaseStage.level == stages_mod.BRANCH,
            ReleaseStage.branch.is_not(None)),
        select(TaskOutcome.project_id).where(TaskOutcome.merged_at >= since, TaskOutcome.project_id.is_not(None)),
        select(MergeLaneRow.project_id).where(MergeLaneRow.state == "merged", MergeLaneRow.merged_at >= since),
        # A task step pressed and waiting (the step sweep re-gates it), a step with a soak or allowed hours (whose
        # waits lapse by themselves: `timed`), and a project frozen by Freeze now.
        select(PipelineApproval.project_id).where(PipelineApproval.kind == "press"),
        select(ReleaseStage.project_id).where(_TIMED),
        select(Project.id).where(Project.freeze_now_at.is_not(None)),
        # A Merge held where a freeze may name the integration stage (it lifts by the clock: the sweep lets it go).
        select(MergeLaneRow.project_id).where(
            MergeLaneRow.held.is_(True), MergeLaneRow.state.in_(LANE_OPEN),
            MergeLaneRow.project_id.in_(select(ReleaseFreeze.project_id).where(ReleaseFreeze.scope == "stages"))),
    ))
    return sorted({int(i) for i in ids if i is not None})


#: A step whose wait can lapse with no one acting: a soak or allowed hours (R4, R5).
_TIMED = or_(ReleaseStage.soak_minutes > 0, ReleaseStage.allowed_hours.is_not(None))


def timed(session: Session, project_id: int) -> bool:
    """Whether any step of the project has a soak or allowed hours."""
    return session.scalar(select(ReleaseStage.id).where(ReleaseStage.project_id == project_id, _TIMED).limit(1)) \
        is not None


def run(scheduled_at: Optional[datetime] = None) -> Report:
    """One pass over every candidate project, each in its own org-bound session and commit."""
    from app.tenancy import seam
    from app.trigger_intake import fleet_session

    now = scheduled_at or utcnow()
    try:
        upgraded = stages_mod.upgrade_pending()
    except Exception:  # noqa: BLE001 — the upgrade's fault must not stop the release pass
        log.exception("release tick: pipeline upgrade failed")
        upgraded = []
    with fleet_session() as session:
        project_ids = candidates(session, now)
    report = Report(projects=len(project_ids))
    report.acted.extend(f"{project_id}:pipeline" for project_id in upgraded)
    for project_id in project_ids:
        try:
            acted = _tick_one(project_id, now)
        except seam.TenancyRowMissing:
            continue  # Deleted since the listing.
        except Exception:  # noqa: BLE001 — one project's fault must not stop the others
            log.exception("release tick: project %s failed", project_id)
            report.failed.append(project_id)
            continue
        report.acted.extend(f"{project_id}:{a}" for a in acted)
    return report


def _tick_one(project_id: int, now: datetime) -> list[str]:
    """One project's pass in its own org-bound session and commit (rolled back when it raises)."""
    from app.db import BridgedSession
    from app.tenancy import seam

    with BridgedSession() as session, seam.detached(), seam.bind_from(session, Project, project_id):
        project = session.scalar(select(Project).where(Project.id == project_id))
        if project is None:
            return []
        acted = tick_project(session, project, now)
        session.commit()
        acted += steps(session, project, now)
        # Notices last, in a commit of their own: a notice is sent at once and only its activity row waits for the
        # commit, so nothing after it may roll that row back (and the next pass send it again).
        acted += notices(session, project, now)
        session.commit()
        return acted


# ── One project ──────────────────────────────────────────────────────────────────────────────────────

def tick_project(session: Session, project: Project, now: datetime) -> list[str]:
    """Every step but the notices for one project; what it did, as short tags. Flushes; the caller commits, then
    runs `notices`."""
    stages = stages_mod.branch_stages(stages_mod.materialise(session, project))
    if len(stages) < 2:
        return []
    acted: list[str] = []
    acted += _approval_timeouts(session, project, stages, now)
    acted += _schedule(session, project, stages, now)
    acted += _executor(session, project, now)
    # The database re-stamps `updated_at` of what moved: step 7 measures "unclaimed" from it, never from the stale
    # stamp of a release this very pass queued.
    session.flush()
    acted += _moving(session, project, now)
    acted += _unclaimed(session, project, now)
    # An open release's changes still count: the survey and notices measure the step (auto-deploy skips an open target).
    pending = waiting_mod.waiting(session, project, stages, less_in_flight=False)
    acted += _auto_deploy(session, project, stages, pending, now)
    acted += _survey(session, project, pending, now)
    session.flush()
    return acted


def steps(session: Session, project: Project, now: datetime) -> list[str]:
    """The step sweep (architecture §4.5), after the rest of the pass committed and each item in a commit of its own,
    so one refused item never undoes the pass or another item: every task step a person pressed is judged again, and
    a wait that ended (a save that lowered the approvals) opens it without another press. An open Start starts the
    task as its first presser's own Start would (only while that person may still press the step); an open Move
    passes a task still right before the step; an open Merge lets its held lane rows go (`gate.release_held`), as
    does a held Merge nobody pressed once the freeze holding it lifts. A refused item stays pressed and is judged
    again next pass.

    A project with a soak or allowed hours on a step (`timed`) is also swept for the waits that lapse by themselves:
    an auto step's approved item (started as its creator, as the last approval would), a merged change pressed
    through a Move, and a lane row its Merge holds with no press at all."""
    from app.pipeline import gate as gate_mod

    lapsing = timed(session, project.id)
    pressed = list(session.execute(
        select(PipelineApproval.stage_id, PipelineApproval.task_id, PipelineApproval.merge_sha)
        .where(PipelineApproval.project_id == project.id,
               *(() if lapsing else (PipelineApproval.kind == gate_mod.PRESS,))).distinct()))
    # Held lane rows are always judged again: a freeze or a time rule holding a Merge lifts by the clock.
    merge = session.scalar(select(MergeLaneRow.id).where(
        MergeLaneRow.project_id == project.id, MergeLaneRow.held.is_(True), MergeLaneRow.state.in_(LANE_OPEN))
        .limit(1)) is not None
    if not pressed and not merge:
        return []
    stages = gate_mod.stages_of(session, project)
    acted: list[str] = []
    for stage_id, task_id, sha in pressed:
        step = gate_mod.step_into(stages, stage_id)
        if step is None or (task_id is None and not lapsing):
            continue  # a merged change's press opens through its own step's press
        try:
            done = _sweep_item(session, project, stages, step, task_id, now, auto_start=lapsing) \
                if task_id is not None else _sweep_change(session, project, step, sha, now)
            session.commit()
        except Exception:  # noqa: BLE001 — one item's fault must not stop the others or the pass
            log.exception("release tick: step sweep of %s on project %s failed", task_id or sha, project.id)
            session.rollback()
            continue
        if done == "merge":
            merge = True
        elif done is not None:
            acted.append(done)
    if merge and gate_mod.release_held(session, project, now):
        acted.append("merge")
    session.commit()
    return acted


def _sweep_change(session: Session, project: Project, step: object, sha: Optional[str],
                  now: datetime) -> Optional[str]:
    """A merged change pressed through a Move whose soak or allowed hours held it: it passes once they lapse."""
    from app.pipeline import gate as gate_mod
    from app.releases import pipeline as setup

    assert isinstance(step, gate_mod.Step)
    if sha is None or step.action != setup.MOVE:
        return None
    subject = gate_mod.Subject(sha=sha)
    if not gate_mod.gate(session, step, subject, now=now).open:
        return None
    gate_mod.pass_into(session, project, step.target, subject, gate_mod.first_presser(session, step, subject))
    gate_mod.consume(session, step, subject)
    return f"move@{sha[:12]}"


def _sweep_item(session: Session, project: Project, stages: list[ReleaseStage], step: object, task_id: int,
                now: datetime, *, auto_start: bool = False) -> Optional[str]:
    """One pressed task on one step: what the sweep did (`start#…`, `move#…`, "merge": its Merge may go), or None."""
    from app.authz.principal import principal_for_user
    from app.models.identity import AdminUser
    from app.pipeline import gate as gate_mod
    from app.releases import pipeline as setup
    from app.routes.tasks.dispatch import start_one

    assert isinstance(step, gate_mod.Step)
    subject = gate_mod.Subject(task_id=task_id)
    task = session.scalar(select(Task).where(Task.id == task_id))
    if task is None or task.project_id != project.id:
        return None
    if step.action in (setup.MERGE, setup.START_MERGE) and not gate_mod.gated(task):
        state = gate_mod.gate(session, step, subject, now=now)
        if isinstance(state, gate_mod.WaitApprovals):
            return _step_timeout(session, project, step, subject, now)
        return "merge"
    state = gate_mod.gate(session, step, subject, now=now)
    if isinstance(state, gate_mod.WaitApprovals):
        return _step_timeout(session, project, step, subject, now)
    if not state.open:
        return None
    if step.action in (setup.START, setup.START_MERGE):
        starter_id = gate_mod.first_presser(session, step, subject) or (
            task.created_by_id if auto_start and step.target.auto_deploy else None)
        starter = session.scalar(select(AdminUser).where(AdminUser.id == starter_id)) \
            if starter_id is not None else None
        if starter is None or starter.disabled_at is not None or not gate_mod.gated(task):
            return None
        principal = principal_for_user(session, starter)
        if not gate_mod.may_act(principal, project.id, step):
            return None  # the presser may no longer press it: it waits for someone who may
        try:
            started = start_one(session, task_id, None, starter, principal, out=False, pressing=False,
                                starter_id=starter_id)
        except HTTPException as exc:
            log.warning("release tick: start of task %s refused: %s", task_id, exc.detail)
            session.rollback()
            return None
        return None if isinstance(started, gate_mod.StartDecision) else f"start#{task_id}"
    if step.action == setup.MOVE:
        # One step at a time, as a press: only a task in the stage right before it moves through it.
        if gate_mod._place(session, project, stages, task) != step.index - 1:
            return None
        gate_mod.pass_into(session, project, step.target, subject, gate_mod.first_presser(session, step, subject))
        gate_mod.consume(session, step, subject)
        return f"move#{task_id}"
    return None


def _step_timeout(session: Session, project: Project, step: object, subject: object, now: datetime) -> Optional[str]:
    """A pressed task step still waiting for approvals: past its timeout (from its first press, R3.3), `cancel`
    withdraws the press and approvals (the item stays where it is, waiting for a new press) and tells whoever asked;
    `remind` reminds its approvers once per period (stamped on the first press row)."""
    from app.pipeline import gate as gate_mod, notices as step_notices  # noqa: PLC0415
    from app.pipeline.rules import CANCEL, StepRules  # noqa: PLC0415

    assert isinstance(step, gate_mod.Step) and isinstance(subject, gate_mod.Subject)
    rules = StepRules.of(step.target)
    first = session.scalar(select(PipelineApproval).where(
        PipelineApproval.stage_id == step.target.id, PipelineApproval.kind == gate_mod.PRESS,
        *gate_mod._subject_clause(subject)).order_by(PipelineApproval.created_at).limit(1))
    period = timeout_period(first.created_at if first is not None else None, rules.approval_timeout_minutes, now)
    if first is None or not period or rules.approval_timeout_minutes is None:
        return None
    if rules.on_timeout == CANCEL:
        asked = gate_mod.askers(session, step, subject)
        gate_mod.consume(session, step, subject)
        step_notices.step_ended(session, project, step, subject, asked, step_notices.STEP_TIMEOUT)
        return f"step_timeout#{subject.task_id}"
    due = first.created_at + timedelta(minutes=rules.approval_timeout_minutes * period)
    if first.reminded_at is not None and first.reminded_at >= due:
        return None
    first.reminded_at = now
    step_notices.step_reminder(session, project, step, subject)
    return f"step_remind#{subject.task_id}"


def notices(session: Session, project: Project, now: datetime) -> list[str]:
    """Step 4, after the rest of the pass committed. Flushes; the caller commits."""
    stages = stages_mod.branch_stages(stages_mod.stages_of(session, project.id))
    if len(stages) < 2:
        return []
    acted = _notices(session, project, stages, now)
    session.flush()
    return acted


def _open(session: Session, project: Project, state: str) -> list[Release]:
    return list(session.scalars(select(Release).where(
        Release.project_id == project.id, Release.state == state).order_by(Release.id).with_for_update()))


def timeout_period(since: Optional[datetime], minutes: Optional[int], now: datetime) -> int:
    """How many whole approval-timeout periods ran out since a wait for approvals began (0: none yet, or no
    timeout)."""
    if since is None or minutes is None:
        return 0
    return max(0, int((now - since) // timedelta(minutes=minutes)))


def _approval_timeouts(session: Session, project: Project, stages: list[ReleaseStage], now: datetime) -> list[str]:
    """Step 0, on the rule each release was requested under (`snapshot.rule`)."""
    from app.pipeline.rules import CANCEL, StepRules  # noqa: PLC0415 — the pipeline package imports this one

    by_id = {s.id: s for s in stages}
    acted = []
    for release in _open(session, project, lifecycle.AWAITING_APPROVAL):
        stage = snapshot.rule(release, by_id.get(release.to_stage_id))
        if stage is None:
            continue
        rules = StepRules.of(stage)
        if rules.on_timeout == CANCEL and timeout_period(release.requested_at, rules.approval_timeout_minutes, now):
            service.time_out(release)
            acted.append(f"timeout#{release.id}")
    return acted


def _schedule(session: Session, project: Project, stages: list[ReleaseStage], now: datetime) -> list[str]:
    """Step 1."""
    by_id = {s.id: s for s in stages}
    acted = []
    for release in _open(session, project, lifecycle.SCHEDULED):
        if release.scheduled_for is not None and release.scheduled_for > now:
            continue
        stage = by_id.get(release.to_stage_id)
        if stage is None or judge.first_blocking(session, project, release, stage, now) is not None:
            continue
        service._move(release, "due", lifecycle.QUEUED)
        release.failure = None
        acted.append(f"due#{release.id}")
    return acted


def _failed_executor(session: Session, project: Project, now: datetime) -> Optional[Task]:
    """The project's last executor task when it failed within `EXECUTOR_BACKOFF`, else None."""
    from app.task_lifecycle import SYSTEM_FAILED

    last = session.scalar(select(Task).where(Task.project_id == project.id, Task.persona == executor.PERSONA)
                          .order_by(Task.id.desc()).limit(1))
    ok = last is not None and last.state == SYSTEM_FAILED and last.updated_at is not None \
        and now - last.updated_at < EXECUTOR_BACKOFF
    return last if ok else None


def _cooling(session: Session, project: Project, now: datetime) -> bool:
    """Whether the project's last executor task failed within `EXECUTOR_BACKOFF`: the tick files no new one until then,
    so a run that cannot start is not filed again every minute (a person's Release, Retry or Connect still files one)."""
    return _failed_executor(session, project, now) is not None


def _executor(session: Session, project: Project, now: datetime) -> list[str]:
    """Step 2 (a survey job keeps its executor in `_survey`)."""
    if _cooling(session, project, now):
        return []
    queued = _open(session, project, lifecycle.QUEUED)
    by_id = {s.id: s for s in stages_mod.branch_stages(stages_mod.stages_of(session, project.id))}
    takeable = [r for r in queued if not runs.frozen(session, r, by_id, now, project=project)
                and judge.ahead_of(session, r) is None]
    if not takeable:  # none to hand out: a run would end "nothing to do"
        return []
    task_id = executor.ensure_executor(session, project, takeable[0].requested_by_id, for_release=True)
    if task_id is None:
        return []
    moved = [r for r in queued if r.task_id != task_id]
    for release in moved:  # Only a change: an untouched row keeps the `updated_at` step 7 measures from.
        release.task_id = task_id
    return [f"executor#{task_id}"] if moved else []


def _moving(session: Session, project: Project, now: datetime) -> list[str]:
    """Step 5, with the anchoring look-back before it, then the arrivals' sweep (`arrivals.sweep`)."""
    acted = []
    for release in _open(session, project, lifecycle.DEPLOYING):
        if anchoring.sweep(session, project, release, now):
            acted.append(f"anchored#{release.id}")
            continue
        if release.updated_at is not None and now - release.updated_at >= NO_DEPLOY_AFTER \
                and not _driven(session, release):
            service._move(release, "fail", lifecycle.FAILED)
            release.failure = {"kind": "no_deploy", "message": None, "action": "open_run"}
            acted.append(f"no_deploy#{release.id}")
    acted += arrivals.sweep(session, project, now, may_file=not _cooling(session, project, now))
    return acted


def _driven(session: Session, release: Release) -> bool:
    """A release into a target the executor drives: its arrival's sweep fails it (`arrivals.sweep`), never this rule."""
    from app.pipeline_gates import defaults as gate_defaults  # noqa: PLC0415

    stage = session.get(ReleaseStage, release.to_stage_id) if release.to_stage_id is not None else None
    return stage is not None and gate_defaults.target_kind(stage) in arrivals.DRIVEN


def _unclaimed(session: Session, project: Project, now: datetime) -> list[str]:
    """Step 7. A running executor is carrying another release (a PR whose checks it watches): its queue waits, unless
    no node is online at all."""
    by_id = {s.id: s for s in stages_mod.branch_stages(stages_mod.stages_of(session, project.id))}
    # One waiting its turn behind another into its stage (R6), or one its soak or allowed hours hold (R4.3, R5.2), is
    # waiting, not stuck: no executor is owed it yet.
    stale = [r for r in _open(session, project, lifecycle.QUEUED)
             if r.updated_at is not None and now - r.updated_at >= UNCLAIMED_FOR and judge.ahead_of(session, r) is None
             and not (r.to_stage_id in by_id and any(
                 x.blocking for x in judge.timing_reasons(session, project, by_id[r.to_stage_id], now, r)))]
    if not stale:
        return []
    if executor.waiting_for_login(session, project) is not None:
        # Nobody can start a run: the queue waits, saying why, and `_executor` files one once someone connects.
        return []
    state = executor.executor_state(session, project)
    reason = state.get("wait_reason")
    if not state["node_online"]:
        kind, message = "node", None
    elif state["state"] != "running" and isinstance(reason, str) and reason.strip():
        # Only with a reason: an executor still cloning or launching names none, and is not stuck.
        kind, message = "not_started", reason.removeprefix("Waiting:").strip()[:MESSAGE_MAX]
    elif state["state"] == "idle" and (dead := _failed_executor(session, project, now)) is not None:
        # The run died and the backoff files no new one: say why, so a person's Retry starts a fresh run as them.
        kind, message = "not_started", (dead.awaiting_response_body or "The release run stopped.").strip()[:MESSAGE_MAX]
    else:
        return []
    for release in stale:
        service._move(release, "fail", lifecycle.FAILED)
        release.failure = {"kind": kind, "message": message, "action": "retry"}
    return [f"{kind}#{r.id}" for r in stale]


# ── Auto-deploy ──────────────────────────────────────────────────────────────────────────────────────

def gate_green(session: Session, project: Project, newest: waiting_mod.Change) -> bool:
    """Whether the newest waiting change's tree passed the project's gate. Lane on: a certificate names its merge
    commit, or the lane merged it — and the integration branch is not recorded red. Lane off: its outcome is not red
    (no failed head verdict, not reverted or failed); a change no runner made has no verdict and is not red."""
    if edition_features.enabled("merge_lane"):
        from app import merge_lane
        from app.merge_lane_state import lane_state

        if lane_state(session, project).active:
            if merge_lane._develop_red_for_lane(project) is not None:
                return False
            certified = session.scalar(select(func.count()).select_from(MergeLaneCertificate).where(
                MergeLaneCertificate.project_id == project.id, MergeLaneCertificate.develop_sha == newest.merge_sha))
            laned = session.scalar(select(func.count()).select_from(MergeLaneRow).where(
                MergeLaneRow.project_id == project.id, MergeLaneRow.state == "merged",
                MergeLaneRow.merge_sha == newest.merge_sha))
            return bool(certified or laned)
    if newest.task_outcome_id is None:
        return True
    outcome = session.scalar(select(TaskOutcome).where(TaskOutcome.id == newest.task_outcome_id))
    if outcome is None:
        return True
    if outcome.outcome_state in _RED_OUTCOMES:
        return False
    verdict = session.scalar(select(TaskOutcomeHead.verdict).where(TaskOutcomeHead.outcome_id == outcome.id)
                             .order_by(TaskOutcomeHead.pushed_at.desc()).limit(1))
    return verdict != "fail"


def _failed_for(session: Session, project: Project, target: ReleaseStage, newest: waiting_mod.Change) -> bool:
    """Whether the latest release into `target` is a system promote that failed carrying `newest`: filing it again
    would fail the same way every minute. A new change, or a person's Retry, moves it on."""
    last = session.scalar(select(Release).where(Release.project_id == project.id, Release.to_stage_id == target.id)
                          .order_by(Release.id.desc()).limit(1))
    if last is None or last.state != lifecycle.FAILED or last.requested_by_id is not None:
        return False
    return session.scalar(select(func.count()).select_from(ReleaseChange).where(
        ReleaseChange.release_id == last.id, ReleaseChange.merge_sha == newest.merge_sha)) > 0


def _auto_deploy(session: Session, project: Project, stages: list[ReleaseStage],
                 pending: dict[UUID, list[waiting_mod.Change]], now: datetime) -> list[str]:
    """Step 3."""
    targets = [s for s in stages_mod.release_targets(stages) if s.auto_deploy]
    acted = []
    for target in targets:
        source = stages[stages.index(target) - 1]
        changes = pending.get(source.id) or []
        if not changes or service.open_releases_into(session, project.id, target.id):
            continue
        if _failed_for(session, project, target, changes[0]):
            continue
        # Verify after arrival holds the step out of its source (AC11.2); a change rolled back off the target is not
        # promoted into it again by itself (risk R5).
        if not arrivals.step_open(session, project, source)[0] \
                or arrivals.holds_auto(session, project, target, changes[0].merge_sha):
            continue
        # Paused after an off-path release until the normal path catches up or someone resumes it (R8).
        if judge.paused(session, project, stages, target) is not None:
            continue
        # An auto-deploy is nobody's press: every freeze on the target holds it (R7.3), as do its soak and hours (X4).
        if judge.step_blocking(session, project, source, target, [c.merge_sha for c in changes], now) is not None \
                or not gate_green(session, project, changes[0]):
            continue
        try:
            release, created = service.deploy(session, project, None, source.id, system=True)
        except service.Refused:
            continue
        if created:
            acted.append(f"auto#{release.id}")
    return acted


# ── Notices ──────────────────────────────────────────────────────────────────────────────────────────

def _notices(session: Session, project: Project, stages: list[ReleaseStage], now: datetime) -> list[str]:
    """Step 4."""
    acted = []
    by_id = {s.id: s for s in stages}
    from app.pipeline.rules import REMIND, StepRules  # noqa: PLC0415 — the pipeline package imports this one

    for release in session.scalars(select(Release).where(
        Release.project_id == project.id, Release.state == lifecycle.AWAITING_APPROVAL).order_by(Release.id)):
        stage = snapshot.rule(release, by_id.get(release.to_stage_id))
        if stage is None:
            continue
        if notify.approval(session, project, stage, release):
            acted.append(f"approval#{release.id}")
        rules = StepRules.of(stage)
        period = timeout_period(release.requested_at, rules.approval_timeout_minutes, now)
        if rules.on_timeout == REMIND and period and notify.reminder(session, project, stage, release, period):
            acted.append(f"remind#{release.id}")
    production = stages[-1]
    since = now - NO_DEPLOY_AFTER
    for release in session.scalars(select(Release).where(
        Release.project_id == project.id, Release.updated_at >= since,
        Release.state.in_((lifecycle.LIVE, lifecycle.SHIPPED, lifecycle.FAILED, lifecycle.CANCELED)),
    ).order_by(Release.id)):
        if release.state == lifecycle.CANCELED:
            if notify.ended(session, project, release):
                acted.append(f"ended#{release.id}")
        elif release.state == lifecycle.FAILED:
            if notify.failed(session, project, release):
                acted.append(f"failed#{release.id}")
        elif release.kind == lifecycle.KIND_ROLLBACK:
            if notify.rolled_back(session, project, release, by_id.get(release.to_stage_id)):
                acted.append(f"rolled_back#{release.id}")
        elif release.to_stage_id == production.id and notify.live(session, project, release,
                                                                       _included_tasks(session, release)):
            acted.append(f"live#{release.id}")
    return acted


def _included_tasks(session: Session, release: Release) -> list[tuple[int, str, Optional[UUID]]]:
    """`(task_id, title, created_by_id)` of each task whose change the release carries."""
    refs = list(session.scalars(select(ReleaseChange).where(ReleaseChange.release_id == release.id)))
    outcome_ids = [r.task_outcome_id for r in refs if r.task_outcome_id is not None]
    row_ids = [r.merge_lane_row_id for r in refs if r.merge_lane_row_id is not None]
    task_ids: set[int] = set()
    if outcome_ids:
        task_ids |= {t for t in session.scalars(select(TaskOutcome.task_id).where(TaskOutcome.id.in_(outcome_ids)))
                     if t is not None}
    if row_ids:
        task_ids |= {t for t in session.scalars(select(MergeLaneRow.task_id).where(MergeLaneRow.id.in_(row_ids)))
                     if t is not None}
    if not task_ids:
        return []
    return [(t.id, t.title, t.created_by_id) for t in session.scalars(
        select(Task).where(Task.id.in_(task_ids), Task.persona != executor.PERSONA).order_by(Task.id))]


# ── Survey ───────────────────────────────────────────────────────────────────────────────────────────

def survey_on_look(session: Session, project: Project, now: datetime) -> bool:
    """A deployer opened the pipeline page's Release step: a survey when none is open and the last one is missing or
    older than `SURVEY_EVERY`, whether or not a change waits. Its executor starts as the project's "Run jobs as" person
    (`git_access.project_forge_person`), never as the viewer: a look is not a request, and a viewer whose login cannot
    see the repository would start a run that cannot clone. A merge made straight on the
    forge leaves no row (`waiting.py`), so step 6 never files for it; only a survey counts it (`extra_commits`), which
    the Release view shows with Release on. An open one gets its executor back if it died. Not while `_cooling`. Whether one was
    filed."""
    if len(stages_mod.branch_stages(stages_mod.materialise(session, project))) < 2 or _cooling(session, project, now):
        return False
    if (job := jobs.open_job(session, project.id, jobs.SURVEY)) is not None:
        if jobs.claimable(job, now):
            executor.ensure_executor(session, project)
        return False
    last = session.scalar(select(func.max(ReleaseJob.requested_at)).where(
        ReleaseJob.project_id == project.id, ReleaseJob.mode == jobs.SURVEY))
    if last is not None and now - last < SURVEY_EVERY:
        return False
    _job, created = jobs.file(session, project, jobs.SURVEY)
    return created


def _survey(session: Session, project: Project, pending: dict[UUID, list[waiting_mod.Change]],
            now: datetime) -> list[str]:
    """Step 6: a survey while changes wait and the last one is missing or older than `SURVEY_EVERY`, or once a release
    ended since it; an open one keeps its executor (unless `_cooling`)."""
    waiting_any = any(pending.values())
    if (job := jobs.open_job(session, project.id, jobs.SURVEY)) is not None:
        # Only while changes wait: a survey filed for a forge merge has none, and its executor is brought back by the
        # next look (`survey_on_look`), never every minute by an executor that ends without closing the job. Only once
        # a run may take it (`jobs.claimable`): a run that ended holding a fresh claim would otherwise get a new
        # executor on every tick, each ending "nothing to do" and waking the next.
        if waiting_any and jobs.claimable(job, now) and not _cooling(session, project, now):
            executor.ensure_executor(session, project)
        return []
    last_terminal = session.scalar(select(func.max(Release.updated_at)).where(
        Release.project_id == project.id, Release.state.in_(tuple(lifecycle.TERMINAL))))
    last = session.scalar(select(func.max(ReleaseJob.requested_at)).where(
        ReleaseJob.project_id == project.id, ReleaseJob.mode == jobs.SURVEY))
    ended_since = last_terminal is not None and (last is None or last_terminal > last)
    stale = waiting_any and (last is None or now - last >= SURVEY_EVERY)
    if not (ended_since or stale) or _cooling(session, project, now):
        return []
    _job, created = jobs.file(session, project, jobs.SURVEY)
    return ["survey"] if created else []
