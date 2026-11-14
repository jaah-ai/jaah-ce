"""Org pipeline templates: one shared pipeline an org's projects follow (Settings › Pipeline templates).

A template (`app/models/releases.py::PipelineTemplate`) holds an editable `draft` — a pipeline document
(`app/releases/pipeline_doc.py`) whose stages and steps carry lock marks — and the versions published from it
(`PipelineTemplateVersion`, never edited). State: never published (no version), published (the draft equals the
published version), or draft changes. Saving the draft writes the template row only: no project ever reads a draft.

Lock marks (`locks`, true = a linked project may not change it): per stage Branch and Address, per step Who,
Approvals and Auto; a new stage starts Branch and Address open, a new step Who, Approvals and Auto locked. A stage on
each project's work branches (`"*"`) or integration branch (`"$integration"`) always takes the project's own, so its
Branch carries no lock.

Publishing checks the draft (`pipeline_doc.check_doc`) and the org's Block rules on the template itself
(`problems`): either refuses with nothing written. Otherwise it stores version n + 1, points the template at it, and
then applies it to each linked project (`linking.write_version`, through the one pipeline write), each in its own
transaction; a project the write refuses stays on its version and is listed with the reason. `apply_lagging` re-runs
that for the linked projects still on an older version ("Update again"): the version id on each project is the
durable state, so a publish cut short is finished the same way. Project-side rules: `app/releases/linking.py`.
"""
from __future__ import annotations

import copy
import logging
import uuid
from typing import Any, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access_catalog import ROLES_BY_SLUG
from app.actors import stamp_actor
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import PipelineTemplate, PipelineTemplateVersion
from app.releases import basics, guardrails, linking, pipeline, pipeline_doc

log = logging.getLogger(__name__)

NEVER_PUBLISHED, PUBLISHED, DRAFT_CHANGES = "never_published", "published", "draft_changes"

#: A new stage's and step's lock marks: a project keeps its own branch and address, the template sets the step.
STAGE_LOCKS_DEFAULT = {"branch": False, "url": False}
STEP_LOCKS_DEFAULT = {"who": True, "approvals": True, "auto": True}
_EMPTY: dict[str, Any] = {"stages": [], "steps": []}


def normalise(doc: dict[str, Any]) -> dict[str, Any]:
    """`doc` in the stored shape: every stage keyed (a new one gets a fresh key) and every lock present (absent: the
    default); a work or integration stage's Branch never locked; only the document's own fields kept."""
    stages = []
    for stage in doc.get("stages") or []:
        branch = (stage.get("branch") or "").strip()
        locks = {**STAGE_LOCKS_DEFAULT, **{k: bool(v) for k, v in (stage.get("locks") or {}).items()
                                           if k in STAGE_LOCKS_DEFAULT}}
        if branch in (pipeline_doc.WORK, pipeline_doc.INTEGRATION):
            locks["branch"] = False
        url = (stage.get("url") or "").strip() or None
        stages.append({"key": str(stage.get("key") or uuid.uuid4()), "name": str(stage.get("name") or "").strip(),
                       "branch": branch, "url": url, "locks": locks})
    steps = []
    for step in doc.get("steps") or []:
        locks = {**STEP_LOCKS_DEFAULT, **{k: bool(v) for k, v in (step.get("locks") or {}).items()
                                          if k in STEP_LOCKS_DEFAULT}}
        steps.append({"who": step.get("who"), "approvals": int(step.get("approvals") or 0),
                      "auto": bool(step.get("auto")), "locks": locks})
    return {"stages": stages, "steps": steps}


def check_roles(doc: dict[str, Any]) -> None:
    """A step's Who names known roles only, never people (a template is org-wide), else 422 on that step's stage: a
    bad one would otherwise publish and then be refused by every linked project."""
    for index, step in enumerate(doc["steps"], start=1):
        who = step.get("who") or {}
        if who.get("users"):
            raise pipeline.refuse("template_users", f"{doc['stages'][index]['name']}: a template names roles, not "
                                  "people.", index)
        unknown = [r for r in (who.get("roles") or []) if r not in ROLES_BY_SLUG]
        if unknown:
            raise pipeline.refuse("role_unknown", f"{doc['stages'][index]['name']}: unknown role {unknown[0]}.", index)


def starting_draft() -> dict[str, Any]:
    """A new template's draft: the Standard preset (today's default pipeline), freshly keyed, default lock marks."""
    doc = copy.deepcopy(pipeline_doc.PRESETS[pipeline_doc.STANDARD])
    for stage in doc["stages"]:
        stage.pop("key", None)
        stage.pop("locks", None)
    for step in doc["steps"]:
        step.pop("locks", None)
    return normalise(doc)


def published(session: Session, template: PipelineTemplate) -> Optional[PipelineTemplateVersion]:
    """The version linked projects follow, or None when never published."""
    if template.published_version_id is None:
        return None
    return session.scalar(select(PipelineTemplateVersion).where(
        PipelineTemplateVersion.id == template.published_version_id,
        PipelineTemplateVersion.org_id == template.org_id))


def state(template: PipelineTemplate, version: Optional[PipelineTemplateVersion]) -> str:
    if version is None:
        return NEVER_PUBLISHED
    return PUBLISHED if version.body == template.draft else DRAFT_CHANGES


def linked_projects(session: Session, template: PipelineTemplate) -> list[Project]:
    """The projects linked to `template`, by name."""
    return list(session.scalars(select(Project).where(Project.pipeline_template_id == template.id,
                                                      Project.org_id == template.org_id)
                                .order_by(func.lower(Project.name), Project.id)))


def linked_out(session: Session, template: PipelineTemplate) -> list[dict[str, Any]]:
    """Each linked project with the version number it is on and whether that is the published one."""
    projects = linked_projects(session, template)
    numbers = dict(session.execute(select(PipelineTemplateVersion.id, PipelineTemplateVersion.number).where(
        PipelineTemplateVersion.template_id == template.id)).tuples().all())
    return [{"project_id": p.id, "name": p.name, "version_number": numbers.get(p.pipeline_template_version_id),
             "up_to_date": p.pipeline_template_version_id == template.published_version_id} for p in projects]


def lagging_count(session: Session, template: PipelineTemplate) -> int:
    """How many linked projects are not on the published version."""
    return sum(1 for p in linked_projects(session, template)
               if p.pipeline_template_version_id != template.published_version_id)


def linked_count(session: Session, template: PipelineTemplate) -> int:
    """How many projects are linked to `template`: what a delete is refused over."""
    return len(linked_projects(session, template))


def steps_of(doc: dict[str, Any]) -> list[pipeline.StepSpec]:
    return [pipeline.StepSpec(step.get("who"), int(step.get("approvals") or 0), bool(step.get("auto")))
            for step in doc["steps"]]


def problems(session: Session, org_id: Any, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The org's rules set to Block that the template itself breaks, `{code: "guardrail", rule, message,
    stage_index}` each: a template breaking one cannot be published."""
    rules = [r for r in guardrails.enabled(session, org_id) if r.mode == guardrails.BLOCK]
    found = guardrails.evaluate(rules, [s["name"] for s in doc["stages"]], pipeline_doc.levels_of(doc), steps_of(doc))
    return [{"code": "guardrail", **f.out()} for f in found]


def changes(session: Session, template: PipelineTemplate) -> list[pipeline_doc.Change]:
    """What publishing would change: the draft against the published version (against nothing when never
    published, so every stage reads as added)."""
    version = published(session, template)
    return pipeline_doc.diff(version.body if version is not None else _EMPTY, template.draft)


def preview(session: Session, template: PipelineTemplate) -> dict[str, Any]:
    """`GET …/publish-preview`: the changes, the linked projects the publish would update, and what refuses it."""
    return {
        "changes": changes(session, template),
        "linked": [{"project_id": p.id, "name": p.name} for p in linked_projects(session, template)],
        "problems": problems(session, template.org_id, template.draft),
    }


def publish(session: Session, template: PipelineTemplate, actor: AdminUser, principal: Any) -> dict[str, Any]:
    """Store the draft as the next version and apply it to every linked project (module docstring). Refused with
    nothing written: an invalid draft (422, `check_doc`), a draft breaking a Block rule (422 `guardrail`), or a draft
    equal to the published version (409 `nothing_to_publish`). Returns `{version, updated, not_updated}`."""
    pipeline_doc.check_doc(template.draft)
    found = problems(session, template.org_id, template.draft)
    if found:
        first = found[0]
        raise pipeline.refuse("guardrail", first["message"], first["stage_index"], rule=first["rule"])
    current = published(session, template)
    if current is not None and current.body == template.draft:
        raise HTTPException(status_code=409, detail={
            "code": "nothing_to_publish", "message": "Nothing changed since the last publish.",
        })
    number = (session.scalar(select(func.max(PipelineTemplateVersion.number))
                             .where(PipelineTemplateVersion.template_id == template.id)) or 0) + 1
    version = PipelineTemplateVersion(org_id=template.org_id, template_id=template.id, number=number,
                                      body=copy.deepcopy(template.draft), created_by_id=actor.id)
    session.add(version)
    session.flush()
    template.published_version_id = version.id
    stamp_actor(template, actor)
    session.commit()
    updated, not_updated = apply(session, version, linked_projects(session, template), actor, principal)
    return {"version": number, "updated": updated, "not_updated": not_updated}


def apply(session: Session, version: PipelineTemplateVersion, projects: Sequence[Project], actor: AdminUser,
          principal: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Write `version` to each project (`linking.write_version`), one transaction each: a refusal leaves that project
    on its version and lists it with the reason; an updated one carries the org rules set to Warn it breaks."""
    updated: list[dict[str, Any]] = []
    not_updated: list[dict[str, Any]] = []
    for project in projects:
        project_id, name = project.id, project.name
        # A basic project, or a custom one whose org lost the plan, takes no template (`basics.editor_refusal`).
        refusal = basics.editor_refusal(session, project)
        if refusal is not None:
            not_updated.append({"project_id": project_id, "name": name, "reason": refusal})
            continue
        try:
            warnings = linking.write_version(session, project, version, actor, principal, publish=True)
        except HTTPException as exc:
            session.rollback()
            detail = exc.detail
            reason = detail.get("message") if isinstance(detail, dict) else str(detail)
            not_updated.append({"project_id": project_id, "name": name, "reason": reason})
            continue
        except Exception:  # noqa: BLE001 — one project's failure never stops the others; Update again retries it
            session.rollback()
            log.exception("pipeline template %s: applying version %s to project %s failed", version.template_id,
                          version.number, project_id)
            not_updated.append({"project_id": project_id, "name": name,
                                "reason": "Something went wrong. Try Update again."})
            continue
        updated.append({"project_id": project_id, "name": name, "warnings": [f.out() for f in warnings]})
    return updated, not_updated


def apply_lagging(session: Session, template: PipelineTemplate, actor: AdminUser, principal: Any) -> dict[str, Any]:
    """`POST …/apply` ("Update again"): the published version to every linked project not on it yet. 409
    `never_published` before the first publish. Returns `{version, updated, not_updated}` as a publish does."""
    version = published(session, template)
    if version is None:
        raise HTTPException(status_code=409, detail={
            "code": "never_published", "message": "Publish this template first.",
        })
    lagging = [p for p in linked_projects(session, template) if p.pipeline_template_version_id != version.id]
    number = version.number
    updated, not_updated = apply(session, version, lagging, actor, principal)
    return {"version": number, "updated": updated, "not_updated": not_updated}
