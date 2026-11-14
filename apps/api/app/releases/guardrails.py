"""Org pipeline guardrails: three built-in rules on the step into production, each Off, Warn or Block per org.

- `production_approval` (G1) — the step needs at least `approvals_min` approvals (1–5, default 1). Its other half,
  "whoever asked can't approve", holds for every step today (`app/releases/access.py`), so there is nothing to check.
- `no_auto_production` (G2) — the step is pressed by hand, never by itself.
- `production_maintainers` (G3) — the step's effective Who (its own, else the release default) names no Project
  Developer or Project Viewer; named people are allowed.

Production is `stages.production_index`'s; a pipeline without one passes every rule. Every pipeline write evaluates the
org's rules on the pipeline it would store (`app/releases/pipeline.py::save`): a Block finding refuses the save with
nothing written, a Warn finding saves and is returned. Block applies to everyone, operators included; the way past it
is an Org Owner/Admin changing the rule. Turning a rule on never touches a stored pipeline.

Absence is not a state: an org's three rows are written Off the first time they are read for it (`for_org`), so a mode
change's who and when is the row's own stamp.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.releases import GUARDRAIL_RULES, PipelineGuardrail
from app.releases import stages as stages_mod

if TYPE_CHECKING:
    from app.releases.pipeline import StepSpec

PRODUCTION_APPROVAL, NO_AUTO_PRODUCTION, PRODUCTION_MAINTAINERS = GUARDRAIL_RULES
OFF, WARN, BLOCK = "off", "warn", "block"
APPROVALS_MIN_DEFAULT = 1

#: Each rule's title, as Settings › Pipeline rules shows it.
TITLES = {
    PRODUCTION_APPROVAL: "Production needs approval",
    NO_AUTO_PRODUCTION: "No automatic production",
    PRODUCTION_MAINTAINERS: "Production pressed by maintainers",
}
#: The roles G3 refuses on the step into production — every built-in role below Project Maintainer — as the Pipeline
#: tab's Who picker names them.
_BELOW_MAINTAINER = {"project_developer": "Developer", "project_viewer": "Viewer"}


class Setting(Protocol):
    """What `evaluate` reads off a rule: a `PipelineGuardrail` row, or an `As` stand-in."""

    @property
    def rule(self) -> str: ...
    @property
    def mode(self) -> str: ...
    @property
    def approvals_min(self) -> Optional[int]: ...


@dataclass(frozen=True)
class As:
    """A rule's setting with another mode, never stored: Settings › Pipeline rules counts what breaks each rule as if
    it were on."""

    rule: str
    mode: str
    approvals_min: Optional[int]


@dataclass(frozen=True)
class Finding:
    """One rule a pipeline breaks: `stage_index` is the production stage (the step into it), `message` one sentence
    naming the rule, the stage and the fix."""

    rule: str
    mode: str
    stage_index: int
    message: str

    def out(self) -> dict[str, Any]:
        """As a pipeline response's `warnings` carries it."""
        return {"rule": self.rule, "stage_index": self.stage_index, "message": self.message}


def for_org(session: Session, org_id: Optional[UUID]) -> list[PipelineGuardrail]:
    """The org's three rule rows in `GUARDRAIL_RULES` order, writing the missing ones Off first (idempotent and
    race-safe: `ON CONFLICT DO NOTHING`). Flushes, never commits."""
    if org_id is None:
        return []
    rows = _stored(session, org_id)
    missing = [rule for rule in GUARDRAIL_RULES if rule not in rows]
    if missing:
        session.execute(insert(PipelineGuardrail).values([
            {"org_id": org_id, "rule": rule, "name": TITLES[rule], "mode": OFF,
             "approvals_min": APPROVALS_MIN_DEFAULT if rule == PRODUCTION_APPROVAL else None}
            for rule in missing
        ]).on_conflict_do_nothing(index_elements=["org_id", "rule"]))
        rows = _stored(session, org_id)
    return [rows[rule] for rule in GUARDRAIL_RULES]


def enabled(session: Session, org_id: Optional[UUID]) -> list[PipelineGuardrail]:
    """The org's rules that are not Off, read without writing (a rule never written is Off): what a read evaluates."""
    if org_id is None:
        return []
    return [row for row in _stored(session, org_id).values() if row.mode != OFF]


def _stored(session: Session, org_id: UUID) -> dict[str, PipelineGuardrail]:
    return {row.rule: row for row in session.scalars(
        select(PipelineGuardrail).where(PipelineGuardrail.org_id == org_id)
        .execution_options(populate_existing=True))}


def evaluate(rules: Sequence[Setting], names: Sequence[str], levels: Sequence[str],
             steps: Sequence["StepSpec"]) -> list[Finding]:
    """The findings of every rule that is not Off on a pipeline of these stage `names` and `levels`, whose `steps[i]`
    is the step into stage `i + 1`. None without a production stage."""
    index = stages_mod.production_index(levels)
    if index is None or index == 0:
        return []
    step = steps[index - 1]
    stage = names[index].strip()
    found = []
    for row in rules:
        if row.mode == OFF:
            continue
        message = _broken(row, stage, step)
        if message is not None:
            found.append(Finding(row.rule, row.mode, index, message))
    return found


def _broken(row: Setting, stage: str, step: "StepSpec") -> Optional[str]:
    """`row`'s message when `step` (the step into `stage`, production) breaks it, else None."""
    from app.releases import pipeline  # noqa: PLC0415 — `pipeline` imports this module

    if row.rule == PRODUCTION_APPROVAL:
        need = row.approvals_min or APPROVALS_MIN_DEFAULT
        if step.approvals < need:
            plural = "" if need == 1 else "s"
            return (f"Org rule: the step into {stage} needs at least {need} approval{plural}. "
                    f"Set Approvals to {need} or more.")
    elif row.rule == NO_AUTO_PRODUCTION:
        if step.auto:
            return f"Org rule: the step into {stage} must not run by itself. Set it to By hand."
    elif row.rule == PRODUCTION_MAINTAINERS:
        who = step.who if step.who is not None else pipeline.default_who(pipeline.RELEASE)
        below = [label for role, label in _BELOW_MAINTAINER.items() if role in (who.get("roles") or [])]
        if below:
            return (f"Org rule: only maintainers and above may press the step into {stage}. "
                    f"Untick {' and '.join(below)}.")
    return None


def evaluate_stored(session: Session, project: Any, rules: Sequence[Setting]) -> list[Finding]:
    """`evaluate` on the project's pipeline as a read shows it (`pipeline.stages_for`): what breaks today."""
    from app.releases import pipeline  # noqa: PLC0415 — `pipeline` imports this module

    if not rules:
        return []
    stages = pipeline.stages_for(session, project)
    steps = [pipeline.StepSpec(s.deployers, s.approvals_required or 0, bool(s.auto_deploy)) for s in stages[1:]]
    return evaluate(rules, [s.name for s in stages], [s.level for s in stages], steps)
