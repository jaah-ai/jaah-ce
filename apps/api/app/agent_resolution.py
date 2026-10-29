"""Which engine runs a task — the effective agent.

`resolve_agent(session, task, project)` is the single home of the rule. Every dispatch reader asks it — the
pass's allocation and plan (`routes/fleet.py`), the MicroVM admission (`microvm_placement.admit`) and the
unrunnable-agent sweep (`lease_drain._fail_unrunnable_agents`) — and none re-derives it:

1. a task's own pick (`tasks.agent_id`) → `Sdk` when the agent is active, visible to the task's org, fetched
   at least once and declares the persona, else `Refused` with the sentence an operator reads;
2. a task's built-in binding (`tasks.bound_provider`) → `Builtin(provider)`, today's behaviour;
3. the project's default agent (`projects.default_agent_id`): one that does not declare the persona falls
   through (DA3 — the default covers only what it says it serves); one that is disabled, removed, invisible
   or never fetched is `Refused`, never a silent fallback to a built-in (AC-13);
   an agent whose provider-grid cell is off for the org (`agent_cell`: `sdk`/`own`, or `acp:<slug>`/`api_key`
   for a registry install, whose bound key account's own pair must be on too) refuses every pick and default
   (1 and 3) alike, named (`provider_auth_policy.refusal`);
   a playbook stage row (`tasks.playbook_stage`) of an org whose plan has Playbooks off is `Refused` before 1;
4. the project's default built-in (`projects.default_provider`) → `Builtin(provider)`, a narrowing input like
   `bound_provider`;
5. otherwise `Builtin(None)`: any account, today's behaviour.

A `Refused` choice spends one attempt with `[agent_unrunnable] <sentence>` (DD19); `refusal_reason` is that
string's one home. "Visible" is `agent.org_id = task.org_id` for an org agent, or a catalog connector
(`agents.is_catalog`, a row of the operator org) the task's org has enabled (`agent_enablements`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import (
    account_auth, acp_registry, actors, agents, fleet_verdicts, node_platform, plan_terms, playbook_engine,
    provider_auth_policy,
)
from app.models.agent_sessions import Agent, AgentEnablement
from app.models.credentials import Account
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.node_identity import is_uuid_shaped

#: The stable code an unrunnable agent's failure reason starts with (architecture §4.6).
UNRUNNABLE_CODE = "[agent_unrunnable]"

#: `session.info` key of the plan-switch memo: `(transaction, {(switch, org_id): verdict})`.
_SWITCH_MEMO = "_agent_resolution_plan_switches"
#: `session.info` key of the provider-grid memo: `(transaction, {org_id: cells})` (`provider_auth_policy.allowed`).
_GRID_MEMO = "_agent_resolution_provider_grid"


def _txn_memo(session: Session, key: str) -> Optional[dict]:
    """A memo dict for the CURRENT TRANSACTION only: a dispatch pass asks per task, and a later transaction re-reads,
    so a plan changed meanwhile is never served stale (`slot_allocation.task_has_repo`'s pin, same rule). None
    between transactions: nothing to scope it to (the read begins the next one)."""
    txn = session.get_transaction()
    if txn is None:
        return None
    memo = session.info.get(key)
    if memo is None or memo[0] is not txn:
        memo = session.info[key] = (txn, {})
    return memo[1]


def _switch_on(session: Session, org_id: Optional[UUID], name: str) -> bool:
    """`plan_terms.switch_on`, memoised per transaction (`_txn_memo`)."""
    memo = _txn_memo(session, _SWITCH_MEMO)
    if memo is None:
        return plan_terms.switch_on(session, org_id, name)
    key = (name, org_id)
    if key not in memo:
        memo[key] = plan_terms.switch_on(session, org_id, name)
    return memo[key]


def agents_off(org_id: Optional[UUID]) -> str:
    """Why the Agents pages refuse an org whose provider grid has no agent row on (`agents_on`)."""
    return provider_auth_policy.off_sentence(org_id, "Agents", plural=True)


def agents_on(session: Session, org_id: Optional[UUID]) -> bool:
    """Whether any agent row (`sdk` or an `acp:*`) is on `org_id`'s provider grid: what opens the Agents pages."""
    return provider_auth_policy.agents_on(session, org_id, memo=_txn_memo(session, _GRID_MEMO))


def agent_cell(agent: Agent) -> provider_auth_policy.Cell:
    """The provider-grid cell that runs `agent`: `acp:<slug>`/`api_key` for a registry install, else `sdk`/`own` (a
    manifest agent or a catalog connector)."""
    if acp_registry.is_registry(agent):
        return provider_auth_policy.registry_row(agent.slug), account_auth.AUTH_KIND_API_KEY
    return provider_auth_policy.SDK_ROW, provider_auth_policy.OWN


def cell_refusal(session: Session, agent: Agent, org_id: Optional[UUID]) -> Optional[str]:
    """Why `org_id`'s provider grid refuses `agent`, named, or None: the agent's own cell (`agent_cell`) off. Only for
    an agent the caller may name (its own org's, or a catalog connector)."""
    row, auth = agent_cell(agent)
    memo = _txn_memo(session, _GRID_MEMO)
    if provider_auth_policy.is_allowed(session, org_id, row, auth, memo=memo):
        return None
    label = agent.display_name if acp_registry.is_registry(agent) else None
    return provider_auth_policy.refusal(session, org_id, row, auth, label=label)


def key_pair_refusal(session: Session, account: Account, agent: Agent, org_id: Optional[UUID]) -> Optional[str]:
    """Why `org_id`'s provider grid refuses the pair of `agent`'s bound key `account`, named, or None."""
    memo = _txn_memo(session, _GRID_MEMO)
    if provider_auth_policy.is_allowed(session, org_id, account.provider, account.auth_kind, memo=memo):
        return None
    sentence = provider_auth_policy.refusal(session, org_id, account.provider, account.auth_kind)
    return f"{account.slug}, {agent.display_name}'s key account: {sentence}"


@dataclass(frozen=True)
class Builtin:
    """A built-in runner; `provider` narrows the account pool (None = any, the claude default)."""

    provider: Optional[str] = None


@dataclass(frozen=True)
class Sdk:
    """A registered SDK agent, runnable now: active, visible to the task's org, fetched, serving the persona."""

    agent: Agent


@dataclass(frozen=True)
class Refused:
    """The task's agent cannot run; `reason` is the sentence an operator reads (no code prefix)."""

    reason: str


Choice = Union[Builtin, Sdk, Refused]


def refusal_reason(choice: Refused) -> str:
    """The failure reason a refused dispatch records on its task (DD19)."""
    return f"{UNRUNNABLE_CODE} {choice.reason}"


def _persona_words(persona: Optional[str]) -> str:
    return (persona or "").replace("_", " ")


def load_agent(session: Session, agent_id: Optional[UUID]) -> Optional[Agent]:
    """The `agents` row for `agent_id`, catalog connectors included. A tenant-bound session cannot see the
    operator org a connector lives in, so a miss is retried on the catalog's own read
    (`routes/agents.py::_catalog_rows`, detached and read-only)."""
    if agent_id is None:
        return None
    row = session.scalars(select(Agent).where(Agent.id == agent_id)).first()
    if row is not None:
        return row
    from app.routes.agents import _catalog_rows

    rows = _catalog_rows([agent_id])
    return rows[0] if rows else None


def visible_to(session: Session, agent: Agent, org_id: UUID) -> bool:
    """Whether `org_id` may run `agent`: its own org agent, or a catalog connector it enabled."""
    if not agent.is_catalog:
        return agent.org_id == org_id
    return session.scalar(
        select(AgentEnablement.agent_id).where(
            AgentEnablement.agent_id == agent.id, AgentEnablement.org_id == org_id
        )
    ) is not None


def _unrunnable(session: Session, agent: Optional[Agent], org_id: UUID) -> Optional[str]:
    """Why this agent cannot run for `org_id` whatever the persona, or None when it can."""
    if agent is None:
        return "The picked agent no longer exists"
    if agent.is_catalog and agent.status == agents.STATUS_REMOVED:
        return f"{agent.display_name} was removed from the catalog"
    if not visible_to(session, agent, org_id):
        # Never name another org's agent (N7): an org agent of a different org is not this org's to describe.
        if not agent.is_catalog:
            return "The picked agent is not available to this organization"
        return f"{agent.display_name} is not enabled for this organization — turn it on in the agent catalog"
    off = cell_refusal(session, agent, org_id)
    if off is not None:
        return off
    if agent.status == agents.STATUS_DISABLED:
        return f"{agent.display_name} is disabled"
    if agent.status != agents.STATUS_ACTIVE:
        return f"{agent.display_name} was removed from the catalog"
    retired = agents.unsupported(agent.manifest)
    if retired is not None:
        return retired
    if agent.resolved_commit is None and not acp_registry.is_registry(agent):
        # A registry install has no commit to fetch: its pin is what the node installs.
        return f"{agent.display_name} has never been fetched — open Agents and press Refresh"
    if acp_registry.key_account_id(agent) is not None:
        # A bound key account that stopped being usable refuses the run here, named, not at the host's authenticate.
        account = acp_registry.bound_account(session, agent)
        why = acp_registry.key_account_refusal(account, agent)
        # The bound account's own pair is re-checked here too: the grid may have turned it off since the bind.
        return why if why is not None or account is None else key_pair_refusal(session, account, agent, org_id)
    return None


def resolve_agent(session: Session, task: Task, project: Optional[Project]) -> Choice:
    """The effective agent for `task` (§3.3, the module docstring). `project` is the task's own project row, or
    None when the caller has none (then only the task's own fields decide)."""
    if task.playbook_stage is not None and not _switch_on(session, task.org_id, "playbooks_enabled"):
        # A playbook's stage row filed before the plan turned Playbooks off runs no further (`playbook_engine`).
        return Refused(playbook_engine.OFF_DETAIL)
    if task.agent_id is not None:
        agent = load_agent(session, task.agent_id)
        why = _unrunnable(session, agent, task.org_id)
        if why is not None:
            return Refused(why)
        if task.persona not in (agent.personas or []):
            return Refused(f"{agent.display_name} cannot run {_persona_words(task.persona)} tasks")
        return Sdk(agent)
    if task.bound_provider:
        return Builtin(task.bound_provider)
    if project is not None and project.default_agent_id is not None:
        agent = load_agent(session, project.default_agent_id)
        if agent is None or task.persona in (agent.personas or []):
            why = _unrunnable(session, agent, task.org_id)
            if why is not None:
                return Refused(why)
            return Sdk(agent)
    if project is not None and project.default_provider:
        return Builtin(project.default_provider)
    return Builtin(None)


def stamp_run(session: Session, task_id: int, agent: Optional[Agent]) -> None:
    """Write the run stamps a dispatch decides (`tasks.run_agent_id`/`run_agent_commit`): the agent and commit
    an SDK run is leased with, or NULL for a built-in run, so a task that once ran an agent and now runs a
    built-in never carries a stale stamp (the bundle route and answer delivery key on it). Written with the
    lease, BEFORE the claim: the node fetches the bundle in the lease executor, ahead of claiming (§4.4).
    Does not commit."""
    if agent is None:
        session.execute(
            update(Task)
            .where(Task.id == task_id, Task.run_agent_id.is_not(None))
            .values(run_agent_id=None, run_agent_commit=None, **actors.task_actor_values(session))
        )
        return
    session.execute(
        update(Task).where(Task.id == task_id)
        .values(run_agent_id=agent.id, run_agent_commit=agent.resolved_commit, **actors.task_actor_values(session))
    )


#: `placement_class`: how an agent run is placed — the ACP host, or the ACP host installing a registry pin. Each is
#: one placement pass (`routes/fleet.py`) and one `sdk_capable_nodes` filter. `PLACE_UNSUPPORTED` is a stored SDK v1
#: row (`agents.unsupported`): no node runs it, and `resolve_agent` refuses it before placement.
PLACE_ACP, PLACE_REGISTRY, PLACE_UNSUPPORTED = "acp", "registry", "unsupported"
PLACEMENT_CLASSES: tuple[str, ...] = (PLACE_ACP, PLACE_REGISTRY)


def placement_class(agent: Agent) -> str:
    if acp_registry.is_registry(agent):
        return PLACE_REGISTRY
    return PLACE_ACP if agents.is_acp_manifest(agent.manifest) else PLACE_UNSUPPORTED


def sdk_capable_nodes(session: Session, node_ids: list[str], *, placement: str = PLACE_ACP) -> set[str]:
    """The node uuid strings among `node_ids` (the fleet maps' key, `pool_state.live_nodes`) that can run an
    SDK agent: jaahd reports the `agent` and `agent_acp` launch kinds (`nodes.launch_kinds`, NULL = never
    reported = cannot) AND the declared OS supports it (`node_platform.CAP_SDK_AGENT`, Ubuntu only). Placement
    offers an `Sdk` task nowhere else (§3.4). `placement` `PLACE_REGISTRY`: a registry install, which also needs
    `agent_acp_registry` and a static node (its MicroVM run is not built). `PLACE_UNSUPPORTED`, or a value that is
    not a uuid, names no node."""
    if placement == PLACE_UNSUPPORTED:
        return set()
    wanted = [UUID(str(node_id)) for node_id in node_ids if is_uuid_shaped(str(node_id))]
    if not wanted:
        return set()
    kinds = {
        PLACE_ACP: (fleet_verdicts.LAUNCH_KIND_AGENT, fleet_verdicts.LAUNCH_KIND_AGENT_ACP),
        PLACE_REGISTRY: (fleet_verdicts.LAUNCH_KIND_AGENT, fleet_verdicts.LAUNCH_KIND_AGENT_ACP,
                         fleet_verdicts.LAUNCH_KIND_AGENT_ACP_REGISTRY),
    }[placement]
    return {
        str(row.id)
        for row in session.execute(
            select(Node.id, Node.os, Node.arch, Node.microvm, Node.distro_id, Node.distro_version,
                   Node.host_features).where(
                Node.id.in_(wanted), *(Node.launch_kinds.any(kind) for kind in kinds),
                *((Node.microvm.is_(False),) if placement == PLACE_REGISTRY else ()),
            )
        ).all()
        if node_platform.node_reason(row, node_platform.CAP_SDK_AGENT) is None
    }


def launch_for(session: Session, task: Task, agent: Agent, *, key_account: Optional[str] = None) -> dict:
    """The `launch` block for running `agent` on `task` — `fleet_verdicts.agent_launch` fed the task's staged
    prerequisite inputs (`dispatch_compose.input_staging.stage_input_artifacts`, the helper compose uses). Shared by the static
    lease and the MicroVM admission, so both emit the same payload. `key_account`: the bound account's slug as the
    caller already read it (the lease binds that same account); None reads it here."""
    from app.dispatch_compose import input_staging
    from app.routes.tasks.task_wire import _task_prerequisite_artifacts

    rows = [row.model_dump() for row in (_task_prerequisite_artifacts(session, task, None) or [])]
    staged, where = input_staging.stage_input_artifacts(
        rows, task.id, task.persona, getattr(task, "playbook_stage", None) or None
    )
    if key_account is None:
        account = acp_registry.bound_account(session, agent) if acp_registry.is_registry(agent) else None
        key_account = account.slug if account is not None else ""
    return fleet_verdicts.agent_launch(agent, org_id=task.org_id, input_artifacts=staged, input_artifacts_dir=where,
                                       prompt=first_prompt(task, agent), key_account=key_account)


def first_prompt(task: Task, agent: Optional[Agent] = None) -> str:
    """An agent's first ACP prompt: the task's title and body. A
    registry agent, which knows nothing of Jaah, is also told how to settle (`acp_registry.SETTLE_NOTE`)."""
    text = f"# {task.title}\n\n{task.body}".rstrip()
    if agent is not None and acp_registry.is_registry(agent):
        text += f"\n\n{acp_registry.SETTLE_NOTE}"
    return text + "\n"


def agent_ref_for_task(session: Session, task_id: int) -> Optional[dict]:
    """`sessions.agent_ref` for the run a task is stamped with (`run_agent_id`/`run_agent_commit`):
    `{agent_id, display_name, version, commit}`, or None for a built-in run. The commit is the stamp's; the name
    and version are the row's as it stands (a refresh between lease and start can move the version)."""
    stamp = session.execute(
        select(Task.run_agent_id, Task.run_agent_commit).where(Task.id == task_id)
    ).first()
    if stamp is None or stamp.run_agent_id is None:
        return None
    agent = load_agent(session, stamp.run_agent_id)
    return {
        "agent_id": str(stamp.run_agent_id),
        "display_name": agent.display_name if agent is not None else "",
        "version": agent.version if agent is not None else "",
        "commit": stamp.run_agent_commit,
    }
