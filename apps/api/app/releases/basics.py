"""A project's pipeline mode: one of the two basic pipelines every plan has, or a custom one (Customize › CI Pipeline).

`projects.pipeline_mode` is `pr_integration` ("PR into integration branch", the `trunk_prod` preset),
`integration_release` ("Integration + Release", the `standard` preset), `custom` (the Pipeline tab's full editor) or
`none`. A project with no repository has no basic to pick, whatever the column says (`effective_mode`). Which basic
a project's stages already are is decided once, by the release migration (revision 202611140800).

Custom pipelines are a plan feature (`plan_terms.PIPELINE_CUSTOM`). The full editor's writes (`editor_refusal`) need
both the switch and `custom` mode. An org that loses the switch keeps its custom pipelines running, read-only; only the
one-time release migration (revision 202611140800) ever reset one. Picking a basic rewrites the stages to its preset
through the one pipeline write (`basic_body`): each preset stage reuses the id of the current stage of the same role
(Ideas, Building, the integration stage, the release stage), else of the same name, so its release, pass and approval
history, gates and step rules stay.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import plan_terms
from app.models.projects import INTEGRATION_RELEASE, PIPELINE_MODES, Project, ProjectRepo
from app.models.releases import ReleaseStage
from app.releases import pipeline, pipeline_doc
from app.releases import stages as stages_mod

NONE, PR_INTEGRATION, _, CUSTOM = PIPELINE_MODES
#: Each basic and the preset it is (`pipeline_doc.PRESETS`).
PRESET_OF: dict[str, str] = {PR_INTEGRATION: "trunk_prod", INTEGRATION_RELEASE: pipeline_doc.STANDARD}
BASICS = tuple(PRESET_OF)

NOT_ON_PLAN = "Custom pipelines are not on your plan."
NOT_CUSTOM = "This project uses a basic pipeline. Tick Custom in Customize › CI Pipeline first."
NO_REPO = "Add a repository first."

def mode_of_preset(slug: str) -> str:
    """The mode a project started from preset `slug` is in: its basic, `none` for Ideas only, else `custom`."""
    if slug == pipeline_doc.IDEAS_ONLY:
        return NONE
    return next((mode for mode, preset in PRESET_OF.items() if preset == slug), CUSTOM)


def effective_mode(session: Session, project: Project) -> str:
    """The stored mode; `none` while the project has no repository. One that gained a repository while it held Ideas
    only (stored `none`) reads as the default basic, so picking one gives it its first branch stages."""
    if not stages_mod.has_repo(project):
        return NONE
    mode = project.pipeline_mode or INTEGRATION_RELEASE
    return INTEGRATION_RELEASE if mode == NONE else mode


def custom_offered(session: Session, project: Project) -> bool:
    """Whether the project's org has custom pipelines on its plan."""
    return plan_terms.switch_on(session, project.org_id, plan_terms.PIPELINE_CUSTOM)


def editor_refusal(session: Session, project: Project) -> Optional[str]:
    """Why the full editor may not write this project's pipeline (a 409's sentence), None when it may: the plan must
    have custom pipelines and the project must be in `custom` mode."""
    if not custom_offered(session, project):
        return NOT_ON_PLAN
    if project.pipeline_mode != CUSTOM:
        return NOT_CUSTOM
    return None


def require_editor(session: Session, project: Project) -> None:
    """409 with `editor_refusal`'s sentence: every full-editor write's check (the suite opens the editor for its
    fixtures by replacing `editor_refusal`: `tests/conftest.py::_full_pipeline_editor_open`)."""
    refusal = editor_refusal(session, project)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)


def _repos(session: Session, project: Project) -> list[dict[str, Any]]:
    rows = list(session.scalars(select(ProjectRepo).where(ProjectRepo.project_id == project.id)
                                .order_by(ProjectRepo.position, ProjectRepo.id)))
    if rows:
        return [{"repo_id": r.id, "name": r.name, "branch": r.integration_branch} for r in rows]
    if not stages_mod.has_repo(project):
        return []
    return [{"repo_id": None, "name": project.repo_url, "branch": project.integration_branch}]


def out(session: Session, project: Project) -> dict[str, Any]:
    """`GET /api/projects/{id}/pipeline/basic`. Addresses are keyed by stage role, so they survive a switch of basic:
    `dev` the integration stage's while a release stage follows it, `production` the release stage's (or, with none,
    the integration stage's)."""
    mode = effective_mode(session, project)
    offered = custom_offered(session, project)
    stages = pipeline.stages_for(session, project)
    integration = stages_mod.integration_stage(stages)
    production = stages_mod.production_stage(stages)
    live = production if production is not None else integration
    return {
        "mode": mode,
        "custom_offered": offered,
        "read_only": mode == CUSTOM and not offered,
        "integration_branches": _repos(session, project),
        "release_branch": production.branch if production is not None else None,
        "addresses": {
            "dev": integration.url if integration is not None and production is not None else None,
            "production": live.url if live is not None else None,
        },
        "reset_at": project.pipeline_reset_at.isoformat() if project.pipeline_reset_at is not None else None,
    }


def _role(levels: list[str], index: int) -> str:
    level = levels[index]
    if level != stages_mod.BRANCH:
        return level
    first = levels.index(stages_mod.BRANCH)
    return "integration" if index == first else "release"


def basic_body(session: Session, project: Project, mode: str, release_branch: Optional[str],
               addresses: dict[str, Optional[str]]) -> dict[str, Any]:
    """The save body (`routes/releases.py::PipelineIn`'s shape) that makes the project's pipeline basic `mode`:
    `pipeline_doc.instantiate` of its preset, each preset stage taking the id of the current stage of the same role,
    else of the same name; the release stage's branch `release_branch` (else its current one, else `main`); `addresses` sent
    keys written onto the matching stage (None clears it). A step into a kept stage keeps its rules (who may press,
    approvals, approvers, timing, concurrency)."""
    doc = copy.deepcopy(pipeline_doc.PRESETS[PRESET_OF[mode]])
    current = pipeline.stages_for(session, project)
    production = stages_mod.production_stage(current)
    levels = pipeline_doc.levels_of(doc)
    roles = [_role(levels, i) for i in range(len(levels))]
    if mode == INTEGRATION_RELEASE:
        chosen = (release_branch or "").strip() or (production.branch if production is not None else "") \
            or stages_mod.PRODUCTION_BRANCH_DEFAULT
        doc["stages"][roles.index("release")]["branch"] = chosen
    body = pipeline_doc.instantiate(doc, project, current)
    # Each preset stage takes the current stage of its ROLE first (the merge stage stays the merge stage, with its
    # history, full gate and target), then of its name; a stage taken keeps its address.
    current_levels = [s.level for s in current]
    current_roles = [_role(current_levels, i) for i in range(len(current))]
    by_role: dict[str, ReleaseStage] = {}
    for stage, role in zip(current, current_roles, strict=True):
        if role == "release" or role not in by_role:
            by_role[role] = stage  # the first Ideas and Building, the last release stage
    role_of = {str(stage.id): role for stage, role in zip(current, current_roles, strict=True)}
    by_name = {s.name.strip().lower(): s for s in current}
    for stage in body["stages"]:
        stage.pop("id", None)
        stage.pop("url", None)
    taken: set[str] = set()
    for pick in (lambda st, role: by_role.get(role), lambda st, role: by_name.get(st["name"].strip().lower())):
        for stage, role in zip(body["stages"], roles, strict=True):
            found = pick(stage, role)
            if stage.get("id") is None and found is not None and str(found.id) not in taken:
                stage["id"] = str(found.id)
                taken.add(stage["id"])
    for stage in body["stages"]:
        if stage.get("id") is None:
            stage["url"] = None
    # The step into each kept stage keeps its rules (a basic never quietly drops an approval), and whether it runs by
    # itself too while the stage keeps its role.
    shown = pipeline_doc.from_project(session, project)
    rules = {str(stage["key"]): step for stage, step in zip(shown["stages"][1:], shown["steps"], strict=True)}
    for index, (stage, role) in enumerate(zip(body["stages"][1:], roles[1:], strict=True)):
        kept = rules.get(str(stage.get("id")))
        if kept is not None:
            same_role = role_of.get(str(stage.get("id"))) == role
            body["steps"][index] = {**{k: v for k, v in kept.items() if k != "locks"},
                                    "auto": kept["auto"] if same_role else body["steps"][index]["auto"]}
    live = {"integration": "dev" if mode == INTEGRATION_RELEASE else "production", "release": "production"}
    for stage, role in zip(body["stages"], roles, strict=True):
        key = live.get(role)
        if key is not None and key in addresses:
            stage["url"] = (addresses[key] or "").strip() or None
    return body
