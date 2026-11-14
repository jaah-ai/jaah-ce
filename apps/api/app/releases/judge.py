"""Why a release step waits: the release side of the judging contract (`app/pipeline/reasons.py`). The same
`Reason` type over the release's snapshot rule (`snapshot.rule`), read by the claim (`runs.frozen`), the schedule
(`tick._schedule`) and the Tasks pipeline's step (`app/pipeline/board.py`).

Freezes are read live, never from the snapshot (R7.7): `service.freeze_on` is the one freeze judgement. Soak (R4)
and allowed hours (R5) are the step's own rules: an open release's from its snapshot, a step with none (an auto-deploy
about to file one, the board's waiting changes) from the live stage. Both hold a release only before it runs: one
merging or deploying always finishes. One at a time (R6.2, `order_reason`) reads the waiting release's own snapshot
`concurrency`; no downgrade (R6.3, `downgrade`) is always on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Collection
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.actors import actor_names
from app.clock import utcnow
from app.models.projects import Project
from app.models.releases import PipelinePass, Release, ReleaseChange, ReleaseContained, ReleaseStage
from app.pipeline import reasons as reasons_mod
from app.pipeline.reasons import Reason
from app.pipeline.rules import StepRules
from app.releases import hours as hours_mod, lifecycle, service, snapshot, stages as stages_mod, waiting as waiting_mod
from app.releases.freezes import next_start

#: How far ahead a freeze about to start is shown ("Freeze starts Fri 18:00").
FREEZE_SOON = timedelta(days=7)
#: The arrival of a release that has none recorded: never newer than anything (a Resume, a soak start).
LONG_AGO = datetime.min.replace(tzinfo=timezone.utc)


def arrival(release: Release) -> datetime:
    """When `release` arrived on its target, for the pause cause: `arrived_at`, else -∞."""
    return release.arrived_at or LONG_AGO


def soak_arrival(session: Session, project: Project, source: Optional[ReleaseStage],
                 shas: Collection[str]) -> Optional[datetime]:
    """When the NEWEST of the changes `shas` arrived on the step's `source` stage (R4.2, R4.4): its integration merge
    (`Change.merged_at`) for the integration stage; its pass into a no-branch stage (`pipeline_passes`, by the change
    or by its task's top-level row, as a sign-off records it); else, per change, the later of the `arrived_at` of the
    arrived release into `source` carrying it and when a survey saw it on that branch
    (`release_contained.observed_at`). None when no arrival is known (a removed source, a change seen nowhere)."""
    if source is None or not shas:
        return None
    shas = list(shas)
    if stages_mod.is_integration(source):
        merged = [c.merged_at for c in waiting_mod.merged_changes(session, project, shas=shas) if c.merged_at]
        return max(merged) if merged else None
    if source.level == stages_mod.NONE:
        return _passed_into(session, project, source, shas)
    per_sha: dict[str, datetime] = {}
    carried = session.execute(
        select(ReleaseChange.merge_sha, func.max(Release.arrived_at))
        .join(Release, Release.id == ReleaseChange.release_id)
        .where(Release.project_id == project.id, Release.to_stage_id == source.id,
               Release.state.in_(tuple(lifecycle.ARRIVED)), Release.arrived_at.is_not(None),
               ReleaseChange.merge_sha.in_(shas))
        .group_by(ReleaseChange.merge_sha))
    observed = session.execute(select(ReleaseContained.merge_sha, ReleaseContained.observed_at).where(
        ReleaseContained.project_id == project.id, ReleaseContained.stage_id == source.id,
        ReleaseContained.merge_sha.in_(shas)))
    for sha, at in [*carried, *observed]:
        if at is not None and (sha not in per_sha or at > per_sha[sha]):
            per_sha[sha] = at
    return max(per_sha.values()) if per_sha else None


def _passed_into(session: Session, project: Project, source: ReleaseStage, shas: list[str]) -> Optional[datetime]:
    from app.pipeline import membership  # noqa: PLC0415 — the pipeline package imports this one

    tasks = [c.task_id for c in waiting_mod.merged_changes(session, project, shas=shas) if c.task_id is not None]
    roots = set(membership.roots_of(session, tasks).values()) if tasks else set()
    return session.scalar(select(func.max(PipelinePass.created_at)).where(
        PipelinePass.project_id == project.id, PipelinePass.stage_id == source.id,
        or_(PipelinePass.merge_sha.in_(shas), PipelinePass.task_id.in_(list(roots)))))


def step_source(session: Session, project: Project, target: ReleaseStage,
                branch_source: Optional[ReleaseStage], kind: Optional[str] = None) -> Optional[ReleaseStage]:
    """The stage a release step's soak counts in (R4.2): the stage right before `target` when it has no branch (a
    sign-off: the changes pass into it), else the branch stage the release runs from. A hotfix skips the path and
    soaks on its own source."""
    if kind == lifecycle.KIND_HOTFIX:
        return branch_source
    stages = stages_mod.stages_of(session, project.id)
    index = next((i for i, s in enumerate(stages) if s.id == target.id), None)
    before = stages[index - 1] if index else None
    return before if before is not None and before.level == stages_mod.NONE else branch_source


def soak_reason(session: Session, project: Project, source: Optional[ReleaseStage], rules: StepRules,
                shas: Collection[str], now: datetime) -> Optional[Reason]:
    """`soak{until, source}` while the newest change has been on `source` for less than the step's soak (R4)."""
    if not rules.soak_minutes:
        return None
    arrived = soak_arrival(session, project, source, shas)
    if arrived is None:
        return None
    until = arrived + timedelta(minutes=rules.soak_minutes)
    if until <= now:
        return None
    assert source is not None
    return reasons_mod.of("soak", until=until.isoformat(), source=source.name)


def hours_reason(rules: StepRules, now: datetime) -> Optional[Reason]:
    """`hours{next, timezone}` while the step is outside its allowed hours (R5)."""
    upcoming = hours_mod.next_allowed(rules.hours, now)
    if upcoming is None:
        return None
    assert rules.hours is not None
    return reasons_mod.of("hours", next=upcoming.isoformat(), timezone=rules.hours.tz)


def _source_of(session: Session, release: Release) -> Optional[ReleaseStage]:
    if release.from_stage_id is None:
        return None
    return session.scalar(select(ReleaseStage).where(ReleaseStage.id == release.from_stage_id))


def _carried_shas(session: Session, release: Release) -> list[str]:
    return list(session.scalars(select(ReleaseChange.merge_sha).where(ReleaseChange.release_id == release.id)))


def timing_reasons(session: Session, project: Project, stage: ReleaseStage, now: datetime,
                   release: Optional[Release] = None, *, source: Optional[ReleaseStage] = None,
                   shas: Collection[str] = ()) -> list[Reason]:
    """Soak and hours of the step into `stage`: for an open `release`, on its snapshot rule and the changes it
    carries (none once it runs, or for a back-merge, which only writes Dev); else on the live stage, over `shas`
    waiting on `source` (none while nothing waits)."""
    if release is not None:
        if release.kind == lifecycle.KIND_BACKMERGE or release.state not in (
                lifecycle.AWAITING_APPROVAL, lifecycle.SCHEDULED, lifecycle.QUEUED):
            return []
        rule = snapshot.rule(release, stage)
        if rule is None:
            return []
        rules = StepRules.of(rule)
        source, shas = _source_of(session, release), _carried_shas(session, release)
    elif not shas:
        return []
    else:
        rules = StepRules.of(stage)
    if rules.soak_minutes:
        source = step_source(session, project, stage, source, release.kind if release is not None else None)
    return [r for r in (soak_reason(session, project, source, rules, shas, now), hours_reason(rules, now))
            if r is not None]


def freeze_reason(session: Session, project: Project, stage: ReleaseStage, now: datetime,
                  release: Optional[Release] = None, *, pressed: bool = False) -> Optional[Reason]:
    """`freeze_now` or `freeze` while one holds the step (for `release`, with its own facts: pressed, queued,
    override; else as an auto or scheduled step faces it, or a press with `pressed`: a Merge)."""
    hit = (service.release_freeze(session, project, release, stage, now) if release is not None
           else service.freeze_on(session, project, stage, now, pressed=pressed))
    if hit is None:
        return None
    if hit.freeze_now:
        by = actor_names(session, {hit.now_by_id}).get(hit.now_by_id) if hit.now_by_id is not None else None
        return reasons_mod.of("freeze_now", by_name=by, at=hit.now_at.isoformat() if hit.now_at else None)
    assert hit.until is not None
    return reasons_mod.of("freeze", name=hit.name, until=hit.until.isoformat())


def freeze_soon_reason(session: Session, project: Project, stage: ReleaseStage, now: datetime) -> Optional[Reason]:
    """The next freeze on the step starting within `FREEZE_SOON`."""
    integration = stages_mod.is_integration(stage)
    windows = [w for w in service.freeze_windows(session, project.id)
               if (w.scope_stage_ids is None and not integration)
               or (w.scope_stage_ids is not None and stage.id in w.scope_stage_ids)]
    found = next_start(windows, now, FREEZE_SOON)
    if found is None:
        return None
    starts, window = found
    return reasons_mod.of("freeze_soon", name=window.name, starts=starts.isoformat())


def concurrency(release: Release) -> str:
    """The step's concurrency as `release` was requested (its snapshot; Newest wins without one)."""
    return str((release.settings or {}).get("concurrency") or lifecycle.CONCURRENCY_NEWEST)


def ahead_of(session: Session, release: Release) -> Optional[Release]:
    """The release `release` waits behind on its target, or None (R6.2): one into the same stage merging or
    deploying (never two at once, whatever the setting); under "In order", else the latest older open one."""
    if release.state not in lifecycle.NOT_STARTED or release.to_stage_id is None:
        return None
    into = (Release.project_id == release.project_id, Release.to_stage_id == release.to_stage_id,
            Release.id != release.id)
    running = session.scalar(select(Release).where(*into, Release.state.in_(lifecycle.RUNNING))
                             .order_by(Release.id).limit(1))
    if running is not None or concurrency(release) != lifecycle.CONCURRENCY_IN_ORDER:
        return running
    return session.scalar(select(Release).where(*into, Release.state.in_(lifecycle.OPEN), Release.id < release.id)
                          .order_by(Release.id.desc()).limit(1))


def order_reason(session: Session, release: Optional[Release]) -> Optional[Reason]:
    """`order` while `release` waits behind another release into its stage (`ahead_of`)."""
    ahead = ahead_of(session, release) if release is not None else None
    if ahead is None:
        return None
    return reasons_mod.of("order", after_release_id=ahead.id, version=ahead.version)


def downgrade(session: Session, project: Project, to_stage_id: Optional[UUID],
              changes: list[waiting_mod.Change]) -> bool:
    """No downgrade (R6.3, always on): whether a release carrying `changes` into `to_stage_id` would put an older
    version over a newer one — every change it carries is already on the stage, and its newest merged earlier than
    the newest change there. "Version" is integration merge order (`Change.merged_at`, decision D-8). A release
    carrying nothing is never judged one."""
    if to_stage_id is None or not changes:
        return False
    live = waiting_mod.arrived_shas(session, project.id, [to_stage_id])
    if any(c.merge_sha not in live for c in changes):
        return False
    ours = max((c.merged_at for c in changes if c.merged_at is not None), default=None)
    theirs = max((c.merged_at for c in waiting_mod.merged_changes(session, project, shas=live)
                  if c.merged_at is not None), default=None)
    return ours is not None and theirs is not None and ours < theirs


def release_downgrade(session: Session, project: Project, release: Release) -> bool:
    """`downgrade` for an open `release`, over the changes it carries."""
    carried = waiting_mod.carried(session, project, [release.id]).get(release.id) or []
    return downgrade(session, project, release.to_stage_id, carried)


@dataclass(frozen=True)
class Pause:
    """A by-itself step paused after an off-path release (R8): the release that caused it."""

    cause: Release

    def out(self, session: Session) -> dict[str, Any]:
        """GET pipeline's step `pause`: `{release_id, version, by_name, at}`."""
        return {**self.reason_data(session), "at": arrival(self.cause).isoformat() if self.cause.arrived_at else None}

    def reason_data(self, session: Session) -> dict[str, Any]:
        """The board's `paused` reason fields: `{release_id, version, by_name}`."""
        by = self.cause.requested_by_id
        return {"release_id": self.cause.id, "version": self.cause.version,
                "by_name": actor_names(session, {by}).get(by) if by is not None else None}


def paused(session: Session, project: Project, stages: list[ReleaseStage], target: ReleaseStage) -> Optional[Pause]:
    """Whether the by-itself release step into `target` is paused (architecture §4.8, derived, never stored): its
    target holds a change its source lacks (the normal path has not caught up) and the newest off-path release into
    it (a hotfix, a pick, or one from another stage since the source existed; never a backmerge, the way back) arrived after the step's last Resume. `stages`: the project's
    stages, `target` among them. A step out of the integration stage never pauses (it holds every merged change), nor
    does a step into a stage a hotfix skipped past (D-13: its source holds all it holds). Only the auto-deploy
    (`tick._auto_deploy`) obeys it: a hand press still runs (R8.2)."""
    if not target.auto_deploy:
        return None
    branch = stages_mod.branch_stages(stages)
    if target not in branch or branch.index(target) < 1:
        return None
    source = branch[branch.index(target) - 1]
    if stages_mod.is_integration(source):
        return None
    ahead = waiting_mod.arrived_shas(session, project.id, [target.id]) \
        - waiting_mod.arrived_shas(session, project.id, [source.id])
    if not ahead:
        return None
    cause = session.scalar(select(Release).where(
        Release.project_id == project.id, Release.to_stage_id == target.id,
        Release.state.in_((lifecycle.LIVE, lifecycle.SHIPPED)), Release.kind != lifecycle.KIND_BACKMERGE,
        (Release.kind.in_((lifecycle.KIND_HOTFIX, lifecycle.KIND_PICK)))
        # From another stage — counted only once the source existed: a stage inserted later is no skip.
        | ((Release.from_stage_id != source.id) & (Release.requested_at > source.created_at)),
    ).order_by(Release.arrived_at.desc().nulls_last(), Release.id.desc()).limit(1))
    if cause is None or arrival(cause) <= (target.resumed_at or LONG_AGO):
        return None
    return Pause(cause)


def reasons(session: Session, project: Project, stage: ReleaseStage, now: Optional[datetime] = None,
            release: Optional[Release] = None, *, source: Optional[ReleaseStage] = None,
            shas: Collection[str] = ()) -> list[Reason]:
    """The time, freeze and order reasons of the release step into `stage` (the press, approval and sign-off reasons
    are the step's own counts, added by the board). With no `release`, soak counts over `shas` waiting on `source`."""
    now = now or utcnow()
    held = freeze_reason(session, project, stage, now, release)
    return reasons_mod.ordered([
        held, freeze_soon_reason(session, project, stage, now) if held is None else None,
        order_reason(session, release),
        *timing_reasons(session, project, stage, now, release, source=source, shas=shas),
    ])


def first_blocking(session: Session, project: Project, release: Release, stage: ReleaseStage,
                   now: Optional[datetime] = None) -> Optional[Reason]:
    """What holds an open `release` before it may be claimed or queued, or None: a freeze, its turn, its soak, its
    hours."""
    now = now or utcnow()
    return reasons_mod.first_blocking([freeze_reason(session, project, stage, now, release),
                                       order_reason(session, release),
                                       *timing_reasons(session, project, stage, now, release)])


def step_blocking(session: Session, project: Project, source: ReleaseStage, target: ReleaseStage,
                  shas: Collection[str], now: Optional[datetime] = None) -> Optional[Reason]:
    """What holds the auto step `source` → `target` from filing a release of `shas` now (nobody's press, so every
    freeze holds it, R7.3), or None."""
    now = now or utcnow()
    return reasons_mod.first_blocking([freeze_reason(session, project, target, now),
                                       *timing_reasons(session, project, target, now, source=source, shas=shas)])
