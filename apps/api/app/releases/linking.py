"""A project linked to an org pipeline template (Project settings › Pipeline › Link to template…).

A linked project (`projects.pipeline_template_id`) is on one published version (`pipeline_template_version_id`) of
its template (`app/releases/templates.py`) and holds that version's stages, in its order, under its names. Each field
the version locks (per stage Branch and Address, per step Who, Approvals and Auto) holds the version's value; every
other field is the project's own. Whether an open field is changed here is derived (project value ≠ version value),
never stored.

- **Link** (`write_version`, no previous version) replaces the pipeline with the version: locked fields take its
  values, open fields keep the project's own on a stage it already had. A stage is the project's current stage with
  the same template stage key (`release_stages.template_stage_key`), else the same name, so its tasks stay put.
- **A save** on a linked project (`check_save`, from `pipeline.save`) is refused 422 `template_locked`, naming the
  field and the template, when it changes the stages (count, order, names) or a locked field.
- **Publish** (`write_version` with the previous version, from `templates.apply`) takes, per open field, the new value
  where the project still holds the previous version's value, else keeps its own; locked fields take the new value. A
  refusal (an org rule set to Block, a dropped stage that still holds tasks) leaves the project on its version.
- **Unlink** (`unlink`) keeps every value and clears the link.

Every write goes through the one pipeline write (`pipeline.write`), with one history row.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity_shape import iso_or_none
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import PipelineTemplate, PipelineTemplateVersion
from app.releases import guardrails, history, pipeline, pipeline_doc, stages as stages_mod

STAGE_FIELDS = ("branch", "url")
STEP_FIELDS = ("who", "approvals", "auto")
#: Each lockable field as the Pipeline tab names it (`apps/web/src/components/projects/PipelineEditor.tsx`).
LABEL = {"branch": "Branch", "url": "Address", "who": "Who", "approvals": "Approvals", "auto": "How it runs"}


def linked(session: Session, project: Project) -> Optional[tuple[PipelineTemplate, PipelineTemplateVersion]]:
    """The template and version `project` follows, or None when not linked."""
    if project.pipeline_template_id is None or project.pipeline_template_version_id is None:
        return None
    template = session.scalar(select(PipelineTemplate).where(
        PipelineTemplate.id == project.pipeline_template_id, PipelineTemplate.org_id == project.org_id))
    version = session.scalar(select(PipelineTemplateVersion).where(
        PipelineTemplateVersion.id == project.pipeline_template_version_id,
        PipelineTemplateVersion.org_id == project.org_id))
    if template is None or version is None:
        return None
    return template, version


def _who(value: Any) -> Any:
    if value is None:
        return None
    return (tuple(sorted(str(r) for r in value.get("roles") or [])),
            tuple(sorted(str(u) for u in value.get("users") or [])))


def same(field: str, a: Any, b: Any) -> bool:
    """Whether two values of one lockable field are equal (an empty address is none; Who as sets)."""
    if field == "who":
        return bool(_who(a) == _who(b))
    if field == "url":
        return (a or None) == (b or None)
    if field == "branch":
        return (a or "").strip() == (b or "").strip()
    if field == "approvals":
        return int(a or 0) == int(b or 0)
    return bool(a) == bool(b)


def _resolve(project: Project, token: str) -> str:
    return pipeline_doc.resolve(token, project.branch_prefix or "", project.integration_branch or "")


def template_out(session: Session, project: Project) -> Optional[dict[str, Any]]:
    """`GET /pipeline`'s `template`: the linked template and version, and per stage and step (aligned by index with
    the pipeline's) each lock mark and the version's value (branches resolved for this project). None: not linked."""
    link = linked(session, project)
    if link is None:
        return None
    template, version = link
    doc = version.body
    return {
        "id": str(template.id), "name": template.name, "version_id": str(version.id),
        "version_number": version.number, "published_at": iso_or_none(version.created_at),
        "locks": {
            "stages": [{f: bool((s.get("locks") or {}).get(f)) for f in STAGE_FIELDS} for s in doc["stages"]],
            "steps": [{f: bool((s.get("locks") or {}).get(f)) for f in STEP_FIELDS} for s in doc["steps"]],
        },
        "values": {
            "stages": [{"name": s["name"], "branch": _resolve(project, s.get("branch") or ""), "url": s.get("url")}
                       for s in doc["stages"]],
            "steps": [{"who": s.get("who"), "approvals": int(s.get("approvals") or 0), "auto": bool(s.get("auto"))}
                      for s in doc["steps"]],
        },
    }


def _locked_refusal(template: PipelineTemplate, message: str, index: Optional[int], field: str) -> HTTPException:
    return pipeline.refuse("template_locked", message, index, field=field, template=template.name)


def check_save(session: Session, project: Project, specs: list[pipeline.StageSpec],
               steps: list[pipeline.StepSpec], current: list[Any]) -> None:
    """A save on a linked project (`pipeline.save`, not a link or publish): 422 `template_locked` naming the field
    and the template when it changes the stages (which stage is which, their order or names) or changes a field the
    linked version locks. A locked field the save leaves as stored passes even where the stored value differs from
    the version's (an address or branch detected from a deploy): only the next publish sets it. `current`: the stages
    as read."""
    link = linked(session, project)
    if link is None:
        return
    template, version = link
    doc = version.body
    if ([s.name.strip() for s in specs] != [s["name"] for s in doc["stages"]]
            or [s.id for s in specs] != [s.id for s in current]):
        raise _locked_refusal(template, f"Stages are set by template {template.name}. Unlink it to add, remove, "
                              "rename or move stages.", None, "stages")
    for index, (spec, stage, row) in enumerate(zip(specs, doc["stages"], current, strict=True)):
        locks = stage.get("locks") or {}
        stored = {"branch": stages_mod.branch_of(project, row), "url": row.url}
        sent = {"branch": spec.branch, "url": stored["url"] if spec.url is pipeline.KEEP else spec.url}
        want = {"branch": _resolve(project, stage.get("branch") or ""), "url": stage.get("url")}
        for field in STAGE_FIELDS:
            if (locks.get(field) and not same(field, sent[field], want[field])
                    and not same(field, sent[field], stored[field])):
                raise _locked_refusal(template, f"{stage['name']}: {LABEL[field]} is set by template "
                                      f"{template.name}. Unlink it to change this.", index, field)
    for index, (step, vstep) in enumerate(zip(steps, doc["steps"], strict=True), start=1):
        locks = vstep.get("locks") or {}
        row = current[index]
        stored = {"who": row.deployers, "approvals": row.approvals_required, "auto": row.auto_deploy}
        for field in STEP_FIELDS:
            value = getattr(step, field)
            if locks.get(field) and not same(field, value, vstep.get(field)) and not same(field, value, stored[field]):
                raise _locked_refusal(template, f"{doc['stages'][index]['name']}: {LABEL[field]} is set by template "
                                      f"{template.name}. Unlink it to change this.", index, field)


def _current(session: Session, project: Project) -> dict[str, Any]:
    """The project's pipeline as a document (`pipeline_doc.from_project`) with each stage's id, template stage key
    and address."""
    stages = pipeline.stages_for(session, project)
    doc = pipeline_doc.from_project(session, project)
    for row, stage in zip(doc["stages"], stages, strict=True):
        row.update(id=str(stage.id), template_key=stage.template_stage_key, url=stage.url)
    return doc


def body_for(version_doc: dict[str, Any], project: Project, current: dict[str, Any],
             previous: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The save body that puts `project` on `version_doc` (module docstring): link when `previous` is None, else
    publish from `previous` (the version the project was on)."""
    rows = current["stages"]
    before = {s["key"]: i for i, s in enumerate(previous["stages"])} if previous is not None else {}
    matches = _match(version_doc, rows)
    stages: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for j, stage in enumerate(version_doc["stages"]):
        mine = matches[j]
        was = before.get(stage["key"])
        matched, followed = mine is not None, was is not None

        def pick(field: str, new: Any, locked: bool, own: Any, old: Any,
                 matched: bool = matched, followed: bool = followed) -> Any:
            return _pick(field, new, locked, own, old, matched=matched, followed=followed)

        locks = stage.get("locks") or {}
        old_stage = previous["stages"][was] if previous is not None and was is not None else {}
        own_stage = rows[mine] if mine is not None else {}
        token = pick("branch", stage.get("branch") or "", bool(locks.get("branch")), own_stage.get("branch"),
                     old_stage.get("branch"))
        out: dict[str, Any] = {"name": stage["name"], "branch": _resolve(project, token),
                               "url": pick("url", stage.get("url"), bool(locks.get("url")), own_stage.get("url"),
                                           old_stage.get("url"))}
        if mine is not None:
            out["id"] = rows[mine]["id"]
        stages.append(out)
        if j == 0:
            continue
        step = version_doc["steps"][j - 1]
        step_locks = step.get("locks") or {}
        own_step = current["steps"][mine - 1] if mine else None
        old_step = previous["steps"][was - 1] if previous is not None and was else None
        values: dict[str, Any] = {}
        for field in STEP_FIELDS:
            if own_step is None:
                values[field] = step.get(field)
            else:
                values[field] = pick(field, step.get(field), bool(step_locks.get(field)), own_step.get(field),
                                     old_step.get(field) if old_step is not None else None)
        if own_step is not None and old_step is None and previous is not None:
            # The step into a stage that was first in the previous version (no step then): as a link sets it.
            values = {f: step.get(f) if step_locks.get(f) else own_step.get(f) for f in STEP_FIELDS}
        rules = {k: own_step[k] for k in pipeline.RULE_FIELDS if k in own_step} if own_step is not None else {}
        steps.append({**values, "approvals": int(values["approvals"] or 0), "auto": bool(values["auto"]), **rules})
    return {"stages": stages, "steps": steps}


def _match(version_doc: dict[str, Any], rows: list[dict[str, Any]]) -> list[Optional[int]]:
    """Each version stage's current row: every template-key match first, then by name among the rows no version
    stage claims by key (so a stage renamed while a new one takes its old name keeps its own row)."""
    keys = {r["template_key"]: i for i, r in enumerate(rows) if r.get("template_key")}
    out: list[Optional[int]] = [keys.get(stage["key"]) for stage in version_doc["stages"]]
    taken = {i for i in out if i is not None}
    claimed = {keys[s["key"]] for s in version_doc["stages"] if s["key"] in keys}
    by_name = {r["name"].strip().lower(): i for i, r in enumerate(rows)}
    for j, stage in enumerate(version_doc["stages"]):
        if out[j] is None:
            i = by_name.get(stage["name"].strip().lower())
            if i is not None and i not in taken and i not in claimed:
                out[j] = i
                taken.add(i)
    return out


def _pick(field: str, new: Any, locked: bool, own: Any, old: Any, *, matched: bool, followed: bool) -> Any:
    """One field's value on a link or publish: the version's when locked or on a stage the project lacks; on a publish
    (`followed`: the stage was in the previous version) the version's where the project still holds the previous
    value; else the project's own."""
    if locked or not matched:
        return new
    if followed and same(field, own, old):
        return new
    return own


def _refuse_dropping_held(session: Session, project: Project, current: dict[str, Any], body: dict[str, Any]) -> None:
    """A link or publish never drops a stage that still holds tasks: 422 `stage_has_tasks`, the project stays as it
    is."""
    from app.releases import impact  # noqa: PLC0415 — impact imports the board, which imports the release modules

    kept = {s.get("id") for s in body["stages"]}
    dropped = [r for r in current["stages"] if r["id"] not in kept]
    if not dropped:
        return
    counts = impact.items_by_stage(session, project, None)
    for row in dropped:
        held = next((n for stage_id, n in counts.items() if str(stage_id) == row["id"]), 0)
        if held:
            noun = "task" if held == 1 else "tasks"
            raise pipeline.refuse("stage_has_tasks", f"{row['name']} still has {held} {noun} and this version removes "
                                  "it. Move them first.")


def preview(session: Session, project: Project, version: PipelineTemplateVersion) -> dict[str, Any]:
    """What linking `project` to `version` would change and refuse (`GET …/pipeline/template-preview`)."""
    current = _current(session, project)
    body = body_for(version.body, project, current)
    problems = pipeline_doc.problems(body, project)
    try:
        _refuse_dropping_held(session, project, current, body)
    except HTTPException as exc:
        problems.append(exc.detail)
    return {
        # Against the project's own addresses (`_current`), so an address that stays is no change.
        "changes": pipeline_doc.diff(current, pipeline_doc.as_doc(body, project)),
        "problems": problems,
    }


def write_version(session: Session, project: Project, version: PipelineTemplateVersion, actor: AdminUser,
                  principal: Any, *, publish: bool = False) -> list[guardrails.Finding]:
    """Put `project` on `version` through the one pipeline write, as one save with its history row: a link
    (`publish` False) or a publish to a project already linked to this template (from the version it is on). Returns
    the save's Warn findings; a refusal (422) leaves the project as it was."""
    project = session.scalar(select(Project).where(Project.id == project.id).with_for_update()
                             .execution_options(populate_existing=True)) or project
    previous = None
    if publish:
        link = linked(session, project)
        if link is None or link[0].id != version.template_id:
            # Unlinked since the publish listed it: a publish never links a project.
            raise pipeline.refuse("template_not_linked", "No longer linked to this template.")
        if link[1].number >= version.number:
            # A later publish already reached it: never roll it back.
            raise pipeline.refuse("template_newer", f"Already on version {link[1].number}.")
        previous = link[1].body
    current = _current(session, project)
    body = body_for(version.body, project, current, previous)
    _refuse_dropping_held(session, project, current, body)
    specs, steps = pipeline_doc.specs_of(body)
    before = history.snapshot(session, project)

    def also_write(fields: dict[str, Any]) -> None:
        history.add(session, project, actor, before, history.snapshot(session, project, fields))

    return pipeline.write(session, project, actor, principal, specs, steps, before_commit=also_write,
                          template_version=version)


def unlink(session: Session, project: Project) -> None:
    """Clear the link: every value stays, every field becomes the project's own."""
    project.pipeline_template_id = None
    project.pipeline_template_version_id = None
    session.commit()
