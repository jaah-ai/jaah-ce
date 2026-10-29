"""Epic fan-out and rollup: appending children, failure re-evaluation, completion and the stuck scan."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased

from app import actors, merge_lane_state
from app.events import emit_console_event
from app.models.audit import ActivityEvent
from app.models.projects import Project
from app.models.tasks import Task, TaskDependency
from app.short_title import short_title
from app.task_ancestry import executor_user_id
from app import playbook_engine
from app.task_lifecycle import (
    ACTIVE,
    DONE,
    DRAFT,
    PAUSED,
    SYSTEM_FAILED,
    _APPEND_TASKS_PLAN_STATES,
    _EPIC_FAILING_CHILD_STATES,
    _EPIC_SETTLED_STATES,
)
from app import task_source
from app.task_settle import NO_BUILD_OUTCOME_REASON
from app.task_title import derived_child_title
from app.user_activity import record_task_note
from app.routes.tasks import dispatch, epic_finish
from app.routes.tasks.policy import (
    EPIC_BUILD_PERSONAS,
    PERSONA_EPIC,
    PERSONA_FINISH,
    PERSONA_PLAN,
    _DISPATCH_COMPOSED_BODY,
    epic_fanout_cap,
    project_default_persona,
)
from app.routes.tasks.dependencies import _would_create_cycle
from app.routes.tasks.create import _new_stage_child, _playbooks_on_or_422, _wire_stage_edges

log = logging.getLogger(__name__)


def _epic_has_failed_child() -> Any:
    """Correlated EXISTS keyed on the ENCLOSING row being the epic (unlike its neighbours): does this epic
    still have a LIVE child sitting `system_failed`?

    The single home of "is this epic still failed by a child", read by the child-Start reactivation and
    `_reevaluate_epic_child_failure`. A predicate, not a pre-read, so each stays one atomic statement (a
    sibling could fail in between). `archived_at IS NULL`: an archived child never lives again, so its
    frozen failure must not hold the epic. Fresh alias per call."""
    child = aliased(Task)
    return (
        select(child.id)
        .where(
            child.parent_task_id == Task.id,
            child.state == SYSTEM_FAILED,
            child.archived_at.is_(None),
        )
        .exists()
    )


def archive_epic_children_for_plan(task: Task, session: Session) -> list[int]:
    """Archive an epic's coder/finish children because its `plan` child is STARTING.

    A precondition of running the planner, not a side effect of an append it may never reach: stale
    children otherwise satisfy the settle guard's coder count. An `active` child is left alone (a runner
    holds it); others are stamped `archived_at`, never deleted. Returns the archived ids."""
    if task.persona != PERSONA_PLAN or task.parent_task_id is None:
        return []
    # `with_for_update`: take the epic first. Epic-then-child is the lock order every writer here keeps;
    # reversed, this deadlocks with `append_tasks`.
    parent = session.get(Task, task.parent_task_id, with_for_update=True)
    if parent is None or parent.persona != PERSONA_EPIC:
        return []
    if parent.playbook:
        # A playbook container: only what THIS Plan stage filed is stale — every other stage row (a start
        # stage that already ran, another Plan's filings) is someone else's work.
        plan_key = (task.playbook_stage or {}).get("key")
        live = [
            row for row in session.scalars(select(Task).where(
                Task.parent_task_id == parent.id, Task.archived_at.is_(None), Task.state != ACTIVE,
            )).all()
            if playbook_engine.is_filed_by_plan(row.playbook_stage, plan_key)
        ]
    else:
        live = session.scalars(
            select(Task).where(
                Task.parent_task_id == parent.id,
                Task.persona.in_(EPIC_BUILD_PERSONAS + (PERSONA_FINISH,)),
                Task.archived_at.is_(None),
                Task.state != ACTIVE,
            )
        ).all()
    ids = [c.id for c in live]
    if ids:
        session.execute(
            update(Task).where(Task.id.in_(ids))
            .values(archived_at=datetime.now(timezone.utc), **actors.task_actor_values(session))
        )
        log.info("plan %s starting: archived %s stale epic child(ren) %s", task.id, len(ids), ids)
        # An archived child is not live, so a stale `system_failed` one must stop holding its epic failed; only
        # when one was failed, as `_reevaluate_epic_child_failure` requires.
        if any(c.state == SYSTEM_FAILED for c in live):
            _reevaluate_epic_child_failure(parent.id, session)
    return ids


# ── Epic-child ready (the fan-out's draft → ready edge) ─────────────────────────────────────────────
def ready_pipeline_child_issueless(child_id: int, session: Session) -> bool:
    """Flip an epic child `draft → ready`: a fanned-out child dispatches keyed purely on `task.id`. ONE
    atomic guarded UPDATE acting only on a CHILD still in `draft`, so a re-call returns False. Reaches
    `ready` through `_start` with `run_requested` unset, because the `task_dependencies` edges are the
    ONLY thing sequencing an epic's children.

    Guarded on the parent still `active`: the fan-out reads its worklist in one session and readies each
    child in another, so an epic Reset (or Stop) landing between them must not be undone by `_start`'s
    parent reactivation."""
    parent = aliased(Task)
    return dispatch._start(
        child_id, session,
        sources=(DRAFT,),
        run_requested=False,
        guards=(
            Task.parent_task_id.is_not(None),
            Task.parent_task_id.in_(select(parent.id).where(parent.state == ACTIVE)),
        ),
    )


def record_fanout_attempt_failed(child_id: int, session: Session) -> Optional[dict]:
    """Bound the epic fan-out's `draft → ready` retry, escalating the parent EPIC at the ceiling.

    Two independently guarded writes: increment `fanout_attempts` (UPDATE ... RETURNING; None is a 404),
    and only at `epic_fanout_cap()` flip the parent epic, never the child, to `system_failed`. Either way
    the epic leaves the fan-out worklist. No siblings are cancelled; this epic has no failed child, so
    `_reevaluate_epic_child_failure` will not reopen it. `just_failed` is True only on rowcount > 0."""
    result = session.execute(
        update(Task)
        .where(Task.id == child_id)
        .values(fanout_attempts=Task.fanout_attempts + 1, **actors.task_actor_values(session))
        .returning(Task.fanout_attempts, Task.parent_task_id, Task.org_id)
    )
    row = result.first()
    if row is None:
        session.commit()
        return None
    # A child shares its epic's org (composite key), so the child's org is the epic's.
    attempts, epic_id, org = row
    ceiling_reached = attempts >= epic_fanout_cap(org=org)
    just_failed = False
    if ceiling_reached and epic_id is not None:
        failed_result = session.execute(
            update(Task)
            .where(Task.id == epic_id, Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
            .values(state=SYSTEM_FAILED, **actors.task_actor_values(session))
        )
        if failed_result.rowcount > 0:
            just_failed = True
            emit_console_event(session, entity="task", id=epic_id, action=SYSTEM_FAILED)
    session.commit()
    return {
        "attempts": attempts,
        "ceiling_reached": ceiling_reached,
        "epic_id": epic_id,
        "just_failed": just_failed,
    }


def _epic_filed_count(epic_id: int, session: Session, plan: Optional[Task] = None) -> int:
    """`_epic_coder_count`, for either engine. A playbook container counts the stage rows the Plan stage
    `plan` filed (any persona — a per-item stage need not be a `coder`), so one Plan's filings never
    satisfy another's guard; every other epic counts its coders."""
    epic = session.get(Task, epic_id)
    if epic is None or not epic.playbook:
        return _epic_coder_count(epic_id, session)
    plan_key = (plan.playbook_stage or {}).get("key") if plan is not None else None
    stages = session.scalars(
        select(Task.playbook_stage).where(
            Task.parent_task_id == epic_id, Task.archived_at.is_(None), Task.finish_follow_up_key.is_(None),
        )
    ).all()
    return sum(1 for stage in stages if playbook_engine.is_filed_by_plan(stage, plan_key))


def _epic_coder_count(epic_id: int, session: Session) -> int:
    """Number of LIVE coder children an epic currently has that a DECOMPOSITION produced — the
    `_roll_up_epic` append-pending guard, 0 while a `done` plan child's breakdown has not been appended
    yet. An archived child never counts, so a re-plan in flight reads as zero rather than inheriting
    the stale set's count."""
    return session.scalar(
        select(func.count()).select_from(Task).where(
            Task.parent_task_id == epic_id, Task.persona.in_(EPIC_BUILD_PERSONAS),
            Task.archived_at.is_(None),
            # A Finish-filed follow-up is not a decomposition product; it survives the plan archive when `active`,
            # so counting it would let a plan re-run that died before appending settle `done` undecomposed. Readers:
            # `settle_analysis_child_on_artifact`, `_epic_append_pending`, `settle_task`'s plan pre-check.
            Task.finish_follow_up_key.is_(None),
        )
    ) or 0


def _epic_append_pending(epic_id: int, session: Session) -> bool:
    """True iff the epic has a `done` plan child but no coder children yet — the append is
    still outstanding, so the epic must NOT roll up even though every CURRENT child is done. Plan is
    the LAST analysis child to finish, so without this the epic would complete the instant it reached `done`
    and strand the coders under a `done` container. An epic with no plan child is never held, nor one whose
    plan settled that there is nothing to build (`NO_BUILD_OUTCOME_REASON`): that is a settled
    decomposition of zero children, and the epic completes on its analysis children."""
    done_plans = session.scalars(
        select(Task).where(
            Task.parent_task_id == epic_id, Task.persona == PERSONA_PLAN, Task.state == DONE,
            Task.outcome_no_pr_reason.is_distinct_from(NO_BUILD_OUTCOME_REASON),
        )
    ).all()
    if not done_plans:
        return False
    epic = session.get(Task, epic_id)
    if epic is not None and epic.playbook:
        # Per Plan stage: any done Plan whose own stages are not filed yet holds the container.
        return any(_epic_filed_count(epic_id, session, plan) == 0 for plan in done_plans)
    return _epic_coder_count(epic_id, session) == 0


def append_tasks(
    plan_child: Task,
    proposals: list[dict[str, Any]],
    session: Session,
) -> dict[str, Any]:
    """Append coder child Tasks to an epic from its plan child's breakdown — an ATOMIC REPLACE
    of the epic's current coder + finish children, never a skip.

    Skipping on "already has children" would make a planner re-run a silent no-op. Each call archives
    the current set (only `archived_at`) and creates the new one in one transaction. An `active` child is
    left to its runner (a Task is idempotent). Children are inserted before edges, every edge is
    cycle-vetted, and a fresh `persona=finish` child is created every time."""
    epic_id = plan_child.parent_task_id
    if not (
        plan_child.persona == PERSONA_PLAN
        and epic_id is not None
        and plan_child.state in _APPEND_TASKS_PLAN_STATES
    ):
        return {"created": [], "archived": []}
    # A plan that SETTLED nothing-to-build filed no children and its epic completed on that: a late
    # or retried append would strand coders under a `done` container, so it is refused loudly, never a no-op.
    # On a still-`active` plan the stamp is a refused-settle leftover (the payload write precedes the manifest
    # gate) and an append is the decision to build: clear it, so the stale-row settle counts the children.
    if plan_child.outcome_no_pr_reason == NO_BUILD_OUTCOME_REASON:
        if plan_child.state == DONE:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"task {plan_child.id} settled that there is nothing to build, so its epic {epic_id} "
                    f"completed without children — nothing appended; Reset the plan to re-plan"
                ),
            )
        plan_child.outcome_no_pr_reason = None
        session.flush()
    # Row-lock the epic before reading its children, so racing appends serialize (a no-op on SQLite).
    epic = session.get(Task, epic_id, with_for_update=True)
    if epic is None or epic.persona != PERSONA_EPIC:
        return {"created": [], "archived": []}
    # A playbook container files the stages this Plan stage creates, not the coder + finish recipe.
    if epic.playbook:
        return _append_playbook_stages(plan_child, epic, proposals, session)

    current = session.scalars(
        select(Task)
        .where(
            Task.parent_task_id == epic_id,
            Task.persona.in_(EPIC_BUILD_PERSONAS + (PERSONA_FINISH,)),
            Task.archived_at.is_(None),
        )
        .order_by(Task.id)
    ).all()

    # ACTIVE-CHILD RULE (see docstring): a live runner is LEFT ALONE and everything else is archived. Its
    # work landing twice is a non-issue — a Task is idempotent, so a duplicate in the new set converges.
    now = datetime.now(timezone.utc)
    archived_ids = [c.id for c in current if c.state != ACTIVE]
    if archived_ids:
        # ARCHIVED, NOT DESTROYED: only `archived_at` is written, so the row stays queryable evidence of
        # what this decomposition actually produced (outcomes, PRs, timings, transcript pointers).
        session.execute(
            update(Task).where(Task.id.in_(archived_ids))
            .values(archived_at=now, **actors.task_actor_values(session))
        )
        # Same rule as `archive_epic_children_for_plan`'s: an archived child is not a live child, so a
        # `system_failed` one from the superseded decomposition must stop holding its epic failed (owner
        # decision).
        if any(c.state == SYSTEM_FAILED for c in current if c.state != ACTIVE):
            _reevaluate_epic_child_failure(epic_id, session)

    # The original executor (starter, else creator) owns every row this append files. No
    # `started_by_id` stamp: a child pushes as its epic's starter through the walk, so a re-Start of the
    # epic by someone else moves the whole subtree with it.
    executor_id = executor_user_id(session, plan_child.id)
    project = session.get(Project, epic.project_id)
    default_child_persona = project_default_persona(
        project.default_persona if project is not None else None, EPIC_BUILD_PERSONAS,
    )

    # Insert every coder first (flush for ids), THEN wire edges — so an inter-coder `depends_on` can
    # reference a sibling by list index and `_would_create_cycle` sees the full sibling set.
    created: list[Task] = []
    for proposal in proposals:
        # Capped at <=6 content words (`app/short_title.py`) at the writer. Normalize, never reject: this is the
        # runner's single terminal write. WARNING because no INFO handler is configured.
        title = short_title(proposal["title"], prose=False)
        if title != proposal["title"]:
            log.warning(
                "append_tasks: shortened coder title for epic %s (the plan persona "
                "asks for 5-6 words): %r -> %r",
                epic_id, proposal["title"], title,
            )
        row = Task(
            project_id=epic.project_id,
            parent_task_id=epic_id,
            source=task_source.source_for(epic, task_source.AGENT),
            # INHERITED, for `_create_analysis_children`'s reason — an epic kept off the board does not
            # get to fan out onto it.
            hidden=epic.hidden,
            is_plannable=False,
            # Validated against `EPIC_BUILD_PERSONAS` at the route; when the planner names none, the project's
            # default if it is a build persona, else `coder`.
            persona=proposal.get("persona") or default_child_persona,
            state=DRAFT,
            title=title,
            body=proposal["body"],
            priority=epic.priority,
            phase_index=proposal.get("phase_index"),
            phase_name=proposal.get("phase_name"),
            # Owner: the plan child's executor (above). NULL stays NULL.
            created_by_id=executor_id,
            # THE ADDRESSEE — the epic's own column, one hop: a coder child is a stage of the epic's work
            # item and is filed under whoever the epic is.
            assigned_to_id=epic.assigned_to_id,
        )
        session.add(row)
        session.flush()   # assign row.id for the dependency edges + the next sibling's index reference
        created.append(row)
        emit_console_event(session, entity="task", id=row.id, action=DRAFT)

    # The `impl → plan` edge on every child. The plan child is the coders'
    # prerequisite, never a dependent of a just-created sibling, so this cannot cycle; the guard is a
    # defensive assertion reusing the existing invariant, matching `_create_epic_with_children`.
    for row in created:
        if _would_create_cycle(row.id, plan_child.id, session):
            session.rollback()
            raise HTTPException(
                status_code=422,
                detail=(
                    f"coder {row.id} -> plan {plan_child.id} would create a cycle "
                    "(unexpected for a fresh append — aborting)"
                ),
            )
        session.add(TaskDependency(task_id=row.id, depends_on_task_id=plan_child.id))

    # Inter-impl edges from the planner's proposed ordering (a proposal's `depends_on` = 0-based
    # indices into `proposals`). Every edge is cycle-guarded; ANY bad edge aborts the whole append.
    for idx, proposal in enumerate(proposals):
        for dep_idx in proposal.get("depends_on") or []:
            if not isinstance(dep_idx, int) or dep_idx == idx or not (0 <= dep_idx < len(created)):
                session.rollback()
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"proposal {idx} declares an invalid inter-coder dependency index {dep_idx!r} "
                        f"(must be a distinct 0-based index in [0, {len(created)}))"
                    ),
                )
            task_row, dep_row = created[idx], created[dep_idx]
            # A duplicate index within one build is a harmless no-op (idempotent, like add_task_dependency).
            if session.get(
                TaskDependency, {"task_id": task_row.id, "depends_on_task_id": dep_row.id}
            ) is not None:
                continue
            if _would_create_cycle(task_row.id, dep_row.id, session):
                session.rollback()
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"inter-coder dependency {task_row.id} -> {dep_row.id} (proposal {idx} -> "
                        f"{dep_idx}) would create a dependency cycle — aborting append"
                    ),
                )
            session.add(TaskDependency(task_id=task_row.id, depends_on_task_id=dep_row.id))

    # Phase-DAG cross-phase edges: every phase k+1 child depends on every phase k child, reusing the gate.
    # Wired from the sorted set of `phase_index` values present in this append.
    phases: dict[int, list[Task]] = {}
    for row, proposal in zip(created, proposals):
        phase_index = proposal.get("phase_index")
        if phase_index is not None:
            phases.setdefault(phase_index, []).append(row)
    ordered_phase_indices = sorted(phases)
    for prev_phase, next_phase in zip(ordered_phase_indices, ordered_phase_indices[1:]):
        for child_row in phases[next_phase]:
            for prereq_row in phases[prev_phase]:
                # Already wired above as an explicit inter-coder edge — do not double-insert (composite PK).
                if session.get(
                    TaskDependency, {"task_id": child_row.id, "depends_on_task_id": prereq_row.id}
                ) is not None:
                    continue
                if _would_create_cycle(child_row.id, prereq_row.id, session):
                    session.rollback()
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"phase edge {child_row.id} (phase {next_phase}) -> {prereq_row.id} "
                            f"(phase {prev_phase}) would create a dependency cycle — aborting append"
                        ),
                    )
                session.add(
                    TaskDependency(task_id=child_row.id, depends_on_task_id=prereq_row.id)
                )

    # One terminal `persona=finish` child after every coder, on the same edge machinery; no finish→plan edge
    # (transitive). Always created: the prior finish child was archived in this batch.
    finish_child = Task(
        project_id=epic.project_id,
        parent_task_id=epic_id,
        source=task_source.source_for(epic, task_source.AGENT),
        is_plannable=False,
        persona=PERSONA_FINISH,
        state=DRAFT,
        # Derived, and exempt from the coder title cap, which would shred the `[<persona>]` prefix
        # `RAW_PERSONA_PREFIX_RE` (apps/web) parses. Built with `PERSONA_FINISH` because auto-title refinement
        # matches this against `child.persona`.
        title=derived_child_title(PERSONA_FINISH, epic.title),
        body=(
            "Terminal analytical phase: verify every build subtask has completed and resolved, "
            "optionally note UI acceptance evidence, then settle. Dispatched as a read-only runner "
            "role whose settle is server-computed (`task.complete` "
            "declaring a `finish` terminal) — the server derives the full finish report and flips this "
            "child `-> done`; the runner attests nothing. Readied by the fan-out sweep like its "
            "siblings; its dependency edges to every coder keep `dispatchable_tasks` from "
            "dispatching it until those deps are done."
        ),
        priority=epic.priority,
        # The executor's, for the reasons written out above the coders.
        created_by_id=executor_id,
        assigned_to_id=epic.assigned_to_id,
    )
    session.add(finish_child)
    session.flush()   # assign finish_child.id for the dependency edges below
    emit_console_event(session, entity="task", id=finish_child.id, action=DRAFT)
    for row in created:
        if _would_create_cycle(finish_child.id, row.id, session):
            session.rollback()
            raise HTTPException(
                status_code=422,
                detail=(
                    f"finish {finish_child.id} -> coder {row.id} would create a cycle "
                    "(unexpected for a fresh append — aborting)"
                ),
            )
        session.add(TaskDependency(task_id=finish_child.id, depends_on_task_id=row.id))

    # One durable `task_note:plan_fanout` row on the EPIC, not the plan child — this is the
    # epic-level "fan-out happened" moment, keyed like every other task_note so it reads back on the epic's
    # own activity feed. Names the archived predecessors too, so a re-plan shows as a REPLACE, not an add.
    record_task_note(
        session,
        epic_id,
        "plan_fanout",
        # The counts only — the label already says "Plan fan-out".
        f"{len(created)} coder child(ren) added"
        + (f" ({len(archived_ids)} prior child(ren) archived)" if archived_ids else ""),
        extra={
            "created_ids": [c.id for c in created],
            "archived_ids": archived_ids,
            "plan_child_id": plan_child.id,
        },
    )
    session.commit()
    return {"created": [c.id for c in created], "archived": archived_ids}


def _append_playbook_stages(
    plan_child: Task, epic: Task, proposals: list[dict[str, Any]], session: Session,
) -> dict[str, Any]:
    """`append_tasks` for a playbook container: the same atomic replace, filing the stages the Plan stage
    `plan_child` creates (`playbook_engine.stage_indexes_created_by`) instead of coders + a finish.

    A "runs once for each planned item" stage becomes one row per proposal (title, body, `depends_on` and
    phases as `append_tasks` reads them; a proposal's `persona` is ignored — the stage decides); a
    "runs once" stage one row working the container's body. Every row waits on the Plan stage and on the
    rows of each stage it depends on (`playbook_engine.dependency_indexes`)."""
    _playbooks_on_or_422(session, epic.org_id)
    snap = epic.playbook or {}
    plan_key = (plan_child.playbook_stage or {}).get("key")
    live = session.scalars(
        select(Task).where(Task.parent_task_id == epic.id, Task.archived_at.is_(None)).order_by(Task.id)
    ).all()

    def filed_here(row: Task) -> bool:
        stage = row.playbook_stage or {}
        return playbook_engine.is_filed_by_plan(stage) and (stage.get("created") or {}).get("stage") == plan_key

    # The ACTIVE-CHILD RULE, as in `append_tasks`: a live runner is left alone, everything else archived.
    current = [row for row in live if filed_here(row)]
    archived_ids = [row.id for row in current if row.state != ACTIVE]
    if archived_ids:
        session.execute(
            update(Task).where(Task.id.in_(archived_ids))
            .values(archived_at=datetime.now(timezone.utc), **actors.task_actor_values(session))
        )
        if any(row.state == SYSTEM_FAILED for row in current if row.state != ACTIVE):
            _reevaluate_epic_child_failure(epic.id, session)
    # The rows a new stage may wait on: every surviving child, by its stage's position.
    rows_by_index: dict[int, list[Task]] = {}
    for row in live:
        if row.id in archived_ids:
            continue
        index = playbook_engine.index_of(snap, (row.playbook_stage or {}).get("key"))
        if index is not None:
            rows_by_index.setdefault(index, []).append(row)
    executor_id = executor_user_id(session, plan_child.id)

    created: list[Task] = []
    new_indexes = playbook_engine.stage_indexes_created_by(snap, plan_key)
    for index in new_indexes:
        stage = playbook_engine.resolve_stage(snap, index, cap=epic.model)
        if playbook_engine.runs_per_item(stage):
            rows = [
                _new_stage_child(
                    epic, stage,
                    title=short_title(proposal["title"], prose=False),
                    body=proposal["body"],
                    created_by_id=executor_id,
                    phase_index=proposal.get("phase_index"),
                    phase_name=proposal.get("phase_name"),
                )
                for proposal in proposals
            ]
        else:
            rows = [_new_stage_child(
                epic, stage,
                title=derived_child_title(stage["persona"], epic.title),
                body=_DISPATCH_COMPOSED_BODY,
                created_by_id=executor_id,
            )]
        for row in rows:
            session.add(row)
        rows_by_index[index] = rows
        created.extend(rows)
    session.flush()   # every id first, so a filed stage may wait on one filed after it
    for row in created:
        emit_console_event(session, entity="task", id=row.id, action=DRAFT)
    _wire_stage_edges(session, snap, rows_by_index, new_indexes, extra_prereqs=(plan_child,))
    for index in new_indexes:
        if playbook_engine.runs_per_item(snap["stages"][index]):
            _wire_item_edges(session, rows_by_index[index], proposals)

    record_task_note(
        session,
        epic.id,
        "plan_fanout",
        f"{len(created)} stage task(s) added"
        + (f" ({len(archived_ids)} prior task(s) archived)" if archived_ids else ""),
        extra={
            "created_ids": [c.id for c in created],
            "archived_ids": archived_ids,
            "plan_child_id": plan_child.id,
        },
    )
    session.commit()
    return {"created": [c.id for c in created], "archived": archived_ids}


def _wire_item_edges(session: Session, rows: list[Task], proposals: list[dict[str, Any]]) -> None:
    """A per-item stage's ordering among its own rows: each proposal's `depends_on` indices and the
    phase DAG (phase k+1 waits on phase k), the rules `append_tasks` applies to coders. Any bad index or
    cycle rolls the whole append back (422)."""
    def edge(row: Task, prereq: Task, what: str) -> None:
        if session.get(TaskDependency, {"task_id": row.id, "depends_on_task_id": prereq.id}) is not None:
            return
        if _would_create_cycle(row.id, prereq.id, session):
            session.rollback()
            raise HTTPException(status_code=422, detail=f"{what} {row.id} -> {prereq.id} would create a cycle")
        session.add(TaskDependency(task_id=row.id, depends_on_task_id=prereq.id))

    for idx, proposal in enumerate(proposals):
        for dep_idx in proposal.get("depends_on") or []:
            if not isinstance(dep_idx, int) or dep_idx == idx or not (0 <= dep_idx < len(rows)):
                session.rollback()
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"proposal {idx} declares an invalid dependency index {dep_idx!r} "
                        f"(must be a distinct 0-based index in [0, {len(rows)}))"
                    ),
                )
            edge(rows[idx], rows[dep_idx], "item dependency")
    phases: dict[int, list[Task]] = {}
    for row in rows:
        if row.phase_index is not None:
            phases.setdefault(row.phase_index, []).append(row)
    ordered = sorted(phases)
    for prev_phase, next_phase in zip(ordered, ordered[1:]):
        for row in phases[next_phase]:
            for prereq in phases[prev_phase]:
                edge(row, prereq, "phase edge")


def _child_rollup_ready(
    state: str, persona: str, outcome_pr_url: Optional[str],
    outcome_no_pr_reason: Optional[str] = None,
) -> bool:
    """Is a `done` epic child legitimately done FOR ROLLUP purposes? An `artifact`-criterion child is done
    the moment its state is. A build child (`EPIC_BUILD_PERSONAS`) also needs `outcome_pr_url` or `outcome_no_pr_reason` — the
    anti-spoof floor: a bare close never rolls up.

    Nothing fills them in later: only the child's own settle writes them. A `coder` reaching `done`
    with neither (an operator `complete`) holds its epic `active`; the remedy is currently a DB update.
    `task_pipeline_cp.reconcile_epic_rollups` only re-runs the rollup."""
    if state != DONE:
        return False
    if persona in EPIC_BUILD_PERSONAS:
        return outcome_pr_url is not None or outcome_no_pr_reason is not None
    return True


def _roll_up_epic(parent: Task, child: Task, session: Session) -> None:
    """Roll an EPIC parent up when one of its children reaches a terminal state. Every guarded
    UPDATE keys on `persona == 'epic' AND state == 'active'`, which is last-writer-wins safe.
      - A `system_failed` child fails the epic. Only that state — a `paused` child is a human's Stop.
      - A `paused` child may PAUSE the epic once EVERY unsettled child is paused; any
        `awaiting_response`/`ready`/`active`/`draft` child keeps it `active`, a human gate in particular.
      - A `done` child completes the epic ONLY when every sibling is `done` and rollup-ready.
    Readying the next child is the dependency gate's job; retryable failures never reach here.

    Failure does not propagate downwards (owner decision): no sibling is cancelled, and no floor gates
    a child on its epic's `system_failed`. `Stop` is the one verb that cascades."""
    if child.state in _EPIC_FAILING_CHILD_STATES:
        # Lists every failed child, since an epic can collect several. Lock the epic first: the list is a
        # pre-read, and two siblings failing at once would otherwise overwrite each other's reason. Every writer
        # of this column (`_reevaluate_epic_child_failure` too) takes this lock.
        session.execute(select(Task.id).where(Task.id == parent.id).with_for_update())
        failed_refs = _epic_failed_child_refs(parent.id, session)
        if not failed_refs:
            # The failing child is archived, so not a live child (every epic rule filters `archived_at IS NULL`);
            # failing the container on it would write a blank reason `_reevaluate_epic_child_failure` never clears.
            session.commit()
            return
        result = session.execute(
            update(Task)
            .where(Task.id == parent.id, Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
            .values(
                state=SYSTEM_FAILED, awaiting_response_body=failed_refs,
                **actors.task_actor_values(session),
            )
        )
        if result.rowcount > 0:
            emit_console_event(session, entity="task", id=parent.id, action=SYSTEM_FAILED)
        else:
            # Already failed by an earlier sibling: refresh the list and emit nothing (no second `task:system_failed`).
            session.execute(
                update(Task)
                .where(Task.id == parent.id, Task.persona == PERSONA_EPIC, Task.state == SYSTEM_FAILED)
                .values(awaiting_response_body=failed_refs, **actors.task_actor_values(session))
            )
        session.commit()
        return

    if child.state == PAUSED:
        # R4.5 pause-collapse (T20) — see the helper. Commits either way, mirroring the branch above.
        _pause_epic_if_children_all_paused(parent.id, session)
        session.commit()
        return

    # Child done: try the shared completion; if it did not complete, re-check the pause-collapse.
    if not _try_complete_epic(parent.id, session):
        _pause_epic_if_children_all_paused(parent.id, session)
        session.commit()


def _epic_failed_child_refs(epic_id: int, session: Session) -> str:
    """The epic's reason line while it is failed by its children: every LIVE child still `system_failed`,
    ascending by id, as prose naming ids an operator can open. Empty only for an epic with no failed
    child, which no caller writes."""
    ids = session.scalars(
        select(Task.id)
        .where(
            Task.parent_task_id == epic_id,
            Task.state == SYSTEM_FAILED,
            Task.archived_at.is_(None),
        )
        .order_by(Task.id)
    ).all()
    if not ids:
        return ""
    refs = ", ".join(f"task {tid}" for tid in ids)
    return f"epic child(ren) system_failed: {refs}"


def _reevaluate_epic_child_failure(epic_id: Optional[int], session: Session) -> None:
    """Re-derive a `system_failed` epic's CHILD-FAILURE state after one of its children has left
    `system_failed` (owner decision). No live failed child left → back to `active`, reason dropped; some
    remain → stays failed, reason rewritten to name exactly those.

    Called only from sites that just took a child out of `system_failed` (Complete, Reset, delete,
    archive, done-on-artifact, Start); that is the guard against reopening an epic failed by
    `record_fanout_attempt_failed`, which has no failed child. Such an epic can still be cleared here if
    one of its children later fails and recovers; the next fan-out sweep re-fails it. `runner_breaks.
    archive` only touches system personas, never epic children.

    Lock-then-write: the refresh arm's list is a pre-read, and under READ COMMITTED the clear arm's
    EXISTS uses the statement snapshot, so a sibling failing while it waited could be missed. Taking the
    epic row first serializes every writer. `None` and non-epic parents are no-ops. Does NOT commit."""
    if epic_id is None:
        return
    session.execute(select(Task.id).where(Task.id == epic_id).with_for_update())
    failed_refs = _epic_failed_child_refs(epic_id, session)
    if failed_refs:
        # Still failed: rewrite the list, change no state; guarded so a non-failed container never gets a reason.
        session.execute(
            update(Task)
            .where(Task.id == epic_id, Task.persona == PERSONA_EPIC, Task.state == SYSTEM_FAILED)
            .values(awaiting_response_body=failed_refs, **actors.task_actor_values(session))
        )
        return
    result = session.execute(
        update(Task)
        .where(
            Task.id == epic_id,
            Task.persona == PERSONA_EPIC,
            Task.state == SYSTEM_FAILED,
            ~_epic_has_failed_child(),
        )
        .values(state=ACTIVE, awaiting_response_body=None, **actors.task_actor_values(session))
    )
    if result.rowcount > 0:
        emit_console_event(session, entity="task", id=epic_id, action=ACTIVE)


def _pause_epic_if_children_all_paused(epic_id: int, session: Session) -> None:
    """An epic whose UNSETTLED children are all `paused` — at least one of them — follows them to `paused`:
    it waits on a human. Any `draft`/`ready`/`active`/`awaiting_response` child keeps it `active`; the
    >=1-paused floor keeps an all-done epic awaiting rollup from being mis-parked. The probe spells its
    set out rather than reading `_NO_RUNNER_STATES` (a different question). Does NOT commit."""
    # `archived_at IS NULL`: an archived child is never a LIVE child again — it must neither hold this epic
    # open nor count toward the all-paused floor.
    child_states = session.scalars(
        select(Task.state).where(Task.parent_task_id == epic_id, Task.archived_at.is_(None))
    ).all()
    blocking = [s for s in child_states if s not in _EPIC_SETTLED_STATES + (PAUSED,)]
    if blocking or PAUSED not in child_states:
        return
    result = session.execute(
        update(Task)
        .where(Task.id == epic_id, Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
        .values(state=PAUSED, **actors.task_actor_values(session))
    )
    if result.rowcount > 0:
        emit_console_event(session, entity="task", id=epic_id, action=PAUSED)


def _try_complete_epic(epic_id: int, session: Session) -> bool:
    """Complete an ACTIVE epic when every CURRENT child is done AND rollup-ready and the append-pending
    guard has cleared, re-reading counts each call. The one guarded UPDATE shared by both rollup paths:
    the per-child trigger can be missed, so `scan_active_epics_for_rollup` re-drives this on a cadence.
    Idempotent; True iff this call flipped the epic to `done`."""
    # Append-pending guard: a `done` plan child whose coders are not appended yet means the epic
    # is NOT complete — plan finishes last, so without this the epic would complete the instant it
    # reached `done` and strand them. `archived_at IS NULL`: an archived child counts as neither.
    child_rows = session.execute(
        select(Task.state, Task.persona, Task.outcome_pr_url, Task.outcome_no_pr_reason, Task.id)
        .where(Task.parent_task_id == epic_id, Task.archived_at.is_(None))
    ).all()
    if not (child_rows and all(_child_rollup_ready(s, k, pr, no_pr) for s, k, pr, no_pr, _ in child_rows)):
        return False
    # A child done on queue is not delivered until the lane merges it.
    if merge_lane_state.awaiting_lane_merge(session, [cid for *_, cid in child_rows]):
        return False
    if _epic_append_pending(epic_id, session):
        return False
    result = session.execute(
        update(Task)
        .where(Task.id == epic_id, Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
        .values(state=DONE, **dispatch._RUNNER_BINDING_CLEARED, **actors.task_actor_values(session))
    )
    if result.rowcount > 0:
        emit_console_event(session, entity="task", id=epic_id, action=DONE)
        # Behind the rowcount: an epic that did not complete (it was `paused`) must not get a final report.
        # Normally a no-op: the per-child trigger already published.
        epic = session.get(Task, epic_id)
        if epic is not None:
            epic_finish._publish_epic_final_report_at_completion(epic, session)
    session.commit()
    return result.rowcount > 0


def scan_active_epics_for_rollup(session: Session) -> list[int]:
    """Reconcile-scan self-heal for the missed-edge-trigger gap: re-evaluate EVERY active epic via
    `_try_complete_epic` — the SAME guarded UPDATE the per-child trigger uses, never a second code path.
    Driven by jaahd's `sweep_epic_rollups` on the 60s pass, so an epic whose last child's trigger was
    missed still completes within one cycle. Returns the ids that completed on THIS pass."""
    epic_ids = session.scalars(
        select(Task.id).where(Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
    ).all()
    return [epic_id for epic_id in epic_ids if _try_complete_epic(epic_id, session)]


def _stuck_epic_reason(epic_id: int, session: Session) -> Optional[str]:
    """Name the CONCRETE reason an epic whose NON-finish children are ALL `done` did not roll up, reusing
    `_try_complete_epic`'s predicate chain. The finish child is evaluated separately: its own not-`done`
    state is itself a stuck condition. Checked in order: a non-finish child not rollup-ready,
    `_epic_append_pending`, the finish child. None when there are no non-finish children, one is still
    in flight, or every check passes."""
    # `archived_at IS NULL` — see `_try_complete_epic`; an archived child must not surface here either.
    child_rows = session.execute(
        select(Task.id, Task.state, Task.persona, Task.outcome_pr_url, Task.outcome_no_pr_reason)
        .where(Task.parent_task_id == epic_id, Task.archived_at.is_(None))
        .order_by(Task.id)
    ).all()
    non_finish_rows = [row for row in child_rows if row[2] != PERSONA_FINISH]
    finish_rows = [row for row in child_rows if row[2] == PERSONA_FINISH]
    if not non_finish_rows or any(s != DONE for _, s, _, _, _ in non_finish_rows):
        return None
    for cid, s, k, pr, no_pr in non_finish_rows:
        if not _child_rollup_ready(s, k, pr, no_pr):
            if k in EPIC_BUILD_PERSONAS:
                return (
                    f"{k} #{cid} done but no outcome_pr_url / no FLEET_NO_PR marker "
                    "(outcome backfill hasn't landed yet)"
                )
            return f"child #{cid} (persona={k!r}) done but not rollup-ready"
    if _epic_append_pending(epic_id, session):
        return "plan child done but coder children have not been appended yet"
    for cid, s, _, _, _ in finish_rows:
        if s != DONE:
            return f"finish child #{cid} has not completed (the Finish phase has not executed)"
    return None


def _epic_all_children_done_since(
    epic_id: int, child_ids: list[int], session: Session
) -> Optional[datetime]:
    """The instant every CURRENT child of `epic_id` had most recently reached `done` — the timestamp
    `scan_stuck_epics` measures from. The latest `task:done` per child, then the max across them, since the
    LAST child to finish is when the epic FIRST became all-done. None if any child has none."""
    if not child_ids:
        return None
    str_ids = [str(i) for i in child_ids]
    rows = session.execute(
        select(ActivityEvent.data["id"].astext, ActivityEvent.created_at)
        .where(ActivityEvent.kind == "task:done", ActivityEvent.data["id"].astext.in_(str_ids))
        .order_by(ActivityEvent.created_at)
    ).all()
    last_done: dict[int, datetime] = {}
    for id_str, created_at in rows:
        last_done[int(id_str)] = created_at  # ascending order — last write wins (latest done)
    if len(last_done) < len(child_ids):
        return None
    return max(last_done.values())


def scan_stuck_epics(session: Session) -> list[dict[str, Any]]:
    """Report every ACTIVE epic whose NON-finish children are ALL `done` but which `_stuck_epic_reason`
    finds not rollup-ready. Called after `scan_active_epics_for_rollup` in the same request.
    `stuck_seconds` is anchored on the non-finish children's `task:done` events. Unfiltered by age:
    `FLEET_STUCK_EPIC_MINS` is jaahd's decision."""
    rows = session.execute(
        select(Task.id, Project.name)
        .join(Project, Task.project_id == Project.id)
        .where(Task.persona == PERSONA_EPIC, Task.state == ACTIVE)
        .order_by(Task.id)
    ).all()
    out: list[dict[str, Any]] = []
    for epic_id, project_name in rows:
        reason = _stuck_epic_reason(epic_id, session)
        if reason is None:
            continue
        non_finish_child_ids = session.scalars(
            select(Task.id).where(
                Task.parent_task_id == epic_id, Task.persona != PERSONA_FINISH,
                Task.archived_at.is_(None),
            )
        ).all()
        since = _epic_all_children_done_since(epic_id, non_finish_child_ids, session)
        if since is None:
            continue
        stuck_seconds = (datetime.now(timezone.utc) - since).total_seconds()
        out.append({
            "epic_id": epic_id, "project": project_name, "reason": reason,
            "stuck_seconds": max(stuck_seconds, 0.0),
        })
    return out


def _maybe_roll_up_parent(child_task_id: int, session: Session) -> None:
    """Roll an epic parent up when one of its children reaches a terminal state.

    Called from `settle_task` after it stores a coder's `outcome_pr_url` (which may be what makes a `done` child rollup-ready). Acts only on an epic
    parent. Readying the next child is the gate's job."""
    child = session.get(Task, child_task_id)
    if child is None or child.parent_task_id is None:
        return
    parent = session.get(Task, child.parent_task_id)
    if parent is None or parent.persona != PERSONA_EPIC:
        return
    # Before the rollup because `_roll_up_epic` commits on every branch; publishing afterwards would be left
    # uncommitted. The predicate lives in `_publish_epic_final_report`.
    epic_finish._publish_epic_final_report(child, parent, session)
    _roll_up_epic(parent, child, session)
