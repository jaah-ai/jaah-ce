"""Basic pipelines and the Custom pipelines plan switch: `projects.pipeline_mode`, `pipeline_reset_at`, and the one reset

Schema (additive): `projects.pipeline_mode` (`none` | `pr_integration` | `integration_release` | `custom`, default
`integration_release`, the Standard preset every new project already gets) and `projects.pipeline_reset_at`; the plan
switch `billing_plans.pipeline_custom_enabled` (default false; true on `business`, which Business Trial and Enterprise
include) and `orgs.pipeline_custom_enabled_override`. The operator's own org (org zero) has no plan: its override is
set true, so the deployment's own pipelines are never reset.

Data, once (re-running finds nothing to reset): every project gets the mode its stages are — `none` without a
repository or with Ideas only; a basic when its stages are that basic's (names and branches); else `custom` when its
org has the switch. An org without it has that project's pipeline RESET to the nearest basic: `pr_integration` when
every branch stage is on the integration branch, else `integration_release` with the last branch stage's branch as the
release branch. The reset keeps one stored stage per role (Ideas, Building, the integration stage, the release stage)
with its id, address, gates, target, history and step rules (who may press, approvals, timing: a reset never drops an
approval), renames it to the basic's name, sets whether its step runs by itself as the preset does, and deletes the
other stages (their history goes with them); a role with no stage gets a new one (Ideas and Building only once the
project was upgraded, `projects.pipeline_upgraded_at`). It stamps `pipeline_reset_at` and writes one `pipeline.reset`
activity event carrying the dropped pipeline as a document (the shape of `app/releases/pipeline_doc.py::from_project`,
each stage's gates and deploy target beside it, addresses without credentials), so a person can re-import it. Every
project of an org without the switch leaves its org pipeline template: a basic takes no template. This is the only reset: an
org that loses the switch later keeps its custom pipeline, read-only (`app/releases/basics.py`).

The downgrade drops the columns; a reset pipeline stays reset (its documents remain in the activity log).

Revision ID: 202611140800
Revises: 202611140700
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140800"
down_revision: Union[str, Sequence[str], None] = "202611140700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

# Frozen copies (a migration never imports the live code): the mode domain, org zero, the stage levels and the
# presets `standard` and `trunk_prod` as each basic's stages (names and branch tokens).
_MODES = ("none", "pr_integration", "integration_release", "custom")
_ORG_ZERO = "01900000-0000-7000-8000-000000000010"
_NONE, _WORK, _BRANCH = "none", "work", "branch"
_INTEGRATION = "$integration"
_NAMED = "<named>"
_SHAPES: dict[str, list[tuple[str, str]]] = {
    "integration_release": [("ideas", ""), ("building", "*"), ("dev", _INTEGRATION), ("production", _NAMED)],
    "pr_integration": [("ideas", ""), ("building", "*"), ("production", _INTEGRATION)],
    "none": [("ideas", "")],
}
#: Each basic's stages: (name, role, the step into it runs by itself) — the presets `trunk_prod` and `standard`.
_TARGETS: dict[str, list[tuple[str, str, bool]]] = {
    "pr_integration": [("Ideas", _NONE, False), ("Building", _WORK, False), ("Production", "integration", True)],
    "integration_release": [("Ideas", _NONE, False), ("Building", _WORK, False), ("Dev", "integration", True),
                            ("Production", "release", False)],
}
_STEP_FIELDS = ("approvers", "requester_cant_approve", "approval_timeout_minutes", "on_timeout",
                "approver_instructions", "soak_minutes", "allowed_hours", "concurrency")

# Each org's switch: its own override (or its parent's, for a staging org), else ON when its plan or any plan that
# plan includes has it ON. With the column just added every override but org zero's is NULL and every plan but
# `business` off, so `(default)` (off) never decides.
_ORGS_ON = """
WITH RECURSIVE terms AS (
    SELECT o.id AS org_id, COALESCE(parent.id, o.id) AS terms_id FROM orgs o LEFT JOIN orgs parent
        ON parent.id = o.parent_org_id
), chain(org_id, key, depth) AS (
    SELECT t.org_id, ot.plan_key, 0 FROM terms t JOIN orgs ot ON ot.id = t.terms_id
    UNION ALL
    SELECT c.org_id, p.includes_plan_key, c.depth + 1 FROM chain c JOIN billing_plans p ON p.key = c.key
    WHERE p.includes_plan_key IS NOT NULL AND c.depth < 20
)
SELECT t.org_id FROM terms t JOIN orgs ot ON ot.id = t.terms_id
WHERE COALESCE(ot.pipeline_custom_enabled_override, EXISTS (
    SELECT 1 FROM chain c JOIN billing_plans p ON p.key = c.key
    WHERE c.org_id = t.org_id AND p.pipeline_custom_enabled AND t.terms_id <> CAST(:org_zero AS uuid)))
"""


def _token(stage: dict[str, Any], first_branch: bool) -> str:
    if stage["level"] == _NONE:
        return ""
    if stage["level"] == _WORK:
        return "*"
    return _INTEGRATION if first_branch else (stage["branch"] or "")


def _shown(project: Any, stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The stages a reader shows (`app/releases/stages.py::composed`): Ideas and Building synthesised ahead of an
    un-upgraded project's branch stages; the Standard defaults for a project with a repository and no stages."""
    ahead = [{"id": None, "name": "Ideas", "level": _NONE, "branch": None, "url": None},
             {"id": None, "name": "Building", "level": _WORK, "branch": None, "url": None}]
    if not stored:
        return [*ahead, {"id": None, "name": "Dev", "level": _BRANCH, "branch": None, "url": None},
                {"id": None, "name": "Production", "level": _BRANCH, "branch": "main", "url": None}]
    if project.pipeline_upgraded_at is None and all(s["level"] == _BRANCH for s in stored):
        return [*ahead, *stored]
    return stored


def _tokens(stages: list[dict[str, Any]]) -> list[str]:
    seen, out = False, []
    for stage in stages:
        first = stage["level"] == _BRANCH and not seen
        seen = seen or stage["level"] == _BRANCH
        out.append(_token(stage, first))
    return out


def _matching(stages: list[dict[str, Any]]) -> Optional[str]:
    signature = list(zip([str(s["name"]).strip().lower() for s in stages], _tokens(stages), strict=True))
    for mode, shape in _SHAPES.items():
        if len(shape) != len(signature):
            continue
        if all(name == want and (branch == want_branch or (
                want_branch == _NAMED and branch not in ("", "*", _INTEGRATION) and "*" not in branch))
               for (name, branch), (want, want_branch) in zip(signature, shape, strict=True)):
            return mode
    return None


def _address(url: Optional[str]) -> Optional[str]:
    """An address without any `user:password@` it carries: the event is readable wherever Audit is."""
    if not url:
        return url
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=parts.hostname + (f":{parts.port}" if parts.port else ""))) \
        if parts.hostname and "@" in parts.netloc else url


def _doc(stages: list[dict[str, Any]], gates: dict[Any, list[dict[str, Any]]]) -> dict[str, Any]:
    """The pipeline as a document (`pipeline_doc.from_project`'s shape), its addresses, gates (credentials as scope
    paths, never values) and deploy targets kept: what a person re-imports."""
    tokens = _tokens(stages)
    return {
        "stages": [{"key": str(s["id"]) if s["id"] else None, "name": s["name"], "branch": t,
                    "url": _address(s["url"]), "locks": {"branch": False, "url": False},
                    "target": {"kind": s.get("target_kind"), "config": s.get("target_config"),
                               "credential": s.get("target_credential")},
                    "gates": gates.get(s["id"], [])} for s, t in zip(stages, tokens, strict=True)],
        "steps": [{"who": s.get("deployers"), "approvals": s.get("approvals_required", 0),
                   "auto": bool(s.get("auto_deploy")), **{k: s.get(k) for k in _STEP_FIELDS},
                   "locks": {"who": False, "approvals": False, "auto": False}} for s in stages[1:]],
    }


def _gates(bind: Any, project_id: int) -> dict[Any, list[dict[str, Any]]]:
    """Each stage's gates in order, as the document carries them."""
    out: dict[Any, list[dict[str, Any]]] = {}
    for row in bind.execute(sa.text(
            "SELECT stage_id, phase, kind, enabled, required, timeout_s, retries, config, credential, name "
            "FROM pipeline_gates WHERE project_id = :p ORDER BY stage_id, phase, position"), {"p": project_id}).mappings():
        out.setdefault(row["stage_id"], []).append({k: row[k] for k in row.keys() if k != "stage_id"})
    return out


def _roles(stages: list[dict[str, Any]]) -> list[str]:
    branches = [i for i, s in enumerate(stages) if s["level"] == _BRANCH]
    out = []
    for i, stage in enumerate(stages):
        if stage["level"] != _BRANCH:
            out.append(stage["level"])
        elif i == branches[0]:
            out.append("integration")
        else:
            out.append("release")
    return out


def _reset(bind: Any, project: Any, stored: list[dict[str, Any]]) -> str:
    """Rewrite `stored` to the nearest basic (module docstring); the basic's mode."""
    branch_rows = [s for s in stored if s["level"] == _BRANCH]
    integration = project.integration_branch or ""
    if all(not s["branch"] or s["branch"] == integration for s in branch_rows):
        mode, release_branch = "pr_integration", None
    else:
        mode = "integration_release"
        release_branch = next((s["branch"] for s in reversed(branch_rows) if s["branch"] and s["branch"] != integration),
                              "main")
    roles = _roles(stored)
    # One stored stage per role: the first Ideas/Building, the integration stage, the last release stage.
    keep: dict[str, dict[str, Any]] = {}
    for stage, role in zip(stored, roles, strict=True):
        if role == "release" or role not in keep:
            keep[role] = stage
    targets = _TARGETS[mode]
    if project.pipeline_upgraded_at is None and all(s["level"] == _BRANCH for s in stored):
        targets = [t for t in targets if t[1] in ("integration", "release")]
    kept_ids = {keep[role]["id"] for _, role, _ in targets if role in keep}
    gone = [s["id"] for s in stored if s["id"] not in kept_ids]
    if gone:
        bind.execute(sa.text("DELETE FROM release_stages WHERE id = ANY(:ids)"), {"ids": gone})
    # Out of the way of the (project, position) and (project, lower(name)) keys before the final values.
    bind.execute(sa.text("UPDATE release_stages SET position = position + 1000, name = id::text "
                         "WHERE project_id = :p"), {"p": project.id})
    for position, (name, role, auto) in enumerate(targets):
        level = role if role in (_NONE, _WORK) else _BRANCH
        branch = release_branch if role == "release" else None
        source = keep.get(role)
        if source is None:
            bind.execute(sa.text(
                "INSERT INTO release_stages (org_id, project_id, position, name, level, branch, auto_deploy) "
                "VALUES (:org, :p, :pos, :name, :level, :branch, :auto)"),
                {"org": project.org_id, "p": project.id, "pos": position, "name": name, "level": level,
                 "branch": branch, "auto": auto})
            continue
        bind.execute(sa.text(
            "UPDATE release_stages SET position = :pos, name = :name, level = :level, branch = :branch, "
            "auto_deploy = :auto, template_stage_key = NULL, updated_at = now() WHERE id = :id"),
            {"pos": position, "name": name, "level": level, "branch": branch, "auto": auto, "id": source["id"]})
    return mode


def upgrade() -> None:
    op.add_column("projects", sa.Column("pipeline_mode", sa.Text(), nullable=False,
                                        server_default="integration_release"))
    op.add_column("projects", sa.Column("pipeline_reset_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_projects_pipeline_mode", "projects", "pipeline_mode IN (" + ", ".join(f"'{m}'" for m in _MODES) + ")")
    op.add_column("billing_plans", sa.Column("pipeline_custom_enabled", sa.Boolean(), nullable=True,
                                             server_default=sa.text("false")))
    op.add_column("orgs", sa.Column("pipeline_custom_enabled_override", sa.Boolean(), nullable=True))
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE billing_plans SET pipeline_custom_enabled = true WHERE key = 'business'"))
    bind.execute(sa.text("UPDATE orgs SET pipeline_custom_enabled_override = true WHERE id = CAST(:z AS uuid) "
                         "AND pipeline_custom_enabled_override IS NULL"), {"z": _ORG_ZERO})
    _set_modes(bind)


def _set_modes(bind: Any) -> None:
    on = {row[0] for row in bind.execute(sa.text(_ORGS_ON), {"org_zero": _ORG_ZERO})}
    # Every project row locked first, as a pipeline save locks its own (`app/releases/pipeline.py::save`), so no save
    # or upgrade tick of the outgoing release interleaves with the stages read below.
    projects = bind.execute(sa.text(
        "SELECT id, org_id, name, repo_url, integration_branch, pipeline_upgraded_at, pipeline_mode, "
        "pipeline_reset_at FROM projects ORDER BY id FOR UPDATE")).all()
    columns = ("id", "name", "level", "branch", "url", "auto_deploy", "approvals_required", "deployers",
               "target_kind", "target_config", "target_credential", *_STEP_FIELDS)
    rows: dict[int, list[dict[str, Any]]] = {}
    for row in bind.execute(sa.text(
            f"SELECT project_id, {', '.join(columns)} FROM release_stages ORDER BY project_id, position")).mappings():
        rows.setdefault(row["project_id"], []).append({k: row[k] for k in columns})
    reset = 0
    for project in projects:
        stored = rows.get(project.id, [])
        has_repo = bool((project.repo_url or "").strip())
        shown = _shown(project, stored) if has_repo else stored
        found = _matching(shown)
        if not has_repo or found == "none":
            mode = "none"
        elif found is not None:
            mode = found
        elif project.org_id in on:
            mode = "custom"
        else:
            doc = _doc(shown, _gates(bind, project.id))
            mode = _reset(bind, project, stored)
            bind.execute(sa.text("UPDATE projects SET pipeline_reset_at = now() WHERE id = :p"), {"p": project.id})
            label = "PR into integration branch" if mode == "pr_integration" else "Integration + Release"
            bind.execute(sa.text(
                "INSERT INTO activity_events (org_id, action_type, kind, summary, data) "
                "VALUES (:org, 'write', 'pipeline.reset', :summary, CAST(:data AS jsonb))"),
                {"org": project.org_id,
                 "summary": f"Reset the pipeline of {project.name} to {label}: custom pipelines are not on the plan",
                 "data": json.dumps({"id": str(project.id), "project_id": project.id, "mode": mode,
                                     "pipeline": doc}, default=str)})
            reset += 1
        if mode != "custom" and project.org_id not in on:
            bind.execute(sa.text(
                "UPDATE projects SET pipeline_template_id = NULL, pipeline_template_version_id = NULL "
                "WHERE id = :p AND pipeline_template_id IS NOT NULL"), {"p": project.id})
        if mode != project.pipeline_mode:
            bind.execute(sa.text("UPDATE projects SET pipeline_mode = :m WHERE id = :p"),
                         {"m": mode, "p": project.id})
    log.info("pipeline mode: %d project(s) reset to a basic pipeline", reset)


def downgrade() -> None:
    op.drop_column("orgs", "pipeline_custom_enabled_override")
    op.drop_column("billing_plans", "pipeline_custom_enabled")
    op.drop_constraint("ck_projects_pipeline_mode", "projects", type_="check")
    op.drop_column("projects", "pipeline_reset_at")
    op.drop_column("projects", "pipeline_mode")
