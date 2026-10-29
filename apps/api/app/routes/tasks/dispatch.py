"""Task dispatch: the dispatchable queue, claim, and Start (`_start`, `_start_epic`)."""
from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import String, and_, bindparam, case, cast, false, func, literal, or_, select, true, update
from sqlalchemy.orm import Session, aliased

from app import actors, git_access, merge_lane_state, placement_grants, runner_terminations, slot_allocation, slot_state
from app import comment_notify, comment_service
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.object_access import named_user_may_start
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.comment_service import Subject
from app.events import emit_console_event
from app.identities import display_name
from app.tenancy.copy import WITHHELD_ACTOR
from app.models.audit import ActivityEvent
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task, TaskDependency
from app.models.worktrees import Worktree
from app.task_ancestry import may_stamp_starter
from app import ce_caps, edition_features
from app.task_lifecycle import (
    ACTIVE,
    DONE,
    DRAFT,
    PAUSED,
    READY,
    SYSTEM_FAILED,
    TRANSITION_BY_NAME,
    _DISPATCHABLE_STATES,
    _OPEN_DISPATCH_STATES,
    _RUNNER_RUNNING_STATES,
)
from app.task_lookup import get_task_or_404
from app.task_title import draft_title
from app.org_gate import require_org_active
from app.routes.tasks import epics
from app.routes.tasks.policy import (
    PERSONA_EPIC,
    TRANSITION_HOLD,
    _DISPATCH_COMPOSED_BODY,
    _PERSONA_TASK_TEMPLATES,
    _PRIORITY_ORDER,
    _WORK_ITEM_OWN_BODY,
    _WORK_ITEM_PARENT_EPIC,
    _advisor_chat_cap_or_409,
)
from app.routes.tasks.qa_render import _task_merge_lane_verdict, _task_qa_trail
from app.routes.tasks.task_wire import (
    TaskDispatchOut,
    TaskDispatchProjectOut,
    TaskOut,
    _task_attachments_meta,
    _task_effective_autonomous,
    _task_out_with_children,
    _task_prerequisite_artifacts,
    _task_prerequisite_outputs,
    _task_prerequisite_recon_map,
    _task_prior_session_ended,
)
from app.routes.tasks.create import _create_epic_children

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)


def _dispatch_work_item(task: Task, source: str, session: Session) -> str:
    """Resolve `{work_item}` at dispatch by `source`: `parent_epic` reads the LIVE parent body (own body
    only if the parent row is missing); `own_body` reads the task's own."""
    if source == _WORK_ITEM_PARENT_EPIC and task.parent_task_id is not None:
        parent = session.get(Task, task.parent_task_id)
        if parent is not None:
            return parent.body or ""
    return task.body or ""


def _compose_dispatch_body(task: Task, session: Session) -> str:
    """The body a runner is ACTUALLY dispatched with, composed fresh on every dispatch read. A persona
    with no template entry returns `task.body` unchanged."""
    entry = _PERSONA_TASK_TEMPLATES.get(task.persona)
    # A playbook stage child works the container's live body when it holds the placeholder (a stage created
    # at start, or a once-stage a Plan filed) and its own otherwise (a planned item), RAW: its persona was
    # chosen for its deliverable, so that persona's template would describe a pipeline the stage is not in
    # (its role section is the stage's own — `playbook_engine.system_role`).
    if task.playbook_stage and task.parent_task_id is not None:
        source = _WORK_ITEM_PARENT_EPIC if task.body == _DISPATCH_COMPOSED_BODY else _WORK_ITEM_OWN_BODY
        return _dispatch_work_item(task, source, session)
    if entry is None:
        return task.body
    template, source = entry
    return template.format(work_item=_dispatch_work_item(task, source, session))


def _resolve_open_task_by_id(task_id: int, session: Session) -> Optional[Task]:
    """The composer's resolver: a Task is dispatched keyed on its own `task.id`. Returns the row iff it
    exists AND is in an open dispatch state. No ambiguity gate — a primary key is unique."""
    task = session.get(Task, task_id)
    if task is None or task.state not in _OPEN_DISPATCH_STATES:
        return None
    return task


def task_dispatch_out(
    task: Task, session: Session, principal: Principal | None = None
) -> TaskDispatchOut:
    """Build the dispatch projection for one open Task — the shape the composer (`app/dispatch_compose/`)
    builds every runner prompt from.

    `principal` decides what this discloses: four fields reach objects other than `task` (`autonomous`
    reaches the parent epic, deliberately unfiltered). `None` is the unfiltered infrastructure principal
    dispatch requires; the default exists for `app/dispatch_compose/`, while the four helpers carry no
    default so a session-path caller fails closed."""
    return TaskDispatchOut(
        id=task.id,
        project_id=task.project_id,
        state=task.state,
        persona=task.persona,
        # The row's OWN column, unfiltered by principal for the same reason `finish_follow_up_key` below
        # is: it names a structural fact about this Task rather than another object's content.
        parent_task_id=task.parent_task_id,
        title=task.title,
        # Composed at DISPATCH from this persona's template plus its live input, via the one uniform path —
        # the runner's actual work item, never the stored snapshot (`_compose_dispatch_body`).
        body=_compose_dispatch_body(task, session),
        qa_trail=_task_qa_trail(session, task),
        prior_stage_output=_task_prerequisite_outputs(session, task, principal),
        merge_lane_verdict=_task_merge_lane_verdict(session, task),
        # Reads the termination record, not the slot: a restart onto a fresh tree is still a restart.
        prior_session_ended=_task_prior_session_ended(session, task),
        # The effective switch (`_task_effective_autonomous`), not the row's own column.
        autonomous=_task_effective_autonomous(session, task),
        prerequisite_recon_map=_task_prerequisite_recon_map(session, task, principal),
        prerequisite_artifacts=_task_prerequisite_artifacts(session, task, principal),
        attachments=_task_attachments_meta(session, task, principal),
        # The row's own column, unfiltered: a structural fact about this Task, not another object's content.
        finish_follow_up_key=task.finish_follow_up_key,
        outcome_summary=task.outcome_summary,
        outcome_pr_url=task.outcome_pr_url,
        outcome_no_pr_reason=task.outcome_no_pr_reason,
        outcome_no_pr_evidence=task.outcome_no_pr_evidence,
        # The row's own pin, unfiltered (a fact about this Task); `dispatch_compose.compose._resolve_model` reads it.
        model=task.model,
        project=_task_project_delivery(session, task),
    )


def _task_project_delivery(session: Session, task: Task) -> TaskDispatchProjectOut:
    """The delivery columns of `task`'s own project — never another object's, so unfiltered by principal."""
    project = session.scalars(select(Project).where(Project.id == task.project_id)).one()
    return TaskDispatchProjectOut(
        integration_branch=project.integration_branch, branch_prefix=project.branch_prefix,
        gate_quick_cmd=project.gate_quick_cmd, gate_full_cmd=project.gate_full_cmd,
        push_enabled=project.push_enabled,
    )


def dispatchable_tasks(project_id: int, session: Session) -> list[Task]:
    """The project's dispatchable Tasks — every `ready` Task in project `project_id`, ordered
    `priority DESC, id`; an unknown project matches nothing (`[]`). Keyed purely on `task.id`.

    Dependency gate: a Task with any unmet prerequisite is excluded, as is one whose parent
    epic has one (`_parent_epic_has_unmet_prerequisite`); the NOT EXISTS reads live `state`. A `ready`
    task is dispatchable iff `run_requested_at IS NOT NULL` (the Run bypass over hold mode and both
    dependency gates) OR its epic is not in `hold` and every prerequisite is met."""
    return list(session.scalars(_dispatchable_statement(), {"dispatch_project_id": project_id}).all())


@functools.cache
def _dispatchable_statement() -> Any:
    """`dispatchable_tasks`' SELECT, built once per process with the project as a bound parameter.

    Built once because building it was the control plane's single hottest line: its four aliased
    correlated EXISTS cost ~9 ms of pure Python per construction, and the drain pass runs it (nodes + 3) x
    projects times every pass — ~20% of the api's CPU in the main profile. A `Select` is
    immutable, so one instance is safe to share across threads; the tenancy seam adds its criteria at
    execute time, per call, exactly as before."""
    parent = aliased(Task)
    parent_epic_holds = (
        select(parent.id)
        .where(parent.id == Task.parent_task_id, parent.transition_mode == TRANSITION_HOLD)
        .exists()
    )
    return (
        select(Task)
        .where(
            Task.project_id == bindparam("dispatch_project_id"),
            # Rendered into the SQL, not bound: the driver prepares a statement it runs this often and
            # Postgres then caches a GENERIC plan, which cannot prove a bound `$n` satisfies the partial
            # `ix_tasks_project_state_dispatchable` (`state = 'ready'`) and falls back to every row the
            # project has — ~13x slower, the top statement in Performance Insights.
            Task.state.in_(
                bindparam("dispatchable_states", _DISPATCHABLE_STATES, expanding=True, literal_execute=True)
            ),
            # An archived child must NEVER dispatch — it is kept only as evidence of a replaced
            # decomposition, excluded unconditionally whatever `state` it was frozen at.
            Task.archived_at.is_(None),
            # A Stopped epic beats a manual Run: unconditional, outside the or_() below, since `run_requested_at`
            # bypasses the other gates. Same predicate at both claim floors (`_parent_epic_paused`).
            ~_parent_epic_paused(),
            or_(
                Task.run_requested_at.is_not(None),
                and_(
                    ~parent_epic_holds,
                    ~_has_unmet_prerequisite(),
                    ~_parent_epic_has_unmet_prerequisite(),
                    ~_vanish_backoff_holds(),
                ),
            ),
        )
        .order_by(_PRIORITY_ORDER, Task.id)
    )


# ── Re-dispatch backoff after a vanished runner ─────────────────────────────────────────
# A task whose runner vanished waits `base * 2^(n-1)` seconds (capped) after its latest vanish before it is
# dispatchable again, `n` counting its vanishes inside the window. Without it a node that keeps losing runners
# took the same 17 tasks back every ~3 minutes, 3-8 times each, and the node-fault refund kept every attempt
# cap from ending the loop. Derived from the `runner_terminated` notes, so no column holds it; the Run
# bypass (`run_requested_at`) skips it, so an operator's Start is never delayed.
VANISH_BACKOFF_BASE_SECONDS = 60
VANISH_BACKOFF_CAP_SECONDS = 900
VANISH_BACKOFF_WINDOW_SECONDS = 3600


def vanish_backoff_secs(vanishes: int) -> int:
    """How long a task with `vanishes` recent vanishes waits after the latest one. Pure."""
    if vanishes <= 0:
        return 0
    return min(VANISH_BACKOFF_BASE_SECONDS * 2 ** (vanishes - 1), VANISH_BACKOFF_CAP_SECONDS)


def _lost_runner_notes_in_window(note: Any) -> list[Any]:
    """The backoff's counted notes, on `note` (an aliased `ActivityEvent`): lost-runner notes
    (`RUNNER_LOST_CAUSES`) inside `VANISH_BACKOFF_WINDOW_SECONDS`. The gate's."""
    return [
        note.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
        note.data["cause"].astext.in_(sorted(runner_terminations.RUNNER_LOST_CAUSES)),
        note.created_at > func.now() - func.make_interval(0, 0, 0, 0, 0, 0, VANISH_BACKOFF_WINDOW_SECONDS),
    ]


def _vanish_backoff_holds() -> Any:
    """Correlated EXISTS: the enclosing Task is still inside its vanish backoff (`vanish_backoff_secs`, in SQL).

    One aggregate over the task's lost-runner notes (`RUNNER_LOST_CAUSES`) in the window, read through
    `ix_activity_events_data_id`; `HAVING` with no `GROUP BY` yields no row for a task that never lost one,
    so the EXISTS is false."""
    note = aliased(ActivityEvent)
    waited = func.extract("epoch", func.now() - func.max(note.created_at))
    backoff = func.least(
        VANISH_BACKOFF_BASE_SECONDS * func.power(2, func.count() - 1), VANISH_BACKOFF_CAP_SECONDS
    )
    return (
        select(literal(1))
        .select_from(note)
        .where(note.data["id"].astext == cast(Task.id, String), *_lost_runner_notes_in_window(note))
        .having(waited < backoff)
        .correlate(Task)
        .exists()
    )


def _parent_epic_paused() -> Any:
    """Correlated EXISTS: the enclosing Task row's parent is a `paused` epic — "a Stopped epic beats a
    manual Run". One shared predicate for all three dispatch/claim gates, always an unconditional conjunct
    OUTSIDE the Run-bypass `or_()`. Keyed on `persona == 'epic'`. Fresh alias per call."""
    parent = aliased(Task)
    return (
        select(parent.id)
        .where(
            parent.id == Task.parent_task_id,
            parent.persona == PERSONA_EPIC,
            parent.state == PAUSED,
        )
        .exists()
    )


def _parent_epic_has_unmet_prerequisite() -> Any:
    """Correlated EXISTS: the enclosing Task row's PARENT EPIC has a prerequisite that is neither `done`
    nor archived. Same shape and fresh-alias rule as `_parent_epic_paused`, same unmet semantics as
    `_has_unmet_prerequisite`.

    An epic is never dispatched, so its own edge has no row to gate: without this, epic B sequenced
    behind task A would start its children immediately. Placed inside the Run-bypass `or_()` at every
    gate, like the child's own dependency gate."""
    parent = aliased(Task)
    prereq = aliased(Task)
    return (
        select(TaskDependency.task_id)
        .join(parent, parent.id == TaskDependency.task_id)
        .join(prereq, prereq.id == TaskDependency.depends_on_task_id)
        .where(
            parent.id == Task.parent_task_id,
            parent.persona == PERSONA_EPIC,
            or_(prereq.state != DONE, merge_lane_state.awaiting_lane_merge_clause(prereq.id)),
            prereq.archived_at.is_(None),
        )
        .exists()
    )


def _has_unmet_prerequisite() -> Any:
    """Correlated EXISTS: the enclosing Task row has a prerequisite that is neither `done` NOR archived.
    One shared predicate for all three dispatch/claim gates, fresh alias per call. ARCHIVED PREREQUISITES
    NEVER COUNT AS UNMET: an archived row will never reach `done` again, so a live dependent must not wedge
    waiting on a row that can no longer move."""
    prereq = aliased(Task)
    return (
        select(TaskDependency.task_id)
        .join(prereq, prereq.id == TaskDependency.depends_on_task_id)
        .where(
            TaskDependency.task_id == Task.id,
            or_(prereq.state != DONE, merge_lane_state.awaiting_lane_merge_clause(prereq.id)),
            prereq.archived_at.is_(None),
        )
        .exists()
    )


def _active_dispatch_values(now: datetime) -> dict[str, Any]:
    """Shared value-set for a `ready → active` dispatch edge: lands `active` and increments
    `dispatch_attempts` in the same atomic statement, so the counter bumps exactly once per attempt
    started. Shared by both `ready → active` writers.

    The ceiling is evaluated in `end_attempt`, which makes a double-report idempotent. Also clears
    `run_requested_at`, so a Run fires once."""
    return {
        "state": ACTIVE,
        "dispatch_attempts": Task.dispatch_attempts + 1,
        "run_requested_at": None,
        "github_synced_at": now,
        # A new runner is a live runner: a stale "question runner stopped" would make readers treat a live frozen
        # session as dead.
        "question_runner_stopped_at": None,
    }


def _bound_provider_stamp():
    """The value `bound_provider` takes ON THE CLAIM — the vendor this task dispatches on, stamped at its
    first successful dispatch and never again (PRD R4.1/R4.4).

    An expression, not a read, so it rides the claim's own atomic UPDATE, whose `WHERE state = 'ready'`
    decides the winner. `coalesce` makes it first-dispatch-wins: relaunch, resume and replacement runner
    keep the binding, so `routes/fleet.py`'s narrowing is one mechanism.

    The source is the CP's decision (`worktrees.leased_account_id` via `slot_state.bind_account`), not
    the runner's self-report. The trigger is the runner's claim, a step later: a dispatch that dies
    before SessionStart leaves the row unbound and free to move vendor. "Dispatched" in R4.1 means a
    session claimed the task; stamping at `bind_account` is a deferred follow-up.

    An unresolved source stamps nothing (NULL stays "not bound"); never default to a vendor."""
    return func.coalesce(
        Task.bound_provider,
        select(Account.provider)
        .select_from(Worktree)
        .join(Account, Account.id == Worktree.leased_account_id)
        # Correlated on the row being updated (`Task.id`), never a captured id.
        .where(Worktree.task_id == Task.id, Worktree.state == slot_state.TASK)
        # Ordered, because the write is permanent: a redispatch race can briefly leave two task-state slots, and
        # the most recently dispatched hold is this claim's; `nullslast` keeps an unstamped hold from outranking it.
        .order_by(Worktree.dispatched_at.desc().nullslast())
        .limit(1)
        .scalar_subquery(),
    )


def _terminates_a_running_runner(task_id: int, session: Session) -> bool:
    """Was a runner RUNNING against this Task at the instant the caller is about to move it — read under
    a ROW LOCK, so the answer is still true when the caller's own guarded UPDATE lands.

    The lock is the point: `stop`, `reset`, `complete` and `_start`'s restart all have `sources` wider
    than `_RUNNER_RUNNING_STATES`, so under READ COMMITTED `end_attempt` could land the row `ready` (and
    write its own termination row) just before the operator's UPDATE writes a second. `FOR UPDATE`
    closes that. Reads the column, not the ORM object, which the caller's `update()` may refresh.

    The persona is part of the test: an `epic`'s `active` is a rollup and no runner ever existed, so a
    Stop must not write `operator_stop` on the container. Same statement as the state read.

    Held to the caller's single commit: an epic Reset takes one per descendant, in id order, under the
    epic's own lock."""
    row = session.execute(
        select(Task.state, Task.persona).where(Task.id == task_id).with_for_update()
    ).first()
    return (
        row is not None
        and row.state in _RUNNER_RUNNING_STATES
        and row.persona != PERSONA_EPIC
    )


def _node_grant_floor(session: Session, task_id: Optional[int], node_id: Optional[str]):
    """`true()` or `false()` — the NODE arm of the claim edge, as a plain conjunct.

    A conjunct beside the archived / paused-epic / prerequisite floors, so a row leaked into an outside
    candidate set cannot be claimed; `false()` makes the UPDATE match nothing. Fail-closed: an absent or
    unresolvable `node_id` refuses. `placement_grants.node_may_claim` is the whole decision."""
    return true() if placement_grants.node_may_claim(session, task_id, node_id) else false()


def claim_task_active_ready_by_id(
    task_id: int, session: Session, node_id: Optional[str] = None,
) -> bool:
    """Dispatcher-owned `ready → active` claim: ONE atomic guarded UPDATE advances the `ready` Task keyed
    by `task.id`, called after the dispatcher wins the slot lease. `state == READY` is the concurrency
    guard, so a second call returns False.

    The floors (archived, paused epic, node grant, dependencies, hold) mirror `dispatchable_tasks`. Run
    bypasses the three sequencing floors, never the node grant (an access decision). Bumps
    `dispatch_attempts` via `_active_dispatch_values`; the ceiling is evaluated when the attempt ends.
    True iff this call won."""
    now = datetime.now(timezone.utc)
    parent = aliased(Task)
    parent_epic_holds = (
        select(parent.id)
        .where(parent.id == Task.parent_task_id, parent.transition_mode == TRANSITION_HOLD)
        .exists()
    )
    row = session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.state == READY,
            # An archived row must never be claimed even if its frozen `state` reads `ready` (defense in depth).
            Task.archived_at.is_(None),
            # A paused epic's child is never claimable — unconditional (see `_parent_epic_paused`).
            ~_parent_epic_paused(),
            # Claimable only from a node its launcher is granted; unconditional, and Run does not bypass it.
            _node_grant_floor(session, task_id, node_id),
            # Manual Run bypasses every sequencing floor exactly as in `dispatchable_tasks`.
            or_(
                Task.run_requested_at.is_not(None),
                and_(
                    ~parent_epic_holds,
                    ~_has_unmet_prerequisite(),
                    ~_parent_epic_has_unmet_prerequisite(),
                    ~_vanish_backoff_holds(),
                ),
            ),
        )
        .values(
            **_active_dispatch_values(now),
            # The vendor binding, on the claim's own statement (see `_bound_provider_stamp`). An SDK agent run
            # (`run_agent_id`, stamped with its lease by `agent_resolution.stamp_run`) is no vendor's: the
            # binding stays as it was, so a slot's stale leased account can never bind it.
            bound_provider=case(
                (or_(Task.run_agent_id.is_not(None), Task.agent_id.is_not(None)), Task.bound_provider),
                else_=_bound_provider_stamp(),
            ),
            **actors.task_actor_values(session),
        )
        .returning(Task.id, Task.state)
    ).first()
    if row is None:
        return False
    claimed_id, final_state = row
    # The plan precondition: starting a planner archives the epic's existing subtasks in the claim's
    # transaction, so a do-nothing re-run cannot settle `done` on stale children.
    claimed = session.get(Task, claimed_id)
    if claimed is not None:
        epics.archive_epic_children_for_plan(claimed, session)
    emit_console_event(session, entity="task", id=claimed_id, action=final_state)
    session.commit()
    log.info(
        "task %s claimed ready->%s by the dispatcher (github_synced_at=%s)",
        claimed_id, final_state, now,
    )
    return True


def _finalize_unsaved_draft(task: Task, session: Session) -> Optional[list[Task]]:
    """The moment an auto-saved draft becomes everybody else's to see. Returns the analysis children this
    call built (an epic), or None — which also means "this row was not unsaved", the ordinary case.

    Pays what the auto-save withheld: the audience (`unsaved` false, so `authz.visible_task_clause` stops
    narrowing to the author); the title (placeholder replaces `UNSAVED_DRAFT_TITLE`; the caller enqueues
    the refinement after commit); an epic's children (`_create_analysis_children`); and the "created"
    `activity_events` row.

    Mutates the session, never commits. Two callers, the two "I meant this" gestures: an explicit `PUT`
    and `start_task` (else an unsaved epic would dispatch with no children). Idempotent by its own guard."""
    if not task.unsaved:
        return None
    # Only the constant is replaced: the author may have typed a title into their unsaved draft
    # (`task_title.is_placeholder_title` makes the same distinction). Before the flag clears: `draft_title` reads it.
    task.title = draft_title(task)
    task.unsaved = False
    children = (
        _create_epic_children(task, session)
        if task.persona == PERSONA_EPIC
        else None
    )
    # `created`, the only creation row this task ever gets; the auto-saved create emitted a notify without
    # recording.
    emit_console_event(session, entity="task", id=task.id, action="created")
    return children


def _save_unsaved_draft_before_dispatch(
    task: Task, session: Session, principal: Principal | None
) -> None:
    """Every lever that makes a `draft` row live finalizes it first — the single home for that step.

    Asking a row to run is stronger intent than saving it, so these doors finalize rather than refuse.
    It commits, unlike `_finalize_unsaved_draft`, so visibility does not depend on which door's later
    guard raised. A no-op on a saved row."""
    if not task.unsaved:
        return
    task_id = task.id
    _finalize_unsaved_draft(task, session)
    session.commit()
    # After the commit and deferred, for the two reasons `_create_coder_task` states at the same seam.
    from app.durable.task_title_queue import enqueue_title

    enqueue_title(task_id)
    session.refresh(task)


def _start_epic(
    task_id: int, project: Project, session: Session, principal: Principal | None,
    *, push_as_id: Optional[UUID] = None,
) -> TaskOut:
    """Shared epic "go" transition, called by `start_task` on a draft epic:
    flips the epic `draft → active` via a single atomic guarded UPDATE, 409ing if already started. Assumes
    the caller validated the persona. `push_as_id` is a `_push_identity_or_422`-vetted stand-in starter."""
    starter = _starter_stamp(session, task_id, push_as_id or (principal.user_id if principal is not None else None))
    landed = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.persona == PERSONA_EPIC, Task.state == DRAFT)
        .values(
            state=ACTIVE,
            # The epic is where the stamp matters most: never dispatched, but every pipeline child inherits it
            # through `starter_user_id`'s walk (none carries its own), so the subtree pushes as whoever pressed Go. `principal.user_id`
            # because this helper receives only the `Principal`. The `None` arm is an agent-filed epic started by
            # its filer (`file_agent_top_level_task`), whose row was stamped its executor at filing, or a cross-org
            # operator (`_starter_stamp`); the absent key preserves that stamp, where `None` would erase it.
            **({"started_by_id": starter} if starter is not None else {}),
            **actors.task_actor_values(session),
        )
    )
    session.commit()
    if landed.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail=f"epic {task_id} is not 'draft' (already started, or a concurrent request won)",
        )
    emit_console_event(session, entity="task", id=task_id, action=ACTIVE)
    session.commit()
    if push_as_id is not None:
        _tell_push_as_member(session, task_id, principal, push_as_id)
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)


# ── The one `* -> ready` edge, and the shared subtask dispatch flip on top of it ────────────────────
# `_RESET_FRESH_COLUMNS` clears a prior attempt's residue on Start/Run; `_RUNNER_BINDING_CLEARED` is
# spread into each terminal settle (non-NULL `worktree_ref` = a runner holds it). `bound_provider` is in
# neither (PRD R4.1): it is what the task is, and clearing it would let a relaunch change vendor
# (`tests/test_bound_provider_affinity.py`). Only the operator's provider pin (`TaskUpdate.bound_provider`) moves it.
_RUNNER_BINDING_CLEARED: dict[str, Any] = {"runner_session_id": None, "worktree_ref": None}

_RESET_FRESH_COLUMNS: dict[str, Any] = {
    "outcome_summary": None,
    "outcome_pr_url": None,
    "outcome_no_pr_reason": None,
    "outcome_no_pr_evidence": None,
    "merged_at": None,
    "clarity_score": None,
    "spec_verdict": None,
    "critic_verdict": None,
    "metrics": None,
    "awaiting_response_body": None,
    "stale_reason": None,
    "stale_since": None,
    "runner_session_id": None,
    "worktree_ref": None,
    "fanout_attempts": 0,
    # The last attempt's dispatch wait (`app/dispatch_wait.py`): still fresh, it would greet the restart.
    "dispatch_wait_reason": None,
    "dispatch_wait_at": None,
}


# ── Who started it: `started_by_id` ─────────────────────────────────────────────────
# Whose GitHub credential the pushes borrow (`task_ancestry.starter_user_id`, `git_access.py`); not
# `created_by_id`. Written on edges toward running (`start`, its epic branches) and once more at filing: a
# row a runner files at top level (`file_agent_top_level_task`, `create_system_task`) carries its chain's
# executor (`task_ancestry.executor_user_id`). Pipeline children carry none and inherit the epic's
# through the walk. `reset`/`stop`/`complete` leave it; a host-filed row stays NULL (machine-filed). Only a
# member of the task's org is stamped (`_starter_stamp`). The answer verbs argue themselves at
# `_settle_gate_if_nothing_outstanding`.
def _starter_stamp(session: Session, task_id: int, user_id: Optional[UUID]) -> Optional[UUID]:
    """`user_id` when `task_ancestry.may_stamp_starter` allows it, else None: an operator acting as the task's org
    keeps the earlier stamp instead of erasing the human starter with an org-zero seat."""
    if user_id is None:
        return None
    task = session.get(Task, task_id)
    if task is not None and may_stamp_starter(session.get(AdminUser, user_id), task.org_id):
        return user_id
    log.info("start: actor %s is not in task %s's org, so the task keeps its starter", user_id, task_id)
    return None


def _tell_push_as_member(session: Session, task_id: int, principal: Principal | None, push_as_id: UUID) -> None:
    """After a Start that named a stand-in (`push_as_id`) has committed: a comment from the operator mentioning
    the member ("Started by Jaah support as @Bob."), so they learn their git account now answers for this task — the
    mention is their bell entry and DM (`comment_notify`). Best-effort: the Start has landed, and a comment
    that fails must not read as a failed Start."""
    try:
        operator = session.get(AdminUser, principal.user_id) if principal is not None else None
        member = session.get(AdminUser, push_as_id)
        task = session.get(Task, task_id)
        if operator is None or member is None or task is None:
            return
        operator_label = actors.actor_names(session, [operator.id], withhold_staff=True).get(
            operator.id, WITHHELD_ACTOR,
        )
        comment, fresh = comment_service.create(
            session,
            Subject("task", task_id),
            author=operator,
            # The operator's label as the tenant reads staff ("Jaah support"): stored text both sides read.
            body=f"Started by {operator_label} as @{display_name(member)}.",
            mentioned=lambda _comment: [member],
        )
        session.commit()
        session.refresh(comment)
        comment_notify.notify_mentioned(session, task=task, comment=comment, author=operator, mentioned=fresh)
    except Exception:  # noqa: BLE001 — see the docstring
        session.rollback()
        log.exception("start: could not tell %s they were named starter of task %s", push_as_id, task_id)


def _start(
    task_id: int,
    session: Session,
    *,
    sources: tuple[str, ...] | None,
    run_requested: bool,
    started_by: Optional[UUID] = None,
    values: Optional[dict[str, Any]] = None,
    events: tuple[str, ...] = (READY,),
    guards: tuple[Any, ...] = (),
) -> bool:
    """The single `* -> ready` edge — the one place in apps/api that starts a Task.

    `run_requested` says whether this is the per-subtask Run, dispatching now past the epic's `hold` and
    unmet prerequisites; it persists to `run_requested_at`, which the dispatch query and claim floors read
    and the claim clears. One conditional UPDATE whose `rowcount` is the caller's guard; `sources` is the
    legal source states, or None for the transition table's own. Resets `dispatch_attempts`, or a Task
    restarted from `system_failed` would sit at the cap."""
    # ── The chat cap belongs here, on the one start edge ────────────────────────────────────────────────
    # Mint-time checks alone are defeated by `stop -> create -> start` (`paused` is uncounted, since
    # `lease_drain._pause_unattended_advisors` parks chats there) and by other `tasks:edit` routes reaching
    # `ready`. As the single start edge, this is the only place with one answer for every door. The mint-time
    # arm stays (drafts count, and an early 409 is actionable). A no-op for other personas; raised before the
    # UPDATE.
    starting = session.get(Task, task_id)
    if starting is not None:
        _advisor_chat_cap_or_409(
            session,
            project_id=starting.project_id,
            creator_id=starting.created_by_id,
            persona=starting.persona,
            # The row being started already exists and must never count against its own start.
            exclude_task_id=task_id,
        )
    payload: dict[str, Any] = {"state": READY, **(values or {})}
    if TRANSITION_BY_NAME["start"].resets_attempts:
        payload["dispatch_attempts"] = 0
    # Written both ways: a queueing start must clear a stale Run stamp, or it would bypass gates it declined.
    payload["run_requested_at"] = datetime.now(timezone.utc) if run_requested else None
    # The key is absent, not NULL, when no starter is named: NULL would erase an earlier human starter and
    # move pushes onto the system credential. A named starter always overwrites.
    if started_by is not None:
        payload["started_by_id"] = started_by
    # `sources=None` means the transition table's own sources, not a literal. A literal `state != active`
    # admitted `awaiting_response` and orphaned a live runner inside `AskUserQuestion`; the operator decided
    # Start must not touch a parked row (only Stop may). The table is also what the board reads
    # (`available_transitions`), so button and route cannot drift.
    source_guard = (
        Task.state.in_(TRANSITION_BY_NAME["start"].sources)
        if sources is None
        else Task.state.in_(sources)
    )
    # Under a row lock, read before the source guard (`_terminates_a_running_runner`). Unreachable under
    # `sources=None` (start excludes both running states) but kept because `sources` is a parameter.
    # ── The epic row lock, taken here ────────────────────────────────────────────────────────────────
    # Epic-then-child: the UPDATE below takes this child's row, so locking the epic later would deadlock
    # against `stop_task`. Scoped to `persona = 'epic'`, so `locked_epic is None` means "no epic parent". A
    # lock, because Postgres takes no tuple lock on a row failing the UPDATE's qualification.
    child = aliased(Task)
    parent_id_subquery = (
        select(child.parent_task_id).where(child.id == task_id).scalar_subquery()
    )
    locked_epic = session.execute(
        select(Task.id)
        .where(Task.id == parent_id_subquery, Task.persona == PERSONA_EPIC)
        .with_for_update()
    ).first()
    restarted_a_running_runner = _terminates_a_running_runner(task_id, session)
    landed = session.execute(
        update(Task).where(Task.id == task_id, source_guard, *guards)
        .values(**payload, **actors.task_actor_values(session))
    )
    if landed.rowcount == 0:
        # Rolled back here (`apply_complete`'s convention): with the epic lock now unconditional, a start that
        # lands nothing would otherwise hold the epic row for the rest of the request.
        session.rollback()
        return False
    # Before the `run`/`ready` emits and in this transaction, so the row sorts above the transition it explains.
    if restarted_a_running_runner:
        runner_terminations.record(session, task_id, runner_terminations.CAUSE_OPERATOR_RESTART)
    # Starting a child re-activates its parent epic in this transaction, from any state. Exception (owner
    # decision): an epic failed by its children waits until no live child is `system_failed`. Locked at the
    # top of this function (epic-then-child). The epic gets no starter stamp: it would overwrite or invent
    # attribution for future children; their pushes are refused until the epic itself is started.
    reactivated_parent = session.execute(
        update(Task)
        .where(
            Task.id == parent_id_subquery,
            Task.persona == PERSONA_EPIC,
            Task.state != ACTIVE,
            or_(Task.state != SYSTEM_FAILED, ~epics._epic_has_failed_child()),
        )
        # Drop only the reason this reactivation invalidated (a failed epic's child list); a `paused` or `done`
        # epic's body is left alone.
        .values(
            state=ACTIVE,
            awaiting_response_body=case(
                (Task.state == SYSTEM_FAILED, None), else_=Task.awaiting_response_body,
            ),
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if reactivated_parent is not None:
        emit_console_event(session, entity="task", id=reactivated_parent[0], action=ACTIVE)
    elif locked_epic is not None:
        # Not reactivated: on a `system_failed` epic a sibling is still spent, so rewrite the reason to name only
        # what remains. It cannot clear the epic here: that is the same question the UPDATE just answered.
        epics._reevaluate_epic_child_failure(locked_epic[0], session)
    for action in events:
        emit_console_event(session, entity="task", id=task_id, action=action)
    session.commit()
    return True


def _dispatch_subtask_ready(
    task: Task, session: Session, principal: Principal | None, *, run_requested: bool,
    push_as_id: Optional[UUID] = None,
) -> TaskOut:
    """Land a non-epic subtask at `ready` with the one-shot `run_requested_at` dispatch flag — the shared
    flip used by `start_task`'s non-epic branch and `force_start_task`. One conditional `UPDATE ... WHERE
    state != 'active'` whose `rowcount` is the guard, so an `active` row 409s rather than double-dispatching.

    Reset-fresh: every column that is residue of a prior attempt is wiped unconditionally in the same
    UPDATE. The task's definition, identity and planner-authored `recon_map`/`phase_*` inputs stay."""
    task_id = task.id
    if not _start(
        task_id, session,
        sources=None,          # the transition table's own `start.sources` — see `_start`
        run_requested=run_requested,
        # The `is None` arm is unreachable (`require_user`) but degrades to no stamp, per `_start`. The stamp is
        # not part of reset-fresh: it names who answers for the next attempt.
        # `push_as_id`: an acting operator's chosen org member (`_push_identity_or_422`).
        started_by=_starter_stamp(
            session, task_id, push_as_id or (principal.user_id if principal is not None else None),
        ),
        values=dict(_RESET_FRESH_COLUMNS),
        events=("run",),
    ):
        # Sibling-consistency with `stop`: discard the (no-op) UPDATE's transaction before the 409.
        session.rollback()
        # Names the state actually found (`active` or `awaiting_response`), re-read after the rollback.
        blocked_state = session.scalar(select(Task.state).where(Task.id == task_id))
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} is {blocked_state!r} — a runner is on it. Stop it first: that kills the "
                f"runner and says so, where a Start here would silently orphan it"
            ),
        )
    if push_as_id is not None:
        _tell_push_as_member(session, task_id, principal, push_as_id)
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)


def _release_for_fresh_start(task: Task, session: Session) -> list[Any]:
    """The release half of "Start fresh elsewhere" (`microvm_lifecycle.release_for_fresh_start`), uncommitted:
    `_start` commits it with the Start's own `task:run`, so a refused Start rolls it back. A refusal raises 409
    before anything is written. Returns the destroy intents to send after that commit."""
    if not edition_features.enabled("microvm"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)  # The community edition has no MicroVMs.
    from app import microvm_lifecycle

    _, refusal = microvm_lifecycle.fresh_start_refusal(session, task)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    # The epic before the child, the order `_start` takes them (re-taken there), so the release's task-row lock
    # cannot deadlock against `stop_task`.
    session.execute(
        select(Task.id).where(Task.id == task.parent_task_id, Task.persona == PERSONA_EPIC).with_for_update()
    ).first()
    _, issued = microvm_lifecycle.release_for_fresh_start(
        session, task.id, reason=f"task {task.id}: started fresh elsewhere")
    return list(issued)


# ── Action route: start — the unified idempotent Start for EVERY task ──────────────────────────────
class TaskStartIn(BaseModel):
    """The start payload. `run_requested` is the ONE thing a caller declares: "dispatch now, past the epic's
    hold and any unmet prerequisite", persisted to `run_requested_at`. Default False, so a bodyless POST
    only queues — the conservative reading, since a mistaken bypass dispatches past an operator's hold."""

    run_requested: bool = False
    #: "Start fresh elsewhere": let go of the task's MicroVM (destroyed, no longer its place) and keep its next
    #: placement off that VM's node (`launch_failures.excluded_node_ids`), so a Stop or Reset on a node that
    #: never answers does not hold the restart. 409 when the task has no VM or its project no other node.
    fresh_elsewhere: bool = False
    # Whose git account the runner pushes with, when the presser cannot be the starter: an operator acting as
    # the task's org is no member of it (`_starter_stamp`), so their Start leaves no starter and the push is
    # refused at push time. Absent: the presser, as ever. See `_push_identity_or_422`.
    push_as_id: Optional[UUID] = None


def _push_identity_or_422(
    session: Session, task: Task, push_as_id: Optional[UUID], principal: Principal | None, actor: AdminUser,
) -> Optional[UUID]:
    """`push_as_id` when an acting operator may name it as the task's starter, else a 422 naming why.

    Only an operator acting as the task's org (`Principal.acting`) may name someone: everyone else is stamped
    themself. The named person must pass `named_user_may_start`, so the stand-in answers for no more than their
    own Start would, and the token route can find their git account. `Principal.acting` admits this one thing an
    org's own Owner lacks: an Owner is a member, so their Start already names them.

    Absent, the Start must still leave someone to push as (`_refuse_starterless_start`)."""
    if push_as_id is None:
        _refuse_starterless_start(session, task, principal.user_id if principal is not None else None)
        return None
    if principal is None or not principal.acting:
        raise HTTPException(
            status_code=422,
            detail="push_as_id is only for an operator acting as an org; your own Start already pushes as you",
        )
    user = session.get(AdminUser, push_as_id)
    if (
        user is None
        or task.project_id is None
        or not named_user_may_start(session, user, task.project_id, task.org_id, actor=actor)
    ):
        raise HTTPException(
            status_code=422,
            detail="push_as_id must be a member of this task's project who may start its tasks",
        )
    log.info("start: acting operator %s names %s as task %s's starter", actor.id, user.id, task.id)
    return user.id


def _refuse_starterless_start(session: Session, task: Task, presser_id: Optional[UUID]) -> None:
    """422 when this Start would put a repository task live with nobody to do its git work as.

    The presser is stamped only as a seat of the task's org (`_starter_stamp`); an operator acting as the org
    is none. When the chain names no starter either and no cron owner stands in (`git_access.git_person_for_task`),
    the Start used to land anyway and the runner learned at push time, after the work, that every fetch and
    push is refused. A folder task has no repository and needs nobody."""
    if _starter_stamp(session, task.id, presser_id) is not None:
        return
    if not slot_allocation.task_has_repo(session, task) or git_access.git_person_for_task(session, task) is not None:
        return
    raise HTTPException(
        status_code=422,
        detail=(
            f"Task {task.id} has no one in its org to push as, and you aren't a member. Pick a member to push as."
        ),
    )


@router.post("/{task_id}/start", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def start_task(
    task_id: int,
    body: Optional[TaskStartIn] = None,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Unified idempotent Start for ANY task — the single "make this live" lever every row exposes. Routed
    by persona and current state:
      - `active`, any task → NO-OP with no state write and no event, so a double-click cannot double-start.
      - EPIC, non-active: `draft` funnels through `_start_epic`; any other non-active state REACTIVATES the
        epic and resets each child's `fanout_attempts`. A lost race is the idempotent no-op, never a 409.
      - NON-EPIC, non-active → dispatch via the shared `_dispatch_subtask_ready` helper."""
    task = get_task_or_404(task_id, session)

    # `active` → idempotent no-op for ANY task: the set is the table's own `idempotent_noop` on `start`.
    if task.state in TRANSITION_BY_NAME["start"].idempotent_noop:
        return _task_out_with_children(task_id, session, principal)
    push_as_id = _push_identity_or_422(session, task, (body or TaskStartIn()).push_as_id, principal, _user)
    # A pending org dispatches nothing (`app/org_gate.py`); before the draft save, so a refusal writes nothing.
    require_org_active(session, task.org_id)
    # A MicroVM-only project with no usable MicroVM node: refused here, not spent at dispatch. The community
    # edition has no MicroVMs.
    if edition_features.enabled("microvm"):
        from app import microvm_mode

        if task.persona != PERSONA_EPIC and task.project_id is not None \
                and (refusal := microvm_mode.start_blocker(session, task.project_id)) is not None:
            raise HTTPException(status_code=409, detail=refusal)

    # ── Starting an auto-saved draft is saving it ────────────────────────────────────────────────────
    # Otherwise it would dispatch a row others cannot see, titled "New task", and an epic without its
    # analysis children. Before any branch, through the shared door.
    _save_unsaved_draft_before_dispatch(task, session, principal)

    # NON-EPIC → dispatch the subtask via the shared helper.
    if task.persona != PERSONA_EPIC:
        project = session.get(Project, task.project_id)
        if project is None:
            raise HTTPException(status_code=422, detail=f"project {task.project_id} not found")
        start = body or TaskStartIn()
        issued: list[Any] = []
        if start.fresh_elsewhere:
            issued = _release_for_fresh_start(task, session)
        # No cap check here: it lives on the `* -> ready` edge itself (`_start`).
        out = _dispatch_subtask_ready(
            task, session, principal, run_requested=start.run_requested, push_as_id=push_as_id,
        )
        # After the Start's commit, which carried the release: a refused Start rolled it back and sends nothing.
        for batch in issued:
            batch.send()
        return out

    # Epic, non-active: both paths dispatch children.
    project = session.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=422, detail=f"project {task.project_id} not found")
    if task.state == DRAFT:
        # `_start_epic` 409s its own lost race, but a concurrent Start that moved the epic to `active` is success;
        # anything else re-raises.
        try:
            return _start_epic(task_id, project, session, principal, push_as_id=push_as_id)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            session.rollback()
            session.expire_all()
            current = get_task_or_404(task_id, session)
            if current.state != ACTIVE:
                raise
            return _task_out_with_children(task_id, session, principal)

    # Any other non-active state → REACTIVATE a paused/failed/blocked/stopped epic .
    starter = _starter_stamp(session, task_id, push_as_id or _user.id)
    landed = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.persona == PERSONA_EPIC, Task.state != ACTIVE)
        .values(
            state=ACTIVE,
            # Stamped: reactivation moves the epic toward running and dispatches its children. The no-op branch
            # returns before here, so a double-click re-stamps nothing. A cross-org operator keeps the earlier
            # stamp (`_starter_stamp`).
            **({"started_by_id": starter} if starter is not None else {}),
            # The same `case` as `_start`: this is another exit from `system_failed`, and a stale failed-children
            # list left here would stay for ever. A `paused`/`blocked` epic's body is the operator's.
            awaiting_response_body=case(
                (Task.state == SYSTEM_FAILED, None), else_=Task.awaiting_response_body,
            ),
            **actors.task_actor_values(session),
        )
    )
    if landed.rowcount == 0:
        # A concurrent Start already flipped it: the idempotent no-op. Roll back before re-reading.
        session.rollback()
        session.expire_all()
        return _task_out_with_children(task_id, session, principal)
    # Reset every direct child's fan-out budget for a clean retry; archived children keep theirs as evidence.
    session.execute(
        update(Task)
        .where(
            Task.parent_task_id == task_id, Task.fanout_attempts > 0, Task.archived_at.is_(None)
        )
        .values(fanout_attempts=0, **actors.task_actor_values(session))
    )
    emit_console_event(session, entity="task", id=task_id, action=ACTIVE)
    session.commit()
    if push_as_id is not None:
        _tell_push_as_member(session, task_id, principal, push_as_id)
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)
