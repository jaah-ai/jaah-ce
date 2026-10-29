"""Render one playbook stage's dispatch prompt — the "Preview prompt" of the Playbooks editor.

`compose_stage` renders `{system_prompt, task_prompt, model_tier}` for a stage from what the stage IS — its
expert, deliverable, seniority, interviews and Full Auto value, the playbook's shape — against the fixed
`SAMPLE_FIXTURE` task and project (D4). It drives `app/dispatch_compose/`'s data-driven building blocks
(`render_role_section_with` via `build_system_prompt_with`, `build_task_prompt`) with composition data from
`app/playbook_contracts.py`; it never takes a persona and never takes the persona-keyed path, so any expert
× deliverable pair renders (D2). `tests/test_playbook_parity.py` holds every built-in stage byte-equal to
what today's dispatch renders for the matching persona.

Pure: no I/O beyond the charter and policy files `dispatch_compose` reads, and no database beyond the one
read `build_system_prompt_with` makes itself — the operator's runner-behaviour knobs (`_flagged_section`),
so a preview shows the knob-gated sections exactly as a dispatch would today. A real stage row is composed
by `dispatch_compose.compose.compose` through `playbook_engine.system_role` — the same contracts, its own task —
and no task module imports this one (`tests/test_playbook_isolation.py`).
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional

from app.dispatch_compose import input_staging, system_prompt as dc_system_prompt, task_prompt as dc_task_prompt
from app import agent_providers, playbook_contracts as contracts
from app import playbook_vocabulary as v
from app.persona_registry import MODE_BUILD
from app.playbook_validation import derive

#: The one sample task and project every preview renders against (D4), and the parity fixture.
SAMPLE_FIXTURE: Mapping[str, Any] = MappingProxyType({
    "owner_repo": "sample-org/sample-repo",
    "project": "sample",
    "title": "Sample task",
    "body": "A sample task body. Build what it describes.",
    "qa_trail": "",
    "integration_base": "develop",
    "branch_prefix": "",
    "gate_quick_cmd": "./dc check quick",
    "gate_full_cmd": "./dc check full",
    "provider": agent_providers.DEFAULT_PROVIDER,
    "console_task_id": 4242,
    "state": "ready",
    "hub_repo": False,
    # Rendered iff the stage has a direct predecessor (`has_predecessor`), else empty.
    "prior_stage_output": "=== Prior stage ===\nThe prior stage settled its output.",
    # The row id and owning task of a prerequisite document: one per kind the stage receives.
    "input_artifact_id": "019a0000-0000-7000-8000-0000000a0001",
    "input_task_id": 4241,
    "input_size_bytes": 1024,
    # The per-dispatch operator knobs, fixed on both parity sides.
    "prior_session_ended": False,
    "finish_follow_up": False,
    "resume_worktree": "",
    "model_override": "",
})


class StageNotRenderable(ValueError):
    """The stage key, expert or deliverable does not resolve — the route answers 422."""


def _plain(pb: Any) -> dict[str, Any]:
    return pb.model_dump(mode="json") if hasattr(pb, "model_dump") else dict(pb)


def has_predecessor(stage: Mapping[str, Any]) -> bool:
    """Whether the stage has a direct predecessor: it waits on a stage, or a stage creates it."""
    depends = (stage.get("depends_on") or {}).get("kind")
    created = (stage.get("created") or {}).get("kind")
    return depends not in (None, v.DEPENDS_NONE) or created == v.CREATED_BY


def input_rows(kinds: list[str], fixture: Mapping[str, Any] = SAMPLE_FIXTURE) -> list[dict[str, Any]]:
    """One fixed prerequisite-artifact row per kind the stage receives."""
    return [
        {
            "id": fixture["input_artifact_id"],
            "task_id": fixture["input_task_id"],
            "kind": kind,
            "size_bytes": fixture["input_size_bytes"],
        }
        for kind in kinds
    ]


def _resolve(stage: Mapping[str, Any], expert: Optional[str]) -> tuple[str, str]:
    spec = stage.get("expert") or {}
    if spec.get("kind") == v.EXPERT_CHOSEN:
        chosen = expert or spec.get("default")
        if chosen not in (spec.get("allowed") or []) or chosen not in v.EXPERTS:
            raise StageNotRenderable(f"expert {chosen!r} is not allowed on this stage")
        return chosen, v.EXPERTS[chosen].native_deliverable
    pinned = spec.get("expert")
    if expert is not None and expert != pinned:
        raise StageNotRenderable(f"this stage always runs {pinned!r}")
    deliverable = stage.get("deliverable")
    if pinned not in v.EXPERTS or deliverable not in contracts.DELIVERABLE_CONTRACTS:
        raise StageNotRenderable("the stage's expert or deliverable is unknown")
    return pinned, deliverable


def compose_stage(
    pb: Any, stage_key: str, *, expert: Optional[str] = None, full_auto: Optional[bool] = None
) -> dict[str, str]:
    """`{system_prompt, task_prompt, model_tier}` for the stage `stage_key` of playbook `pb` (the `PlaybookIn`
    wire shape, or the model). `expert` picks among a chosen-at-start stage's allowed experts (default: its
    default); `full_auto` defaults to the playbook's `full_auto_default`."""
    pb = _plain(pb)
    stages = pb.get("stages") or []
    index = next((i for i, stage in enumerate(stages) if stage.get("key") == stage_key), None)
    if index is None:
        raise StageNotRenderable(f"no stage {stage_key!r}")
    stage = stages[index]
    expert_key, deliverable = _resolve(stage, expert)
    contract = contracts.DELIVERABLE_CONTRACTS[deliverable]
    fx = SAMPLE_FIXTURE
    conversation = deliverable == v.CONVERSATION
    epic = len(stages) > 1
    instructions = stage.get("extra_instructions") or ""
    full_auto = pb.get("full_auto_default", True) if full_auto is None else full_auto
    role = contracts.role(expert_key, deliverable)

    system_prompt = dc_system_prompt.build_system_prompt_with(
        role,
        mode=contract.mode,
        delivery=contract.delivery,
        terms=contract.terms(expert_key),
        console_present=conversation,
        integration_branch=fx["integration_base"],
        branch_prefix=fx["branch_prefix"],
        gate_quick_cmd=fx["gate_quick_cmd"],
        gate_full_cmd=fx["gate_full_cmd"],
        provider=fx["provider"],
        epic=epic,
        # A preview, dispatched to no node: the current names.
        # A preview of the repository shape.
        repo=True,
        agent_instructions=instructions,
        # A preview carries no PROJECT MEMORY section: it renders the memory-OFF arm (`compose`'s default).
        memory=False,
    )

    derived = derive(pb)[index]
    receives = tuple(derived["receives"])
    input_artifacts, input_artifacts_dir = input_staging._input_artifact_staging(
        input_rows(list(receives)), fx["console_task_id"], receives
    )
    task_prompt = dc_task_prompt.build_task_prompt(
        fx["owner_repo"],
        fx["title"],
        fx["body"],
        fx["qa_trail"],
        fx["integration_base"],
        branch_prefix=fx["branch_prefix"],
        gate_quick_cmd=fx["gate_quick_cmd"],
        gate_full_cmd=fx["gate_full_cmd"],
        provider=fx["provider"],
        epic=epic,
        # A preview, dispatched to no node: the current names.
        repo=True,
        prior_stage_output=fx["prior_stage_output"] if has_predecessor(stage) else "",
        console_task_id=fx["console_task_id"],
        autonomous=True if stage.get("interviews") == v.INTERVIEWS_NEVER else full_auto,
        issue_state=fx["state"],
        recon_map=None,
        attachments_dir=None,
        attachment_count=0,
        expected_inputs=receives,
        input_artifacts=input_artifacts,
        input_artifacts_dir=input_artifacts_dir,
        hub_repo=fx["hub_repo"],
        delivery=contract.delivery,
        ships_pr=contract.mode == MODE_BUILD,
        asks_at_gate=contracts.asks_at_gate(expert_key),
        console_present=conversation,
        resume_worktree=fx["resume_worktree"],
        prior_session_ended=fx["prior_session_ended"],
        finish_follow_up=fx["finish_follow_up"],
        agent_instructions=instructions,
    )
    return {"system_prompt": system_prompt, "task_prompt": task_prompt, "model_tier": derived["tier"] or ""}
