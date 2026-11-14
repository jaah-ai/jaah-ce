"""The release verbs a person calls: deploy (one step, stage k → k+1), hotfix (one change, straight to Production),
approve and reject (each with an optional comment), cancel, retry — and the control plane's own (`deploy(system=True)`, auto-deploy). Each guards its move through `lifecycle.check`, refuses with `Refused` (the route renders the
status and body as they are — no prose), and files the executor when a release becomes `queued`. None commits.

One at a time (R6): requests into one stage are serialised by a row lock on the target stage (`_request`), and the
index `uq_releases_running` holds at most one release merging or deploying into it. Under the step's concurrency a
new request carrying a change the open ones lack either supersedes those not started ("Newest wins": canceled
"Superseded by #N", their approvals not carried over) or waits behind them ("In order"); a press carrying nothing new
answers the newest open release (`created=False`). A back-merge never supersedes nor is superseded. No release puts an
older version over a newer one (409 `older_than_live`, `judge.downgrade`).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.authz.principal import Principal
from app.clock import utcnow
from app.models.projects import Project
from app.models.releases import (
    Release,
    ReleaseApproval,
    ReleaseChange,
    ReleaseFreeze,
    ReleaseFreezeStage,
    ReleaseStage,
)
from app.releases import access, executor, lifecycle, override as override_mod, snapshot, stages as stages_mod, \
    waiting as waiting_mod
from app.releases.freezes import Window, frozen_until, occurrence_covering


@dataclass
class Refused(Exception):
    """A refusal the route renders verbatim: `status` and an optional machine body (`{code, ...}`)."""

    status: int
    body: dict[str, Any] = field(default_factory=dict)


def freeze_windows(session: Session, project_id: int) -> list[Window]:
    """The project's freezes as windows, scope and let-through included (`app/releases/freezes.py`)."""
    rows = list(session.scalars(select(ReleaseFreeze).where(ReleaseFreeze.project_id == project_id)))
    scoped: dict[UUID, set[UUID]] = {}
    ids = [f.id for f in rows if f.scope == "stages"]
    if ids:
        for freeze_id, stage_id in session.execute(select(ReleaseFreezeStage.freeze_id, ReleaseFreezeStage.stage_id)
                                                   .where(ReleaseFreezeStage.freeze_id.in_(ids))):
            scoped.setdefault(freeze_id, set()).add(stage_id)
    return [
        Window(f.starts_at, f.ends_at, f.weekly, f.timezone, repeat=f.repeat, name=f.name, freeze_id=f.id,
               scope_stage_ids=frozenset(scoped.get(f.id, ())) if f.scope == "stages" else None,
               let_pressed=bool(f.let_pressed), let_queued=f.let_queued is not False)
        for f in rows
    ]


@dataclass(frozen=True)
class FreezeHit:
    """A freeze stopping a step now: when it lifts (None: Freeze now, until switched off), the freeze's name and id,
    and who switched Freeze now on."""

    until: Optional[datetime]
    name: str = ""
    freeze_id: Optional[UUID] = None
    now_by_id: Optional[UUID] = None
    now_at: Optional[datetime] = None
    #: When the occurrence holding the step began (Freeze now: when it was switched on): an override given before
    #: it never covers it (`override.covers`).
    starts: Optional[datetime] = None

    @property
    def freeze_now(self) -> bool:
        return self.until is None


def _covers(window: Window, stage: ReleaseStage, integration: bool) -> bool:
    if window.scope_stage_ids is None:
        return not integration
    return stage.id in window.scope_stage_ids


def freeze_on(session: Session, project: Project, stage: ReleaseStage, now: Optional[datetime] = None, *,
              pressed: bool = False, queued_at: Optional[datetime] = None, override: bool = False,
              windows: Optional[list[Window]] = None) -> Optional[FreezeHit]:
    """The freeze stopping a step into `stage` now, read live (never from a snapshot), or None (architecture §4.2):

    1. Freeze now on and `stage` after the integration stage → a hit, no let-through;
    2. the windows covering now whose scope includes `stage`, less those that let it through: `pressed` (a person
       pressed it to run now) past a `let_pressed` freeze, a release `queued_at` before the occurrence began past a
       `let_queued` one;
    3. `override` passes every freeze; otherwise the hit lifts when the remaining windows do (`frozen_until`).
    """
    if override:
        return None
    now = now or utcnow()
    integration = stages_mod.is_integration(stage)
    if project.freeze_now_at is not None and not integration:
        return FreezeHit(None, now_by_id=project.freeze_now_by_id, now_at=project.freeze_now_at,
                         starts=project.freeze_now_at)
    if windows is None:
        windows = freeze_windows(session, stage.project_id)
    covering: list[tuple[Window, tuple[datetime, datetime]]] = []
    passed: set[int] = set()
    for index, window in enumerate(windows):
        if not _covers(window, stage, integration):
            passed.add(index)
            continue
        if pressed and window.let_pressed:
            passed.add(index)
            continue
        occurrence = occurrence_covering(window, now)
        if occurrence is None:
            continue
        if queued_at is not None and window.let_queued and queued_at < occurrence[0]:
            passed.add(index)
            continue
        covering.append((window, occurrence))
    if not covering:
        return None
    # It lifts when every freeze still holding the step does, back-to-back ones included.
    scoped = [w for index, w in enumerate(windows) if index not in passed]
    until = frozen_until(scoped, now) or max(o[1] for _, o in covering)
    first, occurrence = max(covering, key=lambda c: c[1][1])
    return FreezeHit(until, name=first.name, freeze_id=first.freeze_id, starts=occurrence[0])


def pressed(release: Release) -> bool:
    """Whether a person pressed `release` to run now (R7.3): it has a requester and no schedule. An auto-deploy has
    no requester and a scheduled release has `scheduled_for`, so both wait for a freeze."""
    return release.requested_by_id is not None and release.scheduled_for is None


def release_freeze(session: Session, project: Project, release: Release, stage: ReleaseStage,
                   now: Optional[datetime] = None) -> Optional[FreezeHit]:
    """`freeze_on` for an open `release` into `stage`, with its own facts; its override passes only the freeze
    occasion it was given for (`override.covers`)."""
    hit = freeze_on(session, project, stage, now, pressed=pressed(release),
                    queued_at=release.queued_at if release.state in (lifecycle.QUEUED, lifecycle.MERGING) else None)
    return None if override_mod.covers(release.freeze_override, hit) else hit


def open_releases_into(session: Session, project_id: int, to_stage_id: UUID) -> list[Release]:
    """The open releases into one stage, oldest first: several may wait at once (R6)."""
    return list(session.scalars(select(Release).where(
        Release.project_id == project_id, Release.to_stage_id == to_stage_id, Release.state.in_(lifecycle.OPEN),
    ).order_by(Release.id)))


def open_releases(session: Session, project_id: int) -> list[Release]:
    """The project's open releases, oldest first."""
    return list(session.scalars(
        select(Release).where(Release.project_id == project_id, Release.state.in_(lifecycle.OPEN)).order_by(Release.id)
    ))


def _stage(stages: list[ReleaseStage], stage_id: Any) -> ReleaseStage:
    found = next((s for s in stages if str(s.id) == str(stage_id)), None)
    if found is None:
        raise Refused(404)
    return found


def _step(stages: list[ReleaseStage], from_stage_id: Any) -> tuple[ReleaseStage, ReleaseStage]:
    source = _stage(stages, from_stage_id)
    index = stages.index(source)
    if index + 1 >= len(stages):
        raise Refused(422, {"code": "last_stage"})
    return source, stages[index + 1]


def _move(release: Release, name: str, target: str) -> None:
    """The one writer of `releases.state` here: `name` must move the release's state to `target`. Stamps when it
    entered `queued` (`queued_at`) and when it first arrived on its target (`arrived_at`: shipped or live)."""
    lifecycle.check(name, release.state, target)
    release.state = target
    if target == lifecycle.QUEUED:
        release.queued_at = utcnow()
    elif target in (lifecycle.SHIPPED, lifecycle.LIVE) and release.arrived_at is None:
        release.arrived_at = utcnow()


def _queue(session: Session, project: Project, release: Release, requester_id: Optional[UUID]) -> None:
    """A release just became `queued`: make sure the executor will pick it up. One its soak or allowed hours still
    hold (R4.3, R5.2: accepted, it waits) files none yet: the tick files the executor once it may run."""
    from app.releases import judge  # noqa: PLC0415 — the judge imports this module

    target = session.scalar(select(ReleaseStage).where(ReleaseStage.id == release.to_stage_id)) \
        if release.to_stage_id is not None else None
    if target is not None and any(r.blocking for r in judge.timing_reasons(session, project, target, utcnow(),
                                                                           release)):
        return
    task_id = executor.ensure_executor(session, project, requester_id, for_release=True)
    if task_id is not None:
        release.task_id = task_id


def _insert(session: Session, project: Project, release: Release, changes: list[waiting_mod.Change]) -> None:
    """Write `release` and its change references."""
    session.add(release)
    session.flush()
    if changes:
        session.execute(insert(ReleaseChange), [
            {"release_id": release.id, "org_id": project.org_id, "merge_sha": c.merge_sha,
             "task_outcome_id": c.task_outcome_id, "merge_lane_row_id": c.merge_lane_row_id}
            for c in changes
        ])


def lock_target(session: Session, stage_id: UUID, *, skip_locked: bool = False) -> bool:
    """Serialise every request into (and claim of) one stage: a concurrent one waits for this transaction's commit,
    then sees what it wrote (R6, replacing the old one-open-release index's race guard). `skip_locked` (the claim,
    which already holds release rows a request may supersede): False instead of waiting, so the two never deadlock."""
    query = select(ReleaseStage.id).where(ReleaseStage.id == stage_id).with_for_update(skip_locked=skip_locked)
    return session.scalar(query) is not None


def _carried_shas(session: Session, releases: list[Release]) -> set[str]:
    if not releases:
        return set()
    return set(session.scalars(select(ReleaseChange.merge_sha).where(
        ReleaseChange.release_id.in_([r.id for r in releases]))))


def _supersede(session: Session, superseded: list[Release], by: Release) -> None:
    """Newest wins (R6.2): each waiting release `by` replaces is canceled "Superseded by #N"; its approvals stay its
    own (the new release starts with none)."""
    for old in superseded:
        _move(old, "cancel", lifecycle.CANCELED)
        old.cancellation = {"kind": "superseded", "by_release_id": by.id}
    session.flush()


def _answer(others: list[Release], kind: str) -> Release:
    """A press carrying nothing new answers the newest open release of its kind; one of another kind refuses."""
    same = [r for r in others if r.kind == kind]
    if not same:
        raise Refused(409, {"code": "open", "release_id": others[-1].id})
    return same[-1]


def _override_reason(principal: Optional[Principal], project: Project, target: ReleaseStage,
                     reason: Optional[str], field: str = "override_reason") -> Optional[str]:
    """The checked reason of a release override (`app/releases/override.py`), or None when none is asked."""
    try:
        return override_mod.reason_of(principal, project.id, reason,
                                      may_press=access.may_deploy(principal, project.id, target), field=field)
    except override_mod.OverrideRefused as exc:
        raise Refused(exc.status, exc.body) from None


def _apply_override(session: Session, project: Project, release: Release, target: ReleaseStage,
                    principal: Optional[Principal], reason: Optional[str], hit: Optional[FreezeHit], *,
                    fresh: bool = False) -> None:
    """Let `release` through the freeze `hit` on `reason` (R7.5): stored with its audit row, replacing an override of
    another freeze occasion. Nothing when no freeze holds it or its override already covers this one — unless `fresh`
    (a retry, a new run: its own override, its own audit row)."""
    if reason is None or hit is None or principal is None:
        return
    if not fresh and override_mod.covers(release.freeze_override, hit):
        return
    release.freeze_override = override_mod.record(session, project, target, principal, reason, hit,
                                                  release_id=release.id)


def _override_queue(session: Session, project: Project, target: ReleaseStage, principal: Optional[Principal],
                    reason: Optional[str], releases: list[Release]) -> None:
    """An override press lets every not-started release it waits behind through the freeze too (R7.5 with "In order",
    R6.2: the press's own release runs only after them); each gets its own override and audit row."""
    now = utcnow()
    for release in releases:
        if release.state not in lifecycle.RUNNING:
            _apply_override(session, project, release, target, principal, reason,
                            release_freeze(session, project, release, target, now))


def _request(
    session: Session, project: Project, principal: Optional[Principal], *, kind: str, source: ReleaseStage,
    target: ReleaseStage, stages: list[ReleaseStage], changes: list[waiting_mod.Change],
    scheduled_for: Optional[datetime], system: bool = False, override_reason: Optional[str] = None,
    inputs: Optional[dict[str, Any]] = None,
) -> tuple[Release, bool]:
    """`system`: the control plane files it (auto-deploy, `app/releases/tick.py`): nobody's access is asked. The
    release carries the step's settings as they are now (`snapshot.take`); `stages` are the branch-level stages.
    `override_reason`: run this one request through a freeze (R7.5); a second press with one lets the step's open
    release through."""
    if not system and not access.may_deploy(principal, project.id, target):
        raise Refused(403)
    reason = None if system else _override_reason(principal, project, target, override_reason)
    from app.releases import judge  # noqa: PLC0415 — judge reads this module's freeze judgement

    lock_target(session, target.id)
    # A back-merge never supersedes nor is superseded, and never answers a press.
    others = [r for r in open_releases_into(session, project.id, target.id) if r.kind != lifecycle.KIND_BACKMERGE]
    superseded: list[Release] = []
    if others:
        if {c.merge_sha for c in changes} <= _carried_shas(session, others):
            existing = _answer(others, kind)
            if inputs and existing.state in lifecycle.NOT_STARTED:
                existing.inputs = {**(existing.inputs or {}), **inputs}  # a corrected ticket, before it runs
            _override_queue(session, project, target, principal, reason, [r for r in others if r.id <= existing.id])
            return existing, False
        if target.concurrency != lifecycle.CONCURRENCY_IN_ORDER:
            waiting_ones = [r for r in others if r.state in lifecycle.NOT_STARTED]
            # Newest wins replaces waiting releases of its own kind only (a promote another promote); a waiting
            # hotfix or pick is never dropped for it.
            if any(r.kind != kind or kind == lifecycle.KIND_PICK for r in waiting_ones):
                raise Refused(409, {"code": "open", "release_id": waiting_ones[-1].id})
            superseded = waiting_ones
    if judge.downgrade(session, project, target.id, changes):
        raise Refused(409, {"code": "older_than_live"})
    now = utcnow()
    if scheduled_for is not None and scheduled_for.tzinfo is None:
        raise Refused(422, {"code": "timezone"})
    scheduled = scheduled_for is not None and scheduled_for > now
    hit = None
    if not scheduled:
        hit = freeze_on(session, project, target, now, pressed=not system and principal is not None)
        if hit is not None and reason is None:
            raise Refused(409, _frozen_body(hit))
    requester = principal.user_id if principal is not None else None
    state = lifecycle.route(approvals_required=target.approvals_required, approvals=0, scheduled=scheduled)
    lifecycle.check("request", lifecycle.REQUESTED, state)
    release = Release(
        org_id=project.org_id, project_id=project.id, kind=kind, from_stage_id=source.id, to_stage_id=target.id,
        state=state, requested_by_id=requester, requested_at=now, scheduled_for=scheduled_for if scheduled else None,
        settings=snapshot.take(project, source, target, stages, session), queued_at=now if state == lifecycle.QUEUED else None,
        inputs=inputs,
    )
    if superseded:
        # What the replaced releases carried waits again, so the new one carries it too (it merges the whole step).
        have = {c.merge_sha for c in changes}
        for old_changes in waiting_mod.carried(session, project, [r.id for r in superseded]).values():
            for change in old_changes:
                if change.merge_sha not in have:
                    have.add(change.merge_sha)
                    changes = [*changes, change]
    _insert(session, project, release, changes)
    _supersede(session, superseded, release)
    _apply_override(session, project, release, target, principal, reason, hit)
    if target.concurrency == lifecycle.CONCURRENCY_IN_ORDER:
        _override_queue(session, project, target, principal, reason, others)
    if release.state == lifecycle.QUEUED and release.task_id is None:
        _queue(session, project, release, requester)
    wake_tick(session)
    return release, True


def _frozen_body(hit: FreezeHit) -> dict[str, Any]:
    """409 `frozen`: until when (`null` under Freeze now, which stays on until switched off)."""
    return {"code": "frozen", "until": hit.until.isoformat() if hit.until is not None else None,
            "freeze_now": hit.freeze_now}


def wake_tick(session: Session) -> None:
    """Something moved that owes notices and schedule checks: the release tick runs once this commits."""
    from app.releases import tick

    tick.wake(session)


def deploy(session: Session, project: Project, principal: Optional[Principal], from_stage_id: Any,
           scheduled_for: Optional[datetime] = None, *, system: bool = False,
           change_ids: Optional[Iterable[UUID]] = None, override_reason: Optional[str] = None,
           ticket: Optional[str] = None, untested_reason: Optional[str] = None) -> tuple[Release, bool]:
    """Request the step `from_stage_id` → the next stage, carrying everything waiting on it; with `change_ids` short
    of all of it, only those (a pick, oldest merged first). A step whose ticket gate asks when pressed needs `ticket`
    (D8), kept on a new release as `inputs.ticket`; the gate checks it when the release runs. 409 `verifying` while
    the source still verifies. `untested_reason`: a whole promote that ships the source's head WITHOUT the full gate
    (`inputs.untested`, read by `runs._release_wire` and the merge lane's release batch); the same right and reason
    as a freeze override (`override.reason_of`), never with a pick."""
    stages = stages_mod.branch_stages(stages_mod.materialise(session, project))
    source, target = _step(stages, from_stage_id)
    untested = None if system else _override_reason(principal, project, target, untested_reason, "untested_reason")
    if untested is not None and change_ids is not None:
        raise Refused(422, {"code": "untested_pick"})
    from app.pipeline_gates import tickets  # noqa: PLC0415 — pipeline_gates imports the releases package

    asks = target.id in tickets.ask_gates(session, project.id)
    try:
        ticket = tickets.clean_key(ticket) if asks else None  # a step that asks nothing ignores one
    except ValueError:
        raise Refused(422, {"code": "ticket_invalid"}) from None
    if asks and ticket is None:
        # A press again (a freeze override, a newer release replacing a waiting one) keeps the open release's ticket.
        ticket = next((str(r.inputs["ticket"]) for r in reversed(open_releases_into(session, project.id, target.id))
                       if (r.inputs or {}).get("ticket")), None)
    if asks and ticket is None and not system:
        raise Refused(422, {"code": "ticket_required"})
    # D16 "Deploy waits for all": a release out of a no-branch stage (a sign-off) waits until every waiting change
    # passed into it (`app/pipeline/gate.py::signoff_missing`).
    from app.pipeline import gate as pipeline_gate  # noqa: PLC0415 — the gate imports this package

    missing = pipeline_gate.signoff_missing(session, project, target.id)
    if missing:
        raise Refused(409, {"code": "not_signed_off", "count": missing})
    # Verify after arrival (AC11.2): the step out of a stage that verifies opens once its head verified.
    from app.releases import arrivals  # noqa: PLC0415 — arrivals imports this module

    open_, reason = arrivals.step_open(session, project, source)
    if not open_:
        raise Refused(409, {"code": "verifying", "reason": reason})
    pending = waiting_mod.waiting(session, project, stages).get(source.id, [])
    kind, changes = lifecycle.KIND_PROMOTE, pending
    if change_ids is not None:
        picked = set(change_ids)
        if not picked:
            raise Refused(422, {"code": "no_changes"})
        if not picked <= {c.change_id for c in pending}:
            raise Refused(409, {"code": "state"})
        if len(picked) < len(pending):
            kind, changes = lifecycle.KIND_PICK, [c for c in reversed(pending) if c.change_id in picked]
            # An open release never answers a pick: its changes are not the ones asked for.
            existing = open_releases_into(session, project.id, target.id)
            if existing:
                raise Refused(409, {"code": "open", "release_id": existing[-1].id})
    release, created = _request(
        session, project, principal, kind=kind, source=source, target=target, stages=stages,
        changes=changes, scheduled_for=scheduled_for, system=system, override_reason=override_reason,
        inputs={"ticket": ticket} if asks and ticket is not None else None,
    )
    if untested is not None and principal is not None:
        release = session.scalar(select(Release).where(Release.id == release.id).with_for_update())
        approved = session.scalar(select(func.count()).select_from(ReleaseApproval)
                                  .where(ReleaseApproval.release_id == release.id))
        if release.kind != lifecycle.KIND_PROMOTE or release.state not in lifecycle.NOT_STARTED or approved:
            # A release already running pinned its commit and its gate, and an approval was given for a tested one:
            # neither drops its tests now.
            raise Refused(409, {"code": "open", "release_id": release.id})
        session.flush()
        stored = override_mod.record_untested(session, project, target, principal, untested, release.id)
        release.inputs = {**(release.inputs or {}), "untested": stored}
    return release, created


def hotfix(session: Session, project: Project, principal: Optional[Principal], change_id: UUID, *,
           override_reason: Optional[str] = None) -> tuple[Release, bool]:
    """Request one change straight from the integration stage to production."""
    stages = stages_mod.branch_stages(stages_mod.materialise(session, project))
    if len(stages) < 2:
        raise Refused(404)
    source, target = stages[0], stages[-1]
    change = waiting_mod.find_change(session, project, change_id)
    if change is None:
        raise Refused(404)
    if change.merge_sha in waiting_mod.arrived_shas(session, project.id, [target.id]):
        raise Refused(409, {"code": "already_live"})
    return _request(
        session, project, principal, kind=lifecycle.KIND_HOTFIX, source=source, target=target, stages=stages,
        changes=[change], scheduled_for=None, override_reason=override_reason,
    )


def _release(session: Session, project: Project, release_id: int, *, live: bool = False) -> tuple[Release, ReleaseStage]:
    """The release (locked: a concurrent approve, cancel or retry of the same release waits for this one's commit)
    and its target as it was requested (`snapshot.rule`), or as it is now (`live`, a retry: it runs again)."""
    release = session.scalar(
        select(Release).where(Release.id == release_id, Release.project_id == project.id).with_for_update()
    )
    if release is None:
        raise Refused(404)
    stage = session.scalar(select(ReleaseStage).where(ReleaseStage.id == release.to_stage_id)) \
        if release.to_stage_id is not None else None
    if not live:
        stage = snapshot.rule(release, stage)
    if stage is None:
        raise Refused(404)
    return release, stage


def approval_count(session: Session, release_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(ReleaseApproval).where(ReleaseApproval.release_id == release_id)
    ) or 0


def _may_approve(principal: Optional[Principal], project: Project, stage: ReleaseStage, release: Release) -> None:
    """403 unless `principal` may approve (or reject) `release`: `own_item` for its requester while R2 keeps them
    out, else `not_approver`."""
    if access.may_approve(principal, project.id, stage, release):
        return
    own = principal is not None and release.requested_by_id is not None \
        and str(principal.user_id) == str(release.requested_by_id) and access.may_approve_step(principal, project.id, stage)
    raise Refused(403, {"code": "own_item" if own else "not_approver"})


def approve(session: Session, project: Project, principal: Optional[Principal], release_id: int,
            comment: Optional[str] = None) -> Release:
    """Record the caller's approval (with its optional `comment`); the last one required routes the release on
    (schedule, else queue). Who may: `access.may_approve` on the rule it was requested under."""
    release, stage = _release(session, project, release_id)
    _may_approve(principal, project, stage, release)
    assert principal is not None
    if release.state != lifecycle.AWAITING_APPROVAL:
        raise Refused(409, {"code": "state", "state": release.state})
    try:
        with session.begin_nested():
            session.add(ReleaseApproval(release_id=release.id, org_id=release.org_id, user_id=principal.user_id,
                                        comment=comment or None))
            session.flush()
    except IntegrityError:
        pass  # Already approved by this person: the same answer.
    count = approval_count(session, release.id)
    scheduled = release.scheduled_for is not None and release.scheduled_for > utcnow()
    state = lifecycle.route(approvals_required=stage.approvals_required, approvals=count, scheduled=scheduled)
    if state != lifecycle.AWAITING_APPROVAL:
        _move(release, "approve", state)
        if state == lifecycle.QUEUED:
            _queue(session, project, release, release.requested_by_id)
        wake_tick(session)
    session.flush()
    return release


def cancel(session: Session, project: Project, principal: Optional[Principal], release_id: int) -> Release:
    release, stage = _release(session, project, release_id)
    if not access.may_cancel(principal, project.id, stage, release):
        raise Refused(403)
    if release.state == lifecycle.CANCELED:
        return release
    if release.state not in lifecycle.OPEN:
        raise Refused(409, {"code": "state", "state": release.state})
    _move(release, "cancel", lifecycle.CANCELED)
    release.cancellation = {"kind": "manual", "by_id": str(principal.user_id) if principal is not None else None}
    session.flush()
    return release


def reject(session: Session, project: Project, principal: Optional[Principal], release_id: int,
           comment: Optional[str] = None) -> Release:
    """An approver rejects a release waiting for approvals (R3.5): it is canceled, `cancellation{rejected}` naming
    them and their optional `comment`; the tick tells the requester (`notify.ended`)."""
    release, stage = _release(session, project, release_id)
    _may_approve(principal, project, stage, release)
    assert principal is not None
    if release.state != lifecycle.AWAITING_APPROVAL:
        raise Refused(409, {"code": "state", "state": release.state})
    _move(release, "cancel", lifecycle.CANCELED)
    release.cancellation = {"kind": "rejected", "by_id": str(principal.user_id), "comment": comment or None}
    wake_tick(session)
    session.flush()
    return release


def time_out(release: Release) -> None:
    """The approval timeout of a release waiting for approvals ran out under `cancel` (R3.2): it is canceled,
    `cancellation{timeout}`; the tick tells the requester (`notify.ended`)."""
    _move(release, "cancel", lifecycle.CANCELED)
    release.cancellation = {"kind": "timeout"}


def retry(session: Session, project: Project, principal: Optional[Principal], release_id: int, *,
          override_reason: Optional[str] = None) -> Release:
    """A failed release back to the queue (its approvals stand); 409 when its step already has another open one.
    `override_reason`: run it through a freeze (R7.5)."""
    release, stage = _release(session, project, release_id, live=True)
    if not access.may_deploy(principal, project.id, stage):
        raise Refused(403)
    reason = _override_reason(principal, project, stage, override_reason)
    if release.state != lifecycle.FAILED:
        raise Refused(409, {"code": "state", "state": release.state})
    # A retry is a new run: an earlier override never carries over to it (R7.5, one request).
    hit = freeze_on(session, project, stage, pressed=principal is not None)
    if hit is not None and reason is None:
        raise Refused(409, _frozen_body(hit))
    # A retry runs again, on the step's settings as they are now: both its stages must still be release stages.
    stages = stages_mod.branch_stages(stages_mod.stages_of(session, project.id))
    source = next((s for s in stages if s.id == release.from_stage_id), None)
    if source is None or stage not in stages:
        raise Refused(409, {"code": "stage_removed"})
    lock_target(session, stage.id)
    if open_releases_into(session, project.id, stage.id):
        raise Refused(409, {"code": "open"})
    release.settings = snapshot.take(project, source, stage, stages, session)
    _move(release, "retry", lifecycle.QUEUED)
    release.failure = None
    # A fresh pin and PR: the source branch may carry the fix for what failed.
    release.head_sha = release.pr_number = release.pr_url = None
    # Shipping untested was a decision about that run's head: a retry pins a fresh one, gated again unless a person
    # presses Release untested anew (its own reason and audit row).
    if override_mod.untested_of(release):
        release.inputs = {k: v for k, v in (release.inputs or {}).items() if k != "untested"} or None
    session.flush()
    _apply_override(session, project, release, stage, principal, reason, hit, fresh=True)
    requester = principal.user_id if principal is not None else None
    _queue(session, project, release, requester)
    session.flush()
    return release
