"""The control-plane calls `app/task_pipeline.py` makes — in process, on the DB, and the one seam its
tests double.

Each function opens one session and calls the `routes/tasks/` helpers with their guards and commits,
so a refusal or error costs one row, never the pass. Scope is fleet-wide (`seam.OPERATOR`, entitled by
the tenancy scope census): the sweep is the crash-recovery backstop for every tenant.

Returns plain dicts/lists read with `.get(...)`. Failures raise, except `create_system_task` and
`block_task`, which return `{}`.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator, Optional

# Stdlib-only at import (beyond the stdlib-only `app.task_lifecycle`): `task_pipeline.py`'s quick-tier
# suites run on a bare host, so ORM, seam and route imports stay inside functions.
from app.task_lifecycle import ACTIVE, DONE, DRAFT, _LIVE_UNIT_STATES

if TYPE_CHECKING:  # pragma: no cover — annotations only
    from sqlalchemy.orm import Session

log = logging.getLogger("cockpit-scheduler")


@contextmanager
def _cp_session() -> Iterator[Session]:
    """One session for one pipeline call, on the fleet-wide plane (see the module docstring).

    One per call, not per pass: a session shared across the pass would let one refused row poison the
    rest."""
    from app.db import SessionLocal
    from app.tenancy import seam

    with seam.operator_scope(), SessionLocal() as session:
        yield session


# ── The work-lists ────────────────────────────────────────────────────────────────────────────────────


def list_open_tasks() -> list[dict]:
    """Open Tasks for the stale-runner reclaimers: every row in `_LIVE_UNIT_STATES`
    (`ready`/`active`/`awaiting_response`/`system_failed`; never `draft`). Consumers filter on `state`
    themselves. Each row carries `parent_task_id`, `persona`, `project` and the two terminal-reference
    columns the coder reclaimer reads."""
    from sqlalchemy import select

    from app.models.projects import Project
    from app.models.tasks import Task

    with _cp_session() as session:
        rows = session.execute(
            select(Task, Project.name)
            .join(Project, Task.project_id == Project.id)
            .where(Task.state.in_(_LIVE_UNIT_STATES))
        ).all()
        return [
            {
                "id": t.id, "project": project_name,
                "state": t.state, "parent_task_id": t.parent_task_id, "persona": t.persona,
                "outcome_no_pr_reason": t.outcome_no_pr_reason,
                "outcome_pr_url": t.outcome_pr_url,
            }
            for t, project_name in rows
        ]


def list_epics_fanout() -> list[dict]:
    """Started epics (`persona='epic' AND state='active'`) with at least one `draft` child — the eager
    fan-out's worklist, with the epic's project and children. Drains to empty once fully
    fanned out. Archived children (`archived_at` set) never re-enter the worklist."""
    from sqlalchemy import select

    from app.models.projects import Project
    from app.models.tasks import Task
    from app.routes.tasks.policy import PERSONA_EPIC

    with _cp_session() as session:
        rows = session.execute(
            select(Task, Project.name)
            .join(Project, Task.project_id == Project.id)
            .where(Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
            .order_by(Task.id)
        ).all()
        out: list[dict] = []
        for epic, project_name in rows:
            child_rows = session.scalars(
                select(Task)
                .where(Task.parent_task_id == epic.id, Task.archived_at.is_(None))
                .order_by(Task.id)
            ).all()
            if not any(c.state == DRAFT for c in child_rows):
                continue  # every child readied — nothing left to fan out
            out.append({
                "id": epic.id,
                "project": project_name,
                "children": [
                    {"id": c.id, "title": c.title, "body": c.body, "state": c.state}
                    for c in child_rows
                ],
            })
        return out


def list_finish_pending() -> list[dict]:
    """The work-list `sweep_finish_phases` drains: every not-yet-`done` finish child whose epic siblings
    are all rollup-ready, with its `state` + `project` so the backstop can gate on `state==active AND
    lease-not-held`. Archived siblings excluded."""
    from sqlalchemy import select

    from app.models.projects import Project
    from app.models.tasks import Task
    from app.routes.tasks.epic_finish import list_finish_pending as _pending

    with _cp_session() as session:
        out: list[dict] = []
        for finish in _pending(session):
            project_name = session.scalar(select(Project.name).where(Project.id == finish.project_id))
            siblings = session.scalars(
                select(Task).where(
                    Task.parent_task_id == finish.parent_task_id, Task.id != finish.id,
                    Task.archived_at.is_(None),
                )
                .order_by(Task.id)
            ).all()
            out.append({
                "id": finish.id,
                "epic_id": finish.parent_task_id,
                "state": finish.state,
                "project": project_name,
                "children": [{"id": c.id, "persona": c.persona, "state": c.state} for c in siblings],
            })
        return out


def list_needs_attention() -> list[dict]:
    """The Needs-attention set (all personas incl. hidden, in blocked/failed/triaged). `[]` when the
    read fails: a miss skips one notify tick; the require_user view is authoritative."""
    from app.routes.agent import task_reads as agent_task_reads

    try:
        with _cp_session() as session:
            rows = agent_task_reads.agent_list_needs_attention(_=None, session=session)
            return [row.model_dump() for row in rows]
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        log.warning("task-pipeline: needs-attention read failed: %s", exc)
        return []


# An `active` task is silent when every sign of its runner is older than `SILENCE_THRESHOLD`: its claim, every
# held slot's node sighting and transcript write (`greatest` skips NULLs, so an all-NULL row is never judged),
# and no observation that fresh reports a session. The transcript signal is what keeps a working runner with a
# wrong session flag safe. A slot on a STARVED node (still talking, no fresh observation) is never judged: its
# three signals froze together, and the reclaimers' longer starved-node grace owns it. A MicroVM run (no held
# slot) uses its running VM's last sighting instead; a provisioning VM has its own give-up bound.
_SILENT_ACTIVE_SQL = """
SELECT t.id FROM tasks t
JOIN worktrees w ON w.task_id = t.id AND w.state = 'task'
LEFT JOIN nodes n ON n.id = w.node_id
WHERE t.state = :active AND (CAST(:only AS bigint) IS NULL OR t.id = :only)
GROUP BY t.id, t.github_synced_at
HAVING max(greatest(t.github_synced_at, w.last_seen_live, w.transcript_written_at)) <= :cutoff
   AND NOT bool_or(coalesce(w.session, false) AND w.observed_at > :cutoff)
   AND NOT bool_or(coalesce(n.last_seen > :talking, false)
                   AND (w.observed_at IS NULL OR w.observed_at <= :observed_fresh))
UNION
SELECT t.id FROM tasks t
JOIN microvms vm ON vm.task_id = t.id AND vm.state NOT IN ('provisioning', 'destroyed', 'failed')
WHERE t.state = :active AND (CAST(:only AS bigint) IS NULL OR t.id = :only)
  AND NOT EXISTS (SELECT 1 FROM worktrees w WHERE w.task_id = t.id AND w.state = 'task')
  AND greatest(t.github_synced_at, vm.last_sighted_at) <= :cutoff
ORDER BY 1
"""


def list_silent_active_tasks(now: Optional[datetime] = None, *, only: Optional[int] = None) -> list[int]:
    """The ids of the `active` tasks the stall detector reclaims (`_SILENT_ACTIVE_SQL`), or just `only` when it
    still qualifies; `now` is the clock, injectable for tests."""
    from datetime import timedelta

    from sqlalchemy import text

    from app.runner_liveness import NODE_STILL_TALKING_SECONDS, OBSERVATION_FRESH_SECONDS, SILENCE_THRESHOLD

    now = now or datetime.now(timezone.utc)
    params = {
        "active": ACTIVE, "only": only, "cutoff": now - SILENCE_THRESHOLD,
        "talking": now - timedelta(seconds=NODE_STILL_TALKING_SECONDS),
        "observed_fresh": now - timedelta(seconds=OBSERVATION_FRESH_SECONDS),
    }
    with _cp_session() as session:
        return list(session.scalars(text(_SILENT_ACTIVE_SQL), params).all())


# ── The transitions ──────────────────────────────────────────────────────────────────────────────────

# What a `vanished` incident says when the loss was node-wide: points the operator at the node, since N
# incidents with one timestamp share one cause (`app/runner_liveness.py`).
_NODE_WIDE_LOSS_DETAIL = (
    "SEVERAL runners on this node were lost in the SAME observation, so this is the NODE's fault and not "
    # Scope-neutral: lands on an incident in the task's org (`app/tenancy/copy.py`).
    "this task's: look at the node (its tmux server, its daemon) rather than at this task, and "
    "expect sibling `vanished` incidents carrying the same stamp. This task's dispatch attempt was "
    "REFUNDED rather than charged, so the node fault did not spend its retry budget."
)


def _record_vanished_runner(
    task_id: int, observed_by: str, session: Session, *, node_fault: bool = False,
    facts: Any = None, cause: str = "vanished",
) -> None:
    """Record the break causes no runner can report: its own disappearance (`vanished`), or a launch its node
    never saw running (`launch_failed`). `facts` (`runner_liveness.RunnerFacts`, read before the reclaim cleared
    the binding) names the node, account, session and launch on the incident, so it survives the node's deletion,
    and marks whether the attached pane log is a predecessor launch's (`runner_breaks.predecessor_pane`).

    Called only on a won reclaim (`if reclaimed:`), and each reclaim helper's atomic UPDATE carries
    positive evidence of a runner, so a row that never had one produces no incident.

    Best-effort: the transition is already committed, so failures are logged, never raised.
    `node_fault` puts the node-wide cause in the incident body so N sibling incidents point at the
    node."""
    from app import runner_breaks
    from app.models.tasks import Task

    task = session.get(Task, task_id)
    if task is None:
        return
    try:
        runner_breaks.record(
            session, task=task, cause=cause, observed_by=observed_by,
            detail=_NODE_WIDE_LOSS_DETAIL if node_fault else "",
            node_id=runner_breaks.node_label(
                getattr(facts, "node", ""), getattr(facts, "node_id", ""),
                own_node=getattr(facts, "own_node", False),
            ),
            account=getattr(facts, "account", ""),
            session_id=getattr(facts, "session_id", ""),
            launch=getattr(facts, "launch", "") or runner_breaks.UNKNOWN,
            predecessor_pane=runner_breaks.predecessor_pane(
                getattr(facts, "pane_log_written_at", None), getattr(facts, "launch_at", None)
            ),
        )
    except Exception as exc:  # noqa: BLE001 — a recording failure must never fail the reclaim
        log.warning("runner-break record failed for reclaimed task %s: %s", task_id, exc)
        session.rollback()


def _reclaim_active(task_id: int, observed_by: str, *, cause: Optional[str] = None) -> dict:
    """The shared body of the `active` reclaim doors: node-wide read, atomic reclaim, and the `vanished`
    incident on a won reclaim. The node-wide question is asked here because the reclaimers take liveness as
    an injected boolean (`task_pipeline_job._runner_held`), and this call holds the session. `cause` names a
    cause the caller observed itself (`reclaim_stalled`); None infers `launch_failed` or `vanished`."""
    from app import runner_liveness
    from app.routes.tasks.runner_settle import reclaim_active_task

    from app.runner_terminations import CAUSE_LAUNCH_FAILED, CAUSE_VANISHED

    with _cp_session() as session:
        node_fault = runner_liveness.node_wide_runner_loss(str(task_id), session)
        facts = runner_liveness.runner_facts(str(task_id), session)
        cause = cause or (CAUSE_LAUNCH_FAILED if facts.never_sighted else CAUSE_VANISHED)
        reclaimed = reclaim_active_task(task_id, session, node_fault=node_fault, cause=cause)
        if reclaimed:
            _record_vanished_runner(task_id, observed_by, session, node_fault=node_fault, facts=facts,
                                    cause=cause)
        return {"reclaimed": reclaimed}


def merge_lane_tick() -> dict:
    """The merge lane's per-tick pass (`app/merge_lane.py::tick`): requeue rows a lane run no longer holds, and
    re-file lane runs for every project with an open row — the backstop for `recheck_after`. Nothing in an edition
    without the lane."""
    from app import edition_features

    if not edition_features.enabled("merge_lane"):
        return {"filed": []}
    from app import merge_lane

    with _cp_session() as session:
        filed = merge_lane.tick(session)
        session.commit()
        return {"filed": filed}


def reclaim_active(task_id: int) -> dict:
    """Reclaim a stale row `active → ready` (crash recovery). Called by sweeps that first
    confirm the runner is gone: `reclaim_stale_active_analysis_children` and
    `reclaim_stale_active_restartable_tasks`. Without it such a row stays `active` forever.

    No persona gate. Same helper as `reclaim_active_coder`; two doors only so the incident names the
    sweep."""
    return _reclaim_active(task_id, "reclaim-active")


def reclaim_active_coder(task_id: int) -> dict:
    """Reclaim a stale build task `active → ready` — `reclaim_active` under the coder sweep's label.
    Safe because the caller's merge-watch proves the runner gone with no terminal reported. An open PR
    or pushed branch is not a bar: the restarted runner resumes onto it."""
    return _reclaim_active(task_id, "reclaim-active-coder")


def reclaim_stalled(task_id: int, now: Optional[datetime] = None) -> dict:
    """Reclaim an `active` row the stall detector found silent (`list_silent_active_tasks`), recording a
    `stalled` incident: the same reclaim as `reclaim_active`, under the cause the detector observed. Re-judged
    first, so a relaunch or a fresh signal since the pass's read is never reclaimed."""
    from app.runner_terminations import CAUSE_STALLED

    if not list_silent_active_tasks(now, only=task_id):
        return {"reclaimed": False}
    return _reclaim_active(task_id, "stall-detector", cause=CAUSE_STALLED)


def reclaim_awaiting_response(task_id: int) -> dict:
    """Reclaim a task parked on a question whose runner is gone, `awaiting_response → ready`, once the
    liveness probe confirms it. No GH/branch pre-checks: a parked row shipped nothing. The helper is
    atomic and idempotent and drops the open question round so the relaunched runner can ask again."""
    from app import runner_liveness
    from app.routes.tasks.awaiting import reclaim_awaiting_response_task

    with _cp_session() as session:
        node_fault = runner_liveness.node_wide_runner_loss(str(task_id), session)
        # A parked runner asked its question, so it was running: always `vanished`, never `launch_failed`.
        facts = runner_liveness.runner_facts(str(task_id), session)
        reclaimed = reclaim_awaiting_response_task(task_id, session, node_fault=node_fault)
        if reclaimed:
            _record_vanished_runner(task_id, "reclaim-awaiting-response", session, node_fault=node_fault,
                                    facts=facts)
        return {"reclaimed": reclaimed}


def expire_awaiting_response(task_id: int) -> dict:
    """Bound a task parked on a question nobody read, in two stages; called for every parked row.

    The older stage first, so a row past both bounds fails in one pass:
    `expire_timed_out_awaiting_response_task` fails it past the release bound, else
    `stop_runner_on_unanswered_question` stops the runner and leaves the question answerable. No
    liveness probe. Guards live in each stage's atomic write, so a non-qualifying row is a no-op.
    Resolved answers waiting for a launched session are retried through the same fenced hand-off as
    the launch ack, closing races where an answer or the hold arrived after that ack."""
    from sqlalchemy import select

    from app import slot_state
    from app.models.worktrees import Worktree
    from app.routes.tasks.answers import retry_answer_delivery_after_launch
    from app.routes.tasks.awaiting import (
        expire_timed_out_awaiting_response_task,
        stop_runner_on_unanswered_question,
    )

    with _cp_session() as session:
        if expire_timed_out_awaiting_response_task(task_id, session):
            return {"expired": True, "stopped": False}
        stopped = stop_runner_on_unanswered_question(task_id, session)
        if not stopped:
            hold = session.execute(select(
                Worktree.id, Worktree.generation, Worktree.dispatch_id, Worktree.launch_session_id,
            ).where(
                Worktree.task_id == task_id,
                Worktree.state == slot_state.TASK,
                Worktree.launch_session_id.isnot(None),
                Worktree.dispatch_id.isnot(None),
            )).first()
            if hold is not None and hold.launch_session_id:
                retry_answer_delivery_after_launch(
                    session, task_id=task_id, worktree_id=hold.id, generation=hold.generation,
                    dispatch_id=hold.dispatch_id, session_id=hold.launch_session_id,
                )
        return {"expired": False, "stopped": stopped}


def settle_if_artifact(task_id: int) -> dict:
    """Settle a stale epic analysis child `active|system_failed → done` when its stage-output artifact
    is already captured — probed before a reclaim. Idempotent and fail-closed.

    The hard manifest binds here too: the settle route commits the summary note before its 422, so an
    unsatisfied hard row yields `settled=False` and the sweep falls through to the reclaim."""
    from app.models.tasks import Task
    from app.routes.tasks.runner_settle import settle_analysis_child_on_artifact
    from app.task_settle import missing_hard_kinds

    with _cp_session() as session:
        task = session.get(Task, task_id)
        if task is not None and missing_hard_kinds(task, {}, session):
            return {"settled": False}
        return {"settled": settle_analysis_child_on_artifact(task_id, session)}


def ready_issueless(child_id: int) -> dict:
    """Flip an epic child `draft → ready` — the fan-out's edge. Guarded and idempotent
    (`ready_pipeline_child_issueless`): acts only on a child in `draft`."""
    from app.routes.tasks.epics import ready_pipeline_child_issueless

    with _cp_session() as session:
        return {"readied": ready_pipeline_child_issueless(child_id, session)}


def fanout_attempt_failed(child_id: int) -> dict:
    """Bump an epic child's `fanout_attempts` after a failed `draft → ready`. At `epic_fanout_cap()`
    `record_fanout_attempt_failed` escalates the parent epic (never the child) to `system_failed`.
    Raises `LookupError` for an unknown child."""
    from app.routes.tasks.epics import record_fanout_attempt_failed

    with _cp_session() as session:
        result = record_fanout_attempt_failed(child_id, session)
        if result is None:
            raise LookupError(f"task {child_id} not found")
        return dict(result)


def reconcile_epic_rollups() -> dict:
    """Reconcile-scan self-heal for a missed edge trigger: re-evaluate every active epic through the
    same guarded UPDATE, so a finished epic completes within one pass. Idempotent. `stuck` lists
    still-active epics (after the rollup) whose children are all `done` but not rollup-ready; the
    sweep applies its own alert threshold."""
    from app.routes.tasks.epics import scan_active_epics_for_rollup, scan_stuck_epics

    with _cp_session() as session:
        rolled_up = scan_active_epics_for_rollup(session)
        return {"rolled_up": rolled_up, "stuck": scan_stuck_epics(session)}


def finish(finish_id: int) -> dict:
    """The finish crash-backstop's settle: compute the finish report server-side, persist it on the
    epic's `metrics`, then flip the finish child `→ done` idempotently. An already-`done` child returns
    its persisted report with `settled: True`. Raises `LookupError` if `finish_id` is not a finish child."""
    from app.models.tasks import Task
    from app.routes.tasks.policy import PERSONA_FINISH
    from app.routes.tasks.epic_finish import execute_finish_phase

    with _cp_session() as session:
        row = session.get(Task, finish_id)
        if row is None or row.persona != PERSONA_FINISH:
            raise LookupError(f"finish task {finish_id} not found")
        report = execute_finish_phase(finish_id, session)
        session.expire_all()
        row = session.get(Task, finish_id)
        return {
            "id": row.id, "epic_id": row.parent_task_id, "state": row.state,
            "settled": row.state == DONE, "report": report,
        }


# ── The escalation pair ───────────────────────────────────────────────────────────────────────────────


def create_system_task(
    persona: str,
    project: str,
    title: str,
    body: str,
    state: str = "active",
    dedup_open: bool = True,
    dedup_key: str | None = None,
    priority: int | None = None,
    about_task_id: int | None = None,
    project_id: int | None = None,
) -> dict:
    """The deduped hidden-Task upsert behind `POST /api/agent/tasks/system`, called in process, so the
    dedup guard, body marker and `adhoc`-needs-a-key 422 still apply. `HOST_PRODUCER_CALLER` declares a
    producer with no Task behind it. `project_id`, or `about_task_id` (that task's project), names the project
    by id — project names repeat across orgs; with neither, `project` is read as one of the operator's. Best-effort: `{}` on any failure."""
    from app.routes.agent import auth as agent_auth, task_reads as agent_task_reads

    payload: dict[str, Any] = {
        "persona": persona, "project": project, "title": title, "body": body,
        "state": state, "dedup_open": dedup_open,
    }
    if dedup_key is not None:
        payload["dedup_key"] = dedup_key
    if priority is not None:
        payload["priority"] = priority
    if project_id is not None:
        payload["project_id"] = project_id
    try:
        with _cp_session() as session:
            if about_task_id is not None:
                from sqlalchemy import select

                from app.models.tasks import Task

                payload["project_id"] = session.scalar(select(Task.project_id).where(Task.id == about_task_id))
                if payload["project_id"] is None:
                    # Gone: never fall back to the name, which would be read in the operator plane.
                    log.warning("task-pipeline: task %s is gone; no %s system task filed", about_task_id, persona)
                    return {}
            out = agent_task_reads.create_system_task(
                body=agent_task_reads.SystemTaskIn(**payload),
                x_agent_task_id=agent_auth.HOST_PRODUCER_CALLER,
                _=None,
                session=session,
            )
            return out.model_dump()
    except Exception as exc:  # noqa: BLE001 — best-effort, as on the node
        log.warning("task-pipeline: create_system_task failed (%s/%s): %s", persona, project, exc)
        return {}


def block_task(task_id: int, question: str) -> dict:
    """`active → awaiting_response` — the janitor escalation's park, with the question on
    `awaiting_response_body`; a human answers via `POST /api/tasks/{id}/answer`.

    The only caller of this transition. It parks a hidden `adhoc` row created `active` with no runner
    launched — a question the control plane asks the operator.

    Best-effort: `{}` when the row is not a dispatched `active` task or the persona has no terminal
    call."""
    from app.models.tasks import Task
    from app.routes.agent import task_terminal as agent_task_terminal
    from app.routes.tasks.awaiting import await_response_on_dispatched_task
    from app.task_lifecycle import AWAITING_RESPONSE

    # An empty question parks a row on nothing answerable, and `awaiting_response_body` is uncapped.
    if not question or len(question) > 100_000:
        log.warning("task-pipeline: block_task %s refused — question empty or over 100_000 chars", task_id)
        return {}
    try:
        with _cp_session() as session:
            agent_task_terminal._refuse_a_persona_with_no_terminal_call(task_id, session, verb="block")
            state = await_response_on_dispatched_task(task_id, question, session)
            if state is None:
                log.warning("task-pipeline: block_task %s refused — not a dispatched 'active' task", task_id)
                return {}
            # A blocked compliance_fix GROUP Task cascades the park to its per-cell mirrors so the
            # deficiency surfaces blocked in the matrix. A no-op for any other row.
            agent_task_terminal._settle_compliance_group(
                session.get(Task, task_id), AWAITING_RESPONSE, session, reason=question
            )
            return {"state": state}
    except Exception as exc:  # noqa: BLE001
        log.warning("task-pipeline: block_task %s failed: %s", task_id, exc)
        return {}


def this_node_id() -> str:
    """The identity this process reports as: the control plane, not a node, since the sweep runs here.
    `NODE_ID` overrides it only so a local stack can label its own alerts."""
    return os.environ.get("NODE_ID", "control-plane")
