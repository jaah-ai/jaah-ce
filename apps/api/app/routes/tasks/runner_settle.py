"""Runner attempt settlement: end_attempt, charges, reassert/resume, reclaim and node-fault refunds."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import case, func, select, true, update
from sqlalchemy.orm import Session

from app import (
    actors, agent_session_lifecycle, agent_sessions, launch_failures, merge_lane_state, runner_terminations, slot_destroy,
    slot_state,
)
from app.events import emit_console_event
from app.models.agent_sessions import AgentSession
from app.models.audit import ActivityEvent
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.persona_registry import (
    DONE_CRITERION_ARTIFACT_WITH_CODERS,
    DONE_CRITERION_OPERATOR,
    PERSONA_DONE_CRITERION,
)
from app.pr_changes import record_pr_provenance
from app.pr_stats import record_pr_stats
from app.task_artifacts import visible_artifact_clause
from app.task_settle import NO_BUILD_OUTCOME_REASON
from app import carryover, edition_features, task_prs
from app.task_lifecycle import (
    ACTIVE,
    AWAITING_RESPONSE,
    DONE,
    READY,
    SYSTEM_FAILED,
    _ANALYSIS_ARTIFACT_SETTLE_STATES,
)
from app.user_activity import record_task_note
from app.routes.tasks.policy import MERGED_PR_CRITERION_PERSONAS, _ANALYSIS_PERSONAS, fleet_dispatch_cap
from app.routes.tasks.slots import kill_runner_keeping_hold, send_minted_intents
from app.routes.tasks.dispatch import _RUNNER_BINDING_CLEARED
from app.routes.tasks.epics import _epic_filed_count, _maybe_roll_up_parent, _reevaluate_epic_child_failure

if TYPE_CHECKING:
    from app.lane_forge_auth import CredentialSource

log = logging.getLogger(__name__)


def end_attempt(
    task_id: int,
    session: Session,
    *,
    reason: Optional[str] = None,
    final: bool = False,
    sources: tuple[str, ...] = (ACTIVE,),
    guards: tuple[Any, ...] = (),
    cause: Optional[str] = None,
    node: Optional[str] = None,
    note_detail: Optional[str] = None,
    note_vm: Optional[str] = None,
    note_account: Optional[str] = None,
) -> Optional[str]:
    """The single home for what happens when a dispatch attempt ENDS — the reverse of the `ready → active`
    edge, and the only place the re-dispatch ceiling is evaluated. Returns the landed state, or None.

    `dispatch_attempts >= fleet_dispatch_cap()` ⇒ `system_failed`, else `ready`, decided by a CASE in the
    same atomic UPDATE. `final=True` forces terminal. Guarded on `state IN sources` (default `(ACTIVE,)`),
    making a double report idempotent; the break path widens `sources` so a relaunch still spends budget.

    The runner is killed on both branches; the slot is released on neither. A retryable end resumes into
    the same slot. The terminal release belongs to the drain (`lease_drain._release_finished`, waiting on
    `fleet_verdicts.slots_awaiting_a_kill`): the node's kill is fenced on the slot still being bound to
    this task (`jaahd/reconcile/enact_signal.py::_enact_kill_runner`), so releasing here would cancel the kill and
    let a second runner into the same tree. Both writes ride this transaction.

    `cause` puts the end on the operator's timeline: a `task_note:runner_terminated` row written before
    the transition, so it sorts above it. The caller judges whether a runner was running, so callers
    that never launched one (`_fail_unplaceable_tasks`) pass no cause. `node`, `note_detail`, `note_vm` and
    `note_account` ride that note.

    The attempt's session row (Audit > Sessions) ends here too, on both branches, since the runner is killed on
    both: a MicroVM task has no slot, so the drain's release would never end it. Every other row of
    the task still `running` ends with it, carrying `note_detail`, so no session outlives a failed attempt."""
    now = datetime.now(timezone.utc)
    # The cap is the task's own org's; `org_id` never changes, so reading it ahead of the UPDATE is race-free.
    # Every caller is bound to the task's org or the operator scope; an unbound one reads `None` here (the
    # read filter), so say so rather than leave a runnerless task silently `active`. The runner session is read
    # here because the UPDATE clears it (`_RUNNER_BINDING_CLEARED`).
    current = session.execute(
        select(Task.org_id, Task.runner_session_id).where(Task.id == task_id)
    ).first()
    org = current.org_id if current is not None else None
    if org is None:
        log.warning("task %s: end_attempt found no task in its scope; nothing ended", task_id)
        return None
    # Read at the moment the attempt ends, so an operator's edit changes the very next end.
    cap = fleet_dispatch_cap(org=org)
    spent = true() if final else Task.dispatch_attempts >= cap
    values: dict[str, Any] = {
        "state": case((spent, SYSTEM_FAILED), else_=READY),
        "github_synced_at": now,
        **_RUNNER_BINDING_CLEARED,
    }
    if reason is not None:
        values["awaiting_response_body"] = case((spent, reason), else_=Task.awaiting_response_body)
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state.in_(sources), *guards)
        .values(**values, **actors.task_actor_values(session))
        .returning(Task.id, Task.state)
    ).first()
    if row is None:
        return None
    _, final_state = row
    # A lane run's held rows go back to the queue with its end, in this transaction (L10).
    merge_lane_state.lane_task_left_active(session, task_id)
    # The task left the live states: a carry-over ends here, and any re-dispatch goes to its own org's nodes.
    carryover.close(session, task_id=task_id)
    # The killed runner's session row closes with it. A break cause is itself an end reason; any other end
    # (a reported failure, an unplaceable task) is `abandoned`: it stopped without settling. `note_end` keeps a
    # first reason and moves only a `running` row.
    end_reason = cause if cause in agent_sessions.end_reasons() else agent_sessions.END_REASON_ABANDONED
    agent_session_lifecycle.note_end(
        session, agent_session_id=current.runner_session_id or "", end_reason=end_reason, end_detail=note_detail,
    )
    # A relaunch within the attempt, or a session the task never bound, would otherwise stay `running` on
    # Audit > Sessions after the runner was killed.
    agent_session_lifecycle.note_task_sessions_end(
        session, task_id=task_id, end_reason=end_reason, end_detail=note_detail,
    )
    # Same transaction as the state write. The kill is the only slot write here, on both branches; see the
    # docstring for why releasing now would cancel it.
    _, issued = kill_runner_keeping_hold(
        task_id, session,
        # A terminal landing is a fresh decision: the re-mint cooldown targets a loop re-deciding the same thing,
        # and the drain waits on this kill — a suppressed mint would let it free the slot with no kill in flight.
        # The retryable branch is exactly the repeating end the cooldown is for.
        fresh_decision=(final_state == SYSTEM_FAILED),
        reason=(
            f"task {task_id} spent its last attempt ('{final_state}') — stop that runner; the slot goes "
            "back to the pool once this kill has been enacted"
            if final_state == SYSTEM_FAILED else
            f"task {task_id}'s dispatch attempt ended ('{final_state}') — stop that runner. The slot "
            "stays bound to the task for the re-dispatch that resumes it"
        ),
    )
    # Before the transition emit and in this same transaction (see the docstring's `cause` paragraph).
    if cause is not None:
        runner_terminations.record(session, task_id, cause, node=node, detail=note_detail, vm=note_vm,
                                   account=note_account)
    emit_console_event(session, entity="task", id=task_id, action=final_state)
    session.commit()
    send_minted_intents(issued)  # AFTER the commit — see `send_minted_intents`.
    if final_state == SYSTEM_FAILED:
        log.warning(
            "task %s attempt ended and every attempt is spent (cap %s) -> system_failed (reason=%s)",
            task_id, cap, reason,
        )
        # Terminal, so the parent epic may now fail — after this commit, so the rollup reads a fresh count.
        _maybe_roll_up_parent(task_id, session)
    else:
        # The cause, not `reason`: a reason is worded for the spent branch, where it is the row's last word.
        log.info(
            "task %s attempt ended with attempts remaining -> ready for re-dispatch (cause=%s)",
            task_id, cause,
        )
    return final_state


def charge_unannounced_launch(
    task_id: int, session: Session, *, detail: str, commit: bool = True, node: Optional[str] = None,
    account: Optional[str] = None,
) -> Optional[str]:
    """Charge ONE dispatch attempt for a launch that was acked and never announced itself, and end the
    task when that was its last one. Returns the landed state (`ready` when budget remains,
    `system_failed` when it is spent) or None when the task is no longer `ready`.

    Such a launch has neither the claim edge nor an end edge, so the drain's re-dispatch past
    `fleet_decide.ACKED_REDISPATCH_GRACE_SECONDS` looped with the counter never moving. The drain calls
    this before relaunching (`lease_drain::_take_for_dispatch`), bounding it by `fleet_dispatch_cap()`.
    Each charge writes a `launch_unannounced` row via `runner_terminations.record`, so the loop is visible.

    Guarded on `state = 'ready'`, but exactly-once is the caller's: `_take_for_dispatch` passes
    `commit=False` and the generation-fenced `slot_state.redispatch` commits or rolls it back. The
    terminal arm delegates to `end_attempt(final=True, sources=(READY,))`; if that matches no row, the
    increment is rolled back and None returned. `node` (the launch's node uuid) and `account` (the account id it
    spent) ride the note, for `app/launch_failures.py`'s per-node count and its cross-account refund, which is
    given here (`_refund_cross_account_launch`) before the ceiling is judged."""
    due = node is not None and launch_failures.cross_account_refund_due(session, task_id, node, account)
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == READY)
        .values(dispatch_attempts=Task.dispatch_attempts + 1, **actors.task_actor_values(session))
        .returning(Task.dispatch_attempts, Task.org_id)
    ).first()
    if row is None:
        return None
    attempts, org = row
    if due and node is not None and _refund_cross_account_launch(
            task_id, session, sources=(READY,), node=node, account=account):
        attempts -= 1
    cap = fleet_dispatch_cap(org=org)
    if attempts >= cap:
        landed = end_attempt(
            task_id, session, reason=detail, final=True, sources=(READY,),
            cause=runner_terminations.CAUSE_LAUNCH_UNANNOUNCED, node=node, note_account=account,
        )
        if landed is None:
            session.rollback()
        return landed
    runner_terminations.record(session, task_id, runner_terminations.CAUSE_LAUNCH_UNANNOUNCED, node=node,
                               account=account)
    if commit:
        session.commit()
    log.warning(
        "task %s: %s (attempt %s of %s)", task_id, detail, attempts, cap,
    )
    return READY


def _refund_cross_account_launch(
    task_id: int, session: Session, *, sources: tuple[str, ...], node: str, account: Optional[str],
) -> bool:
    """Give back the attempt a failed launch on `node` is charging, the node having already failed the task under
    another account (`launch_failures.cross_account_refund_due`, asked by the caller), and say so on the timeline.
    Reuses `_refund_node_fault_attempt`'s guard and cap (`node_fault_refund_cap`). Does NOT commit."""
    if not _refund_node_fault_attempt(task_id, session, sources=sources):
        return False
    runner_terminations.record(session, task_id, runner_terminations.CAUSE_LAUNCH_REFUNDED, node=node,
                               account=account)
    log.warning("task %s: node %s failed its launch under a second account — attempt refunded", task_id, node)
    return True


def charge_unrunnable_agent(task_id: int, session: Session, *, reason: str) -> Optional[str]:
    """Spend ONE dispatch attempt on a `ready` task whose agent cannot run (DD19), naming why. Returns the
    landed state (`ready` with budget left, `system_failed` once it is spent) or None when the task is no
    longer `ready`.

    A refused agent never leases a slot, so it has neither the claim edge that counts an attempt nor a runner
    to fail: the attempt is counted here and ended through `end_attempt` from `ready`, exactly like any runner
    failure (AC-15), so it reaches `system_failed` at the cap instead of waiting silently. `reason` is the
    `[agent_unrunnable] <sentence>` string (`agent_resolution.refusal_reason`); it rides a
    `task_note:agent_unrunnable` row on every charge and `awaiting_response_body` on the terminal one.
    Commits."""
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == READY)
        .values(dispatch_attempts=Task.dispatch_attempts + 1, **actors.task_actor_values(session))
        .returning(Task.id)
    ).first()
    if row is None:
        session.rollback()
        return None
    record_task_note(session, task_id, "agent_unrunnable", reason, {"reason": reason})
    landed = end_attempt(task_id, session, reason=reason, sources=(READY,))
    if landed is None:
        session.rollback()
    return landed


def reassert_active(task_id: int, session: Session, *, session_id: str, self_waking: bool = False) -> str:
    """The single home for a runner's own "I am still working" heartbeat — the mirror of `end_attempt`.

    A `StopFailure` break may be false (Claude Code retries absorbed it), and `Stop` fires every turn,
    so the next turn undoes it. Sources: `ACTIVE` (a `session_id` mismatch means another attempt owns
    it) and `READY` (the self-heal). `PAUSED` is not a source: an operator Stop must not be undone by a
    session that survived the kill.

    The fence is the hold's launch session (`slot_state.task_hold`), since `end_attempt` clears
    `runner_session_id`; a session the slot no longer names answers `no-hold`. For an operator-criterion
    persona a turn boundary means "your move": the row parks `active -> awaiting_response`
    (`awaiting-operator`); `resume_from_console` is the other half. Unless `self_waking`: the turn ended with
    background work in flight that will start the next turn itself, so nobody owes it a message yet and the row
    stays `active` (`self-waking`). Parking there flipped a busy chat twice per notification, each flip a task
    event that woke the crons and refetched every open board. Any in-flight work counts, by the
    owner's decision: a chat holding a never-ending shell or a recurring loop stays `active` until it is gone."""
    task = session.get(Task, task_id)
    if task is None:
        return "not-found"
    if task.state == ACTIVE:
        is_chat = PERSONA_DONE_CRITERION.get(task.persona) == DONE_CRITERION_OPERATOR
        if task.runner_session_id == session_id and is_chat and self_waking:
            return "self-waking"
        if task.runner_session_id == session_id and is_chat:
            # ── An interactive persona parks at its turn boundary ─────────
            # For a chat a Stop means the turn is over and the next move is the operator's — exactly
            # `awaiting_response`, rendered as Needs attention. Not a forbidden machine park: a real person owes the
            # next message, `resume_from_console` leaves the state, and `complete` is the only edge to `done`. Keyed on
            # `DONE_CRITERION_OPERATOR`, not `advisor`. Guarded on `state == ACTIVE` in the UPDATE, so a Stop or
            # Complete landing in between wins.
            landed = session.execute(
                update(Task)
                .where(Task.id == task_id, Task.state == ACTIVE)
                .values(state=AWAITING_RESPONSE, **actors.task_actor_values(session))
            )
            if landed.rowcount == 0:
                # Someone moved the row out of `active`; their decision stands and nothing is written.
                session.rollback()
                return "wrong-state"
            emit_console_event(session, entity="task", id=task_id, action=AWAITING_RESPONSE)
            session.commit()
            return "awaiting-operator"
        if task.runner_session_id == session_id:
            return "already-active"
        log.info(
            "heartbeat for task %s (session %s) ignored — active under a different session (%s); that "
            "attempt owns the row now",
            task_id, session_id, task.runner_session_id,
        )
        return "superseded"
    if task.state != READY:
        return "wrong-state"
    # The slot the task still owns; its launch identity is the fence. No hold means the CP no longer
    # recognises this dispatch.
    hold = slot_state.task_hold(session, task_id)
    if hold is None or hold["session_id"] != session_id:
        return "no-hold"
    now = datetime.now(timezone.utc)
    # Mirrors apps/jaahd/fleet_launch.py::slot_ref's fleet-hold case (the API and the node daemon share no imports);
    # safe because a fleet lease's `slot` is always a plain int.
    worktree_ref = f"{hold['project']}-{hold['slot']}"
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == READY)
        .values(
            state=ACTIVE,
            runner_session_id=session_id,
            worktree_ref=worktree_ref,
            github_synced_at=now,
            # A heartbeating session is a LIVE runner — `_active_dispatch_values`' reason for the claim.
            question_runner_stopped_at=None,
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if row is None:
        session.rollback()
        log.warning(
            "heartbeat for task %s (session %s) matched its slot's launch identity but the task left "
            "'ready' before it could be flipped back — rolled back, leaving it to whoever moved it",
            task_id, session_id,
        )
        return "race"
    emit_console_event(session, entity="task", id=task_id, action=ACTIVE)
    session.commit()
    log.warning(
        "task %s heartbeat from session %s undid a false death — reasserted active from 'ready' on %s#%s "
        "(a Stop fired after the StopFailure that ended the attempt; the session never actually died, and "
        "the slot it is working in was still its own)",
        task_id, session_id, hold["project"], hold["slot"],
    )
    return "reasserted"


def resume_from_console(task_id: int, session: Session, *, session_id: str) -> str:
    """Un-park a chat the operator has just spoken to: `awaiting_response -> active`, driven by the
    runner's own UserPromptSubmit hook. The other half of `reassert_active`'s park branch.

    Outcomes: `not-found`, `not-a-chat`, `already-active`, `wrong-state`, `superseded`, `resumed`; only
    the last writes. Only `DONE_CRITERION_OPERATOR` personas: elsewhere `awaiting_response` is a runner
    frozen on a question, resumed by `answer` with the answer.

    Fenced on `runner_session_id` (a parked chat keeps it): a prompt from any other session is
    `superseded`, or the row would read `active` under a session nobody dispatches to. `already-active`
    is ordinary: the hook fires on every prompt."""
    task = session.get(Task, task_id)
    if task is None:
        return "not-found"
    if PERSONA_DONE_CRITERION.get(task.persona) != DONE_CRITERION_OPERATOR:
        return "not-a-chat"
    if task.runner_session_id != session_id:
        # Before the state test: a report from a session this row does not name describes the caller, not the row.
        return "superseded"
    if task.state == ACTIVE:
        return "already-active"
    if task.state != AWAITING_RESPONSE:
        # Someone else's decision about this row; a keystroke must not undo it (notably an operator Stop).
        return "wrong-state"
    landed = session.execute(
        update(Task)
        # Both guards restated in the UPDATE: a Stop or re-dispatch can land between the reads and here.
        .where(
            Task.id == task_id,
            Task.state == AWAITING_RESPONSE,
            Task.runner_session_id == session_id,
        )
        # `awaiting_response_body` is cleared: for a chat, typing is how the CP's wedged-dispatch question
        # resolves (`answer` refuses chats, `_refuse_chat_answer_door_or_409`). Left set, the crash reclaimer
        # would read it as an unresolved gate and refuse to restart the chat, silently, for ever.
        .values(state=ACTIVE, awaiting_response_body=None, **actors.task_actor_values(session))
    )
    if landed.rowcount == 0:
        session.rollback()
        return "wrong-state"
    emit_console_event(session, entity="task", id=task_id, action=ACTIVE)
    session.commit()
    return "resumed"


def node_fault_refund_cap(*, org: UUID) -> int:
    """How many node-fault attempt refunds ONE task may be granted before node faults are charged to it
    like anything else.

    Equal to the dispatch cap, so the worst case is twice the ordinary ceiling. Bounded, or a node in a
    crash loop would relaunch its tasks for ever. Derived from the dispatch cap rather than a knob of its
    own, so the two cannot drift; read per call, for the task's ORG. See `_refund_node_fault_attempt`."""
    return fleet_dispatch_cap(org=org)


def _refund_node_fault_attempt(
    task_id: int, session: Session, *, sources: tuple[str, ...]
) -> bool:
    """Give TASK_ID back the dispatch attempt a NODE-WIDE runner loss is about to cost it. Returns whether
    a refund was granted. Does NOT commit — the caller's `end_attempt` does, in the same transaction.

    `dispatch_attempts` is the churn backstop; a runner lost to its node's substrate is not churn, so
    one node fault must not spend several tasks' budgets. The evidence rule is single-homed in
    `app/runner_liveness.py`.

    Decrementing here, before `end_attempt` reads it, spares this end its ceiling and nets out the next
    claim's increment; no flag spans transitions. Guarded: `state IN sources` (the caller's own, so a
    row the reclaim refuses gets nothing), `dispatch_attempts > 0`, and refunds spent below the cap.
    One atomic UPDATE, so observers cannot double-refund. A declined `end_attempt` leaves a refund to
    discard (`_discard_unused_refund`)."""
    org = session.scalar(select(Task.org_id).where(Task.id == task_id))
    if org is None:
        return False
    spent = _node_fault_refunds_spent(task_id, session)
    # One read: the dispatch cap and the refund cap are the same knob (`node_fault_refund_cap`).
    refund_cap = node_fault_refund_cap(org=org)
    if spent >= refund_cap:
        log.warning(
            "task %s: its node lost several runners at once, but this row has already been granted %s "
            "node-fault attempt refund(s) (cap %s) — charging this attempt like any other, so the churn "
            "backstop still terminates",
            task_id, spent, refund_cap,
        )
        return False
    row = session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.state.in_(sources),
            Task.dispatch_attempts > 0,
        )
        .values(
            dispatch_attempts=Task.dispatch_attempts - 1,
            # Named writer, as on every Core UPDATE here: otherwise the previous writer's name stays against an
            # advanced `updated_at` (gated by the task-state write-site census). The writer is the CP.
            **actors.task_actor_values(session),
        )
        .returning(Task.dispatch_attempts)
    ).first()
    if row is None:
        return False
    log.warning(
        "task %s: its NODE lost several runners in one observation, so this is a node fault and not this "
        "task's churn — refunding the dispatch attempt (now %s of cap %s, refund %s of %s)",
        task_id, row[0], refund_cap, spent + 1, refund_cap,
    )
    return True


def _discard_unused_refund(task_id: int, session: Session, *, refunded: bool) -> None:
    """Throw away a node-fault refund whose reclaim did NOT land. A no-op when nothing was refunded.

    An orphaned refund is a free attempt on a row nobody reclaimed. `end_attempt`'s per-caller `guards`
    can decline beyond the state check; it then commits nothing, so rolling back here discards the
    refund. Explicit rather than trusting `app/db.py::get_session` not to commit."""
    if not refunded:
        return
    session.rollback()
    log.info("task %s: its reclaim did not land, so the node-fault attempt refund was discarded", task_id)


def _node_fault_refunds_spent(task_id: int, session: Session) -> int:
    """How many times THIS row's runner has already been recorded lost (`RUNNER_LOST_CAUSES`).

    Counted off `app/runner_terminations.py`, the one home for "a runner stopped, and why" (one row per
    reclaim, never removed), rather than a parallel counter column. Conservative: it includes
    non-node-wide `vanished` rows, a cross-account launch refund's own `launch_refunded` row beside its
    `launch_failed` one, and survives a Reset, so the ceiling arrives earlier, never later."""
    return int(
        session.execute(
            select(func.count())
            .select_from(ActivityEvent)
            .where(
                ActivityEvent.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
                ActivityEvent.data["id"].astext == str(task_id),
                ActivityEvent.data["cause"].astext.in_(sorted(
                    runner_terminations.RUNNER_LOST_CAUSES | {runner_terminations.CAUSE_LAUNCH_REFUNDED})),
            )
        ).scalar()
        or 0
    )


class _VanishedTrace(NamedTuple):
    """What the control plane still knows of a runner it found gone: where it ran and what that place last
    said. Every field may be None; a reclaim never fails over a missing trace."""

    node: Optional[str]  # the node's uuid, for the note (`runner_terminations.record(node=...)`)
    node_name: Optional[str]
    vm: Optional[str]  # the VM's uuid, when a VM was the runner
    vm_failure: Optional[str]  # the node's redacted sentence for that VM, when it `failed`
    detail: Optional[str]  # the note's `detail`: the VM's sentence, else the session's recorded end
    account: Optional[str] = None  # the account id the runner spent (`runner_terminations.record(account=...)`)


def _vanished_trace(session: Session, task_id: int) -> _VanishedTrace:
    """Where TASK_ID's vanished runner ran, for its termination note and failure reason, so a reclaim names a
    node and a VM instead of "left no trace". Read before `end_attempt`, which clears the runner binding.

    The node is the runner's session row's (`Task.runner_session_id`, else the task's newest session), else
    its slot's, else its newest VM's. The VM is named only when no static slot carries the task
    (`microvm_lifecycle.last_task_vm`). The sentence is the VM's when it failed, else the session's
    `end_detail` (already redacted by `agent_session_lifecycle.end_session`), but only one written in this
    attempt: a relaunch that resumes the same session id finds the row an earlier attempt already ended, and
    that attempt's detail is not this one's."""
    task = session.execute(
        select(Task.org_id, Task.runner_session_id, Task.github_synced_at).where(Task.id == task_id)
    ).first()
    run = None
    if task is not None:
        # Pinned to the task's org: `sessions.task_id` carries no composite org key (`_live_session`).
        owned = select(AgentSession).where(AgentSession.task_id == task_id, AgentSession.org_id == task.org_id)
        if task.runner_session_id:
            run = session.scalars(
                owned.where(AgentSession.agent_session_id == task.runner_session_id)
            ).one_or_none()
        if run is None:
            run = session.scalars(
                owned.order_by(AgentSession.started_at.desc(), AgentSession.id.desc()).limit(1)
            ).one_or_none()
    vm = None
    vm_failure = None
    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs.
        from app import microvm_lifecycle

        vm = microvm_lifecycle.last_task_vm(session, task_id)
        vm_failure = microvm_lifecycle.failure_of(vm)
    node_id = (
        (run.node_id if run is not None else None)
        or agent_session_lifecycle.node_for_task(session, task_id)
        or (vm.node_id if vm is not None else None)
    )
    # `github_synced_at` is this attempt's latest transition stamp (its claim, question or answer).
    attempt_began = task.github_synced_at if task is not None else None
    ended_earlier = (
        run is not None and run.ended_at is not None and attempt_began is not None and run.ended_at < attempt_began
    )
    end_detail = getattr(run, "end_detail", None) if run is not None and not ended_earlier else None
    # The account the CP leased the slot for, else this attempt's own session's: a launch never sighted running may
    # have no session row, and the newest one may be an earlier attempt's, on another account.
    account_id = session.scalar(
        select(Worktree.leased_account_id)
        .where(Worktree.task_id == task_id, Worktree.leased_account_id.is_not(None))
        .order_by(Worktree.generation.desc(), Worktree.id.desc())
        .limit(1)
    ) or (run.account_id if run is not None and not ended_earlier else None)
    # The name lands in the stored failure reason, which every project `tasks:view` reader sees: a shared node is
    # another org's machine, so only the task's own org's node is named there (the feed names it at read time,
    # `_explain_terminations`).
    own_node = node_id is not None and task is not None and session.scalar(
        select(Node.org_id).where(Node.id == node_id)
    ) == task.org_id
    return _VanishedTrace(
        node=str(node_id) if node_id is not None else None,
        node_name=(agent_session_lifecycle.node_name(session, node_id) or None) if own_node else None,
        vm=str(vm.id) if vm is not None else None,
        vm_failure=vm_failure,
        detail=vm_failure or (end_detail.strip() if isinstance(end_detail, str) and end_detail.strip() else None),
        account=str(account_id) if account_id is not None else None,
    )


def _vanished_reason(trace: _VanishedTrace, *, parked: bool = False) -> str:
    """The failure reason for a vanished runner, every attempt spent: who, where (the node's name only when it is
    the task's org's, `_vanished_trace`), and what that place last said. Short and factual; the next step rides
    only when there is a place to move away from. `parked`: the
    runner was frozen on a question, so no answer could ever have reached it."""
    who = f"the runner on node {trace.node_name}" if trace.node_name else "the runner"
    if parked:
        who += " parked on a question"
    move = " — retry, or move the task to another node" if trace.node or trace.vm else ""
    if trace.vm_failure:
        what = f"{who} vanished: VM {(trace.vm or '')[:8]}… failed ({trace.vm_failure}){move}"
    elif trace.detail:
        what = f"{who} vanished ({trace.detail}){move}"
    else:
        what = f"{who} stopped reporting and its session ended without a cause"
    return f"{what}; every attempt is spent"


def _reclaim_reason(trace: _VanishedTrace, cause: str) -> str:
    """`reclaim_active_task`'s failure reason, every attempt spent: a failed VM names itself whatever the cause."""
    if trace.vm_failure:
        return _vanished_reason(trace)
    if cause == runner_terminations.CAUSE_LAUNCH_FAILED:
        return "the runner was never seen running after its launch, and every attempt is spent"
    if cause == runner_terminations.CAUSE_STALLED:
        from app.runner_liveness import SILENCE_THRESHOLD

        minutes = int(SILENCE_THRESHOLD.total_seconds() // 60)
        return (f"the runner went silent for {minutes} min (no session, heartbeat or transcript write), "
                "and every attempt is spent")
    return _vanished_reason(trace)


def reclaim_active_task(
    task_id: int, session: Session, *, node_fault: bool = False,
    cause: str = runner_terminations.CAUSE_VANISHED,
) -> bool:
    """Hold-based crash reclaim of a STALE `active` row: end the attempt, requeue it. Every persona is
    reclaimable; there is no persona guard (owner's decision).

    `node_fault`: the whole node lost several runners (`app/runner_liveness.py::node_wide_runner_loss`,
    asked by the route), which buys one bounded refund (`_refund_node_fault_attempt`) and nothing else.
    `cause`: one of `runner_terminations.RUNNER_LOST_CAUSES` — `launch_failed` when the node never sighted
    the launch running (`runner_liveness.runner_facts`), `stalled` from the stall detector, else `vanished`. A `launch_failed` on a node that already
    failed the task's launch under another account is refunded too (`launch_failures.cross_account_refund_due`).

    A persona gate would not protect a `merged_pr` row whose runner died after the merge but before
    settling; no CP-side Git read guards that, so it is requeued and its next runner finds its
    work on `develop` (R20: a no-PR terminal reproduces current state). No parent requirement either,
    so top-level analysis rows are reclaimed.

    A crash is an ended attempt, so this delegates to `end_attempt` (ready, or `system_failed` at the
    ceiling). Called only by a sweep that confirmed the runner gone, failing closed. Both
    `task_pipeline_cp.reclaim_active` and `reclaim_active_coder` land here, keeping their own
    `_record_vanished_runner` source labels."""
    if slot_destroy.destroy_pending(session, task_id):
        # The runner is gone because the CP destroyed its slot; that destroy's ack requeues it.
        log.info("task %s not reclaimed: its slot is being destroyed", task_id)
        return False
    # Before `end_attempt`, whose CASE reads the counter this may lower; same transaction, so a refund cannot
    # outlive a reclaim that did not land.
    refunded = node_fault and _refund_node_fault_attempt(task_id, session, sources=(ACTIVE,))
    # A MicroVM task's runner is its VM: a VM the node reported failed (a clone refusal) is the cause. Either
    # way the note names the node and VM, so the operator knows where to look and what to move away from.
    trace = _vanished_trace(session, task_id)
    if (not refunded and cause == runner_terminations.CAUSE_LAUNCH_FAILED and trace.node is not None
            and launch_failures.cross_account_refund_due(session, task_id, trace.node, trace.account)):
        refunded = _refund_cross_account_launch(task_id, session, sources=(ACTIVE,), node=trace.node,
                                                account=trace.account)
    # A lane run gone with the forge's refusal of its credential still open: a re-dispatch would be refused too.
    refused: Optional[CredentialSource] = None
    reason = _reclaim_reason(trace, cause)
    if edition_features.enabled("merge_lane"):
        from app import lane_forge_auth

        refused = lane_forge_auth.unresolved_forge_rejection(session, task_id)
        if refused is not None:
            reason = lane_forge_auth.blocker_reason(refused)
    landed = end_attempt(
        task_id, session,
        # Persisted only on the spent branch (`end_attempt`), hence its tail.
        reason=reason,
        final=refused is not None,
        # `vanished`, not `crashed`: the evidence is the runner's absence (`runner_breaks`' definition).
        cause=cause,
        node=trace.node, note_detail=trace.detail, note_vm=trace.vm, note_account=trace.account,
    )
    if edition_features.enabled("merge_lane"):
        from app import lane_forge_auth

        if landed == SYSTEM_FAILED and refused is not None:
            lane_forge_auth.alert_credential_blocker(session, task_id, refused)
    if landed is None:
        _discard_unused_refund(task_id, session, refunded=refunded)
        return False
    log.info("task %s stale active row reclaimed by the reaper (runner gone) -> %s", task_id, landed)
    return True


def settle_analysis_child_on_artifact(
    task_id: int,
    session: Session,
    *,
    sources: tuple[str, ...] = _ANALYSIS_ARTIFACT_SETTLE_STATES,
) -> bool:
    """Done-on-artifact `active|system_failed → done` settle of a STALE analysis row whose stage output is
    already captured — the robust completion path, the opposite of `reclaim_active_task`.

    No parent requirement: the settle door admits top-level `business_analysis`/`architecture`, so
    otherwise the note was stored and the row wedged `active`. `_maybe_roll_up_parent` returns early on
    a parentless row.

    Guarded on the analysis personas plus a fixed-param EXISTS for the settled-output note.
    `awaiting_response` is excluded from the default `sources` (settling would answer for a human);
    `sources` is overridable for the one runner-driven caller. `plan` is also held to
    `artifact_with_coders`."""
    now = datetime.now(timezone.utc)
    child = session.get(Task, task_id)
    if (
        child is not None
        and PERSONA_DONE_CRITERION.get(child.persona) == DONE_CRITERION_ARTIFACT_WITH_CODERS
        # `plan` is the only `artifact_with_coders` persona and is deliberately NOT
        # operator-pickable, so `child.parent_task_id` is non-NULL on every reachable path here.

        # A stray top-level `plan` row is refused explicitly: the count below would tally every top-level
        # `coder` portfolio-wide, which is normally large. Fail closed on a shape that should not exist.
        and (
            child.parent_task_id is None
            or (
                # A plan settled as nothing-to-build (the settle stamped `NO_BUILD_OUTCOME_REASON`)
                # is a decomposition of zero children, not an append still outstanding.
                child.outcome_no_pr_reason != NO_BUILD_OUTCOME_REASON
                and _epic_filed_count(child.parent_task_id, session, child) < 1
            )
        )
    ):
        return False
    # Fixed-param EXISTS on the settled-output note: not correlated to the outer Task row, so `str(task_id)`
    # and the watermark read off the child row.

    # A soft-deleted note must not satisfy it (`app/task_artifacts.py`); the race is closed by the
    # `Task.state.in_(sources)` guard (Reset leaves `draft`). `task_note:outcome` only, never the dual-read
    # `_SETTLED_OUTPUT_NOTE_KINDS`: this is the settle gate, paired with the kind every settle writes.
    has_stage_output = (
        select(ActivityEvent.id)
        .where(
            ActivityEvent.kind == "task_note:outcome",
            ActivityEvent.data["id"].astext == str(task_id),
            visible_artifact_clause(
                ActivityEvent.created_at,
                child.artifacts_hidden_before if child is not None else None,
            ),
        )
        .exists()
    )
    # Read before the UPDATE syncs the row to `done`. `sources` admits `system_failed`, so this settle is
    # another way a child leaves `system_failed`, and every such exit re-evaluates the epic (owner
    # decision); otherwise the epic stays failed with nothing failed under it and can never roll up.
    recovered_a_failed_child = child is not None and child.state == SYSTEM_FAILED
    recovered_childs_parent_id = child.parent_task_id if child is not None else None
    if not _settle_done(
        session, task_id,
        Task.persona.in_(_ANALYSIS_PERSONAS), Task.state.in_(sources), has_stage_output,
        now=now,
    ):
        return False
    log.info(
        "task %s stale analysis row SETTLED active|system_failed->done on captured settled-output "
        "artifact (done-on-artifact; github_synced_at=%s)",
        task_id, now,
    )
    # Before the rollup (`complete_task`'s reason): a still-failed epic would fail `_try_complete_epic`'s
    # `state == ACTIVE` guard.
    if recovered_a_failed_child:
        _reevaluate_epic_child_failure(recovered_childs_parent_id, session)
        session.commit()
    # Terminal-transition rollup, mirroring the webhook done path: runs AFTER the child's own commit so it
    # reads a fresh sibling count. Guarded epic-only inside the helper.
    _maybe_roll_up_parent(task_id, session)
    return True


def _settle_done(session: Session, task_id: int, *where: Any, now: datetime) -> bool:
    """The `→ done` UPDATE the runner-side settles share: `task_id` under `where` (the settle's own race
    guard), `dispatch_attempts` reset, the runner binding cleared, the console told, committed. False when
    the guard refused, so the caller logs and rolls up only a row it settled. An empty `where` is refused:
    an unguarded `→ done` is never a settle."""
    if not where:
        raise ValueError("_settle_done needs the caller's own race guard")
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, *where)
        .values(
            state=DONE, github_synced_at=now, dispatch_attempts=0, **_RUNNER_BINDING_CLEARED,
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if row is None:
        return False
    emit_console_event(session, entity="task", id=row[0], action=DONE)
    session.commit()
    return True


def settle_coder_on_merge(
    task_id: int, pr_url: str, session: Session, *, sources: tuple[str, ...] = (ACTIVE,),
    pr_stats: Any = None, pr_provenance: Any = None, attestation: Any = None,
) -> bool:
    """Done-on-merge `→ done` settle of a build child that delivered a merged PR — the merged-PR analogue
    of `settle_analysis_child_on_artifact`, guarded on `MERGED_PR_CRITERION_PERSONAS` alone.

    `sources` is widened only by the settle route, admitting `system_failed` so a runner declared dead
    mid-delivery can still record its PR (`persona_registry.settle_sources`). A runner's `/settle`
    settles because its charter polls the PR until MERGED; the compliance-group cascade
    (`routes/agent/task_terminal.py::_settle_compliance_group`) rests on the same charter. `merged_at` is the settle
    instant, within a poll interval of the merge.

    `pr_stats` is the runner's `SettleIn.pr_stats`, stored in this same transaction (`app/pr_stats.py`); None
    writes no row, which is how the compliance-group mirrors avoid duplicating their group's. `pr_provenance`
    (`SettleIn.pr_provenance`, `app/pr_changes.py`) is stored the same way, and `attestation` (the settle's
    `app/task_prs.py::Attestation`) marks every PR it attests `merged`."""
    now = datetime.now(timezone.utc)
    merged_at_dt = now
    # Read before the UPDATE syncs the row, as in `settle_analysis_child_on_artifact`: a widened `sources`
    # makes this another exit from `system_failed`, which re-evaluates the epic.
    settling = session.get(Task, task_id)
    # Captured as a string: the commit expires the ORM object, so a later read would report `done`.
    settled_from = settling.state if settling is not None else "?"
    recovered_a_failed_child = settled_from == SYSTEM_FAILED
    recovered_childs_parent_id = settling.parent_task_id if settling is not None else None
    row = session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.persona.in_(MERGED_PR_CRITERION_PERSONAS),
            # No parent predicate: a parentless `coder` is a legitimate top-level build unit.
            Task.state.in_(sources),
        )
        .values(
            state=DONE,
            dispatch_attempts=0,
            # The runner's own summary over the canned line. A floor: `settle_task` writes `SettleIn.summary` in
            # the same transaction first, so on that path it is never NULL.
            outcome_summary=func.coalesce(Task.outcome_summary, "delivered — merged to develop"),
            outcome_pr_url=pr_url,
            merged_at=merged_at_dt,
            github_synced_at=now,
            **_RUNNER_BINDING_CLEARED,
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if row is None:
        return False
    emit_console_event(
        session, entity="task", id=row[0], action=DONE,
        transition_extra={"merged_at": merged_at_dt.isoformat()},
    )
    if pr_stats is not None and settling is not None:
        record_pr_stats(
            session, row[0], settling.org_id, session.get(Project, settling.project_id), pr_url, pr_stats
        )
    if pr_provenance is not None and settling is not None:
        record_pr_provenance(
            session, row[0], settling.org_id, session.get(Project, settling.project_id), pr_url, pr_provenance
        )
    if attestation is not None and settling is not None:
        task_prs.record_attested(session, settling, attestation)
    session.commit()
    log.info(
        "task %s SETTLED %s->done on its runner-reported merged PR %s (done-on-merge; "
        "merged_at=%s, github_synced_at=%s)",
        row[0], settled_from, pr_url, merged_at_dt, now,
    )
    # Before the rollup (`complete_task`'s reason): a still-failed epic would fail `_try_complete_epic`.
    if recovered_a_failed_child:
        _reevaluate_epic_child_failure(recovered_childs_parent_id, session)
        session.commit()
    _maybe_roll_up_parent(row[0], session)
    return True


def settle_coder_no_pr(task_id: int, session: Session) -> bool:
    """Done-on-no-PR `active → done` settle of a child that legitimately closed WITHOUT a PR (duplicate /
    already-shipped / no-code-needed) — the no-PR twin of `settle_coder_on_merge`, called on the runner's
    own `no_pr` settle. Idempotent (`state == ACTIVE` is the race guard); also requires
    `outcome_no_pr_reason IS NOT NULL`, and `outcome_pr_url` stays NULL for `_child_rollup_ready`."""
    now = datetime.now(timezone.utc)
    if not _settle_done(
        session, task_id,
        Task.persona.in_(MERGED_PR_CRITERION_PERSONAS),
        # NO PARENT PREDICATE, for the same reason as its sibling above.
        Task.state == ACTIVE,
        Task.outcome_no_pr_reason.is_not(None),
        now=now,
    ):
        return False
    log.info(
        "task %s SETTLED active->done on its runner-declared legitimate no-PR terminal "
        "(done-on-no-PR; github_synced_at=%s)",
        task_id, now,
    )
    _maybe_roll_up_parent(task_id, session)
    return True
