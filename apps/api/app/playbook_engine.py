"""The playbook engine — the ONE bridge from a tenant playbook to the task runtime.

The standard depths (Sketch · Task · Epic) keep today's persona-keyed engine. A tenant playbook, picked from
the task form's Playbook depth while the org's plan has Playbooks on, runs here instead:

- **Snapshot.** Picking a playbook copies it onto the top-level row (`tasks.playbook`), so a later edit of
  the playbook never changes a task created from it.
- **Simple** (one stage): that row IS the stage (`tasks.playbook_stage`), and the operator may override its
  expert (the Persona select) and its model. **Complex** (two or more): the row is an `epic` container,
  each stage is a child carrying its resolved stage, and the Model select caps every stage's tier.
- **Stage → row.** A stage runs on the persona whose settle machinery matches its DELIVERABLE
  (`persona_for`) — the expert's own persona when the deliverable is its native one, so a built-in-shaped
  stage runs exactly as today. The runner's prompt is the stage's (`system_role`): the expert's prose ×
  the deliverable's contract (`app/playbook_contracts.py`), the composition the Playbooks editor previews.
  What it owes at settle is the deliverable's terms for that expert (`settle_terms`).

Everything else in the playbook sandbox (`playbook_vocabulary`/`_validation`/`_contracts`/`_prompt`) is
reached only through this module: `tests/test_playbook_isolation.py` holds the run path to it.

No route imports and one database read (`load`). The row inserts live with the other task builders in
`app/routes/tasks/`, which call the plans this module returns.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping, Optional
from uuid import UUID

from app import model_catalog
from app import playbook_vocabulary as v
from app.playbook_validation import _default_option, _direct_dependencies, _tier, derive, has_errors, validate

#: The persona whose settle machinery each deliverable needs: the persona of the expert that natively
#: delivers it (engineer → `coder`, analyst → `business_analysis`, …). Derived, so a new expert cannot
#: silently change it: the first native expert per deliverable wins, in `EXPERT_RECORDS` order.
_DELIVERABLE_PERSONA: Mapping[str, str] = {}
for _expert in v.EXPERT_RECORDS:
    _DELIVERABLE_PERSONA.setdefault(_expert.native_deliverable, _expert.persona)
assert set(_DELIVERABLE_PERSONA) == set(v.DELIVERABLES), "every deliverable needs a native expert"

#: Deliverables a one-stage playbook cannot run: a Plan files into a container and a Review settles over
#: one, and a one-stage row has neither.
_NEEDS_CONTAINER = (v.PLAN, v.REVIEW)

#: Tier order, cheapest first, for the complex playbook's cap.
_TIER_ORDER: tuple[str, ...] = (
    model_catalog.TIER_LOW, model_catalog.TIER_STANDARD, model_catalog.TIER_HIGH, model_catalog.TIER_MAX,
)


#: The one refusal of an org whose plan has Playbooks off — the engine's 422 and the routes' 403.
OFF_DETAIL = "Playbooks are not on this organization's plan."


class PlaybookNotRunnable(ValueError):
    """A playbook (or an override of it) this engine cannot run — the route answers 422 with the text."""


# ── Snapshot ─────────────────────────────────────────────────────────────────────────────────────────


def load(session: Any, org_id: Optional[UUID], playbook_id: UUID) -> dict[str, Any]:
    """The snapshot of `org_id`'s own playbook `playbook_id`, refused unless the org has playbooks on and
    the playbook is one this engine can run. Built-ins are not offered: the standard depths run them.
    "Not found" and "another org's" are one refusal."""
    from sqlalchemy import select

    from app.models.tasks import Playbook

    if not enabled(session, org_id):
        raise PlaybookNotRunnable(OFF_DETAIL)
    row = session.scalar(select(Playbook).where(Playbook.id == playbook_id, Playbook.org_id == org_id))
    if row is None:
        raise PlaybookNotRunnable(f"playbook {playbook_id} not found")
    snap = snapshot(row)
    reason = not_runnable_reason(snap)
    if reason is not None:
        raise PlaybookNotRunnable(f"{snap['name']}: {reason}")
    return snap


def snapshot(row: Any) -> dict[str, Any]:
    """The copy a task keeps of the playbook `row` (a `models.Playbook`), in the `PlaybookIn` wire shape plus
    its id. Taken once, when a row is given the playbook."""
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "entry": row.entry,
        "full_auto_default": row.full_auto_default,
        "default_seniority": row.default_seniority,
        "stages": copy.deepcopy(list(row.stages or [])),
    }


def stages(snap: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = snap.get("stages")
    return value if isinstance(value, list) else []


def is_simple(snap: Mapping[str, Any]) -> bool:
    """One stage: the row is the stage. Two or more: the row is a container of stage children."""
    return len(stages(snap)) == 1


def not_runnable_reason(pb: Mapping[str, Any]) -> Optional[str]:
    """Why this engine cannot run `pb`, as one line for the picker, or None."""
    if has_errors(validate(dict(pb))):
        return "Fix this playbook's errors first."
    if is_simple(pb):
        _, deliverable = _default_option(stages(pb)[0])
        if deliverable in _NEEDS_CONTAINER:
            return "A one-stage playbook cannot end in a Plan or a Review."
        return None
    all_stages = stages(pb)
    deliverables = [_default_option(stage)[1] for stage in all_stages]
    # A Plan settles only once it has filed something, so one that creates no stage could never settle.
    for stage, deliverable in zip(all_stages, deliverables):
        if deliverable == v.PLAN and not stage_indexes_created_by(pb, stage.get("key")):
            return "Each Plan stage must create at least one stage."
    # A Chat is an operator-ended console session: inside a container it would hold a slot and the whole
    # container open until someone completes it.
    if v.CONVERSATION in deliverables:
        return "A Chat stage can only run alone."
    # A Review runs the epic's finish engine — it settles over EVERY stage row and publishes the final
    # report — so it must be the one closing stage, filed by a Plan once the work exists.
    reviews = [i for i, deliverable in enumerate(deliverables) if deliverable == v.REVIEW]
    if reviews and (
        reviews != [len(all_stages) - 1]
        or (all_stages[reviews[0]].get("created") or {}).get("kind") != v.CREATED_BY
    ):
        return "A Review stage must be the last stage, created by a Plan."
    return None


def default_persona(pb: Mapping[str, Any]) -> Optional[str]:
    """A simple playbook's Persona-select seed: its stage's default expert's persona. None when complex."""
    if not is_simple(pb):
        return None
    expert, _ = _default_option(stages(pb)[0])
    record = v.EXPERTS.get(expert or "")
    return record.persona if record else None


# ── Stage resolution ─────────────────────────────────────────────────────────────────────────────────


def persona_for(expert: str, deliverable: str) -> str:
    """The persona a stage row carries: the expert's own when this is its native deliverable, else the
    deliverable's native persona (whose settle helper, rollup rule and filing licence the stage needs)."""
    record = v.EXPERTS[expert]
    return record.persona if record.native_deliverable == deliverable else _DELIVERABLE_PERSONA[deliverable]


def expert_for_persona(persona: str) -> str:
    """The expert a Persona-select override names. Raises when no expert runs as that persona."""
    for record in v.EXPERT_RECORDS:
        if record.persona == persona:
            return record.key
    raise PlaybookNotRunnable(f"no playbook expert runs as persona {persona!r}")


def persona_of_expert(expert: Optional[str]) -> Optional[str]:
    """The persona an expert runs as natively — what the form's Persona select shows for a stage row."""
    record = v.EXPERTS.get(expert or "")
    return record.persona if record else None


def cap_tier(tier: Optional[str], cap: Optional[str]) -> Optional[str]:
    """`tier`, lowered to `cap` when the cap is lower. A blank or unknown cap caps nothing."""
    cap = model_catalog.tier_of(cap) if cap else None
    if cap not in _TIER_ORDER or tier not in _TIER_ORDER:
        return tier
    return tier if _TIER_ORDER.index(tier) <= _TIER_ORDER.index(cap) else cap


def resolve_stage(
    snap: Mapping[str, Any], index: int, *, expert: Optional[str] = None, cap: Optional[str] = None,
) -> dict[str, Any]:
    """The `tasks.playbook_stage` value for stage `index`: everything a run needs, resolved once.

    `expert` overrides the stage's expert (a simple playbook's Persona select): on a "User selects" stage it
    picks among any expert with that expert's native deliverable, on a pinned stage it keeps the stage's
    deliverable. `cap` lowers the tier (a complex playbook's Model select)."""
    stage = stages(snap)[index]
    default_expert, deliverable = _default_option(stage)
    chosen = (stage.get("expert") or {}).get("kind") == v.EXPERT_CHOSEN
    expert_key = expert or default_expert
    if expert_key not in v.EXPERTS:
        raise PlaybookNotRunnable(f"stage {stage.get('key')!r} has no expert to run")
    if chosen:
        allowed = (stage.get("expert") or {}).get("allowed") or []
        if expert is not None and expert_key not in allowed:
            labels = ", ".join(v.EXPERTS[key].label for key in allowed if key in v.EXPERTS)
            raise PlaybookNotRunnable(f"stage {stage.get('label') or stage.get('key')!r} runs one of: {labels}")
        deliverable = v.EXPERTS[expert_key].native_deliverable
    if deliverable not in v.DELIVERABLES:
        raise PlaybookNotRunnable(f"stage {stage.get('key')!r} has no deliverable")
    derived = derive(dict(snap))
    receives = list(derived[index]["receives"]) if index < len(derived) else []
    return {
        "key": stage.get("key"),
        "label": stage.get("label") or stage.get("key"),
        "expert": expert_key,
        "deliverable": deliverable,
        "persona": persona_for(expert_key, deliverable),
        "tier": cap_tier(_tier(stage.get("seniority"), snap.get("default_seniority")), cap),
        "interviews": stage.get("interviews") or v.INTERVIEWS_NEVER,
        "receives": receives,
        "created": copy.deepcopy(stage.get("created") or {"kind": v.CREATED_AT_START}),
        "runs": stage.get("runs") or v.RUNS_ONCE,
        "extra_instructions": stage.get("extra_instructions") or "",
    }


def start_stage_indexes(snap: Mapping[str, Any]) -> list[int]:
    """The stages a complex playbook files when it is saved: those created at start, in stage order."""
    return [
        index for index, stage in enumerate(stages(snap))
        if (stage.get("created") or {}).get("kind", v.CREATED_AT_START) == v.CREATED_AT_START
    ]


def stage_indexes_created_by(snap: Mapping[str, Any], key: str) -> list[int]:
    """The stages the Plan stage `key` files when it settles, in stage order."""
    return [
        index for index, stage in enumerate(stages(snap))
        if (stage.get("created") or {}).get("kind") == v.CREATED_BY
        and (stage.get("created") or {}).get("stage") == key
    ]


def dependency_indexes(snap: Mapping[str, Any], index: int) -> list[int]:
    """The stages stage `index` waits on directly (`depends_on`, `all_earlier` expanded)."""
    return _direct_dependencies(stages(snap), index)


def creator_index(snap: Mapping[str, Any], index: int) -> Optional[int]:
    """The Plan stage that files stage `index`, or None for a stage created at start."""
    created = stages(snap)[index].get("created") or {}
    return index_of(snap, created.get("stage")) if created.get("kind") == v.CREATED_BY else None


def index_of(snap: Mapping[str, Any], key: Optional[str]) -> Optional[int]:
    return next((i for i, stage in enumerate(stages(snap)) if stage.get("key") == key), None)


def is_filed_by_plan(stage: Optional[Mapping[str, Any]], plan_key: Optional[str] = None) -> bool:
    """Whether a stage row is the product of a Plan stage — of the Plan stage `plan_key` when named (the
    archive, settle and append-pending guards each ask about ONE Plan stage)."""
    created = (stage or {}).get("created") or {}
    return created.get("kind") == v.CREATED_BY and (plan_key is None or created.get("stage") == plan_key)


def check_alone(stage: Mapping[str, Any]) -> None:
    """Refuse a resolved stage that cannot run as a one-stage row (an override can swap its deliverable)."""
    if stage["deliverable"] in _NEEDS_CONTAINER:
        raise PlaybookNotRunnable("A one-stage playbook cannot end in a Plan or a Review.")


def runs_per_item(stage: Mapping[str, Any]) -> bool:
    return stage.get("runs") == v.RUNS_PER_ITEM


def follows_full_auto(stage: Mapping[str, Any]) -> bool:
    """"Asks when Full Auto is off": the stage takes the switch live — its container's, or a one-stage
    row's own. Otherwise ("Never asks") it runs Full Auto."""
    return stage.get("interviews") == v.INTERVIEWS_FOLLOW


def effective_autonomous(stage: Mapping[str, Any], own: bool) -> bool:
    """A stage row's Full Auto given the switch it follows (`follows_full_auto`)."""
    return own if follows_full_auto(stage) else True


# ── What a run is told and owes ──────────────────────────────────────────────────────────────────────


def settle_terms(stage: Mapping[str, Any]):
    """The `dispatch_compose.settle_terms.SettleTerms` a stage row settles under: its deliverable's terms for its expert."""
    from app import playbook_contracts as contracts

    return contracts.DELIVERABLE_CONTRACTS[stage["deliverable"]].terms(stage["expert"])


def system_role(stage: Mapping[str, Any]) -> dict[str, Any]:
    """What `dispatch_compose.system_prompt.build_system_prompt_with` and `build_task_prompt` take for a stage row in place
    of the persona-keyed values — the same composition `app/playbook_prompt.py::compose_stage` previews."""
    from app import playbook_contracts as contracts

    contract = contracts.DELIVERABLE_CONTRACTS[stage["deliverable"]]
    return {
        "role": contracts.role(stage["expert"], stage["deliverable"]),
        "mode": contract.mode,
        "delivery": contract.delivery,
        "terms": contract.terms(stage["expert"]),
        "console_present": stage["deliverable"] == v.CONVERSATION,
        "asks_at_gate": contracts.asks_at_gate(stage["expert"]),
    }


def with_stage_instructions(agent_instructions: str, stage: Mapping[str, Any]) -> str:
    """The project's instructions, then the stage's own (`extra_instructions`), as one block."""
    extra = (stage.get("extra_instructions") or "").strip()
    return "\n\n".join(part for part in (agent_instructions.strip(), extra) if part)


# ── Projections ──────────────────────────────────────────────────────────────────────────────────────


def playbook_out(snap: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """`TaskOut.playbook`: which playbook a row runs, or None."""
    if not snap:
        return None
    return {"id": snap.get("id"), "name": snap.get("name"), "simple": is_simple(snap)}


def stage_out(stage: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """`TaskOut.playbook_stage`: the stage a row runs, labelled, or None."""
    if not stage:
        return None
    record = v.EXPERTS.get(stage.get("expert") or "")
    return {
        "key": stage.get("key"),
        "label": stage.get("label"),
        "expert": stage.get("expert"),
        "expert_label": record.label if record else stage.get("expert"),
        # What the form's Persona select shows: the expert's own persona, which may differ from the row's
        # settle persona (an off-native pair) — resent unchanged, it keeps the expert (`_apply_playbook`).
        "expert_persona": record.persona if record else None,
        "deliverable": stage.get("deliverable"),
    }


def picker_fields(pb: Mapping[str, Any]) -> dict[str, Any]:
    """What the task form's playbook picker needs beside a `GET /api/playbooks` row."""
    reason = not_runnable_reason(pb)
    return {
        "runnable": reason is None,
        "not_runnable_reason": reason,
        "simple": is_simple(pb),
        "default_persona": default_persona(pb),
    }


def enabled(session: Any, org: Optional[UUID]) -> bool:
    """Whether `org`'s billing plan has Playbooks on (`plan_terms` — the plan, then the org's override)."""
    from app import plan_terms

    return plan_terms.switch_on(session, org, "playbooks_enabled")
