"""A project's pipeline setup (`GET`/`PUT /api/projects/{id}/pipeline`): its stages and the step into each.

Every stage is set the same way: name, branch, address. Every step (the arrow into stage k, its settings on stage k's
row) is set the same way: who may do it (`deployers`; NULL is the action's default), approvals needed, by hand or by
itself (`auto_deploy`). What a step does is derived, never chosen (`action`).

Every save is checked against the org's pipeline rules first (`app/releases/guardrails.py`): a Block refuses it with
nothing written (422 `guardrail`), a Warn saves and is returned; a read carries what the stored pipeline breaks today
(`warnings`).

A save is an id-preserving upsert in one transaction and never waits for a running release: each release carries the
settings it was requested with (`app/releases/snapshot.py`), so a save applies from the next one. Its two project
branch fields stay on the project (`app/releases/stages.py`): a save that edits the integration stage's branch or the
work stage's pattern writes `projects.integration_branch` / `branch_prefix` through the project's own write
(`routes/projects.py::apply_project_update`, every guard and dispatch included).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.access_catalog import ALL_KEYS, ROLES, ROLES_BY_SLUG
from app.actors import actor_names, stamp_actor
from app.authz.catalog import permission_key
from app.authz.principal import Principal, principal_for_user
from app.entity_shape import optional_uuid
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import (
    INSTRUCTIONS_MAX,
    PipelineGate,
    MINUTES_MAX,
    TIMEOUT_MIN,
    PipelineTemplateVersion,
    ReleaseFreeze,
    ReleaseStage,
    StageHealth,
)
from app.releases import (
    access, guardrails, health as health_mod, hours as hours_mod, service, snapshot, stages as stages_mod,
)
from app.releases.views import freezes_out
from app.pipeline_gates import defaults as gate_defaults, full_gate, schema as gate_schema

#: Most stages one pipeline holds (a bound on one request's write, not a product limit anyone meets).
STAGES_MAX = 50

#: The derived step actions (architecture §2.2), by the highest code level before the step and the level it enters.
MOVE, START, START_MERGE, MERGE, RELEASE = "move", "start", "start_merge", "merge", "release"
_RANK = {stages_mod.NONE: 0, stages_mod.WORK: 1, stages_mod.BRANCH: 2}
#: The key a step's default `who` holds: Start's own (`authz/object_access.py::START_KEY`) for a task step, today's
#: release default (`access.PROJECTS_EDIT`) for a release.
_DEFAULT_KEY = {
    MOVE: permission_key("tasks", "edit"), START: permission_key("tasks", "edit"),
    START_MERGE: permission_key("tasks", "edit"), MERGE: permission_key("tasks", "edit"),
    RELEASE: access.PROJECTS_EDIT,
}


def action(before: Sequence[ReleaseStage], stage: ReleaseStage) -> str:
    """What the step into `stage` does, from the highest level among the stages `before` it and its own."""
    return action_at([*(s.level for s in before), stage.level], len(before))


def action_at(levels: Sequence[str], index: int) -> str:
    """What the step into stage `index` does, from the stages' levels (`action`, for a draft not stored yet)."""
    if levels[index] == stages_mod.NONE:
        return MOVE
    if levels[index] == stages_mod.WORK:
        return START
    highest = max((_RANK[level] for level in levels[:index]), default=0)
    return {0: START_MERGE, 1: MERGE}.get(highest, RELEASE)


def default_key(step_action: str) -> str:
    """The key a step's default `who` holds (`_DEFAULT_KEY`)."""
    return _DEFAULT_KEY[step_action]


def default_who(step_action: str) -> dict[str, list[str]]:
    """The roles the step's default `who` resolves to: every built-in role holding its key."""
    key = _DEFAULT_KEY[step_action]
    return {"roles": [role.slug for role in ROLES if _holds(role.keys, key)], "users": []}


def default_roles() -> dict[str, list[str]]:
    """Each action's default roles: what a step's `who: null` resolves to."""
    return {a: default_who(a)["roles"] for a in _DEFAULT_KEY}


def _holds(keys: object, key: str) -> bool:
    """Whether a role's keys (`ALL_KEYS`, or a set) include `key`."""
    return keys is ALL_KEYS or (isinstance(keys, frozenset) and key in keys)


#: The step actions a gate list may sit on (D1): a move or a start takes none.
GATED = (START_MERGE, MERGE, RELEASE)


def stages_for(session: Session, project: Project) -> list[ReleaseStage]:
    """The pipeline a read shows: `stages.for_read`, or for a project without a repository its stored stages, else
    the unsaved defaults. Never writes."""
    return shown(project, stages_mod.stages_of(session, project.id))


def shown(project: Project, stored: list[ReleaseStage]) -> list[ReleaseStage]:
    """`stages_for` on stages already read (many projects in one query: `pipeline_doc.chains`)."""
    if stages_mod.has_repo(project):
        return stages_mod.composed(project, stored)
    return stored or stages_mod._defaults(project)


def read(session: Session, project: Project) -> dict[str, Any]:
    """`GET /pipeline`'s body: `steps[i]` is the step into `stages[i + 1]`. `defaults` is each action's default roles
    (what `who: null` resolves to, for a step setup re-derives after a draft edit); `names` the named users' names;
    `warnings` each org rule the stored pipeline breaks (`guardrails.Finding.out`). Never writes."""
    from app.releases import linking  # noqa: PLC0415 — linking imports this module

    stages = stages_for(session, project)
    pending = upgrade_pending(project, stages)
    integration = stages_mod.integration_stage(stages)
    lists = gate_defaults.gates_of(session, project, stages)
    forge = _forge(project)
    alert_source = has_alert_source(session, project) if any(
        g.kind == "alerts" for gates in lists.values() for g in gates) else True

    def gates_out(stage: ReleaseStage, phase: str) -> list[dict[str, Any]]:
        return [gate_out(g, forge, alert_source) for g in lists.get((stage.id, phase), [])]

    health = health_mod.rows_of(session, [s.id for s in stages])
    steps = []
    for index, stage in enumerate(stages[1:], start=1):
        step_action = action(stages[:index], stage)
        who, approvals, auto = stage.deployers, stage.approvals_required, stage.auto_deploy
        if pending and integration is not None and stage.id == integration.id:
            who, approvals, auto = None, 0, True  # the Merge step as `stages.upgrade` writes it, and as it runs today
        who_effective = who if who is not None else default_who(step_action)
        approvers = _rule(stage, "approvers")
        steps.append({
            "to_stage_id": str(stage.id), "action": step_action, "who": who,
            "who_effective": who_effective,
            "approvals": approvals, "auto": auto,
            "gates": gates_out(stage, gate_defaults.BEFORE),
            "approvers": approvers,
            "approvers_effective": approvers if approvers is not None else who_effective,
            "requester_cant_approve": _rule(stage, "requester_cant_approve"),
            "approval_timeout_minutes": _rule(stage, "approval_timeout_minutes"),
            "on_timeout": _rule(stage, "on_timeout"),
            "instructions": _rule(stage, "approver_instructions"),
            "soak_minutes": _rule(stage, "soak_minutes"),
            "allowed_hours": _rule(stage, "allowed_hours"),
            "concurrency": _rule(stage, "concurrency"),
            # A by-itself release step paused after an off-path release (R8.4; `judge.paused`).
            "pause": _pause(session, project, stages, stage) if step_action == RELEASE else None,
        })
    freezes = list(session.scalars(
        select(ReleaseFreeze).where(ReleaseFreeze.project_id == project.id).order_by(ReleaseFreeze.starts_at)))
    named = {user for step in steps for key in ("who_effective", "approvers_effective")
             for u in step[key]["users"] if (user := optional_uuid(u))}
    if project.freeze_now_by_id is not None:
        named.add(project.freeze_now_by_id)
    names = {str(u): name for u, name in actor_names(session, named).items()} if named else {}
    return {
        "stages": [
            {"id": str(s.id), "name": s.name, "branch": stages_mod.branch_of(project, s), "url": s.url,
             "level": s.level, "url_source": s.url_source,
             "target": {"kind": gate_defaults.target_kind(s), "config": s.target_config,
                        "credential": s.target_credential},
             "health": health_mod.out(s, health.get(s.id)),
             "after": gates_out(s, gate_defaults.AFTER)}
            for s in stages
        ],
        "steps": steps,
        "freezes": freezes_out(session, freezes),
        "freeze_now": freeze_now_out(project, names),
        "release_running": bool(service.open_releases(session, project.id)),
        "defaults": default_roles(),
        "names": names,
        "warnings": [f.out() for f in guardrails.evaluate_stored(
            session, project, guardrails.enabled(session, project.org_id))],
        # The org template this project follows, its lock marks and values (`linking.template_out`); None: not linked.
        "template": linking.template_out(session, project),
    }


def _pause(session: Session, project: Project, stages: list[ReleaseStage], stage: ReleaseStage) -> Optional[dict[str, Any]]:
    from app.releases import judge  # noqa: PLC0415 — the judge reads this module's service

    found = judge.paused(session, project, stages, stage)
    return found.out(session) if found is not None else None


def freeze_now_out(project: Project, names: dict[str, str]) -> Optional[dict[str, Any]]:
    """`{by_name, at}` while Freeze now is on, else None."""
    if project.freeze_now_at is None:
        return None
    by = str(project.freeze_now_by_id) if project.freeze_now_by_id is not None else None
    return {"by_name": names.get(by) if by is not None else None, "at": project.freeze_now_at.isoformat()}


def _rule(stage: ReleaseStage, key: str) -> Any:
    """A rule column as stored, or its default on a stage not written yet (`stages.for_read`'s synthesised ones)."""
    value = getattr(stage, key)
    default = snapshot.RULE_DEFAULTS[key]
    return default if value is None and default is not None else value


def gate_out(gate: PipelineGate, forge: Optional[str], alert_source: bool = True) -> dict[str, Any]:
    """`Gate` (architecture §3.3): the stored gate, its server-made one-line `summary` and a `warning` slug."""
    return {
        "id": str(gate.id), "name": gate.name, "kind": gate.kind, "enabled": bool(gate.enabled),
        "required": bool(gate.required), "timeout_s": int(gate.timeout_s), "retries": int(gate.retries),
        "config": gate.config or {}, "credential": gate.credential,
        "summary": gate_schema.summary(gate.kind, gate.config or {}, forge),
        "warning": "no_alert_source" if gate.kind == "alerts" and not alert_source else None,
    }


def _forge(project: Project) -> Optional[str]:
    from app import forges  # noqa: PLC0415

    ref = forges.repo_ref_or_none(project)
    return ref.forge if ref is not None else None


def has_alert_source(session: Session, project: Project) -> bool:
    """Whether a live monitor (or an incident-shaped webhook) reports this project's alerts (D7's warning)."""
    from app.models.automations import TaskTrigger  # noqa: PLC0415
    from app.triggers import MONITOR_SOURCE_KINDS, SOURCE_WEBHOOK  # noqa: PLC0415

    rows = session.scalars(select(TaskTrigger).where(
        TaskTrigger.project_id == project.id, TaskTrigger.paused_at.is_(None),
        TaskTrigger.source_kind.in_((*MONITOR_SOURCE_KINDS, SOURCE_WEBHOOK))))
    return any(t.source_kind != SOURCE_WEBHOOK or (t.source_config or {}).get("shape") == "incident" for t in rows)


def upgrade_pending(project: Project, stages: Sequence[ReleaseStage]) -> bool:
    """Whether Ideas and Building are still synthesised for this project (`stages.for_read`): not upgraded yet and
    every stored stage branch-level."""
    return (stages_mod.has_repo(project) and project.pipeline_upgraded_at is None
            and any(s.level != stages_mod.BRANCH for s in stages))


# ── The save ────────────────────────────────────────────────────────────────────────────────────────

_KEEP: Any = object()
#: A `StageSpec` field the caller left out: the stored value stays.
KEEP: Any = _KEEP


@dataclass
class StageSpec:
    """One stage as a save sends it. `url`, `target` (a `TargetIn`) and `after` (its verify list of `GateIn`) are
    kept as stored when the caller leaves them out."""

    id: Optional[UUID]
    name: str
    branch: str
    url: Any = _KEEP
    target: Any = _KEEP
    after: Any = _KEEP


#: A step's rule fields as `read` shows them and a save sends them (`StepSpec`), each with its words in the history.
RULE_FIELDS: dict[str, str] = {
    "approvers": "approvers", "requester_cant_approve": "requester can't approve",
    "approval_timeout_minutes": "approval timeout", "on_timeout": "when the timeout runs out",
    "instructions": "instructions for approvers", "soak_minutes": "wait after arrival",
    "allowed_hours": "allowed hours", "concurrency": "one at a time",
}
#: A freeze's rule fields (`views.freeze_out`) beyond its window.
FREEZE_RULE_FIELDS = ("repeat", "scope", "stage_ids", "let_pressed", "let_queued")


@dataclass
class StepSpec:
    """The step into a stage: `who` None is the action's default; `approvers` None is "same as who may press". Every
    rule left out is its default (today's behaviour), never the stored value: a save sends every field. `gates` (a
    list of `GateIn`) is the exception: kept when left out."""

    who: Optional[dict[str, list[Any]]]
    approvals: int
    auto: bool
    approvers: Optional[dict[str, list[Any]]] = None
    requester_cant_approve: bool = True
    approval_timeout_minutes: Optional[int] = None
    on_timeout: str = "cancel"
    instructions: str = ""
    soak_minutes: int = 0
    allowed_hours: Optional[dict[str, Any]] = None
    concurrency: str = "newest"
    #: The parsed `allowed_hours` (`validate_rules`).
    hours: Optional[hours_mod.Hours] = field(default=None, compare=False)
    gates: Any = _KEEP


def refuse(code: str, message: str, index: Optional[int] = None, **extra: Any) -> HTTPException:
    """A 422 `{code, message, stage_index?, …extra}`: the console shows `message` on stage `stage_index`'s card."""
    detail: dict[str, Any] = {"code": code, "message": message, **extra}
    if index is not None:
        detail["stage_index"] = index
    return HTTPException(status_code=422, detail=detail)


def validate_rules(steps: Sequence[StepSpec], levels: Sequence[str]) -> None:
    """Each step's rules, or a 422 `{code, message, stage_index, field}` naming the field and the fix (R1.4, R3,
    R4, R5, R6). Parses `allowed_hours` into `StepSpec.hours`."""
    for index, step in enumerate(steps, start=1):
        if step.approvers is not None and step.approvals > 0:
            roles = step.approvers.get("roles") or []
            users = set(str(u) for u in step.approvers.get("users") or [])
            if not roles and len(users) < step.approvals:
                raise refuse("approvers_short",
                             f"{step.approvals} approvals are needed but only {len(users)} people can approve. "
                             "Add an approver or lower the count.", index, field="approvers")
        timeout = step.approval_timeout_minutes
        if timeout is not None and not TIMEOUT_MIN <= timeout <= MINUTES_MAX:
            raise refuse("timeout_range", "Set the approval timeout between 15 minutes and 30 days, or turn it off.",
                         index, field="approval_timeout_minutes")
        if len(step.instructions or "") > INSTRUCTIONS_MAX:
            raise refuse("instructions_long", f"Shorten the instructions to {INSTRUCTIONS_MAX} characters.", index,
                         field="instructions")
        if not 0 <= step.soak_minutes <= MINUTES_MAX:
            raise refuse("soak_range", "Set the wait between 0 minutes and 30 days.", index, field="soak_minutes")
        step.hours = None
        if step.allowed_hours is not None:
            try:
                step.hours = hours_mod.parse(step.allowed_hours)
            except hours_mod.HoursError as exc:
                if exc.field == "timezone":
                    raise refuse("timezone_unknown", "Pick a known time zone for the allowed hours.", index,
                                 field="allowed_hours.timezone") from None
                raise refuse("hours_invalid", f"Allowed hours: {exc}", index,
                             field=f"allowed_hours.{exc.field}") from None
        step_action = action([SimpleNamespace(level=level) for level in levels[:index]],  # type: ignore[misc]
                             SimpleNamespace(level=levels[index]))  # type: ignore[arg-type]
        if step.concurrency != "newest" and step_action != RELEASE:
            raise refuse("concurrency_not_release", "Only a release step runs one at a time in order; "
                         "set this step to newest wins.", index, field="concurrency")


def check_guardrails(session: Session, project: Project, specs: Sequence[StageSpec], levels: Sequence[str],
                     steps: Sequence[StepSpec]) -> list[guardrails.Finding]:
    """The org's rules on the pipeline these specs describe: the first Block finding as a 422 `guardrail` (with its
    `rule`), else the Warn findings."""
    found = guardrails.evaluate(guardrails.for_org(session, project.org_id), [spec.name for spec in specs], levels,
                                steps)
    block = next((f for f in found if f.mode == guardrails.BLOCK), None)
    if block is not None:
        raise refuse("guardrail", block.message, block.stage_index, rule=block.rule)
    return [f for f in found if f.mode == guardrails.WARN]


def _deployers(session: Session, project: Project, who: Optional[dict[str, list[Any]]]) -> Optional[dict[str, Any]]:
    """A step's `who` as stored: None (the default), else known roles and members of the project's org."""
    if who is None:
        return None
    roles = list(dict.fromkeys(str(r) for r in who.get("roles") or []))
    if any(role not in ROLES_BY_SLUG for role in roles):
        raise HTTPException(status_code=422, detail="Unknown role.")
    try:
        users = list(dict.fromkeys(UUID(str(u)) for u in who.get("users") or []))
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown user.") from None
    members = set(session.scalars(
        select(AdminUser.id).where(AdminUser.id.in_(users), AdminUser.org_id == project.org_id)
    )) if users else set()
    if len(members) != len(users):
        raise HTTPException(status_code=422, detail="Unknown user.")
    return {"roles": roles, "users": [str(u) for u in users]}


def _approvers(session: Session, project: Project, who: Optional[dict[str, list[Any]]],
               index: int) -> Optional[dict[str, Any]]:
    """A step's approvers as stored (`_deployers`' rules), or a 422 naming the field."""
    try:
        return _deployers(session, project, who)
    except HTTPException:
        raise refuse("approvers_unknown", "Pick approvers from this org's roles and members.", index,
                     field="approvers") from None


def validate(specs: Sequence[StageSpec], steps: Sequence[StepSpec]) -> list[str]:
    """Each stage's level, or a 422: at least one stage; one step per stage after the first; unique names; ignoring
    `none` stages, levels never go down (any `none`, at most one `work`, then `branch`); unique branch names; a work
    pattern ends in its one `*`."""
    if not 1 <= len(specs) <= STAGES_MAX:
        raise refuse("pipeline_size", f"1 to {STAGES_MAX} stages.")
    if len(steps) != len(specs) - 1:
        raise refuse("pipeline_steps", "One step for every stage after the first.")
    seen: dict[str, int] = {}
    for index, spec in enumerate(specs):
        key = spec.name.strip().lower()
        if not key:
            raise refuse("name_required", "Name required.", index)
        if key in seen:
            raise refuse("name_taken", f"Two stages are named {spec.name.strip()}.", index)
        seen[key] = index
    levels = [stages_mod.level_for(spec.branch) for spec in specs]
    work: Optional[int] = None
    first_branch: Optional[int] = None
    branches: dict[str, int] = {}
    for index, (spec, level) in enumerate(zip(specs, levels, strict=True)):
        text = spec.branch.strip()
        if level == stages_mod.WORK:
            # The project's branch prefix + `*`: a prefix is stored ending in `/` (`routes/projects.py`), so `fleet-*`
            # would silently become `fleet-/*`.
            if text.count("*") != 1 or not (text == "*" or text.endswith("/*")):
                raise refuse("branch_invalid", f"{spec.name.strip()}: a work branch is a prefix ending in /*, "
                             "like fleet/*.", index)
            if work is not None:
                raise refuse("pipeline_order",
                             f"Only one stage can build on work branches: {specs[work].name.strip()} or "
                             f"{spec.name.strip()}.", index)
            if first_branch is not None:
                raise refuse("pipeline_order",
                             f"{spec.name.strip()} must come before {specs[first_branch].name.strip()}.", index)
            work = index
        elif level == stages_mod.BRANCH:
            if text in branches:
                raise refuse("branch_taken",
                             f"{specs[branches[text]].name.strip()} and {spec.name.strip()} both use {text}.", index)
            branches[text] = index
            if first_branch is None:
                first_branch = index
    validate_rules(steps, levels)
    validate_gates(specs, steps, levels)
    return levels


#: A save's parsed gate lists and targets: `(stage index, phase)` → `[(sent id, columns)]`; stage index → columns.
ParsedLists = dict[tuple[int, str], list[tuple[Optional[str], dict[str, Any]]]]


def validate_gates(specs: Sequence[StageSpec], steps: Sequence[StepSpec],
                   levels: Sequence[str]) -> tuple[ParsedLists, dict[int, dict[str, Any]]]:
    """Each sent gate list and target as stored columns, or the 422 `{code, message, stage_index, gate_index}`:
    gates only on a merge or release step (D1), verify lists only on a branch stage, at most `GATES_MAX` per list,
    per-kind config (`app/pipeline_gates/schema.py`)."""
    lists: ParsedLists = {}
    targets: dict[int, dict[str, Any]] = {}
    for index, spec in enumerate(specs):
        step = steps[index - 1] if index else None
        sent = [(gate_defaults.BEFORE, step.gates if step is not None else _KEEP), (gate_defaults.AFTER, spec.after)]
        for phase, gates in sent:
            if gates is _KEEP:
                continue
            if gates and phase == gate_defaults.BEFORE and action_at(levels, index) not in GATED:
                raise gate_schema.refuse("gates_not_allowed", f"{spec.name.strip()}: this step can't have checks. "
                                         "Checks go on a merge or release step.", index)
            if gates and phase == gate_defaults.AFTER and levels[index] != stages_mod.BRANCH:
                raise gate_schema.refuse("gates_not_allowed", f"{spec.name.strip()}: only a stage with a branch "
                                         "can check a change after it arrives.", index)
            try:
                gate_schema.check_list(gates)
            except gate_schema.Refused as exc:
                raise gate_schema.refuse(exc.code, exc.message, index) from None
            # A verify list runs by itself on arrival: nobody is there to type a ticket.
            auto = phase == gate_defaults.AFTER or bool(step is not None and step.auto)
            parsed = []
            for gate_index, gate in enumerate(gates):
                try:
                    parsed.append((gate.id, gate_schema.parse_gate(gate, auto_step=auto)))
                except gate_schema.Refused as exc:
                    raise gate_schema.refuse(exc.code, exc.message, index, gate_index) from None
            lists[(index, phase)] = parsed
        if spec.target is not _KEEP:
            try:
                targets[index] = gate_schema.parse_target(spec.target)
            except gate_schema.Refused as exc:
                raise gate_schema.refuse(exc.code, exc.message, index) from None
    return lists, targets


def _project_fields(project: Project, specs: Sequence[StageSpec], levels: Sequence[str]) -> dict[str, Any]:
    """The project columns a save writes: the integration stage's branch and the work stage's prefix, when they
    differ from the project's."""
    fields: dict[str, Any] = {}
    for spec, level in zip(specs, levels, strict=True):
        text = spec.branch.strip()
        if level == stages_mod.BRANCH and "integration_branch" not in fields:
            fields["integration_branch"] = text
        elif level == stages_mod.WORK:
            prefix = text[:-1].strip()
            if prefix.rstrip("/") != (project.branch_prefix or "").rstrip("/"):
                fields["branch_prefix"] = prefix
    if fields.get("integration_branch") == project.integration_branch:
        fields.pop("integration_branch")
    return fields


def project_fields(project: Project, specs: Sequence[StageSpec], steps: Sequence[StepSpec]) -> dict[str, Any]:
    """The project columns a `save` of these would write (`_project_fields`: only a changed value), or its 422."""
    return _project_fields(project, specs, validate(specs, steps))


def refuse_without_repo(project: Project, levels: Sequence[str]) -> None:
    """422 `repo_required` when a project without a repository would hold a stage with a branch: only no-branch
    stages (the "Ideas only" preset) need no repository."""
    if not stages_mod.has_repo(project):
        index = next((i for i, level in enumerate(levels) if level != stages_mod.NONE), None)
        if index is not None:
            raise refuse("repo_required", "Stages with a branch need a repository. Connect one first.", index)


def save(session: Session, project: Project, actor: AdminUser, principal: Optional[Principal],
         specs: Sequence[StageSpec], steps: Sequence[StepSpec],
         before_commit: Optional[Callable[[dict[str, Any]], None]] = None, *,
         template_version: Optional[PipelineTemplateVersion] = None) -> list[guardrails.Finding]:
    """Replace the project's pipeline (module docstring): the stages are written first, then the project columns
    through the project's own write, whose commit carries both; with no project column to write, one commit here.
    Locks the project row, so a concurrent save or the tick's `stages.upgrade` waits. `before_commit(fields)` runs
    once the stages are flushed and before that commit, with the project columns about to be written: what else
    the same save writes (the freezes, its history row, `routes/releases.py::replace_pipeline`). A project without a
    repository may hold no-branch stages only (`refuse_without_repo`). Returns the org rules' Warn findings; a Block
    one refuses the save before anything is written (`check_guardrails`). On a project linked to an org template the save
    may not change the stages or a locked field (`linking.check_save`, 422 `template_locked`); `template_version` (a
    link or publish, `linking.write_version`) instead links the project to that version, `specs` being its stages in
    its order."""
    from app.releases import linking  # noqa: PLC0415 — linking imports this module

    levels = validate(specs, steps)
    refuse_without_repo(project, levels)
    warnings = check_guardrails(session, project, specs, levels, steps)
    lists, targets = validate_gates(specs, steps, levels)
    sent = [spec.id for spec in specs if spec.id is not None]
    if len(set(sent)) != len(sent):
        raise refuse("stage_repeated", "A stage is sent twice.")
    project = session.scalar(select(Project).where(Project.id == project.id).with_for_update()
                             .execution_options(populate_existing=True)) or project
    current = stages_for(session, project)
    shell_before = _shell_signature(session, project, current)
    merge_before = _merge_commands(session, project, current)
    if any(stage_id not in {s.id for s in current} for stage_id in sent):
        raise HTTPException(status_code=404, detail="unknown stage")
    if template_version is None:
        linking.check_save(session, project, list(specs), list(steps), current)
    template_keys = ({id(spec): str(stage["key"]) for spec, stage in zip(specs, template_version.body["stages"],
                                                                         strict=True)}
                     if template_version is not None else None)
    deployers = [_deployers(session, project, step.who) for step in steps]
    approvers = [_approvers(session, project, step.approvers, index) for index, step in enumerate(steps, start=1)]
    fields = _project_fields(project, specs, levels)
    stored = stages_mod.materialise(session, project)
    if project.pipeline_upgraded_at is None and stages_mod.old_version_gone(session):
        stages_mod.upgrade(session, project)
        stored = stages_mod.stages_of(session, project.id)
    # Until the project is upgraded a previous release may still serve: it reads position 0 as the integration
    # stage, so only branch-level stages are written; Ideas, Building and the Merge step stay as the upgrade will
    # write them, and a save that changes them is refused rather than dropped.
    branch_only = (stages_mod.has_repo(project) and project.pipeline_upgraded_at is None
                   and all(s.level == stages_mod.BRANCH for s in stored))
    if branch_only:
        _refuse_unupgraded_edits(read(session, project), specs, steps, levels)
    wanted = [(spec, level, steps[index - 1] if index else None, deployers[index - 1] if index else None,
               approvers[index - 1] if index else None, index)
              for index, (spec, level) in enumerate(zip(specs, levels, strict=True))
              if not branch_only or level == stages_mod.BRANCH]
    by_id = {s.id: s for s in stored}
    kept = {spec.id for spec, *_ in wanted if spec.id is not None}
    written: list[tuple[ReleaseStage, int]] = []
    # Free the positions and names first: both are unique per project, and a reorder or a swap of two names would
    # otherwise collide mid-write.
    stages_mod.shift(session, stored, [s.position + len(stored) + len(wanted) for s in stored])
    for stage in stored:
        stage.name = f"~{stage.id}"
    session.flush()
    for stage in stored:
        if stage.id not in kept:
            session.delete(stage)
    session.flush()
    first_branch = True
    for position, (spec, level, step, who, approved_by, sent_index) in enumerate(wanted):
        found = by_id.get(spec.id) if spec.id is not None else None
        if found is None:
            stage = ReleaseStage(org_id=project.org_id, project_id=project.id, description="")
            if spec.id is not None:
                stage.id = spec.id  # a synthesised default (`stages.for_read`) keeps the id its read showed
            stamp_actor(stage, actor, created=True)
            session.add(stage)
        else:
            stage = found
            stamp_actor(stage, actor)
        if spec.url is not _KEEP:
            url = (spec.url or "").strip() or None
            if url != stage.url:
                stage.url_source = "manual" if url is not None else None
                if found is not None:  # The last check was of the old address: the dot starts grey again.
                    session.execute(delete(StageHealth).where(StageHealth.stage_id == stage.id))
            stage.url = url
        stage.position = position
        stage.name = spec.name.strip()
        if template_keys is not None:
            stage.template_stage_key = template_keys[id(spec)]
        stage.level = level
        integration = level == stages_mod.BRANCH and first_branch
        stage.branch = spec.branch.strip() if level == stages_mod.BRANCH and not integration else None
        if level == stages_mod.BRANCH:
            first_branch = False
        if step is not None and not (branch_only and integration):
            stage.auto_deploy, stage.approvals_required, stage.deployers = step.auto, step.approvals, who
            _write_rules(stage, step, approved_by)
        elif stage.auto_deploy is None:
            stage.auto_deploy, stage.approvals_required = False, 0
        if sent_index in targets:
            target = targets[sent_index]
            stage.target_kind = target["target_kind"]
            stage.target_config = target["target_config"]
            stage.target_credential = target["target_credential"]
        written.append((stage, sent_index))
    session.flush()
    _write_gates(session, project, actor, written, levels, lists)
    _carry_full_gate(session, project, actor, merge_before, written, levels, lists)
    if _shell_signature(session, project, stages_mod.stages_of(session, project.id)) != shell_before:
        # What a node runs under a shell changed (a command gate's command, its required or credential, or the step
        # it sits on; a script target): the same authority as the project's full gate command (`routes/projects.py`'s
        # governance fields), never `projects:edit` alone. Nothing is committed yet: the refusal rolls it all back.
        from app.routes.projects import (  # noqa: PLC0415 — the route module imports this one
            _reject_governance_from_a_node_token,
            _require_authority_to_author_governance,
        )

        shell = {"gate_full_cmd": True}
        _reject_governance_from_a_node_token(shell, principal)
        _require_authority_to_author_governance(session, shell, actor, principal, project)
    refuse_unusable_credentials(session, project, actor, lists, targets)
    full_gate.write_mirror(session, project)
    if template_version is not None:
        project.pipeline_template_id = template_version.template_id
        project.pipeline_template_version_id = template_version.id
    session.flush()
    if before_commit is not None:
        before_commit(fields)
    if fields:
        from app.routes.projects import apply_project_update  # noqa: PLC0415 — the route module imports this one

        apply_project_update(session, project, fields, actor, principal)
    else:
        session.commit()
    return warnings


UNUSABLE_CREDENTIAL_MESSAGE = "You can't use this credential on this project. Pick one you can."
SHARED_PATH_MESSAGE = "Two credentials share this name. Rename one, then pick it again."
PERSONAL_CREDENTIAL_MESSAGE = "Pick a credential shared with this project, not a personal one."


def refuse_unusable_credentials(session: Session, project: Project, actor: AdminUser, lists: ParsedLists,
                                targets: dict[int, dict[str, Any]]) -> None:
    """422 `credential_invalid` `{stage_index, gate_index}` for the first sent gate `credential` or target
    `target_credential` that does not name exactly one credential `actor` may use on `project`: the broker's own rule
    for a task `actor` launches (`routes/agent/task_secrets.py::granted_to`), so a save never points a node at a
    secret its author could not fetch. Every sent path is checked, a kept one included: what a gate sends it to may
    have changed in the same save. The node resolves it the same way, under the launcher's authority. A personal
    `me/` path is refused outright: the broker resolves it to whoever launches the run, so a gate saved by one person
    would send another person's own secret wherever the gate points."""
    from app.routes.agent.task_secrets import granted_to  # noqa: PLC0415 — the broker's rule; routes import this module

    sent: list[tuple[str, int, Optional[int]]] = [
        (columns["credential"], index, gate_index)
        for (index, _phase), parsed in sorted(lists.items(), key=lambda item: (item[0][0],
                                                                             item[0][1] != gate_defaults.BEFORE))
        for gate_index, (_id, columns) in enumerate(parsed) if columns.get("credential")]
    sent += [(columns["target_credential"], index, None)
             for index, columns in sorted(targets.items()) if columns.get("target_credential")]
    if not sent:
        return
    for path, index, gate_index in sent:
        if path.lower().startswith("me/"):
            raise gate_schema.refuse("credential_invalid", PERSONAL_CREDENTIAL_MESSAGE, index, gate_index)
    principal = principal_for_user(session, actor)
    found: dict[str, int] = {}
    for path, index, gate_index in sent:
        if path.lower() not in found:
            found[path.lower()] = len(granted_to(session, principal, project.id, path))
        if found[path.lower()] != 1:
            message = SHARED_PATH_MESSAGE if found[path.lower()] > 1 else UNUSABLE_CREDENTIAL_MESSAGE
            raise gate_schema.refuse("credential_invalid", message, index, gate_index)


def _write_rules(stage: ReleaseStage, step: StepSpec, approvers: Optional[dict[str, Any]]) -> None:
    """The step's rule columns as the save sent them (`validate_rules` checked them)."""
    stage.approvers = approvers
    stage.requester_cant_approve = step.requester_cant_approve
    stage.approval_timeout_minutes = step.approval_timeout_minutes
    stage.on_timeout = step.on_timeout
    stage.approver_instructions = step.instructions or ""
    stage.soak_minutes = step.soak_minutes
    stage.allowed_hours = step.hours.out() if step.hours is not None else None
    stage.concurrency = step.concurrency


def write(session: Session, project: Project, actor: AdminUser, principal: Optional[Principal],
          specs: Sequence[StageSpec], steps: Sequence[StepSpec], *,
          before_commit: Optional[Callable[[dict[str, Any]], None]] = None,
          template_version: Optional[PipelineTemplateVersion] = None) -> list[guardrails.Finding]:
    """Every pipeline write: `save`, then the lane rows it frees go (a Merge step now by itself, or needing fewer
    approvals, lets its held rows go: `app/pipeline/gate.py`). Returns `save`'s Warn findings."""
    from app.pipeline import gate  # noqa: PLC0415 — gate imports this module

    warnings = save(session, project, actor, principal, specs, steps, before_commit=before_commit,
                    template_version=template_version)
    if gate.release_held(session, project):
        session.commit()
    return warnings


def _write_gates(session: Session, project: Project, actor: AdminUser, written: Sequence[tuple[ReleaseStage, int]],
                 levels: Sequence[str], lists: ParsedLists) -> None:
    """After the stages: seed every stage never seeded with its defaults (`gate_defaults.ensure`), drop the lists a
    stage's new step or level no longer admits, then replace each sent list by id-preserving upsert, in the order
    sent. Flushes."""
    stored = stages_mod.stages_of(session, project.id)
    gate_defaults.ensure(session, project, stored, stored)
    for stage, index in written:
        for phase in (gate_defaults.BEFORE, gate_defaults.AFTER):
            allowed = (action_at(levels, index) in GATED if phase == gate_defaults.BEFORE
                       else levels[index] == stages_mod.BRANCH)
            if (index, phase) not in lists and allowed:
                continue
            existing = {str(g.id): g for g in session.scalars(select(PipelineGate).where(
                PipelineGate.stage_id == stage.id, PipelineGate.phase == phase))}
            keep: set[str] = set()
            for position, (gate_id, columns) in enumerate(lists.get((index, phase), []) if allowed else []):
                gate = existing.get(str(gate_id)) if gate_id else None
                if gate is None:
                    gate = PipelineGate(org_id=project.org_id, project_id=project.id, stage_id=stage.id, phase=phase,
                                        description="")
                    stamp_actor(gate, actor, created=True)
                    session.add(gate)
                else:
                    keep.add(str(gate.id))
                    stamp_actor(gate, actor)
                gate.position = position
                _set_gate(gate, columns)
            for gate_id, gate in existing.items():
                if gate_id not in keep:
                    session.delete(gate)
    session.flush()


def _command_key(gate: PipelineGate) -> tuple[str, bool, str]:
    return str((gate.config or {}).get("run") or ""), bool(gate.required), gate.credential or ""


def _shell_signature(session: Session, project: Project, stages: Sequence[ReleaseStage]) -> tuple[Any, ...]:
    """What the pipeline runs under a shell: the merge step's command gates (the full gate), every other command
    gate as one multiset, and every script target — each command with its required flag, credential and on/off. Names, order and every other kind are not in it, so adding, removing or reordering stages that run
    no command never needs the full gate's authority."""
    lists = gate_defaults.gates_of(session, project, stages)
    merge = stages_mod.integration_stage(stages)
    merge_key = (merge.id, gate_defaults.BEFORE) if merge is not None else None
    # Off ones too: a command switched off today is one switch away from running (`full_gate.apply_command`).
    commands = {key: sorted((*_command_key(g), bool(g.enabled)) for g in gates if g.kind == "command")
                for key, gates in lists.items()}
    scripts = sorted((str((s.target_config or {}).get("run") or ""), s.target_credential or "")
                     for s in stages if gate_defaults.target_kind(s) == "script")
    elsewhere = sorted(c for key, cs in commands.items() if key != merge_key for c in cs)
    return commands.get(merge_key, []) if merge_key else [], elsewhere, scripts


def _merge_commands(session: Session, project: Project,
                    stages: Sequence[ReleaseStage]) -> tuple[Optional[UUID], list[PipelineGate]]:
    """The merge step's stage and its command gates, before a save."""
    merge = stages_mod.integration_stage(stages)
    if merge is None:
        return None, []
    gates = gate_defaults.gates_of(session, project, stages).get((merge.id, gate_defaults.BEFORE), [])
    return merge.id, [g for g in gates if g.kind == "command"]


def _carry_full_gate(session: Session, project: Project, actor: AdminUser,
                     before: tuple[Optional[UUID], list[PipelineGate]], written: Sequence[tuple[ReleaseStage, int]],
                     levels: Sequence[str], lists: ParsedLists) -> None:
    """The full gate follows the merge step: when a save makes another stage the integration stage and sends no
    list for its step, that stage takes the old merge step's command gates (first, ahead of its own), so reshaping
    the pipeline never drops the gate the lane runs. Flushes."""
    old_id, commands = before
    merge = stages_mod.integration_stage(stages_mod.stages_of(session, project.id))
    if merge is None or merge.id == old_id or not commands:
        return
    index = next((i for stage, i in written if stage.id == merge.id), None)
    if index is not None and (index, gate_defaults.BEFORE) in lists:
        return
    have = list(session.scalars(select(PipelineGate).where(
        PipelineGate.stage_id == merge.id, PipelineGate.phase == gate_defaults.BEFORE)
        .order_by(PipelineGate.position)))
    if any(g.kind == "command" for g in have):
        return
    for position, gate in enumerate(have, start=len(commands)):
        gate.position = position
    for position, old in enumerate(commands):
        gate = PipelineGate(org_id=project.org_id, project_id=project.id, stage_id=merge.id,
                            phase=gate_defaults.BEFORE, position=position, description="")
        stamp_actor(gate, actor, created=True)
        _set_gate(gate, {"name": old.name, "kind": "command", "enabled": old.enabled, "required": old.required,
                         "timeout_s": old.timeout_s, "retries": old.retries, "config": dict(old.config or {}),
                         "credential": old.credential})
        session.add(gate)
    session.flush()


def _set_gate(gate: PipelineGate, columns: dict[str, Any]) -> None:
    """A gate's stored columns from `gate_schema.parse_gate`."""
    gate.name, gate.kind, gate.config, gate.credential = (
        columns["name"], columns["kind"], columns["config"], columns["credential"])
    gate.enabled, gate.required = columns["enabled"], columns["required"]
    gate.timeout_s, gate.retries = columns["timeout_s"], columns["retries"]


def _refuse_unupgraded_edits(shown: dict[str, Any], specs: Sequence[StageSpec], steps: Sequence[StepSpec],
                             levels: Sequence[str]) -> None:
    """409 `pipeline_upgrading` when a save before the upgrade would change what only the upgrade writes: the
    no-branch and work stages (where they sit, their names, branches and steps) or the Merge step."""
    def signature(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [row for row in rows if row[2] != stages_mod.BRANCH]

    have = signature([
        (index, s["id"], s["level"], s["name"], s["branch"], s["url"],
         None if index == 0 else (shown["steps"][index - 1]["who"], shown["steps"][index - 1]["approvals"],
                                  shown["steps"][index - 1]["auto"]))
        for index, s in enumerate(shown["stages"])])
    want = signature([
        (index, str(spec.id) if spec.id is not None else None, level, spec.name.strip(), spec.branch.strip(),
         None if spec.url is _KEEP else ((spec.url or "").strip() or None),
         None if index == 0 else (steps[index - 1].who, steps[index - 1].approvals, steps[index - 1].auto))
        for index, (spec, level) in enumerate(zip(specs, levels, strict=True))])
    first = next((i for i, level in enumerate(levels) if level == stages_mod.BRANCH), None)
    shown_first = next((i for i, s in enumerate(shown["stages"]) if s["level"] == stages_mod.BRANCH), None)
    merge_kept = first is None or shown_first is None or first == 0 or (
        (steps[first - 1].who, steps[first - 1].approvals, steps[first - 1].auto)
        == (None, 0, True))
    if have != want or not merge_kept:
        raise HTTPException(status_code=409, detail={
            "code": "pipeline_upgrading",
            "message": "Ideas, Building and Merge can be changed in a few minutes, once the update finishes.",
        })
