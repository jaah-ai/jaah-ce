"""Every stage's default gates and target (D14 as corrected, architecture §6) — the ONE home the migration's
backfill mirrors and every reader uses for a stage whose gates were never written (`gates_seeded_at` NULL: a stage
synthesised by a read, written by an older release, or not yet materialised).

- The integration stage (the Merge / Start-and-merge step into it): the command gate "Full gate" — the project's
  full gate command (the org default `project_default_gate_full_cmd` on a new project), required, 5400 s, one retry —
  when that command is not blank; else none.
- Every later branch stage (a release step): the CI gate "All checks on the release PR" (wait for every check),
  today's `pr checks --watch`.
- No-branch and work stages: none (their steps take no gates). No `after` gates anywhere.
- Target: `forge_env` where the stage names a `deploy_environment`, else `none` with its url kept. The project's
  `deploy_workflows` are the Production `forge_env` target's arrival rule, never a Before gate (risk R3).

`defaults_for` never writes; `ensure` writes them for a stored stage once, under the stage's row lock, and marks it
seeded so a list a person later empties stays empty.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models.projects import Project
from app.models.releases import PipelineGate, ReleaseStage
from app.releases import stages as stages_mod

BEFORE, AFTER = "before", "after"
FULL_GATE_NAME = "Full gate"
FULL_GATE_TIMEOUT_S = 5400
FULL_GATE_RETRIES = 1
RELEASE_CHECKS_NAME = "All checks on the release PR"
RELEASE_CHECKS_TIMEOUT_S = 7200
RELEASE_CHECKS_CONFIG: dict[str, Any] = {"mode": "wait", "workflow": None, "all_checks": True}

#: Namespace of a default gate's id: a read's and the write's are the same, so a save sending a gate a read showed
#: before the stage was seeded keeps it.
_GATE_NS = UUID("6f1b3c52-9a0e-4c8d-b7e1-3d2a5c9f0e41")

Key = tuple[UUID, str]


def _default_id(stage_id: UUID, phase: str, position: int) -> UUID:
    return uuid5(_GATE_NS, f"{stage_id}:{phase}:{position}")


def full_gate_gate(project: Project, stage: ReleaseStage, command: str, position: int = 0, *,
                   fresh: bool = False) -> PipelineGate:
    """The default command gate on the merge step, unsaved. `fresh`: no id, the database mints one."""
    ids = {} if fresh else {"id": _default_id(stage.id, BEFORE, position)}
    return PipelineGate(
        **ids, org_id=project.org_id, project_id=project.id, stage_id=stage.id,
        phase=BEFORE, position=position, kind="command", enabled=True, required=True,
        timeout_s=FULL_GATE_TIMEOUT_S, retries=FULL_GATE_RETRIES, config={"run": command.strip()}, credential=None,
        name=FULL_GATE_NAME, description="",
    )


def _release_checks(project: Project, stage: ReleaseStage) -> PipelineGate:
    return PipelineGate(
        id=_default_id(stage.id, BEFORE, 0), org_id=project.org_id, project_id=project.id, stage_id=stage.id,
        phase=BEFORE, position=0, kind="ci", enabled=True, required=True, timeout_s=RELEASE_CHECKS_TIMEOUT_S,
        retries=0, config=dict(RELEASE_CHECKS_CONFIG), credential=None, name=RELEASE_CHECKS_NAME, description="",
    )


def seed_command(project: Project) -> str:
    """The command the default Full gate runs: the project's full gate command as created or migrated (the org
    default for a new project). Read only to seed a stage never seeded: every later read is the gate's own."""
    return (project.gate_full_cmd or "").strip()


def defaults_for(project: Project, stages: Sequence[ReleaseStage]) -> dict[Key, list[PipelineGate]]:
    """Every stage's default lists, unsaved, keyed `(stage_id, phase)`; a stage with none has no key."""
    out: dict[Key, list[PipelineGate]] = {}
    integration = stages_mod.integration_stage(stages)
    command = seed_command(project)
    for stage in stages_mod.branch_stages(stages):
        if integration is not None and stage.id == integration.id:
            if command:
                out[(stage.id, BEFORE)] = [full_gate_gate(project, stage, command)]
        else:
            out[(stage.id, BEFORE)] = [_release_checks(project, stage)]
    return out


def default_target(stage: ReleaseStage) -> str:
    return "forge_env" if (stage.deploy_environment or "").strip() else "none"


def target_kind(stage: ReleaseStage) -> str:
    """The stage's target as every reader shows it: its own once seeded, else the default."""
    if stage.gates_seeded_at is None and (stage.target_kind or "none") == "none":
        return default_target(stage)
    return stage.target_kind or "none"


def gates_of(session: Session, project: Project, stages: Sequence[ReleaseStage]) -> dict[Key, list[PipelineGate]]:
    """Every list of `stages` as readers see it, in position order: a seeded stage's stored rows, else its
    defaults. Never writes."""
    seeded = [s.id for s in stages if s.gates_seeded_at is not None]
    out: dict[Key, list[PipelineGate]] = {}
    if seeded:
        for gate in session.scalars(select(PipelineGate).where(PipelineGate.stage_id.in_(seeded))
                                    .order_by(PipelineGate.phase, PipelineGate.position)):
            out.setdefault((gate.stage_id, gate.phase), []).append(gate)
    unseeded = [s for s in stages if s.gates_seeded_at is None]
    if unseeded:
        defaults = defaults_for(project, stages)
        for stage in unseeded:
            for phase in (BEFORE, AFTER):
                if (stage.id, phase) in defaults:
                    out[(stage.id, phase)] = defaults[(stage.id, phase)]
    return out


def ensure(session: Session, project: Project, stages: Iterable[ReleaseStage],
           all_stages: Optional[Sequence[ReleaseStage]] = None) -> None:
    """Write the defaults of every stored, never-seeded stage among `stages` and mark it seeded. Locks those stage
    rows first, so a concurrent writer waits and then finds them seeded. `all_stages` (the project's whole pipeline,
    `stages` by default) decides which stage is the integration stage. Flushes."""
    wanted = [s.id for s in stages if s.gates_seeded_at is None]
    if not wanted:
        return
    locked = list(session.scalars(
        select(ReleaseStage).where(ReleaseStage.id.in_(wanted), ReleaseStage.gates_seeded_at.is_(None))
        .with_for_update().execution_options(populate_existing=True)))
    if not locked:
        return
    pipeline = list(all_stages) if all_stages is not None else stages_mod.stages_of(session, project.id)
    defaults = defaults_for(project, pipeline)
    have = set(session.execute(select(PipelineGate.stage_id, PipelineGate.phase)
                               .where(PipelineGate.stage_id.in_([s.id for s in locked]))).tuples())
    now = utcnow()
    for stage in locked:
        for phase in (BEFORE, AFTER):
            if (stage.id, phase) in have:
                continue
            for gate in defaults.get((stage.id, phase), []):
                session.add(gate)
        if (stage.target_kind or "none") == "none":
            stage.target_kind = default_target(stage)
        stage.gates_seeded_at = now
    session.flush()
