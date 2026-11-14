"""A release's settings at request time (`releases.settings`), so a Pipeline save never touches a running release:
the executor merges into the branches it was requested with, the merge lane adopts its PR against that base, and its
approvals follow the rule it was requested under. The next release reads the new settings.

`take` writes it when a release is requested; `rule` and `branches` read it, falling back to the live stage for a
release requested before the column existed (`settings` NULL). Its `gates` are the release step's gate list
(`GateWire`, `app/pipeline_gates/full_gate.py::wire`) as requested; a release without the key runs today's path
(`pr checks --watch`, the D14 default).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session, object_session

from app.models.projects import Project
from app.models.releases import Release, ReleaseStage
from app.pipeline_gates import defaults as gate_defaults, full_gate
from app.releases import stages as stages_mod

#: The step's rule columns (`app/pipeline/rules.py`) a snapshot carries, each with the value a snapshot taken before
#: the key existed reads as (today's behaviour).
RULE_DEFAULTS: dict[str, Any] = {
    "approvers": None, "requester_cant_approve": True, "approval_timeout_minutes": None, "on_timeout": "cancel",
    "approver_instructions": "", "soak_minutes": 0, "allowed_hours": None, "concurrency": "newest",
}


def take(project: Project, source: ReleaseStage, target: ReleaseStage, stages: list[ReleaseStage],
         session: Optional[Session] = None) -> dict[str, Any]:
    """The step's settings as `releases.settings` stores them; `stages` are the project's branch-level stages. With
    a session (`session`, else the project row's own), a release step's gates too (a release into the integration stage — a back-merge — has none: it runs
    today's path)."""
    out = {
        "from_branch": stages_mod.branch_of(project, source),
        "to_branch": stages_mod.branch_of(project, target),
        "deploy_environment": target.deploy_environment,
        "to_is_production": bool(stages) and target.id == stages[-1].id,
        "approvals_required": target.approvals_required,
        "deployers": target.deployers,
        "auto": target.auto_deploy,
        **{key: _stored(target, key) for key in RULE_DEFAULTS},
    }
    session = session or object_session(project)
    if session is not None and not stages_mod.is_integration(target):
        gates = gate_defaults.gates_of(session, project, stages).get((target.id, gate_defaults.BEFORE), [])
        out["gates"] = [full_gate.wire(g, project=project, stage=target.name, step=f"Release to {target.name}")
                        for g in gates]
    return out


def _stored(stage: ReleaseStage, key: str) -> Any:
    value = getattr(stage, key)
    return RULE_DEFAULTS[key] if value is None and RULE_DEFAULTS[key] is not None else value


def rule(release: Release, stage: Optional[ReleaseStage]) -> Optional[ReleaseStage]:
    """The target stage as the release was requested: an unsaved copy carrying the snapshot's approvals, who, auto
    and rules (`app/pipeline/rules.py::StepRules.of` reads it), or the live stage when the release has no snapshot. With a snapshot it stands in for a target removed
    since; None only without both. Never added to a session."""
    settings = release.settings
    if not settings:
        return stage
    return ReleaseStage(
        id=release.to_stage_id, org_id=release.org_id, project_id=release.project_id,
        position=stage.position if stage is not None else 0, name=stage.name if stage is not None else "",
        branch=stage.branch if stage is not None else settings.get("to_branch"), level=stages_mod.BRANCH,
        deploy_environment=settings.get("deploy_environment"),
        url=stage.url if stage is not None else None, url_source=stage.url_source if stage is not None else None,
        auto_deploy=bool(settings.get("auto")), approvals_required=int(settings.get("approvals_required") or 0),
        deployers=settings.get("deployers"),
        **{key: settings.get(key, default) for key, default in RULE_DEFAULTS.items()},
        resumed_at=stage.resumed_at if stage is not None else None,
    )


def branches(project: Project, release: Release, source: Optional[ReleaseStage],
             target: Optional[ReleaseStage]) -> Optional[tuple[str, str]]:
    """`(from_branch, to_branch)` the release runs between: the snapshot's, else the live stages'; None when it has
    no snapshot and a stage it names was removed."""
    settings = release.settings
    if settings and settings.get("from_branch") and settings.get("to_branch"):
        return str(settings["from_branch"]), str(settings["to_branch"])
    if source is None or target is None:
        return None
    return stages_mod.branch_of(project, source), stages_mod.branch_of(project, target)


def to_branch(project: Project, release: Release, target: Optional[ReleaseStage]) -> Optional[str]:
    """The branch the release merges into (its snapshot's, else its live target's)."""
    settings = release.settings
    if settings and settings.get("to_branch"):
        return str(settings["to_branch"])
    return stages_mod.branch_of(project, target) if target is not None else None
