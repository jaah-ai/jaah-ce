"""The pool-slot occupancy store — the control plane's SOLE authority over who may run in a slot.

One row per slot: the `worktrees` row, keyed `(node_id, project_id, number)` with `node_id` NOT NULL.
Nothing renews and nothing frees on silence: an un-acked dispatch is re-dispatched into the same slot,
and only a decision ends a hold (edges in `release`), so unlanded work never reaches `free` by inference.

`generation` is the fencing token for the launch ack and the launch; every lease and
redispatch bumps it. Every mutation is one statement (unique key + WHERE) keyed on `worktree_id`
(`resolve_slot` maps the wire triple). These columns are `app/slot_columns.py`'s `OCCUPANCY` family,
written only here (`tests/test_slot_two_writers.py`). The launch columns (reported on ack) and the sensed
`session_*` columns (last `/proc` scan) must not be collapsed: comparing them is the liveness question.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import Integer, bindparam, func, select, text, update

# The superseded session's ending rides `redispatch` (see that function).
from app import agent_session_lifecycle, carryover, fleet_verdicts
from app.agent_sessions import END_REASON_SUPERSEDED
# The unknown-age sentinel's one home; `idle_hours_expr` binds it as a parameter, never a literal.
from app.fleet_decide import _UNKNOWN_AGE_SENTINEL as UNKNOWN_AGE_SENTINEL
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import WORKTREE_KIND_DYNAMIC, WORKTREE_KIND_STATIC, Worktree
from app.node_identity import node_ref
from app.clock import utcnow as _now

# ── Identities in the columns and the dicts; names for display only ─────────────────────────────────
# Statements filter on `project_id`/`node_id` ids, so a rename cannot move a slot under a hold. Every dict
# carries `project_id` (filter on it), `project` (display), `worktree_id` (release by it), the node uuid
# string under `node_id` (address it by that) and the node's display name under `node_name`. Node names
# repeat (`app/hosts.py` NAMING), so a name never addresses a node.

# The three states. A worktree belongs to a TASK, to a USER, or to NOBODY. Whether a runner is currently
# alive is a liveness question, answered off the launch columns against the sensed ones, never off
# `state` (`app/models/worktrees.py::Worktree.state` mirrors this DDL).
FREE = "free"
# A fleet Task owns this tree (`task_id`), from lease through settle, whether or not a runner is running.
TASK = "task"
# A human owns this tree (`owner_user_id`), released on that user's `/exit` (reported to
# `POST /api/agent/user-session-ended`) or an operator's console release (edges in `release`).
# Invisible to dispatch and untouchable by any sweep or reset.
USER = "user"

# Every state a row may carry, for the CHECK constraint (`ck_worktrees_state`). `FREE` first.
STATES = (FREE, TASK, USER)

# ── When a session in a `free` slot becomes the CP's problem ──────────────────────────────────────────
#
# A live session in a slot promised to nobody is reaped after this grace. 60s is about four measured
# release round trips (mint, node poll, `_clear_tree`, re-observe ≈ 9s, plus ~5s observation cadence), so
# a slot still settling its release is never reaped.
FREE_SLOT_OCCUPANT_GRACE_SECS = 60.0

# Margin on the warrant's clock comparison: `freed_at` comes from the CP's Postgres clock while the node
# computes a session's start from its own `/proc/stat` btime (±1s, NTP steps), so a fast node clock could
# warrant a bystander that started just after the release. The cost: a survivor that started within the
# margin is not warranted and its tree is swept only for cwd-rooted processes. Fails closed.
FREE_SLOT_WARRANT_SKEW_SECS = 60.0


def new_dispatch_id() -> str:
    """A fresh correlation id for one dispatch. Opaque, compared for equality only."""
    return uuid.uuid4().hex


def resolve_slot(session, *, node_id: str, project_id: int, number: int) -> Optional[UUID]:
    """THE ONE PLACE the wire triple becomes the key everything here is spelled in — `worktrees.id`.

    None when no slot answers (unknown node uuid, unenrolled slot, unknown project). `node_id` is the node
    uuid. Callers holding a `Worktree` pass `wt.id` directly. `node_ref` keeps the uuid probe inside the
    statement, so a non-uuid or unknown one matches nothing."""
    return session.scalar(
        select(Worktree.id).where(
            Worktree.node_id == node_ref(node_id),
            Worktree.project_id == int(project_id),
            Worktree.number == int(number),
        )
    )


# ── Taking and ending a hold ──────────────────────────────────────────────────────────────────────────


def _require_org(org_id: Optional[UUID], site: str) -> None:
    """Refuse a `None` org before it reaches a statement. The `is not None` guard, in one place.

    `None == None` is True in Python, and `Worktree.org_id == None` compiles to `IS NULL`, so a `None`
    would turn the org floor into a wildcard (or, with `worktrees.org_id` NOT NULL since 0193, a silent
    no-match; still live for tables not yet contracted). A `ValueError`, not a `None` return: a caller
    that could not resolve an org must decline the placement (`app/placement_grants.py`), not walk the
    pool."""
    if org_id is None:
        raise ValueError(
            f"slot_state.{site}() requires an org to place in; None would compile to `org_id IS NULL` "
            "and match every un-stamped slot. Resolve the org (app/placement_grants.py::placement_org) "
            "or decline the placement."
        )


def lease(
    session,
    *,
    worktree_id: UUID,
    org_id: UUID,
    task_id: Optional[int] = None,
    dispatch_id: Optional[str] = None,
    reason: str = "",
) -> Optional[dict[str, Any]]:
    """Take the hold on one slot for a task, IN ONE ORG. Returns `{"generation", "dispatch_id"}`, or None
    when it is already held — or when the slot belongs to another org.

    One statement: `UPDATE … WHERE id = … AND org_id = … AND state = 'free' AND number > 0 RETURNING`,
    so the org floor has no read-write window. It compares `worktrees.org_id` (kept equal to the node's
    by revision 0181's composite FK; cf. `app/placement_grants.py::_placeable_slot_exists`). A `None`
    org raises (`_require_org`). Only `lease` and `claim_for_user` bind a new occupant and carry the
    floor; the other `worktrees` updates are generation-fenced, free the row, or observe
    (`tests/test_slot_org_floor.py`).

    An UPDATE, never an upsert: only a declaration creates a slot row. Only a `free`
    row is takeable, so `reset_to_base` is safe after a hit; check `task_hold`/`read` first and use
    `redispatch` for an existing hold. `generation` 0 means never leased; slot 0 (main clone) is excluded.
    A node's sighting never vetoes the lease (`occupied_free_slots` feeds the reaper instead)."""
    _require_org(org_id, "lease")
    token = dispatch_id or new_dispatch_id()
    row = session.execute(
        Worktree.__table__.update()
        .where(
            Worktree.id == worktree_id,
            # The org floor, above `state`: another org's slot is not a busy slot, it is not this caller's.
            Worktree.org_id == org_id,
            Worktree.state == FREE,
            Worktree.number > 0,
            # A row being destroyed is never handed to new work (`slot_allocation.retire`).
            Worktree.retiring_at.is_(None),
        )
        .values(
            state=TASK,
            state_since=func.now(),
            task_id=task_id,
            generation=Worktree.generation + 1,
            dispatch_id=token,
            dispatched_at=func.now(),
            acked_at=None,
            pid=None,
            pid_start_time=None,
            launch_session_id=None,
            last_seen_live=None,
            # Blanked with every other occupancy fact on a change of tenant: `routes/agent/task_secrets.py::get_account_api_key`
            # serves an API key to a task-held slot leased to that account, and until `bind_account` runs this
            # row is `state='task'` again, so the previous tenant's account would still match. Not left to
            # the reader's `state == 'task'` guard, which this very statement satisfies.
            leased_account_id=None,
            reason=reason or None,
        )
        .returning(Worktree.generation, Worktree.dispatch_id)
    ).first()
    if row is not None:
        # In the take's own transaction (the commit below), never a later one.
        from app.slot_allocation import snapshot_task_kind  # deferred: slot_allocation imports this module

        snapshot_task_kind(session, worktree_id)
    session.commit()
    if row is None:
        return None
    return {"generation": row.generation, "dispatch_id": row.dispatch_id}


def claim_for_user(
    session, *, worktree_id: UUID, user_id, org_id: UUID, reason: str = "", folder: bool = False,
) -> Optional[dict[str, Any]]:
    """Claim a free slot for a human — `state='user'`, named by `owner_user_id`. Returns `{"generation"}`,
    or None when it is already held, or when the slot belongs to another org.

    The same org floor as `lease`, sourced from the claiming principal (a console claim has no task), so
    the parameter is on the function. Same shape: `UPDATE … WHERE id = … AND state = 'free' AND number > 0
    RETURNING`; pool slots only. Never touches `task_id` (`ck_worktrees_state_ownership` requires NULL,
    and a free row has it NULL). Bumps `generation`. Released through `release`, on the user's `/exit`.

    `folder` claims instead the dynamic FOLDER slot a console just declared for its own session
    (`slot_allocation.add_user_folder`) — a repo-less project holds no static slot to claim. False
    keeps the static-only rule."""
    _require_org(org_id, "claim_for_user")
    row = session.execute(
        Worktree.__table__.update()
        .where(
            Worktree.id == worktree_id,
            Worktree.org_id == org_id,
            Worktree.state == FREE,
            Worktree.number > 0,
            # A console claims a static slot only: a dynamic one exists for its dispatch and goes when freed (A9)
            # — except the folder slot a console declared for itself, which goes when its session frees it.
            *((Worktree.kind == WORKTREE_KIND_DYNAMIC, Worktree.has_repo.is_(False)) if folder
              else (Worktree.kind == WORKTREE_KIND_STATIC,)),
            Worktree.retiring_at.is_(None),
        )
        .values(
            state=USER,
            state_since=func.now(),
            owner_user_id=user_id,
            generation=Worktree.generation + 1,
            dispatch_id=None,
            dispatched_at=None,
            acked_at=None,
            pid=None,
            pid_start_time=None,
            launch_session_id=None,
            last_seen_live=None,
            reason=reason or None,
        )
        .returning(Worktree.generation)
    ).first()
    session.commit()
    if row is None:
        return None
    return {"generation": row.generation}


def record_launch(
    session,
    *,
    worktree_id: UUID,
    generation: int,
    dispatch_id: str,
    pid: Optional[int] = None,
    pid_start_time: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Stamp the launch identity a node reported on its ack. Occupancy stays `task`; this is a liveness
    fact, not a transition.

    Fenced on both `generation` and `dispatch_id` (either alone admits a stale ack). False means stale and
    nothing written; a genuine retry re-stamps the same values. `session_id` lands in `launch_session_id`,
    never `session_id` (see the module docstring)."""
    result = session.execute(
        Worktree.__table__.update()
        .where(
            Worktree.id == worktree_id,
            Worktree.generation == int(generation),
            Worktree.dispatch_id == dispatch_id,
            Worktree.state == TASK,
        )
        .values(
            acked_at=func.now(),
            pid=pid,
            pid_start_time=pid_start_time,
            launch_session_id=session_id,
            # A launch is not a sighting: `last_seen_live` is advanced only by `mark_seen_live`, and a carried
            # value would date this occupancy to a gone session (`app/task_pipeline_job.py` reads
            # `COALESCE(last_seen_live, state_since)`). NULL means "not sighted yet".
            last_seen_live=None,
        )
    )
    session.commit()
    return bool(result.rowcount)


def claim_launch(session, *, worktree_id: UUID, generation: int, dispatch_id: str) -> bool:
    """MAY this dispatch start a runner in this slot? True exactly once per hold. The launch SINGLE-FLIGHT.

    `record_launch`'s fence moved to before the spawn; asked by `jaahd/reconcile/enact_launch.py::_launch_into_slot`.
    One UPDATE answers and consumes, so racing callers serialise on the row. `IS DISTINCT FROM` so no reset
    site needs to know this column: every change of hold bumps `generation`.

    Fails closed: a granted claim whose response is lost cannot be re-asked, so the intent waits for a
    re-dispatch (`fleet_launch.launch_refusal`'s trade)."""
    result = session.execute(
        Worktree.__table__.update()
        .where(
            Worktree.id == worktree_id,
            Worktree.generation == int(generation),
            Worktree.dispatch_id == dispatch_id,
            Worktree.state == TASK,
            Worktree.launch_claimed_generation.is_distinct_from(int(generation)),
        )
        .values(launch_claimed_generation=int(generation))
    )
    session.commit()
    return bool(result.rowcount)


def release(
    session, *, worktree_id: UUID, generation: int, reason: str = "", commit: bool = True
) -> bool:
    """End the hold: back to `free`, launch identity cleared, both owner columns unbound. Idempotent on an
    already-free row, which answers False so a caller can tell a release from a no-op.

    Task-hold edges only: the task went terminal, was deleted, reset or moved to another project; its runner was stopped on a
    question with an observed-clean tree (`stopped_question_clean_holds`); its launches on that node never
    announced themselves twice (`lease_drain._move_off_node`); its node refused the launch at its session cap
    (`node_capacity.return_to_queue`); or an operator's console
    release (`app/routes/pool_slots.py::release_slot`). No runner or session ends a task hold. A user hold
    also ends on its owner's `/exit`, fenced on the session id. No caller may borrow another's trigger.

    `commit=False` lets `end_attempt` commit its terminal state and this release as one unit. Fenced on
    `generation` and does not bump it. Does not end the session record: callers pass a better
    `note_end` reason than "released".

    The hold's own still-queued `lease_slot` order is retired with it: nothing may re-lease the slot to
    supersede it, and the node would otherwise enact a dispatch the task no longer has."""
    held = session.execute(
        select(Worktree.node_id, Worktree.project_id, Worktree.number, Worktree.dispatch_id)
        .where(Worktree.id == worktree_id, Worktree.generation == int(generation), Worktree.state != FREE)
    ).first()
    result = session.execute(
        Worktree.__table__.update()
        .where(
            Worktree.id == worktree_id,
            Worktree.generation == int(generation),
            Worktree.state != FREE,
        )
        .values(
            state=FREE,
            state_since=func.now(),
            task_id=None,
            owner_user_id=None,
            dispatch_id=None,
            dispatched_at=None,
            acked_at=None,
            pid=None,
            pid_start_time=None,
            launch_session_id=None,
            last_seen_live=None,
            # The credential association ends with the lease (see `lease`), so a free slot authorizes nothing
            # in the data, not only via the reader's `state == 'task'` conjunct.
            leased_account_id=None,
            reason=reason or None,
        )
    )
    # Every task-hold edge above is an end edge of a carry-over on this slot, and the release orders its erase
    # (`app/carryover.py`); a no-op otherwise. Only a release that freed the row, before the commit.
    if result.rowcount:
        carryover.slot_released(session, worktree_id)
        if held is not None:
            fleet_verdicts.retire_released_lease(
                session, node_id=held.node_id, project_id=held.project_id, number=held.number,
                dispatch_id=held.dispatch_id, reason=f"its hold was released: {reason}" if reason else
                "its hold was released",
            )
    if commit:
        session.commit()
    return bool(result.rowcount)


def redispatch(
    session,
    *,
    worktree_id: UUID,
    generation: int,
    dispatch_id: Optional[str] = None,
    reason: str = "",
    keep_pid: Optional[int] = None,
    keep_pid_start_time: Optional[str] = None,
    keep_session_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Fence the current hold and re-arm the slot for the SAME task: bump `generation`, drop the launch
    identity, stay at `state='task'`. None when there is no task hold to re-arm.

    What an un-acked dispatch gets: re-dispatched into the same slot, tree untouched. One statement so the
    slot never passes through `free`, where the drain could hand the tree to another task. Fenced on
    `generation`: an unfenced re-arm on a stale read would blank another replica's fresh hold and wedge it.

    `keep_pid`/`keep_pid_start_time`/`keep_session_id` preserve the launch identity for a runner that
    reported a transient `StopFailure`, so the same session can undo the re-arm at the same generation.

    The superseded session record is ended here, the one moment the CP knows its identity is replaced
    (the node never reports the kill); otherwise it stays `running` as a phantom and its transcript is
    never requested. `end_session` moves only a `running` row, keeps a first ending, and rides this
    transaction. A `keep_session_id` equal to the old identity is not ended."""
    token = dispatch_id or new_dispatch_id()
    # Read before the UPDATE blanks it, under the same fence, so the ended identity is exactly the replaced one.
    old_session_id = session.scalar(
        select(Worktree.launch_session_id).where(
            Worktree.id == worktree_id, Worktree.generation == int(generation)
        )
    )
    row = session.execute(
        Worktree.__table__.update()
        .where(
            Worktree.id == worktree_id,
            Worktree.generation == int(generation),
            Worktree.state == TASK,
            # A row being destroyed is not re-armed: the destroy's ack requeues its task.
            Worktree.retiring_at.is_(None),
        )
        .values(
            generation=Worktree.generation + 1,
            state=TASK,
            state_since=func.now(),
            dispatch_id=token,
            dispatched_at=func.now(),
            acked_at=None,
            pid=keep_pid,
            pid_start_time=keep_pid_start_time,
            launch_session_id=keep_session_id,
            # Blank the sighting with the launch identity: a re-arm is a new occupancy.
            last_seen_live=None,
            # A re-arm is a new occupancy (see `lease`). Not redundant: a spillover re-pick can resolve a
            # different account, and if compose then skips, `_undo_take` does not release, so the slot would
            # keep the last bind's account until this pass's `bind_account`.
            leased_account_id=None,
            reason=reason or None,
        )
        .returning(Worktree.generation, Worktree.dispatch_id)
    ).first()
    if row is not None:
        # A task dispatched before slots had a kind has none recorded yet.
        from app.slot_allocation import snapshot_task_kind  # deferred: slot_allocation imports this module

        snapshot_task_kind(session, worktree_id)
    if row is not None and old_session_id and old_session_id != keep_session_id:
        # `note_end`, not `end_session`: its savepoint keeps a bookkeeping failure from aborting the re-arm.
        agent_session_lifecycle.note_end(
            session, agent_session_id=old_session_id, end_reason=END_REASON_SUPERSEDED
        )
    session.commit()
    if row is None:
        return None
    return {"generation": row.generation, "dispatch_id": row.dispatch_id}


def bind_account(session, *, worktree_id: UUID, generation: int, account_id) -> None:
    """Record WHICH credential account the dispatch about to launch on this slot will spend.

    `leased_account_id` is OCCUPANCY (decided by the CP), distinct from the SENSED `account_id` beside it
    (`app/slot_columns.py`, `tests/test_slot_two_writers.py`). It exists because
    `routes/agent/task_secrets.py::get_account_api_key` must authorize the key before any observation arrives (and an
    API-key dispatch never produces one). Written for every kind, so the reader tests one column.

    Fenced on `generation` and `state`: unfenced, a pass that lost the take race could stamp its account
    onto another task's slot and leak that account's key. A lost race writes nothing, silently. `None`
    clears the column."""
    session.execute(
        update(Worktree)
        .where(
            Worktree.id == worktree_id,
            Worktree.generation == generation,
            Worktree.state == TASK,
        )
        .values(leased_account_id=account_id)
    )
    session.commit()


# ── Reads ─────────────────────────────────────────────────────────────────────────────────────────────

def read(session, *, worktree_id: UUID) -> Optional[Worktree]:
    """The slot row, whatever its state, or None when no such slot exists. The mapped row, not a display dict."""
    return session.get(Worktree, worktree_id)


def held_slots(session, project_id: int, *, node_id: Optional[str] = None) -> list[str]:
    """The BUSY set for `project_id` — slot numbers with a non-`free` hold, as strings, for
    `fleet_decide.select_slot`'s `exclude`.

    Not redundant with `fleet_decide.held_slots_from_records` (who is sitting there): this is who the CP
    promised it to, covering a dispatch with no session yet. `node_id` (uuid) narrows to one host; omitting it
    gives the fleet-wide answer."""
    where = [Worktree.project_id == int(project_id), Worktree.state != FREE]
    if node_id is not None:
        where.append(Worktree.node_id == node_ref(node_id))
    return [
        str(number)
        for number in session.scalars(
            select(Worktree.number).where(*where).order_by(Worktree.number)
        ).all()
    ]


def free_slots_by_node(session, project_id: int) -> dict[str, int]:
    """How many LEASABLE static slots for `project_id` are free right now, per node uuid string (a node's
    dynamic room is `slot_allocation.dynamic_room`).

    The fleet-wide capacity question for `app/pool_state.py::assign_nodes`. `number > 0` matches `lease`
    (slot 0 is the main clone). Uuid strings, like every node key on this path; a mismatch would silently
    report zero room. A node with no free slot is absent; callers default to 0."""
    rows = session.execute(
        select(Worktree.node_id, func.count())
        .where(
            Worktree.project_id == int(project_id),
            Worktree.state == FREE,
            Worktree.number > 0,
            # Static and not being destroyed: a free dynamic row is destroyed, never leased again.
            Worktree.kind == WORKTREE_KIND_STATIC,
            Worktree.retiring_at.is_(None),
            # Of the project's kind: a git worktree is never leased to a folder task, nor the reverse.
            Worktree.has_repo == select(Project.has_repo).where(Project.id == int(project_id)).scalar_subquery(),
        )
        .group_by(Worktree.node_id)
    ).all()
    return {str(node_id): int(count) for node_id, count in rows}


def occupied_slots_by_node(session) -> dict[str, int]:
    """How many RUNNERS each node is already carrying IN TOTAL, across EVERY project, per node uuid string.

    What `app/pool_state.py::assign_nodes` divides by core count for its density tie-break: idle runners
    cost little CPU, so load alone would keep piling work onto a node full of waiting runners. Across
    every project because runner count is a fact about the host. `state = 'task'` only (a user session
    is not a runner; CPU covers its load); an un-acked lease counts, so a pass cannot place into room its
    previous pass took. `number > 0`. A node carrying nothing is absent; callers default to 0."""
    rows = session.execute(
        select(Worktree.node_id, func.count())
        .where(
            Worktree.state == TASK,
            Worktree.number > 0,
        )
        .group_by(Worktree.node_id)
    ).all()
    return {str(node_id): int(count) for node_id, count in rows}


def leased_task_ids(session) -> set[str]:
    """Task ids currently holding a slot — the dedup that stops a task whose dispatch is already out from
    being dispatched a second time. Complements `fleet_decide.inflight_task_ids` (observation roster) by
    covering the gap before the first observation. A task with a hold is in flight."""
    return {
        str(task_id)
        for task_id in session.scalars(
            select(Worktree.task_id).where(Worktree.state == TASK, Worktree.task_id.isnot(None))
        ).all()
    }


def live_hold_for_task(session, task_id: object) -> Optional[dict[str, Any]]:
    """The task hold with a LAUNCHED session, or None when it holds none.

    For the warm-answer path, which needs the slot, node and session to write a gate answer into.
    `launch_session_id IS NOT NULL` (an un-acked hold has no pane yet); it is what the node said it
    started, unlike the sensed `session_id`. None signals the cold-resume fallback."""
    if task_id is None:
        return None
    row = session.execute(
        select(
            Worktree.id,
            Worktree.project_id,
            Project.name.label("project"),
            Worktree.number,
            Worktree.node_id,
            Node.name.label("node_name"),
            Worktree.launch_session_id,
        )
        .join(Project, Project.id == Worktree.project_id)
        .join(Node, Node.id == Worktree.node_id)
        .where(
            Worktree.task_id == task_id,
            Worktree.state == TASK,
            Worktree.launch_session_id.isnot(None),
        )
    ).first()
    if row is None:
        return None
    return {
        "worktree_id": row.id,
        "project_id": row.project_id,
        "project": row.project,
        "slot": row.number,
        "node_id": str(row.node_id),
        "node_name": row.node_name or "",
        "session_id": row.launch_session_id or "",
    }


def task_hold(session, task_id: object) -> Optional[dict[str, Any]]:
    """The hold a task occupies, with the `generation` a caller needs to release it.

    Wider than `live_hold_for_task` (an un-acked hold also occupies a slot), and not to be merged with it.
    Carries `generation` (the release fence) and `worktree_id` (the release key) from the same row."""
    if task_id is None:
        return None
    row = session.execute(_task_hold_select().where(Worktree.task_id == task_id)).first()
    if row is None:
        return None
    return _task_hold_dict(row)


def task_holds(session, task_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """`{task id: task_hold(session, task id)}` for those of `task_ids` holding a slot, in one read: the dispatch
    plan's answer for one project's candidates (`routes/fleet.py::build_dispatch_plan`), which had asked
    `task_hold` once per waiting task per node. A task absent from it holds nothing (`task_hold`'s None).

    Only the asked ids, never the fleet's every hold, so a plan's read stays its candidates' size. A task with
    two `task` rows keeps the first read, as `task_hold`'s `.first()` does. No ids, no statement."""
    wanted = sorted({int(t) for t in task_ids})
    out: dict[int, dict[str, Any]] = {}
    if not wanted:
        return out
    for row in session.execute(
        _task_hold_select().add_columns(Worktree.task_id).where(Worktree.task_id.in_(wanted))
    ).all():
        out.setdefault(row.task_id, _task_hold_dict(row))
    return out


def _task_hold_select():
    """`task_hold`'s statement minus its task filter, so the single and the batched read are one shape."""
    return (
        select(
            Worktree.id,
            Worktree.project_id,
            Project.name.label("project"),
            Worktree.number,
            Worktree.generation,
            Worktree.node_id,
            Node.name.label("node_name"),
            Worktree.launch_session_id,
            Worktree.pid,
            Worktree.state,
        )
        .join(Project, Project.id == Worktree.project_id)
        .join(Node, Node.id == Worktree.node_id)
        .where(Worktree.state == TASK)
    )


def _task_hold_dict(row) -> dict[str, Any]:
    return {
        "worktree_id": row.id,
        "project_id": row.project_id,
        "project": row.project,
        "slot": row.number,
        "generation": row.generation,
        "node_id": str(row.node_id),
        "node_name": row.node_name or "",
        "session_id": row.launch_session_id or "",
        "pid": row.pid,
        "state": row.state,
    }


# ── A session in a `free` slot, as the reaper's read ────────────────────────────────────────────────
# Feeds a destructive order, so every conjunct fails closed: an affirmative `session IS TRUE`; `state =
# 'free'` (no decision accounts for the occupant); a run started before the release
# (`session_since < state_since`, the primary guard against a reopened console or SSH shell) plus
# `console = 'not_attached'` by equality (console claims are best effort); `generation > 0` (ever leased);
# `number > 0` (never the main clone).
_OCCUPIED_FREE_SLOT_SQL = text(
    """
    SELECT w.id AS worktree_id, w.project_id, p.name AS project, w.number AS slot,
           w.node_id AS node_id, n.name AS node_name,
           w.session_id AS session_id, w.session_pid AS session_pid,
           -- THE RELEASE INSTANT ITSELF, not just its age. The node needs the ABSOLUTE stamp to tell a
           -- survivor from a session that started beside it in the same tree: `session_since < state_since`
           -- qualifies the SLOT, and the node then has to pick WHICH of the runs in it the order covers.
           -- Formatted to match `worktree_mechanics.processes.proc_started_at`'s `%Y-%m-%dT%H:%M:%SZ` exactly, so the
           -- two are comparable as strings and neither side parses the other's clock.
           -- BACKED OFF BY THE SKEW MARGIN, at the producer, because this is where the fence is
           -- authored — see `FREE_SLOT_WARRANT_SKEW_SECS` for why an exact boundary is not safe to
           -- compare across two machines' clocks and what the margin costs.
           to_char((w.state_since - make_interval(secs => :skew_secs)) AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS freed_at,
           -- THE RELEASE IS THE LATER SIDE BY CONSTRUCTION now that `session_since < state_since` is a
           -- conjunct, so this reads `state_since` directly rather than a `greatest(...)` over a pair
           -- whose order is already decided. Same number, one fewer thing for a reader to verify.
           EXTRACT(EPOCH FROM (now() - w.state_since)) AS occupied_secs
      FROM worktrees w
      JOIN projects p ON p.id = w.project_id
      JOIN nodes n ON n.id = w.node_id
     WHERE w.state = 'free'
       AND w.number > 0
       AND w.generation > 0
       AND w.console = :console_free
       AND w.session IS TRUE
       AND w.session_since IS NOT NULL
       -- **THE SESSION BEGAN BEFORE THE RELEASE — WHICH IS WHAT "SURVIVOR" ACTUALLY MEANS.** Every
       -- other conjunct describes the slot; this one describes the RUN, and it is the sharpest of them.
       -- `session_since` is edge-triggered and never slides (`fleet_observe.session_since_stamp_statement`
       -- COALESCEs so a run that is still true keeps its ORIGINAL start, and NULLs when the run ends),
       -- and neither `lease` nor `release` writes it. So `session_since < state_since` says exactly:
       -- this run was already going when the slot was freed. That is the prod case (04:01 session,
       -- 04:52 release) and it is the only story this reaper has.
       -- WHAT IT EXCLUDES, PERMANENTLY, is every session that STARTED on an already-free slot — a
       -- console user whose claim failed, a person at an SSH shell, anything. The console conjunct above
       -- only holds while a websocket is ATTACHED (a review lane's finding: on detach it flips to
       -- `not_attached` while `state_since` is already old, so the grace is spent and the reap fires
       -- seconds after somebody closes a tab). This one does not care whether anyone is watching.
       -- FAIL-CLOSED ON THE EDGE: if a survivor's probe goes unjudged and its run is re-stamped after
       -- the release, it stops matching and is never reaped. That is a slot left dirty, which is the
       -- safe direction; the alternative reading kills a session nobody can account for.
       AND w.session_since < w.state_since
       -- A STALE OBSERVATION IS NOT EVIDENCE OF AN OCCUPANT — the same freshness floor the leaked-hold
       -- sweep applies, for the same reason: past it the node's reported occupancy stops being trusted.
       -- **READ EXACTLY WHAT IT PROVES, WHICH IS LESS THAN ITS NAME SUGGESTS.** `observed_at` stamps the
       -- PUBLISH, not the agents probe behind `session`, and the node republishes a cached probe reading
       -- every ~5s however old that reading is. So this conjunct proves the NODE is alive and reporting;
       -- it does not by itself bound the age of the sighting. What bounds that is node-side: a slot the
       -- probe found a session in puts its account on the 5s leased floor
       -- (`jaahd/reconcile/observe.py::observe_slot`), which is a change this same tracker made BECAUSE a free
       -- slot never qualified before and its account fell back to the 3600s full-scan interval. The two
       -- halves are one guarantee and neither is sufficient alone; the kill itself is fenced a third time
       -- on the node, which re-verifies the process's own start time before it signals.
       AND w.observed_at > now() - make_interval(secs => :fresh_secs)
       AND w.state_since < now() - make_interval(secs => :grace_secs)
     ORDER BY p.name, w.number
    """
)


def occupied_free_slots(session, *, grace_secs: float, fresh_secs: float) -> list[dict[str, Any]]:
    """Every `free` slot the node affirmatively reports a live Claude session in, past `grace_secs`.

    The population of `lease_drain._reap_occupied_free_slots`. Its conjuncts (see the SQL) live in the
    statement so none can be loosened in Python; without the console and release-instant fences a human
    whose console failed to claim its slot would be killed within a minute.

    No org floor: like `settled_holds` and `leaked_user_holds`, a fleet-wide system pass with no caller
    to scope to.

    `freed_at` is a fence, not a label: the node warrants every session registered against the path, so
    it re-applies the release instant per session (`jaahd/reconcile/enact_reset.py::_enact_release_slot`), and an
    order without it warrants nothing. `session_id`/`session_pid` name the intended occupant; `node_id` is
    the uuid the intent row wants and `node_name` is for the log."""
    return [
        {
            "worktree_id": r.worktree_id,
            "project_id": r.project_id, "project": r.project, "slot": r.slot,
            "node_id": r.node_id, "node_name": r.node_name,
            "session_id": r.session_id or "",
            "session_pid": r.session_pid,
            "freed_at": r.freed_at or "",
            "occupied_secs": float(r.occupied_secs or 0.0),
        }
        for r in session.execute(
            _OCCUPIED_FREE_SLOT_SQL,
            {
                "grace_secs": grace_secs, "fresh_secs": fresh_secs,
                "skew_secs": FREE_SLOT_WARRANT_SKEW_SECS,
                "console_free": fleet_verdicts.CONSOLE_NOT_ATTACHED,
            },
        ).all()
    ]


# ── Releasing: the signal allowed to end a task hold ──────────────────────────────────────────────────

# The columns and joins every hold query below reads, and the one dict `_hold` builds from them.
_HOLD_SELECT = """
    SELECT w.id AS worktree_id, w.project_id, p.name AS project, w.number AS slot, w.task_id,
           w.node_id::text AS node_id, n.name AS node_name, w.launch_session_id, w.pid, w.state, w.generation
"""
_HOLD_FROM = """
      FROM worktrees w
      JOIN projects p ON p.id = w.project_id
      JOIN nodes n ON n.id = w.node_id
"""


def _hold(r) -> dict[str, Any]:
    return {
        "worktree_id": r.worktree_id,
        "project_id": r.project_id, "project": r.project,
        "slot": r.slot, "task_id": r.task_id, "node_id": r.node_id, "node_name": r.node_name,
        "session_id": r.launch_session_id, "pid": r.pid, "state": r.state,
        "generation": r.generation,
    }


# Slots still held for finished work: a contradiction needing no clock or probe, so safe to act on.
_SETTLED_HOLDS_SQL = text(
    _HOLD_SELECT + _HOLD_FROM + """
      JOIN tasks t ON t.id = w.task_id
     WHERE w.state <> 'free'
       AND t.state = ANY(:terminal)
       -- A retiring hold is released by the node's destroy ack, never here.
       AND w.retiring_at IS NULL
     ORDER BY p.name, w.number
    """
)


def settled_holds(session, terminal_states) -> list[dict[str, Any]]:
    """Every non-`free`, non-retiring hold whose task has already reached a terminal state.

    The store has no expiry, so without this `held_slots` would grow to the whole pool. A hold still
    waiting on a human never appears (its task is not terminal). Carries the launch session and pid for
    the teardown that must name its process."""
    rows = session.execute(_SETTLED_HOLDS_SQL, {"terminal": list(terminal_states)}).all()
    return [_hold(r) for r in rows]


_STOPPED_QUESTION_CLEAN_HOLDS_SQL = text(
    _HOLD_SELECT + _HOLD_FROM + """
      JOIN tasks t ON t.id = w.task_id
     WHERE w.state = 'task'
       AND t.state = 'awaiting_response'
       AND t.question_runner_stopped_at IS NOT NULL
       AND w.observed_at > t.question_runner_stopped_at
       AND w.session IS FALSE
       AND w.console = 'not_attached'
       AND w.probes->>'session_cwd' = 'false'
       AND w.dirty IS FALSE
       AND w.probes->>'unpushed' = '0'
     ORDER BY p.name, w.number
    """
)


def stopped_question_clean_holds(session) -> list[dict[str, Any]]:
    """Every task hold whose runner was stopped on an unanswered question AND whose tree holds nothing to
    lose — the one hold a non-terminal task gives back (operator decision).

    Every conjunct is a positive observation: `dirty IS FALSE` and `unpushed = '0'` (NULL never reads
    clean; the same count `reset_to_base` gate 3 preserves), `session IS FALSE`, `console = 'not_attached'`
    and a false `session_cwd` (nobody the teardown would kill; cf. `idle_stacks`), and `observed_at` newer
    than the stop. The task keeps `awaiting_response` and its questions; an answer leases a fresh slot."""
    rows = session.execute(_STOPPED_QUESTION_CLEAN_HOLDS_SQL).all()
    return [_hold(r) for r in rows]


_LEAKED_USER_HOLDS_SQL = text(
    _HOLD_SELECT + """
         , w.owner_user_id, EXTRACT(EPOCH FROM (now() - w.state_since)) / 3600.0 AS held_hours
    """ + _HOLD_FROM + """
     WHERE w.state = 'user'
       AND w.number > 0
       AND w.state_since < now() - make_interval(secs => :held_secs)
       AND w.console = :console_free
       AND w.session IS FALSE
       AND w.session_id IS NULL
       AND w.observed_at > now() - make_interval(secs => :fresh_secs)
     ORDER BY p.name, w.number
    """
)


def leaked_user_holds(session, *, held_secs: float, fresh_secs: float) -> list[dict[str, Any]]:
    """Every `user` hold that has outlived `held_secs` with NO SESSION IN IT, on a slot the node is
    currently reporting. The rows `lease_drain._release_leaked_user_holds` releases.

    The one release without a recorded decision (the exception to "only a recorded
    decision ends a hold"): a console killed without its `SessionEnd` hook leaves a `user` row nothing ages
    out. Guards, all conjuncts:
    `state = 'user'`; `number > 0`; `console = 'not_attached'` by equality (a `term` shell registers no
    agent session; `fleet_verdicts.CONSOLE_NOT_ATTACHED`); `session IS FALSE`, never `IS NOT TRUE` (NULL
    keeps the hold); a fresh `observed_at`, so silence never releases. A dirty tree is still a leak:
    `release` resets nothing, the following `release_slot` intent does."""
    rows = session.execute(
        _LEAKED_USER_HOLDS_SQL,
        {
            "held_secs": float(held_secs),
            "fresh_secs": float(fresh_secs),
            # Bound from `fleet_verdicts`, the one home for which console value licenses a reclaim.
            "console_free": fleet_verdicts.CONSOLE_NOT_ATTACHED,
        },
    ).all()
    return [
        {**_hold(r), "owner_user_id": r.owner_user_id, "held_hours": float(r.held_hours or 0.0)}
        for r in rows
    ]


# ── The read-time expression that replaces a stored column ────────────────────────────────────────────


def idle_hours_expr(last_active=None, *, param_name: str = "unknown_age_sentinel"):
    """Whole hours since `last_active`, computed at read time.

    1. An empty `last_active` is the unknown-age sentinel (past every threshold), never NULL, which would
       invert `stack_ops.select_idle_stacks`' `>= threshold`; hence the bound `COALESCE`.
    2. `FLOOR`, never a bare cast: Postgres rounds `numeric` to `int` half-up.
    """
    column = Worktree.last_active if last_active is None else last_active
    return func.coalesce(
        func.floor(func.extract("epoch", func.now() - column) / 3600),
        bindparam(param_name, UNKNOWN_AGE_SENTINEL, type_=Integer, unique=False),
    ).cast(Integer)


# ── The sighting feed ─────────────────────────────────────────────────────────────────────────────────

def mark_seen_live(
    session, node_id: str, slots: Iterable[tuple[int, int]], *, now: Optional[datetime] = None
) -> int:
    """Record that a live session was observed in each of ``slots``. Returns rows touched.

    `node_id` is the node uuid; pairs carry ids, not names, so a rename cannot silently zero the update. The only place a sensed fact
    reaches an `OCCUPANCY` column, and it never changes a state. Frees nothing; `last_seen_live` feeds
    `app/task_pipeline_job.py`'s `COALESCE(last_seen_live, state_since)` work-unit check."""
    pairs = [(int(project_id), int(number)) for project_id, number in slots]
    if not pairs:
        return 0
    stamp = now or _now()
    # Built once and reused: `node_ref` resolves the uuid inside the statement.
    node = node_ref(node_id)
    touched = 0
    for project_id, number in pairs:
        result = session.execute(
            update(Worktree)
            .where(
                Worktree.node_id == node,
                Worktree.project_id == project_id,
                Worktree.number == number,
                Worktree.state != FREE,
            )
            .values(last_seen_live=stamp)
        )
        touched += result.rowcount or 0
    session.commit()
    return touched

