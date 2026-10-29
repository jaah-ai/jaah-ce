"""Task update: `PUT /api/tasks/{task_id}`, its `_apply_*` edits, and tags/retag."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import actors, edition_features, fleet_verdicts, merge_lane_state, node_targets, runner_terminations, slot_retitle
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.object_access import can_access
from app.authz.principal import Principal, current_principal
from app.authz.visibility import ObjectAccess, has_project_key
from app.db import get_session
from app.events import emit_console_event
from app.features import FEATURE_SHIPPED
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.projects import Feature, Project
from app.models.tasks import Task
from app.entity_shape import get_or_404
from app.persona_registry import CREATABLE_PERSONAS, PERSONA_CREATE_AUTONOMOUS
from app import placement_grants, playbook_engine
from app.task_lifecycle import PAUSED, TRANSITION_BY_NAME, _LIVE_STATES, _RUNNER_RUNNING_STATES
from app.task_lookup import get_task_or_404
from app.task_retag import retag_task
from app.task_tags import TagRefOut, replace_task_tags
from app.tenancy.copy import unavailable_now
from app.tenancy.ids import is_tenant_org
from app.task_title import _rederive_children
from app.routes.tasks.policy import (
    PERSONA_ADVISOR,
    PERSONA_EPIC,
    TRANSITION_MODES,
    _advisor_chat_cap_or_409,
    create_autonomous_for_persona,
)
from app.routes.tasks.task_wire import TaskOut, _actor_label, _may_set_hidden, _subtree_task_ids, _task_out
from app.routes.tasks.slots import _release_task_slots, abandon_task_slots, send_minted_intents
from app.routes.tasks.dependencies import _SIGNIFICANT_EDIT_FIELDS, _propagate_staleness
from app.routes.tasks.create import (
    TaskUpdate,
    _AGENT_OR_PROVIDER,
    _BODY_VERSION_SOURCE_AUTOSAVE,
    _FEATURE_SHIPPED_DETAIL,
    _KEEP_PIN,
    _account_pick_or_422,
    _agent_pick_or_422,
    _may_pin_node_or_403,
    _pinned_node_or_refuse,
    _close_autosave_body_version,
    _create_analysis_children,
    _provider_on_plan_or_422,
    _external_issue_or_422,
    _model_pin_or_refuse,
    _open_autosave_body_version_id,
    _plan_from_snapshot,
    _playbook_plan_or_422,
    _playbooks_on_or_422,
    _provider_pin_or_refuse,
    _snapshot_body_version,
    _validated_assignee,
    sync_epic_child_autonomous,
)
from app.routes.tasks.dispatch import _RUNNER_BINDING_CLEARED, _finalize_unsaved_draft

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)


# States that block a project move: the live three, each a runner running or about to. A `paused` row
# moves (owner decision): Stop is the only way to quiet a tree, so refusing it left a stopped
# epic unmovable. Its held worktree belongs to the OLD project, so the move abandons it, as a Reset does.
_MOVE_BLOCKING_STATES = tuple(_LIVE_STATES)


#: A `TaskUpdate` field stored under another column name (the change record reads the column).
_FIELD_COLUMN = {"account_id": "bound_account_id"}

def _auditable(value):
    """One field value, in a form the `activity_events` JSONB column can actually hold.

    `feature_id` is a `UUID`, not JSON-serializable, so recording a filing raw would 500 inside the
    commit. Narrow on purpose: coerces `UUID` only (no `default=str`), so any other non-JSON type fails
    loudly here."""
    return str(value) if isinstance(value, UUID) else value


def _apply_project_move(
    task: Task,
    new_project_id: Optional[int],
    session: Session,
    principal: Principal | None,
    *,
    feature_id: Optional[UUID] = None,
    landing_pin: Optional[UUID] | object = _KEEP_PIN,
) -> list[fleet_verdicts.IssuedIntents]:
    """Move `task` AND ITS WHOLE SUBTREE to another project. Mutates the session, never commits; returns
    the teardown batches the caller pushes with `send_minted_intents` AFTER its commit.

    The move cascades: `project_id` is copied at creation, so a split subtree breaks dispatch enumeration,
    epic fan-out, the reclaimers' sweep and `visible_task_clause`. One statement over a recursive CTE, so
    no reader sees it half-moved. Top-level rows only. Resending the current project is a silent no-op
    running no guards (the edit form resends it).

    `feature_id` is what the subtree is filed in AFTER the move: None for a plain move (a feature belongs to
    one project, so leaving one leaves its feature), a feature of the destination when
    `_apply_feature_filing` delegates here.

    A `paused` row's held worktree is in the OLD project, where nothing could resume it, so the move
    abandons it in the same transaction — the slot half of a Reset (`abandon_task_slots`, then
    `_release_task_slots`). The row keeps its state and output; its next Start dispatches fresh in the
    destination."""
    if new_project_id is None:
        raise HTTPException(status_code=422, detail="project_id cannot be null")
    if new_project_id == task.project_id:
        return []                       # a resend of the current project — not a move, no guards
    # A child moved alone splits the subtree and crosses a read boundary: it would be republished to the
    # destination's audience, and the epic's editor could move it holding nothing on the destination.
    if task.parent_task_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"task {task.id} is a child of task {task.parent_task_id} — move the top-level task; "
                "the whole subtree moves with it"
            ),
        )
    # The route guard authorizes the row (a key on the source project), never the destination: `tasks:edit`
    # there too. "Does not exist" and "not yours" are one 422, or this is an existence oracle.
    project = session.get(Project, new_project_id)
    if (
        project is None
        or not can_access(session, principal, "project", new_project_id, mode=ObjectAccess.WRITE)
        or not has_project_key(principal, permission_key("tasks", "edit"), new_project_id)
    ):
        raise HTTPException(status_code=422, detail="project not found")
    # Neither into a system project (its rows are pinned node tasks only) nor a node task out of one.
    source = session.get(Project, task.project_id)
    if project.system_kind is not None or (source is not None and source.system_kind is not None):
        raise HTTPException(status_code=422, detail=node_targets.UNPINNED_SYSTEM_TASK)
    ids = _subtree_task_ids(task.id, session)
    # A node pin must still name a node the destination runs on, or the row could never place: every
    # pinned row of the subtree, and for the moved task the pin the same save lands (`landing_pin`), not the old one.
    landing_nodes = placement_grants.project_node_ids(session, project.id, project.org_id)
    pins = dict(session.execute(
        select(Task.id, Task.pinned_node_id).where(Task.id.in_(ids), Task.pinned_node_id.is_not(None))
    ).all())
    if landing_pin is not _KEEP_PIN:
        pins.pop(task.id, None)
        if landing_pin is not None:
            pins[task.id] = landing_pin
    stranded = next((tid for tid, pin in sorted(pins.items()) if pin not in landing_nodes), None)
    if stranded is not None and not placement_grants.node_pin_enabled(task.org_id):
        # The org's node picker is off, so these pins place nothing (`placement_grants.effective_pin`) and their
        # editor may not change them (`_may_pin_node_or_403`): clear the ones the destination cannot run, never
        # refuse the move over them.
        for row in session.scalars(select(Task).where(Task.id.in_([t for t, p in pins.items() if p not in landing_nodes]))):
            row.pinned_node_id = None
        stranded = None
    if stranded is not None:
        raise HTTPException(
            status_code=422,
            detail=f"task {stranded} is pinned to a node that does not run {project.name} — set Node to Any first",
        )
    # Refused while any subtree row is live — a correctness gate: holds are keyed by project but found by
    # `task_id`, so a mid-flight move would keep the old slot while becoming dispatchable in the new
    # project (double dispatch). Same `_LIVE_STATES` as the delete guard; the blocking row is named.
    # `FOR UPDATE`, so a concurrent Start cannot revive a paused row between this read and its abandon.
    rows = session.execute(
        select(Task.id, Task.state).where(Task.id.in_(ids)).order_by(Task.id).with_for_update()
    ).all()
    blocking = next((r for r in rows if r.state in _MOVE_BLOCKING_STATES), None)
    if blocking is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"task {blocking.id} is {blocking.state!r} — stop or reset it before moving the task "
                "to another project"
            ),
        )

    # The paused rows' worktrees and runner binding stay behind in the old project.
    paused_ids = [r.id for r in rows if r.state == PAUSED]
    issued: list[fleet_verdicts.IssuedIntents] = []
    for paused_id in paused_ids:
        reason = (
            f"task {paused_id} moved to another project, abandoning its stopped attempt's worktree"
        )
        _, row_issued = abandon_task_slots(paused_id, session, reason=reason)
        issued.extend(row_issued)
        _release_task_slots(paused_id, session, reason=f"released: {reason}")
    if paused_ids:
        session.execute(
            update(Task).where(Task.id.in_(paused_ids))
            .values(**_RUNNER_BINDING_CLEARED, **actors.task_actor_values(session))
        )

    try:
        # One ORM-enabled UPDATE over the subtree, synchronizing the in-session `task` for `update_task`'s
        # audit. The feature in the SAME `values(...)`: the key `(feature_id, project_id)` would refuse a row
        # whose feature still named the old project.
        session.execute(
            update(Task).where(Task.id.in_(ids))
            .values(
                project_id=new_project_id,
                feature_id=feature_id,
                **actors.task_actor_values(session),
            )
        )
        # The subtree's Live Cast rows follow it, so an admin's override keeps applying (`app/live_cast/state.py`).
        if edition_features.enabled("live_cast"):
            from app.live_cast import state as live_cast_state

            live_cast_state.follow_task_move(session, ids, new_project_id)
        session.flush()
    except IntegrityError as exc:
        # The partial unique index a move could violate (`uq_tasks_compliance_slot`), predicated on a live
        # state; the refusal above makes it unreachable in practice, but the refusal and this write are two
        # statements, so a concurrent claim could race in.
        session.rollback()
        log.warning(
            "project move of task %s -> project %s violated a partial unique index: %s",
            task.id, new_project_id, exc,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task.id} cannot move to project {new_project_id} — a live task in that "
                "project already holds the same compliance path; retry once that task settles"
            ),
        ) from exc
    return issued


def _apply_feature_filing(
    task: Task,
    feature_id: Optional[UUID],
    session: Session,
    principal: Principal | None,
    *,
    project_id: Optional[int] = None,
    landing_pin: Optional[UUID] | object = _KEEP_PIN,
) -> list[fleet_verdicts.IssuedIntents]:
    """File `task` AND ITS WHOLE SUBTREE into feature `feature_id`, or out of any feature when None
    ("Remove from feature"). Mutates the session, never commits, and NEVER writes `state`.

    The feature must be of the task's own project, or of `project_id` (a move submitted in the same
    request, delegated to `_apply_project_move` so every move guard applies); otherwise 422. An epic files
    its stage tasks with it; a stage task alone is refused, as for a move. Resending the current placement
    is a silent no-op (the edit form resends it)."""
    target_project_id = project_id if project_id is not None else task.project_id
    if feature_id == task.feature_id and target_project_id == task.project_id:
        return []                       # a resend of the current placement — not a filing, no guards
    if task.parent_task_id is not None:
        raise HTTPException(status_code=400, detail="a stage task moves with its epic")
    # The root first, `FOR UPDATE`, so two filings (or a filing and a fan-out) of one subtree serialize
    # rather than interleave, on either path below.
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update())
    if feature_id is not None:
        # `FOR SHARE`, for `_create_feature_or_refuse`'s reason: no ship or delete between check and write.
        feature_row = session.get(Feature, feature_id, with_for_update={"read": True})
        # "Does not exist" and "not yours to file into" are one 422: a view-only feature is as unfileable
        # as an invisible one.
        if (
            feature_row is None
            or not can_access(session, principal, "feature", feature_id, mode=ObjectAccess.READ)
            or not has_project_key(principal, permission_key("tasks", "edit"), feature_row.project_id)
        ):
            raise HTTPException(status_code=422, detail="feature not found")
        if feature_row.project_id != target_project_id:
            raise HTTPException(status_code=422, detail=f"feature {feature_id} is in another project")
        if feature_row.status == FEATURE_SHIPPED:
            raise HTTPException(status_code=400, detail=_FEATURE_SHIPPED_DETAIL)
    if target_project_id != task.project_id:
        return _apply_project_move(
            task, target_project_id, session, principal, feature_id=feature_id, landing_pin=landing_pin,
        )
    # One statement over the subtree, so no reader sees it half-filed.
    ids = _subtree_task_ids(task.id, session)
    session.execute(
        update(Task).where(Task.id.in_(ids))
        .values(feature_id=feature_id, **actors.task_actor_values(session))
    )
    session.flush()
    return []


def _apply_hidden(
    task: Task, hidden: Optional[bool], session: Session, principal: Principal | None
) -> None:
    """Set `task`'s `hidden` flag AND ITS WHOLE SUBTREE's (children are born with the epic's value, so the
    subtree agrees). Mutates the session, never commits. Null is a 422; a change on a child a 400; a resend
    of the current value a silent no-op.

    WHO: the row's creator or a superadmin, holding `tasks:edit` on its project — else 403. `hidden` is also
    the read gate (`app/authz/visibility.py::visible_task_clause`: a hidden row is the creator's and a superadmin's), so
    any other editor hiding a row would lock every other grantee, themselves included, out of it; and an
    author-draft-only author (`authored_draft_task_clause` admits unhidden rows only) could never undo it.
    `principal is None` is allow-all."""
    if hidden is None:
        raise HTTPException(status_code=422, detail="hidden cannot be null")
    if hidden == task.hidden:
        return
    if task.parent_task_id is not None:
        raise HTTPException(status_code=400, detail="a stage task is hidden or shown with its epic")
    if principal is not None and not _may_set_hidden(task, principal):
        raise HTTPException(
            status_code=403,
            detail="only the task's creator or a superadmin can hide or show it — a hidden task is theirs alone",
        )
    # One statement over the subtree, so no reader sees it half-hidden.
    session.execute(
        update(Task).where(Task.id.in_(_subtree_task_ids(task.id, session)))
        .values(hidden=hidden, **actors.task_actor_values(session))
    )
    session.flush()


def _apply_persona_conversion(
    task: Task, new_persona: Optional[str], session: Session
) -> Optional[list[Task]]:
    """Apply a requested `epic ⇄ runner-persona` conversion to `task`, returning the resulting child list,
    or None when the row's CHILD SET did not change. Mutates the session, never commits.

    Any top-level source persona, system personas included; the target must be `CREATABLE_PERSONAS`.
    Three directions:
      * `runner persona → epic` — builds the three analysis children (`_create_analysis_children`);
      * `epic → a runner persona` — parks then archives the children ("one row, 0 live children");
      * `runner persona → another runner persona` — a plain `persona` swap; returns None.
    None also covers a resend of the current persona (the form resends every field).

    Convertible at any time (teardown archives). A bad value is a 422; a row the operation is
    meaningless on is a 400. Every direction parks the converting row when live: an `active` row left
    behind could never be rolled up, started or reset, and the runner→runner park ends the outgoing
    persona's runner."""
    if new_persona not in CREATABLE_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=f"persona must be one of {', '.join(CREATABLE_PERSONAS)} (got {new_persona!r})",
        )
    if new_persona == task.persona:
        return None                     # a resend of the current persona — not a conversion, no guards
    if task.parent_task_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"task {task.id} is a child of task {task.parent_task_id} — the epic/runner persona "
                "can only be changed on a top-level task"
            ),
        )
    # ANY top-level source persona converts, a system one (`adhoc`, `triage`, …) included: an editor turns
    # an incident row into the runner that investigates and fixes it. Only the TARGET is the
    # picker's set above; a system row has no children, so it takes the runner→runner or runner→epic branch.

    stop = TRANSITION_BY_NAME["stop"]

    # The converting row is parked whenever it was live: an `active` epic with every child archived could
    # never roll up, and `active` is the state `start` no-ops on and `reset` refuses. Its own termination is
    # recorded here (an epic has no runner of its own; its children get theirs below). A best-effort read of
    # a stale ORM object, as is the unguarded park itself; guarding the park is a deferred follow-up.
    converting_terminated_runner = (
        task.state in _RUNNER_RUNNING_STATES and task.persona != PERSONA_EPIC
    )
    if task.state in stop.sources:
        task.state = stop.target
        task.run_requested_at = None
    # Before either branch returns, so the row is written on every direction.
    if converting_terminated_runner:
        runner_terminations.record(session, task.id, runner_terminations.CAUSE_PERSONA_CHANGED)

    if new_persona == PERSONA_EPIC:
        task.persona = PERSONA_EPIC
        # Not while the row is unsaved: the operator may flip Complex several times before Save, so only the
        # persona is set; `_finalize_unsaved_draft` builds the children at Save (as `_create_epic_with_children`).
        if task.unsaved:
            return None
        # The children take the epic's creator (`_create_epic_children`); no grants, they read through the scope arm.
        return _create_analysis_children(task, session)

    if task.persona != PERSONA_EPIC:
        # Runner persona → a different runner persona: only the persona changes. Neither side has children by
        # construction, so `None` tells `update_task` nothing to re-project; `is_plannable` is already False.
        task.persona = new_persona
        return None

    # Epic → a runner persona. Two guarded UPDATEs, not a read-modify-write loop: otherwise
    # `claim_task_active_ready_by_id` could claim a `ready` child that then gets archived. Park first, then
    # archive (reversed, the archive would remove rows from the park's predicate).
    now = datetime.now(timezone.utc)
    # Pre-read, then park: the children are then archived, so this is the last moment their runners are
    # attributable. Locked for `_roll_up_epic`'s reason.
    converted_running = list(session.scalars(select(Task.id).where(
        Task.parent_task_id == task.id,
        Task.archived_at.is_(None),
        # The park's `stop.sources` narrowed to the two states with a runner; `ready` children are parked too.
        Task.state.in_(_RUNNER_RUNNING_STATES),
    ).with_for_update()).all())
    parked_ids = list(session.execute(
        update(Task)
        .where(
            Task.parent_task_id == task.id,
            Task.archived_at.is_(None),
            Task.state.in_(stop.sources),
        )
        # A `ready` child with `run_requested_at` set is the one shape that would still be claimable.
        .values(state=stop.target, run_requested_at=None, **actors.task_actor_values(session))
        .returning(Task.id)
    ).scalars().all())
    # Intersected with what the UPDATE actually moved (`RETURNING`), as in `stop_task`'s cascade: a child
    # that settled in between must not get a termination row, which the archive would make durable.
    was_running = set(converted_running)
    runner_terminations.record_many(
        session, [cid for cid in parked_ids if cid in was_running],
        runner_terminations.CAUSE_PERSONA_CHANGED,
    )
    session.execute(
        update(Task)
        .where(Task.parent_task_id == task.id, Task.archived_at.is_(None))
        # ARCHIVED, NOT DESTROYED — see the docstring
        .values(archived_at=now, **actors.task_actor_values(session))
    )
    # No `session.expire_all()`: ORM-enabled UPDATEs already sync in-session children, and expiring would
    # discard this function's pending park on `task`.
    task.persona = new_persona      # the operator's Persona choice, never a hardcoded literal
    task.is_plannable = False    # LOAD-BEARING, see the docstring
    return []


def _apply_playbook(
    task: Task, submitted: bool, playbook_id: Optional[UUID], persona: Optional[str], session: Session,
    principal: Principal | None,
) -> bool:
    """Apply an edit's playbook (`TaskUpdate.playbook_id`) and persona to a row that has or gets a playbook.
    Returns True when the row no longer has one and a submitted `persona` is left to
    `_apply_persona_conversion`. Mutates, never commits.

    Before Save (`unsaved`) the playbook may be picked, swapped or dropped, and a simple playbook's persona
    re-picks its stage's expert; the row has no children yet (`_finalize_unsaved_draft` builds them). After
    Save the playbook and the persona it decided are fixed: changing either is a 400, and a resend of the
    same values is the form's ordinary no-op."""
    current_id = (task.playbook or {}).get("id")
    picks = playbook_id is not None and str(playbook_id) != current_id
    drops = submitted and playbook_id is None and task.playbook is not None
    if not task.unsaved:
        if picks or drops or (task.playbook and persona is not None and persona != task.persona
                              and not _same_simple_expert(task, persona)):
            raise HTTPException(
                status_code=400,
                detail=f"task {task.id}'s playbook is fixed after Save — create a new task to run another",
            )
        return task.playbook is None and persona is not None
    if drops:
        task.playbook = None
        task.playbook_stage = None
        return persona is not None
    if not picks and task.playbook is None:
        return persona is not None
    # A resend of the row's settle persona (which the form may show) keeps the expert it was picked as.
    if not picks and (persona is None or persona == task.persona):
        persona = _simple_override(task)
    if picks:
        plan = _playbook_plan_or_422(session, session.get(Project, task.project_id), playbook_id, persona, task.model)
    else:
        _playbooks_on_or_422(session, task.org_id)
        plan = _plan_from_snapshot(task.playbook or {}, persona, task.model)
    if plan.persona == PERSONA_ADVISOR and plan.persona != task.persona:
        # A Chat stage is a chat: the create door's two gates, judged on the persona the playbook resolved.
        if not has_project_key(principal, permission_key("worktrees", "edit"), task.project_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    "saving a chat requires worktrees:edit on this project — the advisor's deliverable is "
                    "a two-way console session, and without that permission the console is read-only"
                ),
            )
        _advisor_chat_cap_or_409(
            session, project_id=task.project_id, creator_id=task.created_by_id, persona=plan.persona,
        )
    task.playbook = plan.snapshot
    task.playbook_stage = plan.stage
    task.persona = plan.persona
    # One row, no children: `is_plannable` False as for every non-epic row (`new_top_level_task`).
    task.is_plannable = plan.persona == PERSONA_EPIC
    return False


def _simple_override(task: Task) -> Optional[str]:
    """The persona a simple-playbook row's expert was picked as, to keep it across an unrelated edit."""
    return playbook_engine.persona_of_expert((task.playbook_stage or {}).get("expert"))


def _same_simple_expert(task: Task, persona: str) -> bool:
    """A saved simple-playbook row resent with the persona its expert runs as (the form shows the expert's
    persona, which may differ from the row's settle persona) — a no-op, not a change."""
    return _simple_override(task) == persona


#: What a lane run (`merge_lane.is_lane_persona`) keeps: `update_task` refuses a write to any of these.
_LANE_RUN_FROZEN = frozenset({"persona", "body", "title", "project_id", "feature_id", "hidden", "playbook_id"})

@router.put("/{task_id}", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
def update_task(
    task_id: int,
    body: TaskUpdate,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Edit a Task in ANY state, `active` included: no state gate, because an epic's `transition_mode` has
    to be changeable mid-run.

    A runner reads body/title at dispatch, so a mid-run edit applies to the next dispatch. `persona`
    edits in every state too (conversions reconcile children by archiving). `project_id` is the one
    exception: a live subtree row holds a slot in the old project, so the move is refused. Resending
    the current persona or project is a no-op. See `_apply_project_move`."""
    task = get_task_or_404(task_id, session)
    fields = body.model_dump(exclude_unset=True)
    if "project_id" in fields and fields["project_id"] is None:
        raise HTTPException(status_code=422, detail="project_id cannot be null")
    # ── What kind of write this is, taken out of the field set first ───────────────────────────────────
    # `autosave` is not a column: every reader below treats a key as a task field (`getattr` would raise,
    # and `changed_fields` would name it). See `TaskUpdate.autosave`.
    autosave = bool(fields.pop("autosave", False))
    # `playbook_id` is not a column either (`tasks.playbook` holds a snapshot): taken out for `_apply_playbook`.
    playbook_submitted = "playbook_id" in fields
    playbook_id = fields.pop("playbook_id", None)
    # A lane run is the control plane's row: whoever may Stop or Start it (any task
    # editor on its project, `authz._lane_run_clause`) may not rewrite what it is.
    if merge_lane_state.is_lane_persona(task.persona):
        frozen = sorted((fields.keys() | ({"playbook_id"} if playbook_submitted else set())) & _LANE_RUN_FROZEN)
        if frozen:
            raise HTTPException(
                status_code=409,
                detail=f"Task {task.id} is a merge lane run filed by Jaah: {', '.join(frozen)} cannot change.",
            )
    # Saving a chat demands `worktrees:edit` on its project, the create gate's second key (rationale at the
    # create site). Judged on the resulting row: converting into a chat is gated, converting away is not
    # (that would trap the row), and a title-only chat save is still gated. First, before any field is
    # read (authorization before validation); 403 with the create site's detail shape. Still the project:
    # a console is delivery.
    if (fields.get("persona") or task.persona) == PERSONA_ADVISOR and not has_project_key(
        principal,
        permission_key("worktrees", "edit"),
        fields.get("project_id") or task.project_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "saving a chat requires worktrees:edit on this project — the advisor's deliverable is "
                "a two-way console session, and without that permission the console is read-only"
            ),
        )
    # The cap applies only where this save mints a chat somewhere new (H-1): a persona or project change.
    # An unscoped cap would refuse a title fix on a chat already inside its allowance.
    resulting_persona = fields.get("persona") or task.persona
    resulting_project_id = fields.get("project_id") or task.project_id
    if resulting_persona != task.persona or resulting_project_id != task.project_id:
        _advisor_chat_cap_or_409(
            session,
            project_id=resulting_project_id,
            # The row's own creator, not the editor, so a conversion cannot launder a chat onto another operator.
            creator_id=task.created_by_id,
            persona=resulting_persona,
            # Excluded when the row is already a chat, so a move is judged on its destination rather than counting
            # itself. A conversion excludes nothing (the row is not a chat yet).
            exclude_task_id=task.id if resulting_persona == task.persona else None,
        )
    # ── The editing burst closes first, before any snapshot ──────────────────────────────────────────
    # If a Save also changes the body, its own `manual` snapshot would become the newest row, and a later
    # close would miss the burst's row, leaving it tagged `autosave` for ever. Closed first, the rows appear
    # in the order they happened. A no-op for an auto-save.
    if not autosave:
        _close_autosave_body_version(task.id, session)
    # Snapshot the significant fields before mutating: staleness fires on a value change, not presence (the
    # form resends title/body every save).
    old_significant_values = {f: getattr(task, f) for f in _SIGNIFICANT_EDIT_FIELDS}
    # Snapshot every incoming field's current value so the "what changed" row names every altered field.
    old_field_values = {f: getattr(task, _FIELD_COLUMN.get(f, f)) for f in fields}
    # The addressee before this request, snapshotted unconditionally: the notice also fires at finalize,
    # on saves that may not submit the field.
    old_assignee = task.assigned_to_id
    # Epic children re-stamped with the epic's new title, for the slot relabel after the commit.
    rederived_children: list[int] = []
    if "title" in fields:
        if not (fields["title"] or "").strip():
            raise HTTPException(status_code=422, detail="title cannot be empty")
        old_title = task.title
        task.title = fields["title"]
        if task.persona == PERSONA_EPIC and task.title != old_title:
            # The children wear `[<persona>] <epic title>` (`derived_child_title`); a hand rename re-stamps them
            # as the LLM refine does (`app/task_title.py`), or a child's own page keeps the old epic title.
            rederived_children = _rederive_children(session, task, old_title, task.title)
    if "body" in fields:
        if not (fields["body"] or "").strip():
            raise HTTPException(status_code=422, detail="body cannot be empty")
        # One row per editing burst, not per tick (see `_open_autosave_body_version_id`); an explicit save
        # snapshots the body it replaced. Nothing while the row is `unsaved`: an unacknowledged task has no
        # previous description — the finalizing Save included, whose replaced body is only the draft's
        # half-typed auto-save.
        if fields["body"] != task.body and not task.unsaved:
            if not autosave:
                _snapshot_body_version(task, "manual", session)
            elif _open_autosave_body_version_id(task.id, session) is None:
                _snapshot_body_version(task, _BODY_VERSION_SOURCE_AUTOSAVE, session)
        task.body = fields["body"]
    if "is_plannable" in fields:
        if fields["is_plannable"] is None:
            raise HTTPException(status_code=422, detail="is_plannable cannot be null")
        task.is_plannable = fields["is_plannable"]
    if "autonomous" in fields:
        if fields["autonomous"] is None:
            raise HTTPException(status_code=422, detail="autonomous cannot be null")
        task.autonomous = fields["autonomous"]
        sync_epic_child_autonomous(session, task)
    elif resulting_persona != task.persona and resulting_persona in PERSONA_CREATE_AUTONOMOUS:
        # The server decides a persona's birth `autonomous`: converting a `coder` into a chat
        # lands the chat's own value, as a create would. `elif`, so a submitted value wins; personas declaring
        # nothing are left as the operator left them.
        task.autonomous = create_autonomous_for_persona(resulting_persona, None)
    if "sweep_opt_out" in fields:
        if fields["sweep_opt_out"] is None:
            raise HTTPException(status_code=422, detail="sweep_opt_out cannot be null")
        task.sweep_opt_out = fields["sweep_opt_out"]
    # The model pin: superadmin-only, refused here because a hidden UI field is not an access control. No
    # feature key reaches it (`authz.Access.SUPERADMIN`), and authorization precedes validation (403 before
    # `validate_model`'s 422). `principal is None` is allow-all. Presence, not change, is gated; the form
    # submits the key only where the control renders.
    if "model" in fields:
        # Both properties are the helper's, shared with the create door; `or None` stores INHERIT as NULL.
        task.model = _model_pin_or_refuse(fields["model"], principal)
    # The one agent picker: either half, never both (DD12), under the route's own `tasks:edit` (DD13). It
    # rewrites the choice in any state: a running runner keeps its engine, and the next dispatch honours the new
    # one. Setting one half clears the other, so a built-in binding never outlives an agent pick.
    if fields.get("agent_id") is not None and fields.get("bound_provider"):
        raise HTTPException(status_code=422, detail=_AGENT_OR_PROVIDER)
    if "bound_provider" in fields:
        pin = _provider_pin_or_refuse(fields["bound_provider"])
        if pin != task.bound_provider:
            # Only a changed pin: the form resends every key, and a kept one must not wedge every save.
            _provider_on_plan_or_422(session, pin, task.org_id)
        task.bound_provider = pin
        if task.bound_provider is not None:
            task.agent_id = None
    if "agent_id" in fields:
        task.agent_id = _agent_pick_or_422(session, fields["agent_id"], task.org_id, current=task.agent_id)
        if task.agent_id is not None:
            task.bound_provider = None
    # The account pin: an account runs one vendor, so it stamps that provider; an agent runs on no account. A pin
    # whose vendor the save moved off (an agent, another provider) is dropped rather than left contradicting it.
    if fields.get("agent_id") is not None and fields.get("account_id") is not None:
        raise HTTPException(status_code=422, detail=_AGENT_OR_PROVIDER)
    if "account_id" in fields:
        account = _account_pick_or_422(session, fields["account_id"], principal, task.org_id,
                                       fields.get("bound_provider") or None)
        task.bound_account_id = account.id if account is not None else None
        if account is not None:
            if account.provider != task.bound_provider:
                _provider_on_plan_or_422(session, account.provider, task.org_id)
            task.bound_provider, task.agent_id = account.provider, None
    elif task.bound_account_id is not None and (task.agent_id is not None or "bound_provider" in fields):
        pinned = session.get(Account, task.bound_account_id)
        if task.agent_id is not None or pinned is None or pinned.provider != task.bound_provider:
            task.bound_account_id = None
    # The external-issue pair; an explicit null clears its half. Judged on the resulting row, so a
    # one-sided edit can never leave it half-linked; both are written (the other half re-normalised).
    if "external_task_system" in fields or "external_task_id" in fields:
        task.external_task_system, task.external_task_id = _external_issue_or_422(
            fields.get("external_task_system", task.external_task_system),
            fields.get("external_task_id", task.external_task_id),
        )
    # Editable any time; null is Default. Not significant: it is read at the next dispatch, nothing goes stale.
    if "project_memory" in fields:
        task.project_memory = fields["project_memory"]
    # Editable any time, validated against `TRANSITION_MODES`. Not significant: it only sequences future
    # children, so nothing goes stale.
    if "transition_mode" in fields:
        if fields["transition_mode"] not in TRANSITION_MODES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"transition_mode must be one of {', '.join(TRANSITION_MODES)} "
                    f"(got {fields['transition_mode']!r})"
                ),
            )
        task.transition_mode = fields["transition_mode"]
    # Applied before the persona conversion: `_create_analysis_children` stamps children from the epic's
    # project, so moving first gives them the new one. A filing names its feature's project (a `project_id`
    # beside it must agree); without one, a submitted move runs alone.
    # A move abandons its paused rows' worktrees; those teardown batches are pushed after the commit.
    move_issued: list[fleet_verdicts.IssuedIntents] = []
    # Presence of a pin is gated first, so the refusal outranks the move's validation.
    if "pinned_node_id" in fields:
        _may_pin_node_or_403(principal, task.org_id)
    # A move checks the pin this same save lands, so "move it and set Node to Any" is one save.
    landing_pin = fields["pinned_node_id"] if "pinned_node_id" in fields else _KEEP_PIN
    if "feature_id" in fields:
        move_issued = _apply_feature_filing(
            task, fields["feature_id"], session, principal, project_id=fields.get("project_id"),
            landing_pin=landing_pin,
        )
    elif "project_id" in fields:
        move_issued = _apply_project_move(task, fields["project_id"], session, principal, landing_pin=landing_pin)
    # The node pin, after any move (the pin must name a node of the landing project). Presence is
    # gated (`_pinned_node_or_refuse`); only a changed value is validated, so the operator form's resend of a kept
    # pin cannot wedge a save, a node task's automation-set pin included.
    if "pinned_node_id" in fields:
        if fields["pinned_node_id"] != task.pinned_node_id:
            task.pinned_node_id = _pinned_node_or_refuse(
                session, fields["pinned_node_id"], principal, session.get(Project, task.project_id)
            )
    # The addressee, editable in every state, `done` included; no state gate may be added. After the move
    # (the probe checks the landing project) and before the conversion (children inherit it). Null is
    # refused, not cleared. `_user` for the probe's actor, as in `create_task`. Only a changed value is
    # probed: the form resends the assignee every save, and re-probing an unchanged one would freeze the
    # whole row once that assignee lost access.
    if "assigned_to_id" in fields:
        if fields["assigned_to_id"] is None:
            raise HTTPException(status_code=422, detail="assigned_to_id cannot be null")
        if fields["assigned_to_id"] != task.assigned_to_id:
            task.assigned_to_id = _validated_assignee(
                session, fields["assigned_to_id"], project_id=task.project_id, actor=_user
            )
            # Children follow the root, one hop: fan-out copies the epic's assignee and
            # `task_ancestry.assignee_user_id` stops at a child's own stamp, so otherwise children keep notifying the
            # previous assignee. Only children still carrying the old stamp move. ORM writes so `updated_by` is
            # stamped; no per-child notice.
            if old_assignee is not None:
                for child in session.scalars(
                    select(Task).where(
                        Task.parent_task_id == task.id,
                        Task.archived_at.is_(None),
                        Task.assigned_to_id == old_assignee,
                    )
                ):
                    child.assigned_to_id = task.assigned_to_id
    # Before the conversion, so analysis children a runner→epic conversion builds inherit the new value.
    # Never on an auto-save: hiding changes who can read the row, so it lands only on an explicit Save, which
    # records it in the feed (an auto-save emits nothing). The form resends it on Save.
    if "hidden" in fields and not autosave:
        _apply_hidden(task, fields["hidden"], session, principal)
    # The last field applied, so a rejected conversion leaves nothing half-committed.
    converted_children: Optional[list[Task]] = None
    if playbook_submitted or task.playbook:
        # A playbook decides the persona (`_apply_playbook`); a dropped one hands it back to the conversion.
        if _apply_playbook(task, playbook_submitted, playbook_id, fields.get("persona"), session, principal):
            converted_children = _apply_persona_conversion(task, fields["persona"], session)
    elif "persona" in fields:
        converted_children = _apply_persona_conversion(task, fields["persona"], session)
    # ── The save itself, after every field is applied ────────────────────────────────────────────────
    # An explicit save finalizes an auto-saved draft, last so it builds from the settled persona and body.
    # A no-op otherwise. Read before the call, which clears it; its return value is None for two cases.
    finalizing = not autosave and task.unsaved
    finalized_children = None if autosave else _finalize_unsaved_draft(task, session)
    session.flush()
    # A significant edit (fields dispatch composes from) marks this task and every transitive dependent
    # stale, only on an actual value change.
    significant = {
        f for f in (fields.keys() & _SIGNIFICANT_EDIT_FIELDS)
        if getattr(task, f) != old_significant_values[f]
    }
    if significant:
        reason = f"edited: {', '.join(sorted(significant))}"
        # Propagated on auto-save too: the change happens on the tick, so skipping would lose the mark, not
        # defer it. Only the root's live event is silenced (`notify_root`).
        _propagate_staleness(task.id, reason, session, notify_root=not autosave)
    # Every field that changed, so the feed names them; `changes` carries before/after (older rows render
    # names only).
    changed_fields = sorted(
        f for f in old_field_values if getattr(task, _FIELD_COLUMN.get(f, f)) != old_field_values[f])
    changes = {
        f: {"before": _auditable(old_field_values[f]), "after": _auditable(getattr(task, _FIELD_COLUMN.get(f, f)))}
        for f in changed_fields
    }
    # Recorded as names, not ids: the feed prints before/after verbatim (`fmtChangeValue`). Resolved via
    # `actors.actor_names` like `assigned_to`; unresolved stays null. The row keeps the id.
    if "assigned_to_id" in changes:
        assignee_names = actors.actor_names(session, (old_assignee, task.assigned_to_id))
        changes["assigned_to_id"] = {
            "before": _actor_label(old_assignee, assignee_names),
            "after": _actor_label(task.assigned_to_id, assignee_names),
        }
    # Read here, before the commit expires the instance, for the notice after it.
    new_assignee = task.assigned_to_id
    # ── Who is told ─────────────────────────────────────────────────────────────────────────────────
    # * An auto-save emits nothing, not even the live notify: the console remounts the edit form when
    # `${task.id}:${task.updated_at}` moves (`apps/web/src/pages/taskDetail/TaskDetailTabs.tsx`), so a refetch would
    # destroy the editor mid-typing, and no other reader can see what changed.
    # * A save that finalized a draft already emitted `created`; an `updated` would report the act twice.
    # * Every other save is unchanged.
    if not finalizing and not autosave:
        emit_console_event(
            session, entity="task", id=task.id, action="updated",
            transition_extra={
                "changed_fields": changed_fields, "changes": changes
            } if changed_fields else None,
        )
    session.commit()
    send_minted_intents(move_issued)  # AFTER the commit — see `send_minted_intents`.
    # ── The slot and its tmux window follow the title (I1) ────────────────────────────────────────────
    # Labels are composed at dispatch, so renames (including the async `app/task_title.py` title) go stale;
    # both title writers call one helper. On an actual change, not presence. After the commit, since the
    # helper re-reads the row. No try/except: `retitle_task_slot` never raises, and a label is cosmetic.
    if "title" in changed_fields:
        # The route's own `task_id`: the commit expired the instance.
        slot_retitle.retitle_task_slot(session, task_id)
    for child_id in rederived_children:
        slot_retitle.retitle_task_slot(session, child_id)
    # ── The "assigned to you" notice ──────────────────────────────────
    # Not on an auto-save; not to the actor; only when the assignee is new to the row (changed on this save,
    # or this save finalized an `unsaved` draft). No "un-assigned" notice. After the commit (it reads the row);
    # best-effort, so a failed DM never fails a save; imported lazily as at the title queue.
    actor_id = principal.user_id if principal is not None else None
    if (
        not autosave
        and new_assignee is not None
        and new_assignee != actor_id
        and (new_assignee != old_assignee or finalizing)
    ):
        from app import task_notify

        task_notify.announce_assignment(task_id, assignee_id=new_assignee, assigned_by_id=actor_id)
    # The refinement the create withheld, now that the body is finished; after commit, deferred, never
    # raises. The worker will not overwrite a chosen title (`task_title.is_placeholder_title`).
    if finalizing:
        from app.durable.task_title_queue import enqueue_title

        enqueue_title(task_id)
    session.refresh(task)
    # A conversion changed the child set, so project it (`coder → epic` returns three children, `epic →
    # coder` an explicit 0). A finalized epic's fresh children are projected the same way, or the board
    # shows a childless epic until refetch.
    if converted_children is None and finalized_children is not None:
        converted_children = finalized_children
    if converted_children is not None:
        return _task_out(
            task,
            children=converted_children or None,
            child_count=len(converted_children),
            # `epic → coder` arrives with an empty list, so `_task_out` cannot derive the 0; ignored once
            # `children` is non-None.
            done_child_count=0,
            session=session,
            principal=principal,
        )
    return _task_out(task, session=session, principal=principal)


# ── Tag assignment ─────────────────────────────────────────────────────────────────────────────────
class TaskTagsIn(BaseModel):
    """The task's COMPLETE intended selection across every ACTIVE tag group.

    A whole-set write (as `app/routes/groups.py`): an idempotent full replace cannot leave a half-applied
    checkbox matrix. A group the payload omits ends up empty (`task_tags.replace_task_tags`). Defaults to
    empty: `{}` is how "clear every tag" is spelled."""

    # Bounded: `Tag.id.in_(...)` binds one parameter per element and Postgres caps a statement at 65535, so
    # an unbounded list would 500 instead of 422.
    tag_ids: list[UUID] = Field(default_factory=list, max_length=512)


class TaskTagsOut(BaseModel):
    """Just the resulting tags — NOT a `TaskOut`.

    The write touches no `tasks` column; a `TaskOut` would invite overwriting a cached task with an
    unchanged `updated_at`."""

    tags: list[TagRefOut]


@router.put("/{task_id}/tags", response_model=TaskTagsOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
def replace_tags(
    task_id: int,
    payload: TaskTagsIn,
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TaskTagsOut:
    """Replace this Task's tags across every active tag group. `app/task_tags.py` is the single home of
    the replace rules; this handler is the transport and the event.

    One transaction for validation, delete, insert and notify, so a 422 writes nothing and no subscriber
    hears of a rolled-back write. `record_transition=False` is mandatory: a same-state `task:tagged`
    row would read as a lifecycle transition in `get_task_activity`; live SSE still refetches.

    The row lock makes last-write-wins true: under READ COMMITTED concurrent PUTs would otherwise commit
    the union, possibly two tags from a single-select group (as `app/routes/groups.py::_require_group`).
    No state guard; object denial is 404 (`app/authz.py`)."""
    task = get_or_404(session, Task, task_id, detail=f"task {task_id} not found", lock=True)
    tags = replace_task_tags(session, task, payload.tag_ids)
    emit_console_event(
        session, entity="task", id=task_id, action="tagged", record_transition=False
    )
    session.commit()
    return TaskTagsOut(tags=tags)


# ── Action route: Re-tag (LLM classification) ──────────────────────────────────────────────────────
#: Classifier statuses whose detail describes the operator's deployment (key, model, timeout, reply
#: budget) rather than the caller's catalogue — see `_retag_refusal_for`.
_DEPLOYMENT_SHAPED_CLASSIFIER_REFUSALS = frozenset({502, 503, 504})


def _retag_refusal_for(exc: HTTPException, reader_org: Optional[UUID]) -> HTTPException:
    """The classifier's refusal, in the words of whoever pressed Re-tag.

    `app/task_classifier.py` words its 502/503/504 for the operator's deployment (missing key, model,
    timeout, budget), which `app/tenancy/copy.py` says a tenant is never told. A tenant reads that the
    control is unavailable to their organization and who can act (the sentence `routes/sessions.py`
    uses), status kept. The 409s describe the tenant's own catalogue and pass unchanged. Decided by org
    (`tenancy/ids.py::is_tenant_org`), never the flag."""
    if exc.status_code in _DEPLOYMENT_SHAPED_CLASSIFIER_REFUSALS and is_tenant_org(reader_org):
        return HTTPException(status_code=exc.status_code, detail=unavailable_now("Re-tag"))
    return exc


@router.post("/{task_id}/retag", response_model=TaskTagsOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
def retag(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TaskTagsOut:
    """Classify this Task with the LLM and REPLACE its tags across every active group.

    The handler is the transport only: orchestration is `app/task_retag.py::retag_task`, shared with the
    on-`done` sweep; `app/task_classifier.py` owns the prompt, parsing, verdicts and statuses; and
    `app/task_tags.py::replace_task_tags` is the single writer.

    Every failure writes nothing (raised before the writer; the session never commits).
    `record_transition=False`, 404 object denial and no state guard, as in the PUT route."""
    task = get_task_or_404(task_id, session)
    # Read before `retag_task`'s rollback expires `_user`; a later read would lazy-refresh (or raise
    # `ObjectDeletedError`). `org_id` likewise, for the refusal.
    actor = str(_user.id)
    reader_org = _user.org_id
    try:
        tags = retag_task(session, task, actor=actor)
    except HTTPException as exc:
        refusal = _retag_refusal_for(exc, reader_org)
        if refusal is exc:
            raise
        raise refusal from exc
    session.commit()
    return TaskTagsOut(tags=tags)
