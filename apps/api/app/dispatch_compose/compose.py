"""One dispatch from the DB: model and node resolution, and `compose()`'s tri-state verdict."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agent_providers, edition, edition_features, model_catalog, playbook_engine
from app.agent_providers import AgentProvider
from app.dispatch_compose import task_prompt
from app.dispatch_compose.input_staging import _attachment_workdir, _expected_inputs, stage_input_artifacts
from app.dispatch_compose.placeholders import respell_vocabulary
from app.dispatch_compose.role_tables import _CONSOLE_INTERVIEW, _NEVER_ASKS, _NO_AUTOCLOSE
from app.dispatch_compose.system_prompt import build_system_prompt, build_system_prompt_with, resolve_provider
from app.dispatch_compose.task_prompt import PromptRepo, build_task_prompt
from app.models.projects import Project
from app.persona_registry import MODE_BUILD, PERSONA_CHARTER, charter_delivery, charter_mode

log = logging.getLogger(__name__)


class _SharedPromptArgs(TypedDict):
    """The keyword arguments `build_system_prompt` and `build_system_prompt_with` take in common: the project
    values and the row's shape. Typed so `**shared` keeps each builder's signature."""

    integration_branch: str
    branch_prefix: str
    gate_quick_cmd: str
    gate_full_cmd: str
    provider: str
    epic: bool
    repo: bool
    agent_instructions: str
    memory: bool
    merge_lane: bool


def _task_resolvers():
    """The by-id resolver + the dispatch projection, imported lazily.

    A module-scope import closes an import cycle with `app/routes/tasks/`."""
    from app.routes.tasks.dispatch import _resolve_open_task_by_id, task_dispatch_out

    return _resolve_open_task_by_id, task_dispatch_out


def _card_default_model(session: Session, project_id: int) -> str:
    """The project card's `default_model`, single home for the per-repo default; empty when the project
    isn't found or has none. A direct column read: an unreachable DB raises rather than resolving empty."""
    return session.scalar(
        select(Project.default_model).where(Project.id == project_id)
    ) or ""


def _guard_rules_for(session: Session, project_id: int, provider: str, has_repo: bool) -> str | None:
    """The rule list a runner reads in its prompt when its CLI has no session-start context channel (its row's
    `capabilities.sessions.start_context` is False; §5.5, `guard_runtime.brief_for`), or None:
    every other runtime gets it at session start, and an unguarded project or folder slot gets nothing at all."""
    caps = agent_providers.capabilities_of(provider)
    if caps is None or caps.sessions.start_context or not has_repo or session is None:
        return None
    if not edition_features.enabled("compliance"):
        return None
    from app.governance import guard_runtime  # lazy: only such a dispatch pays the import

    try:
        row = session.scalars(select(Project).where(Project.id == project_id)).first()
        if row is None or guard_runtime.lease_stanza(session, row.id) is None:
            return None
        return guard_runtime.brief_for(session, row)
    except Exception:  # noqa: BLE001 — a missing brief never costs the dispatch (G-1: the guard fails open)
        log.exception("guard: composing project %s's rule list failed; dispatching without it", project_id)
        return None


def _project_agent_instructions(session: Session, project_id: int) -> str:
    """The project card's `agent_instructions`: per-repo operator directives injected into every
    dispatched persona's prompt, read directly off the row like `_card_default_model`.

    Empty (unset, NULL, or unwritten) is a complete answer and renders no section. An unreachable DB
    raises and fails the dispatch rather than launching a runner without its project's procedure."""
    return session.scalar(
        select(Project.agent_instructions).where(Project.id == project_id)
    ) or ""


def _resolve_model(
    session: Session,
    model_override: str,
    task_model: str | None,
    stage_model: str | None,
    project_id: int,
) -> str:
    """Model precedence: per-request `--model` override > this task's own pin > stage default > card
    `default_model` > empty. The result is a TIER (or, only from an override, an exact model id);
    `_model_for_provider` turns it into the concrete `--model` value. Empty → the CLI's own default.

    The stored order is single-homed in `app/model_catalog.py`; only the per-request override is decided
    here. The task pin is resolved here so both compose call sites (`lease_drain`, direct callers) agree.
    The card read is last and lazy (a callable), costing no query when an earlier rung answers."""
    return model_catalog.resolve_model(
        (model_override or "").strip() or (task_model or ""),
        stage_model,
        lambda: _card_default_model(session, project_id),
    )


def _node_model_catalog(session: Session, node_id: str, provider: AgentProvider) -> list:
    """The model catalog THIS node's `provider` CLI last reported (`nodes.model_catalogs`), or `[]` when
    it reported none or its report is stale (`_catalog_is_fresh`).

    The dispatching node's own report, never another node's: it is the CLI build and the account that will
    run the task, so every slug it lists is one that CLI accepts — and a tenant node cannot steer another
    org's runs. Lazy import: `Node` is only needed on this path."""
    from app.models.nodes import Node
    from app.node_identity import node_matches

    if not node_id:
        return []
    catalogs = session.scalar(select(Node.model_catalogs).where(node_matches(node_id)))
    entry = (catalogs or {}).get(provider.value) if isinstance(catalogs, dict) else None
    if not isinstance(entry, dict) or not _catalog_is_fresh(entry.get("observed_at")):
        return []
    models = entry.get("models")
    return models if isinstance(models, list) else []


def _node_reports(session: Session, node_id: str, key: str, value: str) -> bool:
    """Whether the dispatching node's config snapshot reports `key: value`. False for an empty or unknown
    node and for a snapshot not yet reported: the two naming probes below read False as "tell it the legacy
    names", which every node resolves. Lazy import, as in `_node_model_catalog`."""
    from app.models.nodes import Node
    from app.node_identity import node_matches

    if not node_id:
        return False
    snapshot = session.scalar(select(Node.config_snapshot).where(node_matches(node_id)))
    return (snapshot.get(key) if isinstance(snapshot, dict) else None) == value


def _mounted_nodelinks(session: Session, node_id: str) -> tuple[tuple[str, str], ...]:
    """Every NodeLink `node_id` reports mounted (`nodelinks.mounted_for_prompt`), `()` for an
    empty or unknown node. Degrades to `()` on any failure: the line is supplementary, never worth a dispatch.
    Lazy import, as in `_node_model_catalog`."""
    if edition.IS_CE:
        # The community edition has no NodeLink.
        return ()
    from app.models.nodes import Node
    from app.node_identity import node_matches
    from app.nodelinks import mounted_for_prompt

    if not node_id:
        return ()
    try:
        with session.begin_nested():
            node = session.scalar(select(Node).where(node_matches(node_id)))
            return tuple(mounted_for_prompt(session, node)) if node is not None else ()
    except Exception:  # noqa: BLE001 — logged; the dispatch goes ahead without the line
        log.exception("nodelinks: could not read node %s's mounted NodeLinks — composing without them", node_id)
        return ()


def _node_uses_legacy_vocabulary(session: Session, node_id: str) -> bool:
    """Whether the dispatching node must be told the pre-rename agent verbs and tools
    (`agent_providers.VOCABULARY_SPELLINGS`): True unless its config snapshot reports `vocabulary: industry`.
    The pre-rename names resolve on every node, the industry ones only on an upgraded node."""
    return not _node_reports(
        session, node_id, agent_providers.VOCABULARY_SNAPSHOT_KEY, agent_providers.CURRENT_VOCABULARY
    )


def _catalog_is_fresh(observed_at: object) -> bool:
    """Whether a catalog's CP-stamped `observed_at` is within `model_catalog.CATALOG_FRESH_SECONDS`. An
    unreadable stamp is stale: a catalog nobody can date is not trusted onto an argv."""
    try:
        stamped = datetime.fromisoformat(str(observed_at))
    except ValueError:
        return False
    if stamped.tzinfo is None:
        return False
    age = (datetime.now(timezone.utc) - stamped).total_seconds()
    return age <= model_catalog.CATALOG_FRESH_SECONDS


def _model_for_provider(
    session: Session, model: str, provider: AgentProvider, node_id: str = "",
) -> tuple[str, str]:
    """`(tier, concrete --model value)` for this provider's CLI.

    A tier (or a legacy default-provider alias, folded) resolves through `model_catalog.resolve_tier_model`: the
    provider's moving alias, or its node-reported catalog. Anything else is an exact id from a per-request
    override: it passes through verbatim with no tier, except on a provider declaring
    `foreign_model_ids == "fold"`, where `model_catalog.fold_foreign_id` translates or REFUSES it (raises
    `model_catalog.ForeignModelRefused`, which `compose` turns into an error verdict). Empty stays
    `("", "")` — the CLI's own default."""
    tier = model_catalog.tier_of(model)
    if not tier:
        if model and provider.foreign_model_ids == "fold":
            return model_catalog.fold_foreign_id(provider, model)
        return "", model
    concrete = model_catalog.resolve_tier_model(
        tier, provider.tier_aliases, lambda: _node_model_catalog(session, node_id, provider),
    )
    return tier, model_catalog.keep_context_suffix(concrete, model)


def _verdict(action: str, reason: str, **extra: object) -> dict:
    """A composer verdict dict. `action` ∈ dispatch|skip|error drives the dispatcher's branch; skip/error
    carry empty fields and an empty staging instruction, so the shape is stable.

    `autoclose` defaults True (batch runners are reapable; skip/error launch nothing). `compose` overrides
    it off the role (`_NO_AUTOCLOSE`); `app/lease_drain.py` turns it into `VISIBLE_AUTOCLOSE`."""
    out = {
        "action": action,
        "reason": reason,
        "system_prompt": "",
        "task_prompt": "",
        "model": "",
        "tier": "",
        "title": "",
        "attachments": [],
        "attachments_dir": None,
        "input_artifacts": [],
        "input_artifacts_dir": None,
        "autoclose": True,
        # Project memory's dispatch facts when the arm is ON (`_project_memory_for`), else None.
        "memory": None,
    }
    out.update(extra)
    return out


def compose(
    session: Session,
    project: str,
    owner_repo: str,
    task_id: object,
    *,
    model_override: str,
    integration_base: str,
    branch_prefix: str,
    gate_quick_cmd: str,
    gate_full_cmd: str,
    manual: bool,
    provider: str,
    has_repo: bool,
    resume_worktree: str = "",
    node_id: str = "",
    memory_arm: bool = False,
    merge_lane: bool = False,
    advertise_nodelinks: bool = False,
    repos: tuple[PromptRepo, ...] = (),
) -> dict:
    """Resolve the per-persona dispatch for one fleet unit from the DB (SSOT) and assemble the system
    prompt, fenced task prompt and resolved model. Task is read in-process; charter/model come from its
    `persona` via `PERSONA_CHARTER`, with no fallback, so an unmapped persona cannot dispatch.

    Tri-state verdict: a matched Task -> `dispatch`; no match or an `epic` container -> `skip`; an
    unmapped persona -> `error`. An erroring DB raises.

    `provider` and the four project values are required arguments, never looked up or taken off a wire
    (`lease_drain` passes the account's provider; other callers the Task's `bound_provider` or the fleet
    default); the caller refuses a blank project value. `agent_instructions` alone is read from the row
    here: free text whose empty value is correct and whose omission by a caller would silently drop it.
    `has_repo` is the SLOT's kind (`slot_allocation.task_has_repo`), required for the same reason:
    False composes the folder prompt, with no branch, PR, push or gate passage in either half.

    `memory_arm` is the task's effective project-memory switch, resolved by the caller
    (`project_memory.effective_memory`) because the caller stamps it on the row. False composes exactly what a
    pre-memory composer did; True adds the PROJECT MEMORY section, the trusted guidance, and `verdict["memory"]`.

    `merge_lane` is whether the project is lane-active (`merge_lane.lane_state(...).active`), resolved by the
    caller, which holds the project row. False composes exactly what a pre-lane composer did; True tells a
    PR-shipping runner the merge lane runs the full gate and merges (`_MERGE_LANE_REWRITES`).

    `advertise_nodelinks` tells the runner which NodeLinks `node_id` reports mounted
    (`_mounted_nodelinks`); the slot dispatch passes it. `repos` is every repository the slot holds
    (`slot_repos_for_prompt`), resolved by the slot dispatch, which knows the slot."""
    by_id, project_out = _task_resolvers()
    task_row = by_id(int(task_id), session)
    task = project_out(task_row, session).model_dump() if task_row is not None else None
    if not task:
        return _verdict(
            "skip",
            "no open control-plane DB Task with this id — not dispatchable (the DB is the SSOT)",
        )
    persona = task.get("persona")
    # Persona guard: `persona` is authoritative and NOT NULL. `epic` is a non-dispatched
    # container (`skip`); any other persona absent from PERSONA_CHARTER is an `error` verdict.
    if persona == "epic":
        return _verdict(
            "skip",
            "task is an epic container (persona=epic) — a non-dispatched parent with no charter; its "
            "analysis children carry the dispatchable work",
        )
    if persona not in PERSONA_CHARTER:
        return _verdict(
            "error",
            f"task persona {persona!r} has no charter mapping (absent from PERSONA_CHARTER) — needs human "
            "triage; never defaulted to the implementer",
        )
    charter_key, persona_model = PERSONA_CHARTER[persona]
    # A playbook stage row (`playbook_engine`): its prompt is its stage's expert × deliverable, its stage
    # default model its stage's tier (already capped by its container), its instructions the project's then
    # the stage's own. Its persona — chosen for the deliverable — still decides the charter key, which keys
    # only the guards and autoclose below. None runs today's persona-keyed composition unchanged.
    stage = getattr(task_row, "playbook_stage", None) or None
    staged = playbook_engine.system_role(stage) if stage else None
    if stage and stage.get("tier"):
        persona_model = stage["tier"]
    # The projection's own `model` field, so a row's pin beats the persona's stage default.
    # A tier, resolved to the concrete model on THIS provider (and, for a catalog provider, this node).
    # Outside the `try`: an unknown provider is a hard refusal of its own (`resolve_provider`).
    provider_record = resolve_provider(provider)
    try:
        tier, model = _model_for_provider(
            session,
            _resolve_model(session, model_override, task.get("model"), persona_model, task.get("project_id")),
            provider_record,
            node_id,
        )
    except model_catalog.ForeignModelRefused as exc:
        # A model id the provider cannot run (`model_catalog.fold_foreign_id`): refused here, readable,
        # rather than launched into the vendor's HTTP 400 or its silent remap.
        return _verdict("error", f"model override refused: {exc}")
    # The operator's per-project instructions, read once per dispatch and passed to both builders (which
    # stay pure text and DB-free), after the persona guards so skip/error cost no query. For every persona:
    # they are facts about the repo.
    agent_instructions = _project_agent_instructions(session, task.get("project_id"))
    if stage:
        agent_instructions = playbook_engine.with_stage_instructions(agent_instructions, stage)
    # The `epic` flag: whether this row is an epic child, from `TaskDispatchOut` (the dispatch projection,
    # not the console's `TaskOut`). Not derived from the persona or from prerequisites: the question is
    # the row's shape, and a standalone Architect behind a standalone BA also has prerequisites.
    epic_child = task.get("parent_task_id") is not None
    # The lane's own run is the merger: the "lane merges, you do not" rewrites are for the PR runners it merges.
    merge_lane = merge_lane and persona != "merge_lane"
    # The facts both builders take, spelled once: the staged branch supplies the role from the playbook,
    # the other from the charter key.
    shared = _SharedPromptArgs(
        integration_branch=integration_base,
        branch_prefix=branch_prefix,
        gate_quick_cmd=gate_quick_cmd,
        gate_full_cmd=gate_full_cmd,
        provider=provider,
        epic=epic_child,
        repo=has_repo,
        agent_instructions=agent_instructions,
        memory=memory_arm,
        merge_lane=merge_lane,
    )
    if staged is not None:
        system_prompt = build_system_prompt_with(
            staged["role"],
            mode=staged["mode"],
            delivery=staged["delivery"],
            terms=staged["terms"],
            console_present=staged["console_present"],
            **shared,
        )
    else:
        system_prompt = build_system_prompt(charter_key, persona=persona, **shared)
    # Every role gets its control-plane Task id as a trusted line above the fence; the daemon, not the
    # runner, substitutes it into control calls.
    console_task_id = task.get("id")
    # Dispatch-time attachments: path computed here, bytes written on the node (`_attachment_workdir` is
    # pure). An unkeyable unit is caught: attachments are supplementary, never worth failing a dispatch.
    attachments_meta = task.get("attachments") or []
    attachments_dir: str | None = None
    if attachments_meta:
        try:
            attachments_dir = str(_attachment_workdir(console_task_id))
        except ValueError as exc:
            log.warning("no attachment workdir for task %s (%s) — composing without the attachment block",
                        console_task_id, exc)
            attachments_meta = []
    expected_inputs = _expected_inputs(persona, stage)
    input_artifacts, input_artifacts_dir = stage_input_artifacts(
        task.get("prerequisite_artifacts") or [], console_task_id, persona, stage
    )
    memory_section, memory = None, None
    # The community edition has no project memory: its arm is never ON there.
    if not edition.IS_CE:
        from app.dispatch_compose import project_memory

        if memory_arm:
            memory_section, memory = project_memory._project_memory_or_nothing(session, task_row, task, persona)
    task_text = build_task_prompt(
        owner_repo,
        task.get("title") or "",
        task.get("body") or "",
        task.get("qa_trail") or "",
        integration_base,
        branch_prefix=branch_prefix,
        gate_quick_cmd=gate_quick_cmd,
        gate_full_cmd=gate_full_cmd,
        provider=provider,
        epic=epic_child,
        repo=has_repo,
        prior_stage_output=task.get("prior_stage_output") or "",
        merge_lane_verdict=task.get("merge_lane_verdict") or "",
        console_task_id=console_task_id,
        # `.get(..., True)` only covers a MISSING key; a present-but-null value (a legacy/partial
        # projection row) must still default ON, never silently coerce to OFF.
        autonomous=True if task.get("autonomous") is None else task["autonomous"],
        issue_state=task.get("state") or "",
        recon_map=task.get("prerequisite_recon_map") or None,
        attachments_dir=attachments_dir,
        attachment_count=len(attachments_meta),
        expected_inputs=expected_inputs,
        input_artifacts=input_artifacts,
        input_artifacts_dir=input_artifacts_dir,
        # Hub-ness from the project row, not the `owner_repo` request field (see `_project_is_hub_repo`).
        hub_repo=task_prompt._project_is_hub_repo(session, task.get("project_id")),
        # Keyed on the resolved role (implementer → "pr"); a stage's on its deliverable and expert.
        delivery=staged["delivery"] if staged else charter_delivery(charter_key),
        # …and whether this role integrates through a PR at all (`mode`; see `build_task_prompt`'s `ships_pr`).
        ships_pr=(staged["mode"] if staged else charter_mode(charter_key)) == MODE_BUILD,
        # …and whether this role's gate may ask at all (`_NEVER_ASKS`).
        asks_at_gate=staged["asks_at_gate"] if staged else charter_key not in _NEVER_ASKS,
        # …and whether this role's operator is live at the console (`_CONSOLE_INTERVIEW`).
        console_present=staged["console_present"] if staged else charter_key in _CONSOLE_INTERVIEW,
        # The relaunch-in-place terms (see `build_task_prompt`'s `resume_worktree`). Threaded: slot contents
        # are a pool fact this module cannot see (it never touches a worktree).
        resume_worktree=resume_worktree,
        # …and whether a session on this task already ended with work owed, from the projection, so every
        # caller gets it. Absent means False.
        prior_session_ended=bool(task.get("prior_session_ended")),
        # Whether this is a Finish follow-up child: `finish_follow_up_key` is the authoritative
        # discriminator (never sniff the LLM-authored title/body). Absent from the projection means False.
        finish_follow_up=bool(task.get("finish_follow_up_key")),
        # The SAME string the system prompt rendered, threaded so the trusted "Start here" block can point
        # at that section. Only the fixed pointer sentence renders here — never this text.
        agent_instructions=agent_instructions,
        project_memory=memory_section,
        merge_lane=merge_lane,
        mounted_nodelinks=_mounted_nodelinks(session, node_id) if advertise_nodelinks else (),
        guard_rules=_guard_rules_for(session, task.get("project_id"), provider, has_repo),
        repos=repos,
    )
    if _node_uses_legacy_vocabulary(session, node_id):
        system_prompt, task_text = respell_vocabulary(system_prompt), respell_vocabulary(task_text)
    return _verdict(
        "dispatch",
        f"persona={persona} charter={charter_key}",
        system_prompt=system_prompt,
        task_prompt=task_text,
        model=model,
        # The tier `model` was resolved from ("" for an exact override or nothing at all), recorded on
        # the Task beside it by `app/lease_drain.py` so the console can show "High → gpt-5.6-sol".
        tier=tier,
        title=task.get("title") or "",
        # The staging instruction. Each attachment row carries its own `task_id`, since the projection
        # unions a child's attachments with its parent epic's. Empty list + null dir means "stage nothing".
        attachments=attachments_meta,
        attachments_dir=attachments_dir,
        # The second staging instruction, which the node may not swallow: required inputs, so
        # `materialize_prerequisite_artifacts` fails the attempt rather than launch blind.
        input_artifacts=input_artifacts,
        input_artifacts_dir=input_artifacts_dir,
        # Whether the node may reap this runner as idle, derived from the role: False only for the
        # conversational role, which parks between messages. Consumed by `app/lease_drain.py`.
        autoclose=charter_key not in _NO_AUTOCLOSE,
        memory=memory,
    )
