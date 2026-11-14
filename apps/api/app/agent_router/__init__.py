"""The agent router: which runner a task should run on, and how far the console may act on that choice.

NOT `app/jaah_routing.py`, which routes Bedrock accounts. The modules:

* `modes.py` — the mode a project runs in (Shadow, Recommend, Auto); `policy_settings.py` — the org's routing
  policy. The org's maximum mode, its demotion of projects above it and the project's mode are written by
  `routes/orgs.py::update_org` and `routes/projects.py::apply_project_update`.
* `decisions.py` — the decision record (`routing_decisions`), one row per dispatch, and its one writer.
* `policy.py` — the pick (Thompson sampling, fallbacks), over `features.py` (what is known about the task),
  `candidates.py` (what it may run on, the A9 vendor rule, `router_eligibility`) and `ledger_stats.py` (the
  statistics seam); `reason.py` — the sentence; `personas.py` — which personas are routed; `pins.py` —
  `tasks.pin_source`.

This module is the public API the dispatch path calls (architecture §4.1):

* `effective_choice` — THE ONLY CALLER of `agent_resolution.resolve_agent` outside this package (a test holds it):
  the chain's answer verbatim unless a live row says the router's pick is used, so with routing off dispatch is
  byte-identical.
* `refresh_picks` — once per lease pass, fleet-wide, beside the unrunnable-agent sweep: every dispatchable task's
  `pending` row, recomputed only when its inputs moved. Never inside `routes/fleet.py::build_dispatch_plan`.
* `router_eligibility` — may the router choose this runner for this org, and why not;
* `lock_on_lease` / `unlock` — at the lease and its give-back; `finalize_on_claim` — at the claim;
  `model_override` — at both compose sites; `review_lanes_of` — a coder's review lanes, at compose;
  `set_actual_model` — where the dispatched model is stamped.
* Recommend (AD14): `preview` — the task form's indicative pick, writing nothing; `accept` — the Routing panel's
  "Use this"; `record_answer` — the task form's writes, which accept a recommendation ("Use this") or override it (a
  person's own runner or model) while one is live.

R3: a decision-record write can never delay, fail or reorder a dispatch. Every write runs in a SAVEPOINT whose
failure is logged and swallowed, and nothing here commits except `refresh_picks`, which runs in its own prelude step.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import agent_resolution
from app.agent_router import candidates, decisions, ledger_stats, modes, policy, review_lanes
from app.agent_router import policy_settings
from app.clock import utcnow
from app.entity_shape import optional_uuid
from app.models.orgs import Org
from app.models.projects import Project
from app.agent_router import pins
from app.models.routing import LIVE_STATUSES, LOCKED, PENDING, SUPERSEDED, RoutingDecision
from app.models.tasks import Task

log = logging.getLogger(__name__)

#: Whether the router may choose a runner for an org, in a sentence (the marketplace, C8).
router_eligibility = candidates.router_eligibility


def effective_choice(session: Session, task: Task, project: Optional[Project]) -> agent_resolution.Choice:
    """The runner `task`'s dispatch uses: `resolve_agent`'s answer, unless the task's live decision row chose the
    router's pick (`used = 'router'`), whose agent must still be runnable."""
    base = agent_resolution.resolve_agent(session, task, project)
    if isinstance(base, agent_resolution.Refused):
        return base
    row = _live_pick(session, task.id)
    if row is None or row.used != "router":
        return base
    cfg = row.router_pick or {}
    if cfg.get("kind") == decisions.KIND_AGENT:
        agent = agent_resolution.load_agent(session, optional_uuid(cfg.get("agent_id")))
        if (agent is None or task.persona not in (agent.personas or [])
                or agent_resolution.unrunnable_reason(session, agent, task.org_id) is not None):
            return base
        return agent_resolution.Sdk(agent)
    return agent_resolution.Builtin(cfg.get("provider") or None)


def model_override(session: Session, task_id: int) -> str:
    """The `--model` override compose gets for this dispatch: the router's model when its pick is used, else `""`
    (compose falls through to the task's own pin, as before)."""
    row = _live_pick(session, task_id)
    if row is None or row.used != "router":
        return ""
    return (row.router_pick or {}).get("model") or ""


def review_lanes_of(session: Session, task_id: int, review_tier: Optional[str]) -> list[dict[str, Any]]:
    """The provider-neutral review lanes this dispatch's decision records (`router_pick.review.lanes`), else — no
    row, or a row written before lanes were recorded — `review_tier`'s own (`review_lanes.lanes_for`)."""
    row = _live_pick(session, task_id)
    lanes = ((row.router_pick or {}).get("review") or {}).get("lanes") if row is not None else None
    return list(lanes) if lanes else review_lanes.lanes_for(review_tier)


# `session.info` key of an armed `live_pick_memo`: `{task id: _live_pick's answer}`.
_LIVE_PICK_MEMO = "agent_router.live_pick_memo"


@contextmanager
def live_pick_memo(session: Session, shared: dict) -> Iterator[None]:
    """Answer `_live_pick` on `session` from `shared` for the block, filling it on a miss. `shared` may serve several
    sessions at once (the lease drain's allocation and every node's plan, which read each waiting task's row per node:
    three round trips each). Only for a block no pick is written or locked in; nested, the outer memo serves."""
    armed_here = _LIVE_PICK_MEMO not in session.info
    if armed_here:
        session.info[_LIVE_PICK_MEMO] = shared
    try:
        yield
    finally:
        if armed_here:
            session.info.pop(_LIVE_PICK_MEMO, None)


def _live_pick(session: Session, task_id: int) -> Optional[Any]:
    """`(used, router_pick)` of the task's live row, or None — a read only, never a lock. In a SAVEPOINT: a failed
    read is logged and reads as no row, so the caller dispatches on the chain's answer (R3). From an armed
    `live_pick_memo` when there is one; a failed read is never memoized."""
    memo = session.info.get(_LIVE_PICK_MEMO)
    if memo is not None and task_id in memo:
        return memo[task_id]
    try:
        with session.begin_nested():
            row = session.execute(
                select(RoutingDecision.used, RoutingDecision.router_pick).where(
                    RoutingDecision.task_id == task_id, RoutingDecision.status.in_(LIVE_STATUSES)
                )
            ).first()
    except Exception:  # noqa: BLE001 — a decision read must never cost a dispatch
        log.exception("agent-router: reading task %s's decision failed; dispatching on the chain's answer", task_id)
        return None
    if memo is not None:
        memo[task_id] = row
    return row


def _guarded(what: str, task_id: Any, fn, *args) -> Any:
    """Run one decision-record write in a SAVEPOINT of `args[0]` (the session); a failure is logged and
    swallowed, so the dispatch around it proceeds unchanged (R3). A `task_id` argument is converted inside."""
    session = args[0]
    try:
        args = tuple(int(str(a)) if a is task_id and task_id is not None else a for a in args)
        with session.begin_nested():
            return fn(*args)
    except Exception:  # noqa: BLE001 — a measurement write must never cost a dispatch
        log.exception("agent-router: %s for task %s failed; the dispatch proceeds without it", what, task_id)
        return None


def lock_on_lease(session: Session, task_id: int, dispatch_id: Optional[str]) -> None:
    """`pending → locked` as the lease is taken. Never raises; does not commit."""
    _guarded("locking the decision", task_id, decisions.lock, session, task_id, dispatch_id)


def unlock(session: Session, task_id: int) -> None:
    """`locked → pending` when the lease is given back. Never raises; does not commit."""
    _guarded("unlocking the decision", task_id, decisions.unlock, session, task_id)


def finalize_on_claim(session: Session, task_id: int, dispatch_id: Optional[str] = None) -> None:
    """`→ final` once the claim's UPDATE won, in its transaction, and named on the dispatch's outcome-ledger row so
    its outcome grades it. Never raises; does not commit."""
    from app.outcome_ledger import writer as outcome_writer

    row = _guarded("finalizing the decision", task_id, decisions.finalize, session, task_id, dispatch_id)
    if row is not None:
        _guarded("naming the decision on its outcome row", task_id, outcome_writer.link_routing_decision, session,
                 task_id, row.id)


def set_actual_model(session: Session, task_id: int, model: Optional[str]) -> None:
    """Record the dispatched model on the live row. Never raises; does not commit."""
    _guarded("recording the dispatched model", task_id, decisions.set_actual_model, session, task_id, model)


# ── Recommend: a person's answer (AD14) ─────────────────────────────────────────────────────────────

#: The personas a person is shown a recommendation for: never an analysis persona, and a review child is the
#: system's own (the A9 rule routes it).
RECOMMEND_PERSONAS: tuple[str, ...] = ("coder",)


def recommend_mode(session: Session, task: Task, project: Optional[Project]) -> Optional[modes.Mode]:
    """The project's effective mode when `task` is one the router routes, else None."""
    if project is None or task.persona not in RECOMMEND_PERSONAS:
        return None
    org = session.get(Org, project.org_id)
    return modes.effective_mode(session, org, project) if org is not None else None


def preview(session: Session, task: Task, project: Project, *, inputs_hash: str,
            launcher_id: Any = None) -> Optional[decisions.PickDraft]:
    """The recommendation for `task` (a draft the form has not saved, or a saved task), or None when there is none to
    show (not Recommend mode, or the router fell back). Writes nothing: everything runs in a SAVEPOINT rolled back.
    A draft has no id, so its accounts are read for `launcher_id`, who would launch it."""
    if recommend_mode(session, task, project) != modes.Mode.RECOMMEND:
        return None
    memo: dict = {}
    if task.id is None:
        memo["grants"] = {("_who_places", None): (project.org_id, launcher_id)}
    nested = session.begin_nested()
    try:
        draft = policy.recommendation(session, task, project, inputs_hash=inputs_hash, memo=memo)
    finally:
        nested.rollback()
    return draft if draft.fallback_reason is None else None


def draft_hash(persona: str, body: Optional[str]) -> str:
    """The seed of a recommendation: the task form's preview and the record of the answer to it draw the same pick
    for the same text (the title is left out: a saved task's may be generated after the form showed its pick)."""
    import hashlib
    import json

    return hashlib.sha256(json.dumps([persona, (body or "").strip()]).encode()).hexdigest()


def answerable(row: Optional[RoutingDecision]) -> bool:
    """Whether `row` is a live recommendation a person may still accept: pending, Recommend, unanswered, no fallback."""
    return (row is not None and row.status == PENDING and row.mode == modes.Mode.RECOMMEND.value
            and row.fallback_reason is None and row.recommend_response is None)


def apply_pick(task: Task, cfg: dict[str, Any]) -> None:
    """Write a Config into the task's runner and model fields as a person's pin (A10). Does not flush."""
    if cfg.get("kind") == decisions.KIND_AGENT:
        task.agent_id, task.bound_provider, task.bound_account_id = optional_uuid(cfg.get("agent_id")), None, None
    else:
        provider = cfg.get("provider") or None
        if task.bound_provider != provider:
            task.bound_account_id = None
        task.agent_id, task.bound_provider = None, provider
        task.model = cfg.get("model") or cfg.get("tier") or None
    pins.stamp(task, pins.HUMAN)


def accept(session: Session, task: Task) -> Optional[RoutingDecision]:
    """The Routing panel's "Use this": the live recommendation's pick written into the task's fields as a person's
    pin, the row `accepted` and frozen. None when there is no answerable recommendation. Does not commit."""
    row = decisions.live_row(session, task.id, for_update=True)
    if row is None or not answerable(row):
        return None
    apply_pick(task, row.router_pick)
    decisions.respond(session, row, decisions.ACCEPTED)
    return row


def clean_config(session: Session, task: Task, raw: Any) -> Optional[dict[str, Any]]:
    """A Config the task form sent back (the pick it showed), rebuilt from validated parts only, or None."""
    from app import model_catalog

    if not isinstance(raw, dict):
        return None
    if raw.get("kind") == decisions.KIND_AGENT:
        agent = agent_resolution.load_agent(session, optional_uuid(raw.get("agent_id")))
        if (agent is None or task.persona not in (agent.personas or [])
                or not agent_resolution.visible_to(session, agent, task.org_id)):
            return None
        return decisions.config(kind=decisions.KIND_AGENT, agent_id=agent.id)
    if raw.get("kind") != decisions.KIND_BUILTIN:
        return None
    from app import agent_providers

    provider = raw.get("provider") or None
    tier = raw.get("tier") or None
    if (provider is not None and agent_providers.by_value(provider) is None) or (
            tier is not None and tier not in model_catalog.MODEL_IDENTIFIERS):
        return None
    return decisions.config(kind=decisions.KIND_BUILTIN, provider=provider, tier=tier)


def _same_runner(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """The same agent, or the same built-in vendor — the tier aside."""
    if a.get("kind") != b.get("kind"):
        return False
    if a.get("kind") == decisions.KIND_AGENT:
        return a.get("agent_id") == b.get("agent_id")
    return (a.get("provider") or None) == (b.get("provider") or None)


def record_answer(session: Session, task: Task, project: Optional[Project], *,
                  accepted: Optional[dict[str, Any]] = None) -> None:
    """After a person's write to the task's runner or model (the task form, A10/AD14): `accepted` is the
    recommendation they took with "Use this"; without it, their own pick overrides a live recommendation (or agrees
    with it, which is an acceptance). Only in Recommend, for a routed persona. Never raises; does not commit."""
    _guarded("recording the recommendation's answer", task.id, _record_answer, session, task, project, accepted)


def _record_answer(session: Session, task: Task, project: Optional[Project],
                   accepted: Optional[dict[str, Any]]) -> None:
    if recommend_mode(session, task, project) != modes.Mode.RECOMMEND:
        return
    assert project is not None
    # NOWAIT: a lease pass holding the row must never delay the save (R3); the answer is then not recorded.
    row = decisions.live_row(session, task.id, for_update=True, nowait=True)
    if row is not None and row.status == LOCKED:
        # Dispatching now: the change is for the next attempt, which gets its own row.
        return
    org = session.get(Org, project.org_id)
    basis = modes.mode_basis(session, org, project)
    choice = agent_resolution.resolve_agent(session, task, project)
    if isinstance(choice, agent_resolution.Refused):
        return
    if accepted is not None:
        cfg = clean_config(session, task, accepted)
        if cfg is None:
            return
        if row is not None and row.recommend_response == decisions.ACCEPTED and (
                decisions.config_key(row.router_pick) == decisions.config_key(cfg)):
            return
        if row is None or not (answerable(row) or row.frozen):
            draft = policy.recommendation(session, task, project, inputs_hash=draft_hash(task.persona, task.body))
            if draft.fallback_reason is not None:
                return
            row = decisions.write_pending(session, task, attempt=task.dispatch_attempts + 1,
                                          mode=modes.Mode.RECOMMEND.value, mode_basis=basis, inputs="", draft=draft)
        held = decisions.config_of_choice(choice, task)
        if (decisions.config_key(row.router_pick) == decisions.config_key(cfg)
                and _same_runner(held, cfg) and task.pin_source is not None):
            # The router's own pick, and the runner the save wrote: its tier too, as the panel's "Use this" writes it.
            apply_pick(task, row.router_pick)
            decisions.respond(session, row, decisions.ACCEPTED)
            return
        # Not the router's pick, or not what the save wrote: the person's own choice, judged below like any other.
    if task.pin_source is None:
        # The person took their pin back: the recommendation is unanswered again.
        if row is not None and row.frozen and row.recommend_response is not None:
            decisions.respond(session, row, None)
        return
    if row is None or (not row.frozen and row.fallback_reason is not None):
        draft = policy.recommendation(session, task, project, inputs_hash=draft_hash(task.persona, task.body))
        if draft.fallback_reason is not None:
            return
        row = decisions.write_pending(session, task, attempt=task.dispatch_attempts + 1, mode=modes.Mode.RECOMMEND.value,
                                      mode_basis=basis, inputs="", draft=draft)
    elif row.fallback_reason is not None:
        return
    human = decisions.config_of_choice(choice, task)
    if decisions.config_key(human) == decisions.config_key(row.router_pick):
        decisions.respond(session, row, decisions.ACCEPTED)
    else:
        decisions.respond(session, row, decisions.OVERRIDDEN, human_pick=human)


def refresh_picks(session: Session) -> dict[str, int]:
    """Bring every dispatchable task's `pending` row up to date, fleet-wide, once per lease pass (architecture
    §4.1): a task with no live row, or a pending, unfrozen one whose inputs moved, gets a new pick; a lock whose
    lease left no slot is put back to `pending`. Then rows of tasks no longer dispatchable are superseded. Each
    task in its own SAVEPOINT; never raises; commits. Returns counts for the pass's log."""
    from app import slot_state
    from app.routes.tasks.dispatch import dispatchable_tasks_by_project

    counts = {"written": 0, "unlocked": 0, "failed": 0}
    seen: set[int] = set()
    # One memo per pass: the account set, each org's policy, thresholds and budgets, each project's nodes.
    memo: dict[Any, Any] = {}
    orgs: dict[Any, Optional[Org]] = {}
    policies: dict[Any, str] = {}
    try:
        # One fleet-wide read, not one per project.
        waiting = dispatchable_tasks_by_project(session)
        for project in session.scalars(select(Project)).all():
            tasks = waiting.get(project.id, [])
            if not tasks:
                continue
            if project.org_id not in orgs:
                orgs[project.org_id] = session.scalars(select(Org).where(Org.id == project.org_id)).first()
                policies[project.org_id] = policy_settings.org_policy(session, project.org_id).version
            org = orgs[project.org_id]
            if org is None:
                continue
            mode = modes.effective_mode(session, org, project)
            basis = modes.mode_basis(session, org, project)
            knobs = {"default_agent_id": project.default_agent_id, "default_provider": project.default_provider,
                     "routing_mode": project.routing_mode}
            ids = [task.id for task in tasks]
            seen.update(ids)
            rows = decisions.live_rows(session, ids)
            holds = slot_state.task_holds(session, ids)
            for task in tasks:
                outcome = _guarded(
                    "refreshing the pick", task.id, _refresh_one, session, task, project, org, mode, basis,
                    policies[project.org_id], knobs, rows.get(task.id), task.id in holds, memo,
                )
                if outcome is None:
                    counts["failed"] += 1
                elif outcome:
                    counts[outcome] += 1
            # Per project: a row this pass unlocked or superseded is never held locked across the fleet's pass,
            # where a claim's `finalize` would wait on it.
            session.commit()
        _guarded("retiring stale decisions", None, _retire_stale, session, seen)
        session.commit()
    except Exception:  # noqa: BLE001 — a measurement pass must never cost the fleet its dispatch pass
        session.rollback()
        log.exception("agent-router: the pick refresh failed this pass; dispatch is unaffected")
    return counts


def _refresh_one(session: Session, task: Task, project: Project, org: Org, mode: modes.Mode,
                 basis: dict[str, Any], policy_version: str, knobs: dict[str, Any], row: Optional[RoutingDecision],
                 held: bool, memo: dict) -> str:
    """One task's pick; `"written"`, `"unlocked"` or `""` (unchanged)."""
    unlocked = ""
    if row is not None and row.status == LOCKED:
        if held:
            return ""
        decisions.unlock(session, task.id)
        session.refresh(row)
        unlocked = "unlocked"
    choice = agent_resolution.resolve_agent(session, task, project)
    if isinstance(choice, agent_resolution.Refused):
        # The unrunnable-agent sweep charges it; nothing will dispatch to record.
        return unlocked
    frozen = row is not None and row.frozen and task.persona in RECOMMEND_PERSONAS
    if frozen:
        # A person's answer is never recomputed (AD14) while the task is still one a recommendation is shown for.
        return unlocked
    attempt = task.dispatch_attempts + 1
    default = decisions.config_of_choice(choice, task)
    prepared = policy.prepare(session, task, project, org, memo)
    stats_version = ledger_stats.current().version
    inputs = decisions.inputs_hash(task, attempt=attempt, mode=mode.value, policy_version=policy_version,
                                   default_pick=default, project_knobs=knobs, held=held,
                                   stats_version=stats_version, available=prepared.available())
    unplaced = _unplaced(row, attempt)
    timed_out = _unplaced_timeout(row)
    if timed_out is not None:
        unplaced |= {timed_out}
    elif row is not None and row.inputs_hash == inputs:
        return unlocked
    draft = policy.pick(session, task, project, attempt=attempt, context=policy.PickContext(
        mode=mode, default_pick=default, held=held, inputs_hash=inputs, prepared=prepared,
        unplaced=frozenset(unplaced), memo=memo))
    decisions.write_pending(session, task, attempt=attempt, mode=mode.value, mode_basis=basis, inputs=inputs,
                            draft=draft)
    return "written"


def _unplaced(row: Optional[RoutingDecision], attempt: int) -> set[str]:
    """The candidates an earlier row of this attempt gave up on as unplaceable (AD16), carried forward."""
    if row is None or row.attempt != attempt:
        return set()
    return {decisions.config_key(c["config"]) for c in (row.candidates or [])
            if c.get("excluded_reason") == candidates.UNPLACED}


def _unplaced_timeout(row: Optional[RoutingDecision]) -> Optional[str]:
    """The key of a router pick not leased within `ROUTER_UNPLACED_FALLBACK_SECS` (AD16), or None."""
    from datetime import timedelta

    if row is None or row.status != PENDING or row.used != "router" or row.frozen or row.created_at is None:
        return None
    # From the later of its writing and its last lease, so a lease given back restarts the clock.
    age = utcnow() - max(row.created_at, row.locked_at or row.created_at)
    if age < timedelta(seconds=policy.ROUTER_UNPLACED_FALLBACK_SECS):
        return None
    return decisions.config_key(row.router_pick)


def _retire_stale(session: Session, dispatchable: set[int]) -> None:
    """Supersede the live rows of tasks that are no longer dispatchable: a pending row always, a locked one only
    once its task holds no slot (a lease in flight keeps its lock until the claim). A frozen row — a person's answer
    to a recommendation, often given on a task not yet started — is kept while its task is open."""
    from app.models.worktrees import Worktree
    from app import slot_state
    from app.task_lifecycle import DONE, SYSTEM_FAILED

    stale = select(RoutingDecision.id).where(RoutingDecision.status.in_(LIVE_STATUSES))
    if dispatchable:
        stale = stale.where(RoutingDecision.task_id.not_in(dispatchable))
    holding = select(Worktree.task_id).where(Worktree.task_id == RoutingDecision.task_id,
                                             Worktree.state == slot_state.TASK).exists()
    task_open = select(Task.id).where(Task.id == RoutingDecision.task_id, Task.state.not_in((DONE, SYSTEM_FAILED)),
                                      Task.archived_at.is_(None), Task.persona.in_(RECOMMEND_PERSONAS)).exists()
    session.execute(
        update(RoutingDecision)
        .where(RoutingDecision.id.in_(stale), (RoutingDecision.status == PENDING) | ~holding,
               ~(RoutingDecision.frozen & task_open))
        .values(status=SUPERSEDED)
        .execution_options(synchronize_session=False)
    )
