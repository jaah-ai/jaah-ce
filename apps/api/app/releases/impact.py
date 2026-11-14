"""What an unsaved pipeline draft would change for work already on it (`POST …/pipeline/preview`): the impact preview
Project settings › Pipeline shows before Save. Read only, never writes.

Two kinds of fact, each a structured row the page words (`apps/web/src/lib/pipeline.ts::impactText`):

- **Items**, per stored stage the board places items on (`app/pipeline/board.py::Board.counts`, as the caller sees
  it): the next step those items face, before and after. A stage removed (`stage_removed`); a different stage next
  (`next_stage`); that step's approvals (`approvals`), by hand or by itself (`auto`), or who may do it (`who`).
- **Releases**: an open release with a future `scheduled_for` into a stage freezes cover (every stage but the
  integration stage, `service.freeze_on`) that the draft's freezes would now hold (`release_frozen`) or no
  longer hold (`release_unfrozen`).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.principal import Principal
from app.clock import utcnow
from app.models.projects import Project
from app.models.releases import Release
from app.releases import freezes as freezes_mod, history, lifecycle, pipeline, service, stages as stages_mod
from app.releases.freezes import Window


def items_by_stage(session: Session, project: Project, principal: Optional[Principal]) -> dict[UUID, int]:
    from app.pipeline.board import Board  # noqa: PLC0415 — the board imports the release modules

    counts = Board.load(session, project, principal).counts()
    return {k: v for k, v in counts.items() if isinstance(k, UUID) and v}


def _step_rule(step: Optional[dict[str, Any]]) -> Optional[tuple[Any, int, bool]]:
    return None if step is None else (step["who"], step["approvals"], bool(step["auto"]))


def items(session: Session, project: Project, principal: Optional[Principal], shown: dict[str, Any],
          specs: Sequence[pipeline.StageSpec], steps: Sequence[pipeline.StepSpec]) -> list[dict[str, Any]]:
    """The item rows (module docstring). `shown` is `pipeline.read` of the stored setup."""
    counts = items_by_stage(session, project, principal)
    if not counts:
        return []
    old_stages = shown["stages"]
    old_index = {s["id"]: i for i, s in enumerate(old_stages)}
    new_index = {str(spec.id): i for i, spec in enumerate(specs) if spec.id is not None}
    levels = pipeline.validate(specs, steps)
    out: list[dict[str, Any]] = []
    for stage in old_stages:
        count = counts.get(UUID(stage["id"]), 0)
        if not count:
            continue
        base = {"stage": stage["name"], "count": count}
        if stage["id"] not in new_index:
            out.append({"kind": "stage_removed", **base})
            continue
        i_old, i_new = old_index[stage["id"]], new_index[stage["id"]]
        base["stage"] = specs[i_new].name.strip()
        old_next = old_stages[i_old + 1] if i_old + 1 < len(old_stages) else None
        new_next = specs[i_new + 1] if i_new + 1 < len(specs) else None
        if new_next is None:
            continue  # the last stage: nothing comes after it
        if old_next is None or str(new_next.id) != old_next["id"]:
            out.append({"kind": "next_stage", **base, "to": new_next.name.strip()})
            continue
        was = _step_rule(shown["steps"][i_old])
        step = steps[i_new]
        action = pipeline.action_at(levels, i_new + 1)
        row = {**base, "to": new_next.name.strip(), "action": action}
        assert was is not None
        if was[1] != step.approvals:
            out.append({"kind": "approvals", **row, "before": was[1], "after": step.approvals})
        if was[2] != step.auto:
            out.append({"kind": "auto", **row, "after": step.auto})
        if not history.same_who(was[0], step.who):
            out.append({"kind": "who", **row})
    return out


def releases(session: Session, project: Project, old_windows: Sequence[Window],
             new_freezes: Optional[Sequence[tuple[str, Window]]]) -> list[dict[str, Any]]:
    """The release rows (module docstring); `new_freezes` None: the draft keeps the freezes as stored."""
    if new_freezes is None:
        return []
    now = utcnow()
    stages = {s.id: s for s in stages_mod.for_read(session, project)}
    out: list[dict[str, Any]] = []
    for release in session.scalars(
        select(Release).where(Release.project_id == project.id, Release.state.in_(lifecycle.OPEN),
                              Release.scheduled_for.is_not(None), Release.scheduled_for > now)
        .order_by(Release.scheduled_for)
    ):
        stage = stages.get(release.to_stage_id) if release.to_stage_id else None
        if stage is None or stages_mod.is_integration(stage):
            continue
        at: datetime = release.scheduled_for  # type: ignore[assignment]
        was = freezes_mod.frozen_until(old_windows, at) is not None
        covering = [name for name, window in new_freezes if freezes_mod.frozen_until([window], at) is not None]
        row = {"stage": stage.name, "at": at.isoformat(), "release_id": release.id}
        if covering and not was:
            out.append({"kind": "release_frozen", **row, "freeze": covering[0]})
        elif was and not covering:
            out.append({"kind": "release_unfrozen", **row})
    return out


def preview(session: Session, project: Project, principal: Optional[Principal], specs: Sequence[pipeline.StageSpec],
            steps: Sequence[pipeline.StepSpec], new_freezes: Optional[Sequence[tuple[str, Window]]]) -> list[dict[str, Any]]:
    """Every impact row of the draft: items first, then releases."""
    shown = pipeline.read(session, project)
    return [*items(session, project, principal, shown, specs, steps),
            *releases(session, project, service.freeze_windows(session, project.id), new_freezes)]
