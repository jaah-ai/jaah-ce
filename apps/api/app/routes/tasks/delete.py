"""Task deletion: `DELETE /api/tasks/{task_id}` and the cascade it runs."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import edition_features, fleet_verdicts
from app.auth import require_user
from app.authz.declarations import feature
from app.db import get_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.task_extras import TaskAttachment
from app.models.tasks import Task, TaskDependency
from app.task_lifecycle import DRAFT, SYSTEM_FAILED, _LIVE_STATES
from app.task_lookup import get_task_or_404
from app.routes.tasks.slots import _release_task_slots, kill_runner_keeping_hold, send_minted_intents
from app.routes.tasks.dependencies import _gate_would_deadlock, _would_create_cycle
from app.routes.tasks.epics import _reevaluate_epic_child_failure
from app.routes.tasks.epic_finish import _drop_epic_report_published_by

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _rows_the_cascade_will_remove(root_id: int, session: Session) -> list[Task]:
    """Every DESCENDANT of `root_id`, transitively, ARCHIVED ONES INCLUDED — the set `session.delete(task)`
    is about to take with it through `Task.parent_task_id`'s `ON DELETE CASCADE`.

    Not `_epic_children`, which excludes archived rows: the database cascade does not, and a slot bound
    to a silently deleted row can never be freed. Transitive because a plan's coders are grandchildren
    that hold slots. Ascending by id, so the walk is deterministic and a cycle cannot revisit a row."""
    seen: set[int] = set()
    frontier = [root_id]
    found: list[Task] = []
    while frontier:
        rows = list(session.scalars(
            select(Task).where(Task.parent_task_id.in_(frontier)).order_by(Task.id)
        ).all())
        frontier = []
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            found.append(row)
            frontier.append(row.id)
    return found


def _stored_object_keys_for(session: Session, task_ids: Sequence[int]) -> list[tuple[Optional[str], str]]:
    """Every DISTINCT `(s3_bucket, s3_key)` held by the given tasks' attachment rows.

    Read before the delete: `task_attachments.task_id` cascades, and without `s3:ListBucket` an object whose row is gone
    can never be enumerated or reclaimed. See `app/models/task_extras.py::TaskAttachment`.

    Takes the ids from `_rows_the_cascade_will_remove` (unbounded, cycle-safe) rather than walking again.
    Distinct because an epic's final report shares its child's key; the consuming reap decides whether
    an object may go."""
    refs = session.execute(
        select(TaskAttachment.s3_bucket, TaskAttachment.s3_key)
        .where(TaskAttachment.task_id.in_(list(task_ids)), TaskAttachment.s3_key.is_not(None))
        .distinct()
    ).all()
    return [(bucket, key) for bucket, key in refs if key]


def _bridge_dependencies_around(doomed_ids: set[int], session: Session) -> None:
    """Keep the dependency chain unbroken across a delete: every surviving task that depended on a doomed
    one is re-pointed at the doomed task's own surviving prerequisites (A→X→P with X deleted becomes A→P),
    so deleting a stage never silently frees what waited on it (owner decision).

    Must run BEFORE the delete — both FKs cascade, so afterwards the edges are gone. Walks through doomed
    rows, bounded by `seen`: a doomed row's prerequisites include its doomed parent's (a deleted plan's
    coder waited on whatever the plan waited on). An edge the add route would refuse (cycle, gate deadlock)
    is skipped rather than created."""
    edges = session.execute(
        select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(
            or_(
                TaskDependency.task_id.in_(doomed_ids),
                TaskDependency.depends_on_task_id.in_(doomed_ids),
            )
        )
    ).all()
    parents = dict(session.execute(
        select(Task.id, Task.parent_task_id).where(Task.id.in_(doomed_ids))
    ).all())
    prerequisites: dict[int, set[int]] = {}
    outside_dependents: list[tuple[int, int]] = []
    for dependent_id, prerequisite_id in edges:
        if dependent_id in doomed_ids:
            prerequisites.setdefault(dependent_id, set()).add(prerequisite_id)
        elif prerequisite_id in doomed_ids:
            outside_dependents.append((dependent_id, prerequisite_id))

    def surviving_prerequisites(doomed_id: int) -> set[int]:
        found: set[int] = set()
        stack, seen = [doomed_id], set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if parents.get(current) in doomed_ids:
                stack.append(parents[current])
            for prerequisite_id in prerequisites.get(current, ()):
                if prerequisite_id in doomed_ids:
                    stack.append(prerequisite_id)
                else:
                    found.add(prerequisite_id)
        return found

    # Edges the dependents already hold, plus each one added below: a bridge never duplicates an edge.
    held: set[tuple[int, int]] = {
        (dependent_id, prerequisite_id) for dependent_id, prerequisite_id in session.execute(
            select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(
                TaskDependency.task_id.in_({dependent_id for dependent_id, _ in outside_dependents})
            )
        ).all()
    }
    for dependent_id, doomed_id in sorted(outside_dependents):
        for prerequisite_id in sorted(surviving_prerequisites(doomed_id)):
            pair = (dependent_id, prerequisite_id)
            if dependent_id == prerequisite_id or pair in held:
                continue
            if _would_create_cycle(*pair, session) or _gate_would_deadlock(*pair, session):
                continue
            session.add(TaskDependency(task_id=dependent_id, depends_on_task_id=prerequisite_id))
            session.flush()
            held.add(pair)
            emit_console_event(session, entity="task", id=dependent_id, action="dependency_added")


def delete_task_core(
    task: Task, session: Session, *, allow_live: bool = False, actor: Optional[AdminUser] = None
) -> None:
    """Delete/archive a Task. Deleting a parent cascades via the `ON DELETE CASCADE` self-FK, and every
    dependent outside the deleted set inherits the deleted rows' prerequisites
    (`_bridge_dependencies_around`).

    A LIVE row (`_LIVE_STATES`) is refused with a 409 naming the state unless `allow_live` — the operator
    route passes it (any task is deletable); system callers (GC, an agent's filed draft) keep the
    guard. A row holding a pool slot gets a `kill_runner` intent before the delete;
    `worktrees.task_id` has no FK, so the binding survives for the node to verify."""
    task_id = task.id
    # Permissive on unknowns: `_LIVE_STATES` lists states to refuse, so anything unrecognised is deletable.
    # Every state that can carry a live runner is in the set.
    if task.state in _LIVE_STATES and not allow_live:
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is '{task.state}' — cannot delete a live (in-flight) task",
        )
    issued: list[fleet_verdicts.IssuedIntents] = []
    if task.state != DRAFT:
        # `kill_runner_keeping_hold` mints off the shared `_slots_bound_to_task` addressing read, same as
        # every other kill producer.
        _, issued = kill_runner_keeping_hold(
            task_id, session,
            reason=f"task {task_id} was deleted (was {task.state}) — stop that runner",
        )
        # And the hold is freed (the "task deleted" release edge): `worktrees.task_id` has no FK, and nothing
        # downstream would ever clear a hold naming a deleted task. Same transaction, fenced on the generation
        # read.
        _release_task_slots(
            task_id, session,
            reason=f"released: task {task_id} was deleted (was {task.state})",
        )
    # And the same for every row the cascade removes: an epic holds no slot itself, but its children may
    # (e.g. a Stop leaves them `paused`, which is not in `_LIVE_STATES`, still holding slots). Nothing would
    # recover those slots afterwards (no clock reaper), so this prevents the leak. Not a new
    # release edge: deleting the parent is the delete decision for each removed row, as the cascade defines.
    cascade_rows = _rows_the_cascade_will_remove(task_id, session)
    for doomed in cascade_rows:
        _, child_issued = kill_runner_keeping_hold(
            doomed.id, session,
            reason=(f"task {doomed.id} was deleted with its parent {task_id} "
                    f"(was {doomed.state}) — stop that runner"),
        )
        issued.extend(child_issued)
        _release_task_slots(
            doomed.id, session,
            reason=f"released: task {doomed.id} was deleted with its parent {task_id}",
        )
    # Before the delete: `task_attachments.source_task_id` is `SET NULL`, so afterwards the published epic
    # report would be unreachable by every control (an epic cannot be Reset or Retried).
    _drop_epic_report_published_by(task_id, session)
    # Every MicroVM those rows own, disk included (A7: only `system_failed` keeps one). The cascade takes the
    # `microvms` rows; the `destroy` orders outlive them (`microvm_intents.vm_id` is no foreign key).
    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs.
        from app import microvm_lifecycle

        for doomed_id in (task_id, *(row.id for row in cascade_rows)):
            issued.extend(microvm_lifecycle.destroy_for_task(
                session, doomed_id, retain=False, reason=f"task {task_id} was deleted",
            ))
    # Read before the delete. Deleting a spent child stops it holding its epic failed (owner decision);
    # applied after the delete so `_epic_has_failed_child` no longer sees it.
    recovered_a_failed_child = task.state == SYSTEM_FAILED
    recovered_childs_parent_id = task.parent_task_id
    # Lock the epic before the delete (epic-then-child order), only when this delete recovers a failure.
    if recovered_a_failed_child and recovered_childs_parent_id is not None:
        # `select(...).with_for_update()`, not `session.get(..., with_for_update=True)`: a row lock without the
        # row, and `session.get` would be a registered tenancy-seam bypass.
        session.execute(
            select(Task.id).where(Task.id == recovered_childs_parent_id).with_for_update()
        )
    # Read while the rows exist; afterwards the objects are unreachable for ever (`_stored_object_keys_for`).
    # Over the cascade set already in hand, so object and runner sweeps agree.
    doomed_object_keys = _stored_object_keys_for(
        session, [task_id, *(row.id for row in cascade_rows)]
    )
    # A lane run's `testing` rows back to the queue while their `lane_task_id` still names it: the FK nulls it
    # on delete, and a `testing` row naming no run is one no sweep would requeue.
    if edition_features.enabled("merge_lane"):
        from app import merge_lane

        merge_lane.release_lane_rows(session, task_id)
    # The signed compliance manifests go with the task (operator decision I6): their erase is enqueued in THIS
    # transaction, so it runs iff the delete commits, and writes the security event. `actor` is who deleted. An
    # edition without compliance signs none.
    if edition_features.enabled("compliance"):
        from app.durable import compliance_seal

        compliance_seal.erase_for_tasks(
            session, [task_id, *(row.id for row in cascade_rows)], actor, org_id=task.org_id,
        )
    # The task's org, read while visible: the `deleted` transition row is recorded after the delete. See
    # `record_task_transition`.
    task_org_id = task.org_id
    _bridge_dependencies_around({task_id, *(row.id for row in cascade_rows)}, session)
    session.delete(task)
    if recovered_a_failed_child:
        session.flush()
        _reevaluate_epic_child_failure(recovered_childs_parent_id, session)
    emit_console_event(
        session, entity="task", id=task_id, action="deleted", transition_org_id=task_org_id,
    )
    session.commit()
    send_minted_intents(issued)  # AFTER the commit — see `send_minted_intents`.
    # Objects last, after the intents: this dials S3, and a slow bucket must not delay the `kill_runner`.
    # Each key goes through the single-attachment delete's guard, which spares a key still referenced.
    # Best effort: the row is gone, so the worst case is a logged orphan. Local import: `app/routes/
    # attachments.py` imports `PERSONA_EPIC` from here.
    from app.attachment_storage import reap_unreferenced_object

    for bucket, key in doomed_object_keys:
        reap_unreferenced_object(session, key, bucket)


@router.delete("/{task_id}", status_code=204)
@feature("tasks", "delete", object_type="task", object_params=("task_id",), author_draft=True)
def delete_task(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """See `delete_task_core` for the kill-intent/cascade/dependency contract this route enforces. Any
    state is deletable here, a live one included (its runner is killed)."""
    task = get_task_or_404(task_id, session)
    delete_task_core(task, session, allow_live=True, actor=_user)
    return Response(status_code=204)
