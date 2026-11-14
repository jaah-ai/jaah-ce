"""What is waiting to move from each stage to the next — a set difference, since the control plane has no checkout.

`on(0)` is every change merged into the integration branch: each runner task's merge (`task_outcomes` with a
`merge_sha`) plus each merge-lane merge no outcome records (a non-runner PR). A change is `on(k)` for k ≥ 1 when a
`live`/`shipped` release to stage k or later carries it (`release_changes`), or a node observed it on such a stage's
branch (`release_contained`) — a change on Production is on every stage before it, so a hotfix counts as on
Production. A release a roll back undid carries nothing any more: what it carried waits again before its step.
`waiting(k → k+1) = on(k) − on(k+1) − flight(k+1)`, where `flight(k+1)` is what an open release
(queued, deploying, awaiting approval…) to stage k+1 or later carries: that release's own row lists it, and a failed
or canceled one returns it to waiting. The release executor's own merges (its `release`-persona task's
release and back-merge PRs) are not changes.

Each change is a reference to its owner row (`change_id` is that row's id); title, author and PR are read from there.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app.models.merge_lane import MergeLaneRow
from app.models.outcomes import TaskOutcome
from app.models.projects import Project
from app.models.releases import Release, ReleaseChange, ReleaseContained, ReleaseStage
from app.models.tasks import Task
from app.releases.lifecycle import ARRIVED, OPEN

#: The executor's persona: its PRs (release/*, back-merges) are never changes.
RELEASE_PERSONA = "release"


@dataclass(frozen=True)
class Change:
    change_id: UUID
    merge_sha: str
    title: str
    author_id: Optional[UUID]
    merged_at: Optional[datetime]
    task_id: Optional[int]
    pr_url: Optional[str]
    task_outcome_id: Optional[UUID]
    merge_lane_row_id: Optional[UUID]


def merged_changes(session: Session, project: Project, *, shas: Optional[Collection[str]] = None,
                   not_on: Optional[UUID] = None) -> list[Change]:
    """`on(0)`: every change merged into the integration branch, newest first, one per merge sha. `shas`: only the
    changes with these merge shas. `not_on`: without the changes already on that stage — a step list's last stage,
    whose changes wait on no step (`waiting`). Reads only the columns a `Change` holds."""

    def narrowed(sha: Any) -> list[Any]:
        out = []
        if shas is not None:
            out.append(sha.in_(list(shas)))
        if not_on is not None:
            out.append(~_on_stage(project.id, not_on, sha))
        return out

    outcomes = session.execute(
        select(TaskOutcome.id, TaskOutcome.merge_sha, TaskOutcome.task_title, TaskOutcome.merged_at,
               TaskOutcome.task_id, TaskOutcome.pr_url, Task.started_by_id, Task.created_by_id)
        .outerjoin(Task, Task.id == TaskOutcome.task_id)
        .where(
            TaskOutcome.project_id == project.id,
            TaskOutcome.merge_sha.is_not(None),
            TaskOutcome.merged_at.is_not(None),
            TaskOutcome.shadow.is_(False),
            TaskOutcome.persona != RELEASE_PERSONA,
            *narrowed(TaskOutcome.merge_sha),
        )
        .order_by(TaskOutcome.merged_at, TaskOutcome.id)
    ).all()
    by_sha: dict[str, Change] = {}
    for outcome_id, merge_sha, title, merged_at, task_id, pr_url, started_by, created_by in outcomes:
        by_sha.setdefault(merge_sha, Change(
            change_id=outcome_id, merge_sha=merge_sha, title=title, author_id=started_by or created_by,
            merged_at=merged_at, task_id=task_id, pr_url=pr_url, task_outcome_id=outcome_id, merge_lane_row_id=None,
        ))
    rows = session.execute(
        select(MergeLaneRow.id, MergeLaneRow.merge_sha, MergeLaneRow.merged_at, MergeLaneRow.updated_at,
               MergeLaneRow.task_id, MergeLaneRow.pr_url, Task.title, Task.started_by_id, Task.created_by_id,
               Task.persona)
        .join(Task, Task.id == MergeLaneRow.task_id)
        .where(
            MergeLaneRow.project_id == project.id,
            MergeLaneRow.state == "merged",
            MergeLaneRow.merge_sha.is_not(None),
            MergeLaneRow.base_branch == project.integration_branch,
            *narrowed(MergeLaneRow.merge_sha),
        )
        .order_by(MergeLaneRow.merged_at, MergeLaneRow.created_at)
    ).all()
    for row_id, merge_sha, merged_at, updated_at, task_id, pr_url, title, started_by, created_by, persona in rows:
        if persona == RELEASE_PERSONA or merge_sha in by_sha:
            continue
        by_sha[merge_sha] = Change(
            change_id=row_id, merge_sha=merge_sha, title=title, author_id=started_by or created_by,
            merged_at=merged_at or updated_at, task_id=task_id, pr_url=pr_url,
            task_outcome_id=None, merge_lane_row_id=row_id,
        )
    return sorted(by_sha.values(), key=lambda c: (c.merged_at is not None, c.merged_at), reverse=True)


def _on_stage(project_id: int, stage_id: UUID, sha: Any) -> Any:
    """`sha` is on `stage_id`, as `arrived_shas` reads it: carried by a live/shipped release to it, or observed on
    its branch."""
    return or_(
        exists().where(
            ReleaseChange.merge_sha == sha, Release.id == ReleaseChange.release_id, Release.project_id == project_id,
            Release.to_stage_id == stage_id, Release.state.in_(tuple(ARRIVED)), _not_rolled_back(),
        ),
        exists().where(
            ReleaseContained.project_id == project_id, ReleaseContained.stage_id == stage_id,
            ReleaseContained.merge_sha == sha,
        ),
    )


def _not_rolled_back() -> Any:
    """A release a roll back undid carries nothing onto its stage any more (`arrivals.rolled_back_release_ids`)."""
    from app.releases.arrivals import rolled_back_release_ids  # noqa: PLC0415 — arrivals imports this package

    return Release.id.not_in(rolled_back_release_ids())


def arrived_shas(session: Session, project_id: int, stage_ids: Iterable[UUID]) -> set[str]:
    """The merge shas on any of `stage_ids`: carried by a live/shipped release to one, or observed on its branch."""
    ids = list(stage_ids)
    if not ids:
        return set()
    carried = session.scalars(
        select(ReleaseChange.merge_sha)
        .join(Release, Release.id == ReleaseChange.release_id)
        .where(Release.project_id == project_id, Release.to_stage_id.in_(ids), Release.state.in_(tuple(ARRIVED)),
               _not_rolled_back())
    )
    observed = session.scalars(select(ReleaseContained.merge_sha).where(
        ReleaseContained.project_id == project_id, ReleaseContained.stage_id.in_(ids)))
    return set(carried) | set(observed)


def in_flight(session: Session, project_id: int) -> dict[UUID, set[str]]:
    """`{to_stage_id: shas}` the open releases carry, by target stage."""
    out: dict[UUID, set[str]] = {}
    for to_stage_id, sha in session.execute(
        select(Release.to_stage_id, ReleaseChange.merge_sha)
        .join(ReleaseChange, ReleaseChange.release_id == Release.id)
        .where(Release.project_id == project_id, Release.state.in_(OPEN), Release.to_stage_id.is_not(None))
    ):
        out.setdefault(to_stage_id, set()).add(sha)
    return out


def waiting(session: Session, project: Project, stages: list[ReleaseStage],
            changes: Optional[list[Change]] = None, *, less_in_flight: bool = True) -> dict[UUID, list[Change]]:
    """`{from_stage_id: [Change]}` for every step k → k+1, newest first. `less_in_flight=False`: what an open release
    carries still counts as waiting (the tick's notices and survey, which measure the step, not the screen)."""
    if len(stages) < 2:
        return {}
    # A change on the last stage waits on no step (it is in every `on(k + 1)`): left out in SQL, not after the read.
    changes = merged_changes(session, project, not_on=stages[-1].id) if changes is None else changes
    # on(k) for k ≥ 1, as the shas on stage k or any later one.
    on: list[Optional[set[str]]] = [None]
    for k in range(1, len(stages)):
        on.append(arrived_shas(session, project.id, [s.id for s in stages[k:]]))
    flight = in_flight(session, project.id) if less_in_flight else {}
    out: dict[UUID, list[Change]] = {}
    for k in range(len(stages) - 1):
        source, target = on[k], on[k + 1]
        assert target is not None
        target = target.union(*(flight.get(s.id, set()) for s in stages[k + 1:]))
        out[stages[k].id] = [
            c for c in changes if (source is None or c.merge_sha in source) and c.merge_sha not in target
        ]
    return out


def find_change(session: Session, project: Project, change_id: UUID) -> Optional[Change]:
    """One of `on(0)` by its id: read through its owner row's merge sha."""
    sha = session.scalar(select(TaskOutcome.merge_sha).where(
        TaskOutcome.id == change_id, TaskOutcome.project_id == project.id)) or session.scalar(
        select(MergeLaneRow.merge_sha).where(MergeLaneRow.id == change_id, MergeLaneRow.project_id == project.id))
    if sha is None:
        return None
    return next((c for c in merged_changes(session, project, shas=[sha]) if c.change_id == change_id), None)


def carried(session: Session, project: Project, release_ids: Iterable[int],
            changes: Optional[list[Change]] = None) -> dict[int, list[Change]]:
    """The changes each release carries, resolved through their owner rows (`changes`: `on(0)`, when read)."""
    ids = list(release_ids)
    if not ids:
        return {}
    refs = list(session.scalars(
        select(ReleaseChange).where(and_(ReleaseChange.release_id.in_(ids))).order_by(ReleaseChange.id)
    ))
    if changes is None:
        changes = merged_changes(session, project, shas={ref.merge_sha for ref in refs}) if refs else []
    by_sha = {c.merge_sha: c for c in changes}
    out: dict[int, list[Change]] = {i: [] for i in ids}
    for ref in refs:
        change = by_sha.get(ref.merge_sha)
        if change is not None:
            out[ref.release_id].append(change)
    return out
