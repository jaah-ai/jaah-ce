"""A pipeline as a document: stages and steps that belong to no project, so one shape serves presets, copy from
project and (later) org templates. Every document ends in the one save (`app/releases/pipeline.py::save`), so none
of them can bypass a rule.

    PipelineDoc = {
      "stages": [{"key": str, "name": str, "branch": str, "url": str | None,
                  "locks": {"branch": bool, "url": bool}}],
      "steps":  [{"who": {"roles": [slug], "users": [uuid]} | None, "approvals": 0..5, "auto": bool,
                  "locks": {"who": bool, "approvals": bool, "auto": bool}}],    # steps[i] is the step into stages[i+1]
    }

A stage's branch is a token, resolved per project (`instantiate`): `""` no branch, `"*"` the work stage (the
project's prefix + `*`), `"$integration"` the integration stage (the project's integration branch, the first
branch-level stage), anything else a named branch. Locks matter only in templates; presets and copy carry none.

A copy (`from_project`) takes names, branches and step rules (approval, timing and concurrency rules included),
never addresses (they point at the source's
environments) nor freezes (they belong to the project).
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy.orm import Session, object_session

from app.models.projects import Project
from app.models.releases import ReleaseStage
from app.releases import stages as stages_mod

WORK = "*"
INTEGRATION = "$integration"
#: What `check_doc` resolves a work stage's prefix to: any valid prefix does.
_CHECK_PREFIX = "work/"

_NO_STAGE_LOCKS = {"branch": False, "url": False}
_NO_STEP_LOCKS = {"who": False, "approvals": False, "auto": False}


class Change(TypedDict, total=False):
    """One difference between two documents (`diff`): `stage` names the stage as it is after (as it was, when
    removed); `field` is `branch`, `url`, `who`, `approvals` or `auto`."""

    kind: str  # stage_added | stage_removed | stage_renamed | stage_moved | field | lock
    stage: str
    field: str
    old: Any
    new: Any


# ── The presets ─────────────────────────────────────────────────────────────────────────────────────

def _preset(slug: str, rows: Sequence[tuple[str, str, bool]]) -> dict[str, Any]:
    """A preset document from (name, branch token, the step into it runs by itself)."""
    return {
        "stages": [{"key": str(uuid5(NAMESPACE_URL, f"jaah:preset:{slug}:{name}")), "name": name, "branch": branch,
                    "url": None, "locks": dict(_NO_STAGE_LOCKS)} for name, branch, _ in rows],
        "steps": [{"who": None, "approvals": 0, "auto": auto, "locks": dict(_NO_STEP_LOCKS)} for _, _, auto in rows[1:]],
    }


_IDEAS = (stages_mod.IDEAS, "", False)
_BUILDING = (stages_mod.BUILDING, WORK, False)
_DEV = (stages_mod.DEV, INTEGRATION, True)
_PRODUCTION = (stages_mod.PRODUCTION, stages_mod.PRODUCTION_BRANCH_DEFAULT, False)

STANDARD = "standard"
IDEAS_ONLY = "ideas_only"
#: The presets, in the order they are offered. Standard is exactly the default a new project gets
#: (`stages._defaults`).
PRESETS: dict[str, dict[str, Any]] = {
    STANDARD: _preset(STANDARD, [_IDEAS, _BUILDING, _DEV, _PRODUCTION]),
    "trunk_prod": _preset("trunk_prod", [_IDEAS, _BUILDING, (stages_mod.PRODUCTION, INTEGRATION, True)]),
    "dev_staging_prod": _preset("dev_staging_prod", [_IDEAS, _BUILDING, _DEV, ("Staging", "staging", False), _PRODUCTION]),
    IDEAS_ONLY: _preset(IDEAS_ONLY, [_IDEAS]),
}
#: Each preset's name and one line on what it is for.
PRESET_LABELS: dict[str, tuple[str, str]] = {
    STANDARD: ("Standard", "The default: merging deploys to Dev, releases go to Production."),
    "trunk_prod": ("Trunk → Prod", "Merging deploys straight to Production."),
    "dev_staging_prod": ("Dev → Staging → Prod", "Two releases: Dev to Staging, then Staging to Production."),
    IDEAS_ONLY: ("Ideas only (no deploys)", "One stage for planning. No repository needed."),
}


def chain(names: Sequence[str]) -> str:
    """A pipeline's stage names as one line: "Ideas → Building → Dev → Production"."""
    return " → ".join(names)


def presets_out() -> list[dict[str, Any]]:
    """`GET /api/pipeline/presets`."""
    return [{"slug": slug, "name": PRESET_LABELS[slug][0], "note": PRESET_LABELS[slug][1],
             "chain": chain([s["name"] for s in doc["stages"]]), "needs_repo": slug != IDEAS_ONLY}
            for slug, doc in PRESETS.items()]


def preset_or_422(slug: str) -> dict[str, Any]:
    doc = PRESETS.get(slug)
    if doc is None:
        raise HTTPException(status_code=422, detail=f"Unknown preset. Pick one of: {', '.join(PRESETS)}.")
    return doc


# ── A project as a document, and a document for a project ───────────────────────────────────────────

def from_project(session: Session, project: Project) -> dict[str, Any]:
    """The project's pipeline as a document: the work stage as `"*"`, the integration stage as `"$integration"`; no
    addresses, no freezes."""
    from app.releases import pipeline  # noqa: PLC0415 — pipeline imports this module

    shown = pipeline.read(session, project)
    first_branch = next((i for i, s in enumerate(shown["stages"]) if s["level"] == stages_mod.BRANCH), None)

    def token(index: int, stage: dict[str, Any]) -> str:
        if stage["level"] == stages_mod.NONE:
            return ""
        if stage["level"] == stages_mod.WORK:
            return WORK
        return INTEGRATION if index == first_branch else stage["branch"]

    return {
        "stages": [{"key": s["id"], "name": s["name"], "branch": token(i, s), "url": None,
                    "locks": dict(_NO_STAGE_LOCKS)} for i, s in enumerate(shown["stages"])],
        "steps": [{"who": s["who"], "approvals": s["approvals"], "auto": s["auto"],
                   **{k: s[k] for k in pipeline.RULE_FIELDS}, "locks": dict(_NO_STEP_LOCKS)}
                  for s in shown["steps"]],
    }


def resolve(branch: str, prefix: str, integration: str) -> str:
    """A branch token as the branch a project's stage holds."""
    text = (branch or "").strip()
    if text == WORK:
        return f"{prefix}*"
    if text == INTEGRATION:
        return integration
    return text


def instantiate(doc: dict[str, Any], project: Project, current: Sequence[ReleaseStage]) -> dict[str, Any]:
    """The save body (`routes/releases.py::PipelineIn`'s shape) that makes `project`'s pipeline `doc`: tokens resolved
    with the project's own prefix and integration branch; a stage reuses the id of the project's current stage of the
    same name (case-insensitive), so the tasks on it stay put; an unmatched one gets no id and is created. A matched
    stage the document gives no address keeps its own (`url` left out)."""
    by_name = {s.name.strip().lower(): s for s in current}
    taken: set[Any] = set()
    stages: list[dict[str, Any]] = []
    for item in doc["stages"]:
        out: dict[str, Any] = {
            "name": item["name"],
            "branch": resolve(item["branch"], project.branch_prefix or "", project.integration_branch or ""),
        }
        found = _match(item, by_name, taken)
        if found is not None:
            out["id"] = str(found.id)
            taken.add(found.id)
        if item.get("url") is not None or found is None:
            out["url"] = item.get("url")
        stages.append(out)
    from app.releases import pipeline  # noqa: PLC0415 — pipeline imports this module

    # A step's rules (`pipeline.RULE_FIELDS`) travel when the document carries them (a copy); else their defaults.
    steps = [{"who": step.get("who"), "approvals": step.get("approvals", 0), "auto": bool(step.get("auto")),
              **{k: step[k] for k in pipeline.RULE_FIELDS if k in step}}
             for step in doc["steps"]]
    return {"stages": stages, "steps": steps}


def _match(item: dict[str, Any], by_name: dict[str, ReleaseStage], taken: set[Any]) -> Optional[ReleaseStage]:
    """The current stage a document stage becomes, by name (a template link matches by template stage key first:
    `app/releases/linking.py::body_for`)."""
    found = by_name.get(str(item["name"]).strip().lower())
    return found if found is not None and found.id not in taken else None


def levels_of(doc: dict[str, Any]) -> list[str]:
    """Each stage's level: `""` none, `"*"` work, `"$integration"` and any name branch."""
    return [stages_mod.level_for(s["branch"]) for s in doc["stages"]]


def specs_of(body: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """A save body (`instantiate`'s) as the save's stage and step specs."""
    from app.releases import pipeline  # noqa: PLC0415 — pipeline imports this module

    specs = [pipeline.StageSpec(UUID(s["id"]) if s.get("id") else None, s["name"], s["branch"],
                                s.get("url", pipeline.KEEP)) for s in body["stages"]]
    steps = [pipeline.StepSpec(s.get("who"), int(s.get("approvals") or 0), bool(s.get("auto")),
                               **{k: s[k] for k in pipeline.RULE_FIELDS if k in s})
             for s in body["steps"]]
    return specs, steps


def check_doc(doc: dict[str, Any]) -> None:
    """A document's structure, with no project: the save's own validation with placeholder branches, and
    `"$integration"` only on the first branch-level stage. 422 like a save."""
    from app.releases import pipeline  # noqa: PLC0415 — pipeline imports this module

    levels = levels_of(doc)
    first = next((i for i, level in enumerate(levels) if level == stages_mod.BRANCH), None)
    for index, stage in enumerate(doc["stages"]):
        if (stage.get("branch") or "").strip() == INTEGRATION and index != first:
            raise pipeline.refuse("pipeline_order", f"{stage['name']}: only the first stage with a branch can use "
                                  "each project's integration branch.", index)
    resolved = {"stages": [{**s, "branch": resolve(s["branch"], _CHECK_PREFIX, INTEGRATION)} for s in doc["stages"]],
                "steps": doc["steps"]}
    specs, steps = specs_of(resolved)
    pipeline.validate(specs, steps)


def problems(payload: dict[str, Any], project: Project) -> list[dict[str, Any]]:
    """What a save of `payload` (a save body) on `project` would refuse, without raising: `[{code, message,
    stage_index?}]` — its first structural refusal, else every org rule set to Block it breaks (`code` `guardrail`,
    with its `rule`; read without writing, `guardrails.enabled`). For previews."""
    from app.releases import guardrails, pipeline  # noqa: PLC0415 — pipeline imports this module

    specs, steps = specs_of(payload)
    try:
        levels = pipeline.validate(specs, steps)
        pipeline.refuse_without_repo(project, levels)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "invalid", "message": str(exc.detail)}
        return [detail]
    session = object_session(project)
    rules = guardrails.enabled(session, project.org_id) if session is not None else []
    return [{"code": "guardrail", **f.out()} for f in guardrails.evaluate(rules, [s.name for s in specs], levels, steps)
            if f.mode == guardrails.BLOCK]


# ── Differences ─────────────────────────────────────────────────────────────────────────────────────

def diff(before: dict[str, Any], after: dict[str, Any]) -> list[Change]:
    """What changes from `before` to `after` (both documents): stages matched by `key`, else by name. A field the
    `after` document leaves unset (an address of `None`) is no change. One diff for every preview and review."""
    old_stages, new_stages = before["stages"], after["stages"]
    by_key = {s["key"]: i for i, s in enumerate(old_stages)}
    by_name = {s["name"].strip().lower(): i for i, s in enumerate(old_stages)}
    pairs: list[tuple[int, Optional[int]]] = []
    used: set[int] = set()
    for j, stage in enumerate(new_stages):
        i = by_key.get(stage["key"])
        if i is None or i in used:
            i = by_name.get(stage["name"].strip().lower())
        if i is not None and i in used:
            i = None
        if i is not None:
            used.add(i)
        pairs.append((j, i))

    changes: list[Change] = [{"kind": "stage_removed", "stage": s["name"]}
                             for i, s in enumerate(old_stages) if i not in used]
    matched = [(j, i) for j, i in pairs if i is not None]
    old_order = [i for _, i in sorted(matched, key=lambda p: p[1])]
    new_order = [i for _, i in matched]
    for j, i in pairs:
        stage = new_stages[j]
        if i is None:
            changes.append({"kind": "stage_added", "stage": stage["name"]})
            continue
        old = old_stages[i]
        if old["name"] != stage["name"]:
            changes.append({"kind": "stage_renamed", "stage": stage["name"], "old": old["name"], "new": stage["name"]})
        if old_order.index(i) != new_order.index(i):
            changes.append({"kind": "stage_moved", "stage": stage["name"]})
        if (old.get("branch") or "") != (stage.get("branch") or ""):
            changes.append({"kind": "field", "stage": stage["name"], "field": "branch",
                            "old": old.get("branch") or "", "new": stage.get("branch") or ""})
        if stage.get("url") is not None and stage.get("url") != old.get("url"):
            changes.append({"kind": "field", "stage": stage["name"], "field": "url", "old": old.get("url"),
                            "new": stage["url"]})
        for lock, value in (stage.get("locks") or {}).items():
            if bool((old.get("locks") or {}).get(lock)) != bool(value):
                changes.append({"kind": "lock", "stage": stage["name"], "field": lock,
                                "old": bool((old.get("locks") or {}).get(lock)), "new": bool(value)})
        if i == 0 or j == 0:
            continue
        old_step, new_step = before["steps"][i - 1], after["steps"][j - 1]
        for field in ("who", "approvals", "auto"):
            if old_step.get(field) != new_step.get(field):
                changes.append({"kind": "field", "stage": stage["name"], "field": field,
                                "old": old_step.get(field), "new": new_step.get(field)})
        for lock, value in (new_step.get("locks") or {}).items():
            if bool((old_step.get("locks") or {}).get(lock)) != bool(value):
                changes.append({"kind": "lock", "stage": stage["name"], "field": lock,
                                "old": bool((old_step.get("locks") or {}).get(lock)), "new": bool(value)})
    return changes


def as_doc(body: dict[str, Any], project: Project) -> dict[str, Any]:
    """A save body for `project` back as a document (its branches as tokens), keyed by the ids it carries, so `diff`
    can set it against `from_project`."""
    levels = [stages_mod.level_for(s["branch"]) for s in body["stages"]]
    first = next((i for i, level in enumerate(levels) if level == stages_mod.BRANCH), None)
    stages = []
    for index, (stage, level) in enumerate(zip(body["stages"], levels, strict=True)):
        branch = WORK if level == stages_mod.WORK else INTEGRATION if index == first else stage["branch"]
        stages.append({"key": stage.get("id") or f"new:{index}", "name": stage["name"],
                       "branch": "" if level == stages_mod.NONE else branch, "url": stage.get("url"),
                       "locks": dict(_NO_STAGE_LOCKS)})
    steps = [{**s, "locks": dict(_NO_STEP_LOCKS)} for s in body["steps"]]
    return {"stages": stages, "steps": steps}
