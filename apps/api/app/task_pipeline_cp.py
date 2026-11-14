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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator, Optional

# Stdlib-only at import (beyond the stdlib-only `app.task_lifecycle`): `task_pipeline.py`'s quick-tier
# suites run on a bare host, so ORM, seam and route imports stay inside functions.
from app.clock import utcnow
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, DRAFT, _LIVE_UNIT_STATES, _NEEDS_ATTENTION_STATES

if TYPE_CHECKING:  # pragma: no cover — annotations only
    from sqlalchemy.orm import Session

log = logging.getLogger("cockpit-scheduler")


@contextmanager
def _cp_session() -> Iterator[Session]:
    """One session for one pipeline call, on the fleet-wide plane (see the module docstring).

    One per call, not per pass: a session shared across the pass would let one refused row poison the
    rest."""
    from app.db import BridgedSession
    from app.tenancy import seam

    with seam.operator_scope(), BridgedSession() as session:
        yield session


# ── The work-lists ────────────────────────────────────────────────────────────────────────────────────


def list_open_tasks() -> list[dict]:
    """Open Tasks for the stale-runner reclaimers and the parked-task expirer: every row in `_LIVE_UNIT_STATES`
    (`ready`/`active`/`awaiting_response`/`system_failed`; never `draft`). Consumers filter on `state`
    themselves. Each row carries `parent_task_id`, `persona`, `project`, the two terminal-reference
    columns the coder reclaimer reads, and `unanswered_due`: for an `awaiting_response` row, whether
    `expire_awaiting_response` could act on it (`awaiting.unanswered_question_may_be_due`); False otherwise.

    Those columns only, never whole `Task` rows: hydrating every open task's full row (its body included) was
    measurable CPU. A sweep pass reads this once and hands it to every consumer."""
    from sqlalchemy import case, false, select

    from app.models.projects import Project
    from app.models.tasks import Task
    from app.routes.tasks.awaiting import unanswered_question_may_be_due

    with _cp_session() as session:
        rows = session.execute(
            select(
                Task.id, Project.name.label("project"), Task.state, Task.parent_task_id, Task.persona,
                Task.outcome_no_pr_reason, Task.outcome_pr_url,
                # CASE, so the per-row subqueries run for parked rows only.
                case((Task.state == AWAITING_RESPONSE, unanswered_question_may_be_due()), else_=false())
                .label("unanswered_due"),
            )
            .join(Project, Task.project_id == Project.id)
            .where(Task.state.in_(_LIVE_UNIT_STATES))
        ).all()
        return [dict(row._mapping) for row in rows]


def list_epics_fanout() -> list[dict]:
    """Started epics (`persona='epic' AND state='active'`) with at least one `draft` child — the eager
    fan-out's worklist, with the epic's project and children. Drains to empty once fully
    fanned out. Archived children (`archived_at` set) never re-enter the worklist."""
    from sqlalchemy import select

    from app.models.projects import Project
    from app.models.tasks import Task
    from app.routes.tasks.policy import PERSONA_EPIC

    with _cp_session() as session:
        epics = session.execute(
            select(Task.id, Project.name)
            .join(Project, Task.project_id == Project.id)
            .where(Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
            .order_by(Task.id)
        ).all()
        if not epics:
            return []
        # Every active epic's children in one read, not one per epic: this runs every pass.
        children: dict[int, list[dict]] = {}
        for child in session.execute(
            select(Task.parent_task_id, Task.id, Task.title, Task.body, Task.state)
            .where(Task.parent_task_id.in_([epic_id for epic_id, _ in epics]), Task.archived_at.is_(None))
            .order_by(Task.id)
        ).all():
            children.setdefault(child.parent_task_id, []).append(
                {"id": child.id, "title": child.title, "body": child.body, "state": child.state}
            )
        out: list[dict] = []
        for epic_id, project_name in epics:
            child_rows = children.get(epic_id, [])
            if not any(c["state"] == DRAFT for c in child_rows):
                continue  # every child readied — nothing left to fan out
            out.append({"id": epic_id, "project": project_name, "children": child_rows})
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
    """The Needs-attention set `task_pipeline.sweep_needs_attention_notify` announces: every Task in an actionable
    state (`_NEEDS_ATTENTION_STATES`, the one admission filter: a state missing there is never announced), every
    persona, hidden rows included; the console board shows the same set via "Show hidden". Newest first. A light
    projection plus `age_seconds` on the database's clock, for the sweep's grace gate
    (`task_pipeline.NOTIFY_GRACE_SECONDS`), so the sweep never compares its clock to the database's.

    The one exclusion changes only what is announced, never what the board shows: an interactive chat
    (`OPERATOR_CRITERION_PERSONAS`, derived from the criterion map so a new live-human persona is covered) parks
    `awaiting_response` at every turn with its human at the console, and an entry-notify would ping per turn.
    Scoped to the park, not the persona, matching `task_notify.py`: a chat's `system_failed` (runner died, nobody
    guaranteed watching) is still announced. The OR form is equivalent to `not_(and_(…))` since both columns are
    NOT NULL.

    `[]` when the read fails: a miss skips one notify tick; the require_user view is authoritative."""
    from sqlalchemy import func, or_, select

    from app.models.projects import Project
    from app.models.tasks import Task
    from app.routes.tasks.policy import OPERATOR_CRITERION_PERSONAS

    try:
        with _cp_session() as session:
            rows = session.execute(
                select(
                    Task.id, Task.persona, Task.state, Task.project_id, Project.name, Task.title, Task.hidden,
                    # `updated_at` is the entry instant for a parked row; a later edit only delays this backstop.
                    func.extract("epoch", func.now() - Task.updated_at).label("age_seconds"),
                )
                .join(Project, Project.id == Task.project_id)
                .where(Task.state.in_(_NEEDS_ATTENTION_STATES))
                .where(or_(
                    Task.persona.notin_(OPERATOR_CRITERION_PERSONAS),
                    Task.state != AWAITING_RESPONSE,
                ))
                .order_by(Task.id.desc())
            ).all()
            return [{
                "id": r.id, "persona": r.persona, "state": r.state, "project_id": r.project_id, "project": r.name,
                "title": r.title, "hidden": r.hidden, "age_seconds": float(r.age_seconds or 0.0),
            } for r in rows]
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

    now = now or utcnow()
    params = {
        "active": ACTIVE, "only": only, "cutoff": now - SILENCE_THRESHOLD,
        "talking": now - timedelta(seconds=NODE_STILL_TALKING_SECONDS),
        "observed_fresh": now - timedelta(seconds=OBSERVATION_FRESH_SECONDS),
    }
    with _cp_session() as session:
        return list(session.scalars(text(_SILENT_ACTIVE_SQL), params).all())


# ── The transitions ──────────────────────────────────────────────────────────────────────────────────

def _reclaim_active(task_id: int, *, cause: Optional[str] = None) -> dict:
    """The shared body of the `active` reclaim doors: node-wide read and atomic reclaim. The node-wide question is asked here because the reclaimers take liveness as
    an injected boolean (`task_pipeline_job._runner_held`), and this call holds the session. `cause` names a
    cause the caller observed itself (`reclaim_stalled`); None infers `launch_failed` or `vanished`."""
    from app import runner_liveness
    from app.routes.tasks.runner_settle import reclaim_active_task

    from app.runner_terminations import CAUSE_LAUNCH_FAILED, CAUSE_VANISHED

    with _cp_session() as session:
        node_fault = runner_liveness.node_wide_runner_loss(str(task_id), session)
        if cause is None:
            never_sighted = runner_liveness.launch_never_sighted(str(task_id), session)
            cause = CAUSE_LAUNCH_FAILED if never_sighted else CAUSE_VANISHED
        return {"reclaimed": reclaim_active_task(task_id, session, node_fault=node_fault, cause=cause)}


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

    No persona gate. Same helper as `reclaim_active_coder`."""
    return _reclaim_active(task_id)


def reclaim_active_coder(task_id: int) -> dict:
    """Reclaim a stale build task `active → ready` — `reclaim_active` for the coder sweep.
    Safe because the caller's merge-watch proves the runner gone with no terminal reported. An open PR
    or pushed branch is not a bar: the restarted runner resumes onto it.

    The terminal references are re-read here: the sweep judged them from its pass-wide read, and a settle
    that recorded one since must not be requeued over."""
    from sqlalchemy import select

    from app.models.tasks import Task

    with _cp_session() as session:
        reported = session.execute(
            select(Task.outcome_pr_url, Task.outcome_no_pr_reason).where(Task.id == task_id)
        ).first()
    if reported is not None and (reported.outcome_pr_url or reported.outcome_no_pr_reason):
        return {"reclaimed": False}
    return _reclaim_active(task_id)


def reclaim_stalled(task_id: int, now: Optional[datetime] = None) -> dict:
    """Reclaim an `active` row the stall detector found silent (`list_silent_active_tasks`), under the
    `stalled` cause: the same reclaim as `reclaim_active`, under the cause the detector observed. Re-judged
    first, so a relaunch or a fresh signal since the pass's read is never reclaimed."""
    from app.runner_terminations import CAUSE_STALLED

    if not list_silent_active_tasks(now, only=task_id):
        return {"reclaimed": False}
    return _reclaim_active(task_id, cause=CAUSE_STALLED)


def reclaim_awaiting_response(task_id: int) -> dict:
    """Reclaim a task parked on a question whose runner is gone, `awaiting_response → ready`, once the
    liveness probe confirms it. No GH/branch pre-checks: a parked row shipped nothing. The helper is
    atomic and idempotent and drops the open question round so the relaunched runner can ask again."""
    from app import runner_liveness
    from app.routes.tasks.awaiting import reclaim_awaiting_response_task

    with _cp_session() as session:
        node_fault = runner_liveness.node_wide_runner_loss(str(task_id), session)
        return {"reclaimed": reclaim_awaiting_response_task(task_id, session, node_fault=node_fault)}


def expire_awaiting_response(task_id: int) -> dict:
    """Bound a task parked on a question nobody read, in two stages; called for every parked row that
    `list_open_tasks` marks `unanswered_due`.

    The older stage first, so a row past both bounds fails in one pass:
    `expire_timed_out_awaiting_response_task` fails it past the release bound, else
    `stop_runner_on_unanswered_question` stops the runner and leaves the question answerable. No
    liveness probe. Guards live in each stage's atomic write, so a non-qualifying row is a no-op.
    Resolved answers waiting for a launched session are retried through the same fenced hand-off as
    the launch ack, closing races where an answer or the hold arrived after that ack."""
    from sqlalchemy import select

    from app.models.worktrees import Worktree
    from app.routes.tasks.answers import retry_answer_delivery_after_launch
    from app.routes.tasks.awaiting import (
        addressable_hold_filter,
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
            ).where(*addressable_hold_filter(task_id))).first()
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
    dedup guard and `adhoc`-needs-a-key 422 still apply. `HOST_PRODUCER_CALLER` declares a
    producer with no Task behind it. `project_id`, or `about_task_id` (that task's project), names the project
    by id — project names repeat across orgs; with neither, the route answers 404 and this returns `{}`.
    Best-effort: `{}` on any failure."""
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
            out = agent_task_reads.create_system_task.session_body(
                session,
                body=agent_task_reads.SystemTaskIn(**payload),
                x_agent_task_id=agent_auth.HOST_PRODUCER_CALLER,
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
