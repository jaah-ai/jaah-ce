"""The ONE control for the full gate (PRD D13, architecture §5): the merge step's gate list.

Every reader that once read `projects.gate_full_cmd` reads this module instead: runner prompts and the hook config
(`full_gate_command`), the merge lane's batch and certificate (`merge_step_gates`, `gates_hash`), and the SQL filters
(`has_full_gate_sql`). `projects.gate_full_cmd` is a write-only mirror for the two-release overlap (`write_mirror`),
written on every pipeline save and by the `PUT /projects` shim (`apply_command`), so an older release or toolbox
gates with the right command; nothing here reads it except to seed a stage never seeded (`defaults.seed_command`).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import ColumnElement, and_, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from app.models.projects import Project
from app.models.releases import PipelineGate, ReleaseStage
from app.pipeline_gates import defaults
from app.releases import stages as stages_mod

#: The kinds a lane certificate is bound to: their verdict on a tree does not change with time. Webhook, probe,
#: alerts and ticket answers do, so they never count toward a certificate (and re-run on a certified tree).
CERTIFIED_KINDS = ("command", "ci")


def _merge_stage(stages: Sequence[ReleaseStage]) -> Optional[ReleaseStage]:
    return stages_mod.integration_stage(stages)


def merge_step_gates(session: Session, project: Project,
                     stages: Optional[Sequence[ReleaseStage]] = None) -> list[PipelineGate]:
    """The `before` list of the integration stage (the Merge / Start-and-merge step), in order, off and advisory
    gates included. `stages` defaults to the pipeline a read shows."""
    if stages is None:
        stages = _pipeline(session, project)
    stage = _merge_stage(stages)
    if stage is None:
        return []
    return defaults.gates_of(session, project, [*stages]).get((stage.id, defaults.BEFORE), [])


def _pipeline(session: Session, project: Project) -> list[ReleaseStage]:
    if stages_mod.has_repo(project):
        return stages_mod.for_read(session, project)
    return stages_mod.stages_of(session, project.id) or stages_mod._defaults(project)


def command_of(gates: Iterable[PipelineGate]) -> str:
    """The enabled command gates' commands joined with ` && `, or `""`."""
    runs = [str((g.config or {}).get("run") or "").strip() for g in gates if g.kind == "command" and g.enabled]
    return " && ".join(r for r in runs if r)


def full_gate_command(session: Session, project: Project) -> str:
    """What runner prompts substitute for `{gate_full_cmd}` and the hook config serves as `GATE_FULL_CMD`."""
    return command_of(merge_step_gates(session, project))


def full_gate_commands(session: Session, projects: Iterable[Project]) -> dict[int, str]:
    """`full_gate_command` for many projects in a few queries (a project list): projects whose integration stage is
    seeded read their stored gates; every other reads its defaults."""
    projects = list(projects)
    if not projects:
        return {}
    ids = [p.id for p in projects]
    integration = {
        s.project_id: s for s in session.scalars(select(ReleaseStage).where(
            ReleaseStage.project_id.in_(ids), ReleaseStage.level == stages_mod.BRANCH,
            ReleaseStage.branch.is_(None)))
    }
    seeded = {pid: s.id for pid, s in integration.items() if s.gates_seeded_at is not None}
    runs: dict[int, list[PipelineGate]] = {}
    if seeded:
        for gate in session.scalars(select(PipelineGate).where(
                PipelineGate.stage_id.in_(list(seeded.values())), PipelineGate.phase == defaults.BEFORE)
                .order_by(PipelineGate.position)):
            runs.setdefault(gate.project_id, []).append(gate)
    return {p.id: command_of(runs.get(p.id, [])) if p.id in seeded else defaults.seed_command(p) for p in projects}


def gates_hash(gates: Sequence[PipelineGate]) -> str:
    """The lane certificate's key. Exactly one enabled, required command gate (every migrated project): the bytes
    of today's `sha256(gate_full_cmd.strip())`, so certificates earned before the upgrade stay valid (risk R2); no
    gate at all: `sha256("")`, today's blank gate. Otherwise `sha256` of the canonical JSON of `(kind, config,
    required, enabled)` over the command and CI gates, in order: names, timeouts and time-dependent kinds never
    count."""
    if not gates:
        return hashlib.sha256(b"").hexdigest()
    if len(gates) == 1 and gates[0].kind == "command" and gates[0].enabled and gates[0].required:
        return hashlib.sha256(str((gates[0].config or {}).get("run") or "").strip().encode()).hexdigest()
    canonical = json.dumps(
        [[g.kind, g.config or {}, bool(g.required), bool(g.enabled)] for g in gates if g.kind in CERTIFIED_KINDS],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def current_hash(session: Session, project: Project) -> str:
    return gates_hash(merge_step_gates(session, project))


def wire(gate: PipelineGate, *, project: Optional[Project] = None, stage: Optional[str] = None,
         step: Optional[str] = None) -> dict[str, Any]:
    """`GateWire` (architecture §3.4): the gate as the node driver reads it; never a credential value. `credential` is
    a scope path the driver fetches through its own task's broker (`routes/agent/task_secrets.py::get_task_credential`),
    so it resolves under that task's launcher's authority and never more; the save checked its author may use it
    (`app/releases/pipeline.py::refuse_unusable_credentials`)."""
    out: dict[str, Any] = {
        "id": str(gate.id), "name": gate.name, "kind": gate.kind, "enabled": bool(gate.enabled),
        "required": bool(gate.required), "timeout_s": int(gate.timeout_s), "retries": int(gate.retries),
        "config": gate.config or {}, "credential": gate.credential,
    }
    if project is not None:
        out["project"] = project.name
    if stage is not None:
        out["stage"] = stage
    if step is not None:
        out["step"] = step
    return out


def legacy_expressible(gates: Sequence[PipelineGate]) -> bool:
    """Whether a toolbox that predates gates runs this merge list faithfully off `gate_full_cmd` alone: no gate, or
    one enabled, required command gate with today's 90-minute limit and one rerun (the D14 default)."""
    if not gates:
        return True
    if len(gates) != 1:
        return False
    gate = gates[0]
    return (gate.kind == "command" and gate.enabled and gate.required
            and gate.timeout_s == defaults.FULL_GATE_TIMEOUT_S and gate.retries == defaults.FULL_GATE_RETRIES)


def has_full_gate_sql(project: Any = Project) -> ColumnElement[bool]:
    """SQL: the project's merge step runs a command (replaces `btrim(gate_full_cmd) <> ''`). A seeded integration
    stage answers from its enabled command gates; a project without one reads its seed command (`defaults`)."""
    seeded_stage = and_(ReleaseStage.project_id == project.id, ReleaseStage.level == stages_mod.BRANCH,
                        ReleaseStage.branch.is_(None), ReleaseStage.gates_seeded_at.is_not(None))
    command = func.btrim(func.coalesce(PipelineGate.config["run"].astext, ""), " \t\r\n")
    stored = exists(select(PipelineGate.id).join(ReleaseStage, ReleaseStage.id == PipelineGate.stage_id).where(
        seeded_stage, PipelineGate.phase == defaults.BEFORE, PipelineGate.kind == "command",
        PipelineGate.enabled.is_(True), func.length(command) > 0))
    unseeded = and_(not_(exists(select(ReleaseStage.id).where(seeded_stage))),
                    func.length(func.btrim(project.gate_full_cmd, " \t\r\n")) > 0)
    return or_(stored, unseeded)


def write_mirror(session: Session, project: Project) -> None:
    """`projects.gate_full_cmd` := the merge step's command, for an older release or toolbox still reading it."""
    project.gate_full_cmd = full_gate_command(session, project)


def apply_command(session: Session, project: Project, command: str, actor: Any = None) -> None:
    """The `PUT /projects` shim for one release (§5.2): `gate_full_cmd` sets the merge step's command gate — the
    first one's command, a new default Full gate when it has none, or removes the command gates when blank — then the
    mirror. Flushes."""
    from app.actors import stamp_actor  # noqa: PLC0415

    stages = stages_mod.materialise(session, project) if stages_mod.has_repo(project) else []
    stage = _merge_stage(stages)
    command = (command or "").strip()
    if stage is None:
        project.gate_full_cmd = command
        return
    if command == full_gate_command(session, project):
        # The form resends the field it read (the joined enabled commands) on every save: an unchanged value
        # leaves the gates as they are.
        write_mirror(session, project)
        return
    defaults.ensure(session, project, [stage], stages)
    gates = list(session.scalars(select(PipelineGate).where(
        PipelineGate.stage_id == stage.id, PipelineGate.phase == defaults.BEFORE).order_by(PipelineGate.position)))
    commands = [g for g in gates if g.kind == "command"]
    if not command:
        for gate in commands:
            session.delete(gate)
        session.flush()
        _renumber([g for g in gates if g.kind != "command"])
    elif any(g.enabled for g in commands):
        # One command replaces them all, where the first one that runs stood: the field names the whole command.
        # An off one is never switched on: it was saved off, with settings nobody re-read for this command.
        kept = next(g for g in commands if g.enabled)
        kept.config = {**(kept.config or {}), "run": command}
        if actor is not None:
            stamp_actor(kept, actor)
        for gate in commands:
            if gate is not kept:
                session.delete(gate)
        session.flush()
        _renumber([g for g in gates if g.kind != "command" or g is kept])
    else:
        for gate in commands:  # only off ones: a fresh default Full gate replaces them
            session.delete(gate)
        session.flush()
        gates = [g for g in gates if g.kind != "command"]
        _renumber(gates)
        for gate in gates:
            gate.position += 1
        new = defaults.full_gate_gate(project, stage, command, fresh=True)
        if actor is not None:
            stamp_actor(new, actor, created=True)
        session.add(new)
    session.flush()
    write_mirror(session, project)


def _renumber(gates: Sequence[PipelineGate]) -> None:
    for position, gate in enumerate(gates):
        gate.position = position
