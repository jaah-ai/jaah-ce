"""Slot intents and holds reported by the node: intent ack, launch claim and slot holder."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Float, bindparam, func, select, text, update
from sqlalchemy.orm import Session

from app import (
    actors,
    agent_providers,
    entity_shape,
    fleet_observe,
    fleet_verdicts,
    live_task_edits,
    node_capacity,
    node_credentials,
    project_identity,
    runner_terminations,
    slot_destroy,
    slot_mutate,
    slot_state,
    slot_title,
)
from app.clock import utcnow
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db_async import DbDep, on_session, release, run_blocking_in_session
from app.events import emit_console_event
from app.node_clone import account_slug_from_dir
from app.node_identity import node_ref, resolve_node_id
from app.models.tasks import Task
from app.models.worktrees import SlotIntent, Worktree
# Task transitions go through the watermark-gated helpers; the state machine single-homes in routes/tasks/.
from app.routes.tasks import checkout_gate
from app.routes.tasks.awaiting import (
    await_response_on_wedged_dispatch,
    rearm_undelivered_answer,
    resume_dispatched_task_on_answer,
)
from app.routes.tasks.runner_settle import end_attempt
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, READY
from app.user_activity import record_task_note
from app.entity_shape import get_or_404
from app.routes.agent.auth import _check_token, _wire_project, caller_node_ref
from app.fleet_verdicts import SLOT_INTENT_RETIRING_OUTCOMES
from app.routes.agent.pool_tick import NODE_AUTHORED_OUTCOMES

router = APIRouter(prefix="/api/agent", tags=["agent"])
log = logging.getLogger("agent-routes")


class SlotIntentAckIn(BaseModel):
    outcome: str
    # Bounded like every sibling text field: it becomes `refusal_key`, and on a park is rendered whole
    # into `Task.awaiting_response_body`.
    detail: Optional[str] = Field(default=None, max_length=8_000)
    # The launch ack fence, echoed from the intent payload: both needed (`generation` alone admits a
    # re-leased dispatch's ack, `dispatch_id` alone a superseded duplicate; `slot_state.record_launch`).
    generation: Optional[int] = None
    dispatch_id: Optional[str] = None
    # The launch identity, reported here rather than a tick later. `pid_start_time` is compared for equality
    # and canonicalised on ingest (`fleet_observe.normalise_proc_start`). `""` → None, never a match.
    pid: Optional[int] = None
    pid_start_time: Optional[str] = None

    @field_validator("pid_start_time", mode="after")
    @classmethod
    def _canonical_proc_start(cls, value: Optional[str]) -> Optional[str]:
        return fleet_observe.normalise_proc_start(value)
    session_id: Optional[str] = None
    # `destroy_slot`'s outcome (`app/slot_destroy.py` names and validates the shape); absent on every other verb.
    report: Optional[dict[str, Any]] = None


class SlotIntentAckOut(BaseModel):
    id: int
    outcome: str
    # True iff this ack RETIRED the intent; False means the node is handed it again next tick.
    retired: bool
    # True iff the launch identity was written. False on non-launch and fenced acks (superseded dispatch);
    # the intent is still acked.
    launch_recorded: bool = False


# Identifies an older node's gate-3 (unpushed-commits) reset refusal in its text; a current node never sends
# one. Fragile, but a miss only offers fewer options.
_UNPUSHED_REFUSAL_MARKER = "absent from every origin ref"


# Identifies a refusal that is contention, not a fault: another project's network git op held the node's one
# credential-declaration lock past its wait (an older node's mirror refresh, "clone-busy: "). A first clone
# of a large repository holds it for minutes, so sibling claims refused this way are never charged to the park
# budget; they back off once and retry. A miss only charges contention like a fault, as before.
# Anchored: the marker opens the node's text, after at most its own fixed lead-in ("claim mechanics refused: "),
# so git stderr or a name later in a fault's detail cannot exempt it.
_CLONE_BUSY_REFUSAL = re.compile(r"^(?:[a-z][a-z _-]{0,60}: )?clone-busy: ")


def _is_clone_busy(detail: Optional[str]) -> bool:
    return bool(_CLONE_BUSY_REFUSAL.match(detail or ""))


def _is_uncharged(detail: Optional[str]) -> bool:
    """Contention for the clone lock, or a node at its session cap (`app/node_capacity.py`): a wait, never a
    fault, so neither spends the park budget nor notes a refused dispatch."""
    return _is_clone_busy(detail) or node_capacity.is_capacity_wait(detail) or (detail or "").startswith(
        fleet_verdicts.RUNNER_BUSY_PREFIX)


def _may_park(row: SlotIntent) -> bool:
    """May a spent retry budget retire this intent, or must the row stay pending and keep trying?

    Parking suits verbs the CP re-plans. A `deliver_answer` is re-minted only once retired (the sweep re-sends
    the rounds `awaiting.rearm_undelivered_answer` re-arms), so it keeps trying until its age bound; its refusals
    are transient sensing, and the terminal case is `refused_stale` (arm 3 of
    `fleet_verdicts.purge_unexecutable_intents`). Unparked rows
    keep capped backoff, bounded at `UNEXECUTABLE_INTENT_MAX_AGE_SECS`, enforced here too since the purge
    and the budget run on different clocks. Both measure off `created_at`; an un-timestamped row parks on
    the plain budget.
    """
    if row.verb != fleet_verdicts.VERB_DELIVER_ANSWER:
        return True
    created = row.created_at
    if created is None:
        return True
    # NAIVE MEANS UTC: the column is `timestamptz` and every writer is `func.now()`; reading local would
    # skew the age by the host's offset.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_secs = (utcnow() - created).total_seconds()
    return age_secs >= fleet_verdicts.UNEXECUTABLE_INTENT_MAX_AGE_SECS


# The outcomes that MEAN "a node declined this dispatch" — not every non-retiring outcome: `failed` is a
# fault report, not a refusal (`_note_dispatch_refusal`).
_NOTED_REFUSAL_OUTCOMES = (
    fleet_verdicts.OUTCOME_REFUSED_PRECONDITION,
    fleet_verdicts.OUTCOME_REFUSED_STALE,
)


# Identifies the node's missing-integration-branch refusal (`worktree_mechanics.core.missing_base_refusal`: "integration
# branch '<name>' does not exist on origin and could not be created off the repository's default branch — …"). A
# match puts `fix: DISPATCH_FIX_INTEGRATION_BRANCH` on the note, which the console's Activity tab renders as a link
# to the project's settings; a miss only drops the link. Matched on the unredacted reason.
_MISSING_BRANCH_REFUSAL_MARKER = "does not exist on origin and could not be created off the repository's default branch"


# A roster miss (`apps/jaahd/reconcile/execute.py::execute_intent`): the node has not yet picked up a slot the control plane just
# made, and the intent re-delivers on its next pass. Transient, so it is not put on the task.
_TRANSIENT_ROSTER_REFUSAL_MARKER = "is not in this pass's roster"


# The `fix` value the console keys its link off (`pages/taskDetail/ActivityLog.tsx::DISPATCH_FIX_INTEGRATION_BRANCH`).
DISPATCH_FIX_INTEGRATION_BRANCH = "integration_branch"


def _refused_dispatch_is_dead(session: Session, row: SlotIntent, payload: dict, task_id: Any) -> bool:
    """Is this refusal about a dispatch the task no longer has — so a note saying "did not start, stays
    queued" would be false?

    Dead when the task has moved to another project since, when the slot's hold no longer carries this
    dispatch (released by a Reset/Delete/move, or re-leased), or when the dispatch already launched its
    runner (the node re-enacting an order it ran). An unresolvable slot or a payload without a dispatch
    identity proves nothing, so the refusal is still noted."""
    task_project_id = session.scalar(select(Task.project_id).where(Task.id == int(task_id)))
    if task_project_id is not None and task_project_id != row.project_id:
        return True
    dispatch_id = str(payload.get("dispatch_id") or "")
    if not dispatch_id:
        return False
    hold = session.execute(
        select(Worktree.state, Worktree.task_id, Worktree.dispatch_id, Worktree.generation,
               Worktree.launch_session_id)
        .where(Worktree.node_id == row.node_id, Worktree.project_id == row.project_id,
               Worktree.number == int(row.number))
    ).first()
    if hold is None:
        return False
    generation = payload.get("generation")
    return (
        hold.state != slot_state.TASK
        or hold.task_id != int(task_id)
        or hold.dispatch_id != dispatch_id
        or (generation is not None and hold.generation != int(generation))
        or hold.launch_session_id is not None
    )


def _note_dispatch_refusal(session: Session, row: SlotIntent, outcome: str) -> None:
    """Put a refused dispatch on the task's own record, not only on `slot_intents`.

    `slot_intents` carries no `task_id` on its wire row, so a substituted account was invisible on the task
    page. Fenced on `refusals == 1` (`_account_for_refusal` resets on key change) and on refusal outcomes
    (not `failed`). Called after budgeting, before the caller's commit. Records provider, account slug and
    the node's prose redacted by `fleet_verdicts.redact_node_detail` (served at `tasks:view`). Best effort,
    never fatal; only `lease_slot`.
    """
    if (row.verb != fleet_verdicts.VERB_LEASE_SLOT
            or outcome not in _NOTED_REFUSAL_OUTCOMES
            or (row.refusals or 0) != 1):
        return
    payload = row.payload if isinstance(row.payload, dict) else {}
    task_id = payload.get("task_id")
    if task_id in (None, "") or _TRANSIENT_ROSTER_REFUSAL_MARKER in (row.reason or ""):
        return
    # Absent/empty means the catalogue default (`fleet_verdicts.build_lease_intent`'s node-skew rule).
    value = str(payload.get("provider") or "") or agent_providers.DEFAULT_PROVIDER
    record = agent_providers.by_value(value)
    # `!r` for an unrecognised value, as `lease_drain._provider_label`: the quotes mark a raw value and
    # `repr` escapes control characters out of a line a human reads.
    label = record.label if record is not None else repr(value[:64])
    slug = account_slug_from_dir(payload.get("config_dir")) or ""
    whose = f"{label} account {slug}" if slug else f"a {label} account"
    # What a later pass may do: a model run can be re-leased onto another account, even another vendor's.
    after = "may be offered a different account — possibly a different vendor's — on a later pass"
    launch = payload.get("launch") if isinstance(payload.get("launch"), dict) else {}
    if launch.get("kind") == fleet_verdicts.LAUNCH_KIND_AGENT:
        # An SDK agent runs on no account: name the agent the dispatch was for.
        whose = f"agent {str(launch.get('display_name') or launch.get('agent_id') or '')[:64]!r}"
        after = "is offered again on a later pass"
    try:
        # `ack_slot_intent` already wrote `row.reason`. An empty detail is said outright. Inside the guard: a
        # raising redaction must not take the ack down.
        reason = fleet_verdicts.redact_node_detail(row.reason)
        # Says what was removed as an aside; a line an operator reads once on a card.
        said = f"The node said, with file paths and URL credentials removed: {reason}" if reason else (
            "The node gave no reason.")
        # The cause in words, ahead of the node's own: its raw text alone read as cryptic.
        headline = checkout_gate.headline(row.reason)
        cause = f"{headline} " if headline else ""
        # Add and flush in one savepoint: a flush-time failure would otherwise raise from the caller's commit and
        # drop the ack (then the intent is re-dispatched into a slot with a live runner). Rolling back to the savepoint
        # discards only the note.
        with session.begin_nested():
            # Inside the savepoint: its read and its payload casts must not cost the ack either.
            if _refused_dispatch_is_dead(session, row, payload, task_id):
                return
            if headline and checkout_gate.is_clone_refusal(row.reason):
                # Whose login the clone used (the starter's): reads, so inside the savepoint too.
                task = session.scalars(select(Task).where(Task.id == int(task_id))).first()
                worded = checkout_gate.clone_refusal_sentence(session, task, row.reason) if task is not None else ""
                cause = f"{worded} " if worded else cause
            record_task_note(
                session, task_id, "dispatch_refused",
                f"A dispatch onto {whose} was refused by the node, so this task did not start on it. "
                f"{cause}It stays queued and {after}. {said}",
                # `value[:64]` like the label (open-vocabulary column). `reason` is bounded by `redact_node_detail`.
                {"provider": value[:64], "account": slug, "outcome": outcome, "intent": row.id,
                 "reason": reason,
                 **({"fix": DISPATCH_FIX_INTEGRATION_BRANCH}
                    if _MISSING_BRANCH_REFUSAL_MARKER in (row.reason or "") else {})},
            )
            session.flush()
    except Exception:  # noqa: BLE001 — a note must never cost an ack (see the docstring)
        log.warning("slot-intent %s: could not note the refusal on task %s", row.id, task_id,
                    exc_info=True)


def _account_for_refusal(
    row: SlotIntent, outcome: str, detail: Optional[str]
) -> Optional[str]:
    """Charge one non-retiring ack against the row's retry budget, in place on the pending ORM object.

    Returns a reason only when the human gate should be raised (budget spent and a person can decide). A
    spent budget sets `outcome` to `parked` unless `_may_park` refuses, so None does not mean pending. In
    place so the ack lands in one commit. A retiring outcome clears the budget.
    """
    if outcome in fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES:
        row.refusals = 0
        row.refusal_key = None
        row.next_attempt_at = None
        return None
    if _is_uncharged(detail):
        # Uncharged: the tally and its key stay as they were (`_is_uncharged`).
        row.next_attempt_at = func.now() + text("make_interval(secs => :backoff_secs)").bindparams(
            bindparam("backoff_secs", value=fleet_verdicts.refusal_backoff_secs(1), type_=Float)
        )
        return None
    key = fleet_verdicts.refusal_key(outcome, detail)
    # Same refusal continues the tally, a different one restarts at 1 — compared against the key STORED
    # on the row, so a slot alternating between two genuine refusals never accumulates toward a park.
    row.refusals = (row.refusals or 0) + 1 if row.refusal_key == key else 1
    row.refusal_key = key
    if row.refusals >= fleet_verdicts.REFUSAL_PARK_AFTER and _may_park(row):
        # The row retires either way; a human is asked only for a decision a person can make (unpushed
        # commits; a repository the node cannot clone or check out, `checkout_gate`). Other machine
        # faults (unreachable CP, human-held slot, missing credential) never gate.
        row.outcome = fleet_verdicts.OUTCOME_PARKED
        row.next_attempt_at = None
        detail_text = (detail or "").strip() or "no detail reported"
        summary = (
            f"{row.verb} on this slot has been refused {row.refusals} times identically "
            f"({outcome}): {detail_text}"
        )
        if _UNPUSHED_REFUSAL_MARKER in detail_text or checkout_gate.is_checkout_refusal(detail):
            return summary
        # Not bounded: `dispatch_attempts` counts only `ready -> active`, which a pre-spawn refusal never reaches.
        # This is a ~5x cadence cut; a real bound needs a slot-scoped counter.
        log.warning(
            "slot-intent %s: retired `parked` after %d identical refusals, and NO human gate was raised "
            "— the refusal is a machine fault, not a decision anybody can answer: %s. An un-acked dispatch "
            "is re-dispatched into the same slot (TRACKER-2335 S3), so this WILL repeat — a fresh budget "
            "of %d refusals per pass — until the fault is fixed",
            row.id, row.refusals, summary, fleet_verdicts.REFUSAL_PARK_AFTER,
        )
        return None
    # The DB clock (compared against `func.now()` in pool-tick), so API-host skew cannot skew backoff. Bound,
    # not interpolated, for plan reuse.
    row.next_attempt_at = func.now() + text("make_interval(secs => :backoff_secs)").bindparams(
        bindparam("backoff_secs", value=fleet_verdicts.refusal_backoff_secs(row.refusals), type_=Float)
    )
    return None


def _park_task_on_wedged_intent(session: Session, row: SlotIntent, reason: str) -> None:
    """Put the Task behind a parked intent in front of a human, as a question it can actually answer.

    The machine-raised human gates: commits absent from every origin
    ref, and a repository the node cannot clone or check out (a `checkout_gate` round). Options are
    cause-dependent. `stop_stack`/`reset_to_base` concern a slot, not a
    Task, so the park is the remedy and this only logs.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    task_id = payload.get("task_id")
    label = f"{project_identity.label(session, row.project_id)}#{row.number}"
    if row.verb == fleet_verdicts.VERB_DESTROY_SLOT or (
            task_id is not None and slot_destroy.destroy_pending(session, int(task_id))):
        # Its slot is being destroyed: that ack requeues the task, and a gate now would park it first.
        log.warning(
            "slot-intent %s: PARKED on %s — %s. Task %s's slot is being destroyed, so it is not gated; the "
            "destroy's ack returns it to ready", row.id, label, reason, task_id,
        )
        return
    if task_id is None:
        log.warning(
            "slot-intent %s: PARKED after %d identical refusals on %s — %s. No task rides this verb, so "
            "the row simply retires; whatever pass decided it will re-decide on a later observation",
            row.id, row.refusals, label, reason,
        )
        return
    task = session.scalars(select(Task).where(Task.id == int(task_id))).first()
    if checkout_gate.ends_on_refusal(task):
        landed = _end_unwatched_run(session, int(task_id), row.reason or reason, row.node_id)
        log.warning("slot-intent %s: PARKED after %d identical refusals on %s — %s. Task %s is a control-plane "
                    "run nobody watches: ended -> %s, no question", row.id, row.refusals, label, reason, task_id, landed)
        return
    if _UNPUSHED_REFUSAL_MARKER not in reason and checkout_gate.is_checkout_refusal(row.reason):
        # The node's own words (`row.reason`), not `reason`'s summary: the round names the cause itself.
        raised = checkout_gate.raise_round(session, int(task_id), row.project_id, row.reason or "", row.refusals or 0)
        log.warning(
            "slot-intent %s: PARKED after %d identical refusals on %s — %s. Task %s %s",
            row.id, row.refusals, label, reason, task_id,
            "-> awaiting_response on a checkout round" if raised
            else "was neither 'ready' nor 'active', so no round was raised",
        )
        return
    # Destructive options are safe only because the caller guarantees the cause (`_UNPUSHED_REFUSAL_MARKER`,
    # `_account_for_refusal`). Widen that caller and this menu is wrong.
    question = (
        f"Pool slot {label} cannot enact its `{row.verb}` dispatch: {reason}\n\n"
        f"Retrying has stopped — the refusal was identical {row.refusals} times running, so the condition "
        f"is not transient and this needs a decision rather than another attempt.\n\n"
        f"**Options:**\n"
        f"- Push the branch, then re-run this task\n"
        f"- Discard the unpushed commits and re-run this task from the base\n"
        f"- Leave it parked — I will investigate the worktree by hand\n"
    )
    # `..._on_wedged_dispatch`: a pre-spawn refusal finds the task still `ready` (the runner hook writes
    # `active`), so an `active` guard would make this a no-op.
    if await_response_on_wedged_dispatch(int(task_id), question, session) is None:
        log.warning(
            "slot-intent %s: PARKED after %d identical refusals on %s (%s), but task %s was neither "
            "'ready' nor 'active', so no gate was raised — it has settled, been stopped, or is ALREADY "
            "awaiting a human (whose question this must not overwrite). The intent is retired regardless",
            row.id, row.refusals, label, reason, task_id,
        )
        return
    log.warning(
        "slot-intent %s: PARKED after %d identical refusals on %s — %s. Task %s -> awaiting_response",
        row.id, row.refusals, label, reason, task_id,
    )


def _end_attempt_on_clone_refusal(session: Session, row: SlotIntent, detail: Optional[str],
                                  node_id: Any) -> Optional[str]:
    """End the attempt of the task whose `lease_slot` clone the node refused (`OUTCOME_CLONE_REFUSED`). Returns
    the landed state, or None when the task was no longer live. Commits. A starter whose login cannot see the
    repository is asked instead (`checkout_gate.access_question`): the task parks `awaiting_response` on that round.

    The ack lands before the runner exists, so the task is still `ready`: the runner's own claim is the
    `ready → active` edge that counts an attempt (`routes/tasks/dispatch.py::_active_dispatch_values`). The
    attempt is counted here first, as `runner_settle.charge_unrunnable_agent` does for a launch that never
    reaches a runner, else a refused clone would re-dispatch forever without spending budget. `active` stays a
    source (counted already, so the increment skips it). The hold is `end_attempt`'s to handle."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    task_id = payload.get("task_id")
    if task_id is None:
        return None
    said = fleet_verdicts.redact_node_detail(detail) or "the node refused the clone and gave no reason"
    # What a person reads on the failed task: whose login could not see what, and the fix (`clone_refusal_sentence`);
    # the node's own words stay on the note's `detail`.
    task = session.scalars(select(Task).where(Task.id == int(task_id))).first()
    if checkout_gate.ends_on_refusal(task):
        return _end_unwatched_run(session, int(task_id), detail, node_id)
    # A starter whose login cannot see the repository is asked "Start anyway?" instead: no retry changes it.
    if checkout_gate.access_question(session, task, detail) and checkout_gate.raise_round(
            session, int(task_id), row.project_id, detail or "", 1):
        return AWAITING_RESPONSE
    worded = (checkout_gate.clone_refusal_sentence(session, task, detail) if task is not None else "") or said
    session.execute(
        update(Task).where(Task.id == int(task_id), Task.state == READY)
        .values(dispatch_attempts=Task.dispatch_attempts + 1, **actors.task_actor_values(session))
    )
    landed = end_attempt(int(task_id), session, reason=worded, sources=(READY, ACTIVE),
                         cause=runner_terminations.CAUSE_CLONE_FAILED,
                         node=str(node_id) if node_id is not None else None, note_detail=said)
    if landed is None:
        session.rollback()
    elif landed == READY:
        _leave_refusing_node(session, int(task_id), row)
    return landed


def _end_unwatched_run(session: Session, task_id: int, detail: Optional[str], node_id: Any) -> Optional[str]:
    """End a control-plane run (`checkout_gate.ends_on_refusal`) whose clone or checkout the node refused: straight to
    `system_failed`, no question and no retry, so it holds no slot (the drain releases a terminal task's) and keeps
    nothing waiting on it — the next release or lane pass files a fresh run. Any refusal ends it, a transient one too,
    and so does a parked unpushed-commits refusal: nobody would answer either. Commits; the landed state, or None."""
    said = fleet_verdicts.redact_node_detail(detail) or "the node refused the clone and gave no reason"
    task = session.scalars(select(Task).where(Task.id == task_id)).first()
    # `parked`: worded for a run nothing retries ("Retry once …", never "retrying"); a checkout refusal by `headline`.
    worded = (checkout_gate.clone_refusal_sentence(session, task, detail, parked=True) if task is not None else "") \
        or checkout_gate.headline(detail) or said
    landed = end_attempt(task_id, session, reason=worded, final=True, sources=(READY, ACTIVE),
                         cause=runner_terminations.CAUSE_CLONE_FAILED if checkout_gate.is_clone_refusal(detail) else None,
                         node=str(node_id) if node_id is not None else None, note_detail=said)
    if landed is None:
        session.rollback()
    return landed


def _leave_refusing_node(session: Session, task_id: int, row: SlotIntent) -> None:
    """Free a `ready` task's never-cloned slot on the node that refused its clone, so the next pass places it afresh
    and placement leaves that node out (`launch_failures`, the `clone_failed` note `end_attempt` just wrote). A slot
    kept there was re-offered to the same node only, and a node whose clone fails for its own reasons (the same login
    cloned at once on another) stranded the task for hours. Nothing is lost: the clone never landed. Commits.

    Only while the task's one hold is still the refused slot: `end_attempt` committed `ready` first, so a drain pass
    may already have moved it, and a fresh slot elsewhere must never be wiped."""
    from app.routes.tasks.slots import _release_task_slots, abandon_task_slots, send_minted_intents  # noqa: PLC0415

    holds = session.execute(select(Worktree.node_id, Worktree.number).where(
        Worktree.task_id == task_id, Worktree.state == slot_state.TASK)).all()
    if [(h.node_id, h.number) for h in holds] != [(row.node_id, row.number)]:
        return
    reason = f"task {task_id}'s clone was refused on node {row.node_id}; it moves to another node"
    _, issued = abandon_task_slots(task_id, session, reason=reason)
    _release_task_slots(task_id, session, reason=f"released: {reason}")
    session.commit()
    send_minted_intents(issued)
    log.warning("slot-intent: %s", reason)


def _scrub_resolved_env(row: SlotIntent) -> list[str]:
    """Null the resolved environment out of an acked `lease_slot` payload. Returns the names that went.

    On every ack, not only retiring ones: `env_vars` is credential-sourced plaintext
    (`fleet_verdicts.build_lease_intent`), and non-retiring rows are the ones re-served longest.
    Reassigned, not mutated: `SlotIntent.payload` is plain `JSONB`. A no-op otherwise, so unconditional.
    """
    payload = row.payload
    if not isinstance(payload, dict):
        return []
    resolved = payload.get("env_vars")
    if resolved is None:
        return []
    row.payload = {**payload, "env_vars": None}
    # NAMES only, sorted for a stable line; a resolved VALUE is never logged on this path.
    names = resolved.get("values") if isinstance(resolved, dict) else None
    return sorted(names) if isinstance(names, dict) else []


@router.post("/slot-intents/{intent_id}/ack", response_model=SlotIntentAckOut)
@infra(Tenancy.ORG_SCOPED)
async def ack_slot_intent(
    intent_id: int,
    body: SlotIntentAckIn,
    db: DbDep,
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> SlotIntentAckOut:
    """Record what the node did with one intent, and — on a launch ack — stamp the runner's identity onto the
    lease.

    `done`, `accepted`, `unsupported_verb`, `clone_refused` retire it (a refused clone on a `lease_slot` also
    ends the task's attempt, `_end_attempt_on_clone_refusal`); `refused_stale`, `refused_precondition`, `failed`
    stamp `acked_at` and stay pending, else a half-executed intent would look complete and orphan a runner —
    except on a row the CP already retired (`superseded`/`parked`), which keeps that outcome and is never
    budgeted. Each non-retiring ack is budgeted against `refusal_key` with doubling backoff; the
    `REFUSAL_PARK_AFTER`th identical one parks the row and gates its Task (a changed reason resets;
    `deliver_answer` never parks). The ack lands first; the lease write is fenced and may write nothing for
    a superseded dispatch. `record_launch` admits a re-ack of the same dispatch.
    """
    out, answer_retry = await db.run_sync(_ack_slot_intent, intent_id=intent_id, body=body, caller=caller)
    if answer_retry is not None:
        # The ack's own writes are committed (`record_launch` commits). The retry takes a task's answer lock on a
        # connection of its own (`routes/tasks/answers.py::_AnswerLock`), blocking I/O, so it runs in a worker
        # thread on a session of its own; a lease's ack does nothing after it.
        from app.routes.tasks.answers import retry_answer_delivery_after_launch

        await release(db)
        await run_blocking_in_session(retry_answer_delivery_after_launch, **answer_retry, route=db)
    return out


def _ack_slot_intent(
    session: Session,
    intent_id: int,
    body: SlotIntentAckIn,
    caller: node_credentials.AgentCaller,
) -> tuple[SlotIntentAckOut, Optional[dict[str, Any]]]:
    """The ack, and the answer-delivery retry its launch owes (`retry_answer_delivery_after_launch`'s kwargs), or
    None."""
    answer_retry: Optional[dict[str, Any]] = None
    # Node-authored outcomes only: `superseded` and `parked` are the CP's (`fleet_verdicts.NODE_AUTHORED_OUTCOMES`).
    if body.outcome not in NODE_AUTHORED_OUTCOMES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown outcome {body.outcome!r}: expected one of {list(NODE_AUTHORED_OUTCOMES)}",
        )
    row = get_or_404(session, SlotIntent, intent_id, detail=f"unknown slot intent {intent_id}")
    already_retired = row.outcome in SLOT_INTENT_RETIRING_OUTCOMES
    # A CP retirement (`superseded`/`parked`) outranks a late refusal of the same row: overwriting it would
    # un-retire the row, re-serve it, and make the node redo a dead lease's clone/reset before its fence
    # refuses again — a slow node's queue then never drains to the live dispatch.
    # So does an order the node already finished: a later refusal is its re-enactment of an order that ran
    # (`claim_launch` refuses the second launch), and re-opening it would re-serve and finally park a running
    # task.
    # A superseded answer delivery keeps its outcome on ANY ack: a late `done` would erase that a newer
    # question replaced it (`supersede_pending_answer_delivery`).
    superseded_delivery = (
        row.verb == fleet_verdicts.VERB_DELIVER_ANSWER and row.outcome == fleet_verdicts.OUTCOME_SUPERSEDED
    )
    # A refused clone retires the row but is no launch: a late one must not overwrite a retirement either.
    clone_refused = body.outcome == fleet_verdicts.OUTCOME_CLONE_REFUSED
    if clone_refused and caller.node_id is not None and caller.node_id != row.node_id:
        # It ends a task's attempt: only the node the lease was ordered at may say its clone was refused.
        raise HTTPException(status_code=403, detail=f"slot intent {intent_id} was ordered at another node")
    keeps_cp_retirement = superseded_delivery or (
        row.outcome in (fleet_verdicts.OUTCOME_SUPERSEDED, fleet_verdicts.OUTCOME_PARKED,
                        fleet_verdicts.OUTCOME_DONE, fleet_verdicts.OUTCOME_ACCEPTED)
        and (body.outcome not in SLOT_INTENT_RETIRING_OUTCOMES or clone_refused)
    )
    # Before this ack locks the intent row: `apply_ack` takes the pair lock (`slot_allocation.lock_pair`).
    slot_destroy.lock_before_ack(session, row)
    row.acked_at = utcnow()
    # A kept retirement keeps its reason too: the operator reads why the CP retired it, not the node's echo.
    if not keeps_cp_retirement:
        row.outcome = body.outcome
        if body.detail:
            row.reason = body.detail
    # Before the commit, so ack and scrub are one transaction (`_scrub_resolved_env` for why unconditional).
    scrubbed_names = _scrub_resolved_env(row)
    parked_reason = None
    if not keeps_cp_retirement and node_capacity.returns_to_queue(row, body.detail, caller.node_id):
        # A launch the node refused at its session cap: the hold is returned and the task requeued, no attempt
        # spent — never budgeted, never parked (`app/node_capacity.py`).
        node_capacity.return_to_queue(session, row)
    elif not keeps_cp_retirement:
        parked_reason = _account_for_refusal(row, body.outcome, body.detail)
        if row.outcome == fleet_verdicts.OUTCOME_PARKED and row.verb == fleet_verdicts.VERB_DELIVER_ANSWER:
            # Retired undelivered: a task edit's dismissal gives its rounds back to the operator; an answer's
            # rounds are re-armed for the parked-task sweep's next delivery.
            live_task_edits.reopen_undelivered_dismissal(session, row.payload)
            rearm_undelivered_answer(session, row.payload)
        # On the task's own record, in the SAME transaction, so a rollback takes the note with it. Contention
        # is no refusal of the task's dispatch, and leaves the tally the note fences on untouched.
        if not _is_uncharged(body.detail):
            _note_dispatch_refusal(session, row, body.outcome)
    # A destroyed slot's row, hold and lost-work record land with the ack that reports them.
    slot_destroy.apply_ack(session, row, body.outcome, body.report, caller_node_id=caller.node_id,
                           already_retired=already_retired)
    slot_destroy.apply_park(session, row, caller_node_id=caller.node_id, already_retired=already_retired)
    # An erased carry-over slot's row goes with the ack that reports the tree gone (`app/carryover.py`).
    slot_destroy.apply_erase_ack(session, row, body.outcome, caller_node_id=caller.node_id,
                                 already_retired=already_retired)
    # The task page's message list shows delivery; nothing else announces a delivered message — or a delivered
    # answer whose typing moved no task state.
    messaged_task_id = row.payload.get("task_id") if isinstance(row.payload, dict) else None
    delivered_message = (
        row.verb in (fleet_verdicts.VERB_DELIVER_MESSAGE, fleet_verdicts.VERB_DELIVER_ANSWER)
        and row.outcome == fleet_verdicts.OUTCOME_DONE
    )
    if delivered_message and not already_retired and messaged_task_id is not None:
        emit_console_event(session, entity="task", id=int(messaged_task_id),
                           action="control_message", record_transition=False)
    # A delivered live edit moves the runner's baseline (`app/live_task_edits.py`), only on this `done` of a row the
    # CP had not retired: a superseded or failed delivery leaves it, so the next Save re-sends what never landed.
    if (row.outcome == fleet_verdicts.OUTCOME_DONE and not already_retired
            and row.verb in (fleet_verdicts.VERB_DELIVER_MESSAGE, fleet_verdicts.VERB_DELIVER_ANSWER,
                             fleet_verdicts.VERB_SWITCH_MODEL)
            and isinstance(row.payload, dict)
            and not (body.detail or "").startswith(fleet_verdicts.NOT_SWITCHED_PREFIX)):
        live_task_edits.stamp_delivered_view(session, row.payload.get("task_id"), row.payload.get("runner_view"))
    session.commit()
    if scrubbed_names and row.outcome in SLOT_INTENT_RETIRING_OUTCOMES:
        log.info(
            "slot-intent %s: acked %r — scrubbed the resolved environment (%s) off the payload",
            intent_id, body.outcome, ",".join(scrubbed_names),
        )
    elif scrubbed_names:
        # Loud: a pending row is re-served verbatim, so the eventual claim applies no CP env overlay (the plain
        # `.env`). Keeping plaintext would be worse. Follow-up: re-mint refused `lease_slot`s at the drain.
        log.warning(
            "slot-intent %s: acked %r (NOT retiring) — the resolved environment (%s) is scrubbed, so if "
            "this row is re-served and enacted the claim will apply NO configured variables. The next "
            "dispatch minted for this slot resolves them again",
            intent_id, body.outcome, ",".join(scrubbed_names),
        )
    # After the retiring commit, so the gate never asks about a rolled-back refusal. Best effort.
    if parked_reason:
        _park_task_on_wedged_intent(session, row, parked_reason)
    # After the ack's commit: the attempt ends in its own transaction (`end_attempt` commits). Only on the ack
    # that retires the row, so a re-sent `clone_refused` spends no second attempt.
    if clone_refused and row.verb == fleet_verdicts.VERB_LEASE_SLOT and not already_retired \
            and not keeps_cp_retirement:
        try:
            landed = _end_attempt_on_clone_refusal(session, row, body.detail, caller.node_id or row.node_id)
        except Exception:  # noqa: BLE001 — the ack is committed; a failure here must not 500 it into a re-send
            session.rollback()
            log.exception("slot-intent %s: the node refused the clone, but ending the task's attempt FAILED — "
                          "the task re-dispatches without spending one", intent_id)
        else:
            log.warning("slot-intent %s: the node refused the clone — task attempt ended -> %s", intent_id, landed)

    launch_recorded = False
    if (
        body.outcome in SLOT_INTENT_RETIRING_OUTCOMES
        # A refused clone launched nothing.
        and not clone_refused
        and body.generation is not None
        and body.dispatch_id
    ):
        # Resolved once into `record_launch`'s key. None = slot row gone: logged and skipped; the intent still retires.
        worktree_id = slot_state.resolve_slot(
            session, node_id=row.node_id, project_id=row.project_id, number=row.number
        )
        if worktree_id is None:
            log.warning(
                "slot-intent ack: launch ack for %s#%s (intent %s) names no worktree row any more — "
                "nothing to record",
                project_identity.label(session, row.project_id), row.number, intent_id,
            )
        else:
            launch_recorded = slot_state.record_launch(
                session,
                worktree_id=worktree_id,
                generation=body.generation,
                dispatch_id=body.dispatch_id,
                pid=body.pid,
                pid_start_time=body.pid_start_time,
                session_id=body.session_id,
            )
            if not launch_recorded:
                log.warning(
                    "slot-intent ack: launch ack for %s#%s (intent %s, generation %s, dispatch %s) did not "
                    "match the current hold — the CP superseded this dispatch; the newer hold is kept",
                    # The DISPLAY name: an operator looks for the slot, not a row key.
                    project_identity.label(session, row.project_id), row.number, intent_id,
                    body.generation, body.dispatch_id,
                )
            elif (
                row.verb == fleet_verdicts.VERB_LEASE_SLOT
                and body.outcome in (fleet_verdicts.OUTCOME_DONE, fleet_verdicts.OUTCOME_ACCEPTED)
                and body.session_id
                and isinstance(row.payload, dict)
                and row.payload.get("task_id") is not None
            ):
                answer_retry = dict(
                    worktree_id=worktree_id, generation=body.generation,
                    dispatch_id=body.dispatch_id, task_id=int(row.payload["task_id"]),
                    session_id=body.session_id,
                )
    # A delivered answer resumes a warm-parked Task, and only this ack knows it landed. `done` only;
    # best effort relative to the ack (a re-sent `done` retries it). Not for a superseded delivery: its runner
    # has asked a newer question since, which the resume would hide.
    if (body.outcome == fleet_verdicts.OUTCOME_DONE and row.verb == fleet_verdicts.VERB_DELIVER_ANSWER
            and not superseded_delivery):
        payload = row.payload if isinstance(row.payload, dict) else {}
        answered_task_id = payload.get("task_id")
        if answered_task_id is not None:
            resumed = resume_dispatched_task_on_answer(int(answered_task_id), session)
            session.commit()
            if resumed is None:
                log.warning(
                    "slot-intent ack: answer delivered for task %s but it was no longer "
                    "'awaiting_response' — a human stopped it, or its attempt ended while the answer was "
                    "in flight; not resurrecting it",
                    answered_task_id,
                )
    # The row's outcome, not the body's: an exhausting refusal is stored `parked`.
    return SlotIntentAckOut(
        id=intent_id,
        outcome=row.outcome or body.outcome,
        retired=(row.outcome or body.outcome) in SLOT_INTENT_RETIRING_OUTCOMES,
        launch_recorded=launch_recorded,
    ), answer_retry


class SlotLaunchClaimIn(BaseModel):
    # Required here: a launching caller holds both (`_launch_into_slot`). Bounded for logs and the `int4`
    # column.
    generation: int = Field(ge=0, le=2_147_483_647)
    dispatch_id: str = Field(min_length=1, max_length=64)


class SlotLaunchClaimOut(BaseModel):
    # True iff THIS caller may spawn a runner into the slot. Exactly one claim per hold ever answers True.
    granted: bool
    # Why not, in the node's log and in its refusal detail. Empty on a grant.
    reason: str = ""


@router.post("/slot-intents/{intent_id}/launch-claim", response_model=SlotLaunchClaimOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def claim_slot_launch(
    session: Session,
    intent_id: int,
    body: SlotLaunchClaimIn,
    _: None = Depends(_check_token),
) -> SlotLaunchClaimOut:
    """May the node spawn a runner for this `lease_slot` intent? The pre-launch half of the generation fence.

    The ack is too late to stop a superseded launch; `slot_state.claim_launch` answers and consumes in one
    UPDATE, so one hold yields at most one launch. Not idempotent (the safe direction): a lost grant means
    no launch; the intent stays pending and is re-dispatched into the same slot after
    `fleet_decide.UNACKED_REDISPATCH_DEADLINE_SECONDS`. 404 on an unknown intent; a refusal is a 200
    with `granted: false`.
    """
    row = get_or_404(session, SlotIntent, intent_id, detail=f"unknown slot intent {intent_id}")
    if row.verb != fleet_verdicts.VERB_LEASE_SLOT:
        # A verb that launches nothing consumes no launch permission. Refused, not 422'd: every answer
        # here is one the node can act on.
        return SlotLaunchClaimOut(
            granted=False,
            reason=f"intent {intent_id} is a {row.verb!r}, not a {fleet_verdicts.VERB_LEASE_SLOT!r} — "
                   f"nothing about it launches a runner",
        )
    # The fence must be this intent's own, or another intent carrying the current identity could launch.
    payload = row.payload if isinstance(row.payload, dict) else {}
    if (payload.get("generation"), str(payload.get("dispatch_id") or "")) != (
        body.generation, body.dispatch_id
    ):
        return SlotLaunchClaimOut(
            granted=False,
            reason=f"intent {intent_id} does not carry the dispatch identity presented with this claim "
                   f"(generation {body.generation}, dispatch {body.dispatch_id})",
        )
    # Resolved from the intent's wire triple once, as `ack_slot_intent` does.
    worktree_id = slot_state.resolve_slot(
        session, node_id=row.node_id, project_id=row.project_id, number=row.number
    )
    label = f"{project_identity.label(session, row.project_id)}#{row.number}"
    if worktree_id is None:
        reason = f"{label} names no worktree row any more — nothing to claim a launch on"
        log.warning("slot-intent launch-claim: REFUSED for %s (intent %s) — %s", label, intent_id, reason)
        return SlotLaunchClaimOut(granted=False, reason=reason)
    granted = slot_state.claim_launch(
        session,
        worktree_id=worktree_id,
        generation=body.generation,
        dispatch_id=body.dispatch_id,
    )
    if granted:
        log.info(
            "slot-intent launch-claim: GRANTED for %s (intent %s, generation %s, dispatch %s)",
            label, intent_id, body.generation, body.dispatch_id,
        )
        return SlotLaunchClaimOut(granted=True)
    reason = (
        f"the hold on {label} is no longer this dispatch's to launch (generation {body.generation}, "
        f"dispatch {body.dispatch_id}) — it was superseded, reclaimed, or already claimed by another "
        f"launch. REFUSING before the spawn rather than reporting it afterwards"
    )
    log.warning("slot-intent launch-claim: REFUSED for %s (intent %s) — %s", label, intent_id, reason)
    return SlotLaunchClaimOut(granted=False, reason=reason)


# ── Slot holders: the three start paths declare, the DB records ─────

class SlotHolderIn(BaseModel):
    # The node's uuid (`worktrees.node_id`), checked against the credential.
    node_id: Optional[str] = None
    # The identity (`_wire_project`); `project` is the display name beside it, never resolved. Unsent or
    # unresolvable → 404.
    project_id: Optional[int] = None
    project: str = ""
    number: int
    # The operator-mutation half. `slot_columns.MUTATED` holds the exceptions to the two-writer split
    # (`app/slot_mutate.py`). `None` = not supplied; `title=""` is a real clear.
    last_used: Optional[datetime] = None
    title: Optional[str] = None


class SlotHolderOut(BaseModel):
    # The resolved identity and its current display name.
    project_id: int
    project: str
    number: int
    # Occupancy is read from `GET /api/agent/worktrees`. Rows touched: 0 when the slot has no row or nothing
    # was supplied — how a caller tells a dropped write from a recorded one.
    mutated: int = 0
    # Why a Title was dropped, or `None`. Reported, not 422'd (the request also carries `last_used`);
    # `jaahd/commands.py::_set_title` turns it into a failed verb.
    title_rejected: Optional[str] = None


# The composite title's leading task id (`f"{task_id}|{title}"`, `slot_title.title_for_declaration`
# rule 3). What the title says distinguishes "already stamped" from "freshly bound", which
# `Worktree.task_id` cannot.
_TITLE_TASK_ID_RE = re.compile(r"^(\d+)\|")


def _prior_task_id_in_title(name: Optional[str]) -> Optional[int]:
    match = _TITLE_TASK_ID_RE.match(name or "")
    return int(match.group(1)) if match else None


@router.post("/slot-holder", response_model=SlotHolderOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def set_slot_holder(
    session: Session,
    body: SlotHolderIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> SlotHolderOut:
    """The slot-label mutation: this slot's Title and `last_used` stamp.

    Holds are bound by `slot_state.lease()`, so this route sets no holder; it remains the wire path for
    `apps/jaahd/commands.py::_set_title`. 404 on an unknown project before any write.
    """
    # Derived where possible, so no node can label another's slot. `resolve_node_id()` is the CP fallback.
    resolved_node = caller_node_ref(session, caller, body.node_id) or resolve_node_id()
    if not resolved_node:
        raise HTTPException(
            status_code=422,
            detail="slot-holder needs a node_id: pass it in the body or set NODE_ID on the API",
        )
    project_row = _wire_project(session, body.project_id)
    if project_row is None:
        named = body.project_id if body.project_id is not None else repr(body.project)
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown project {named}: nothing was stamped. A slot is keyed on the project's id, so a "
                f"value that resolves to no project in this node's org cannot name a slot"
            ),
        )
    project_id = project_row.id
    # The title is decided here: `worktrees.name` is CP-owned; `body.title` is a declaration.
    # `slot_title.title_for_declaration` decides; `None` means leave it. LEFT join: a dangling `task_id`
    # reads as no active task.
    slot_now = session.execute(
        select(
            Worktree.state,
            Worktree.task_id,
            Worktree.name,
            # The node-SENSED half of the derived-label pair (`default_title or default_name`): lets
            # `stored_opinion` tell an operator's verbatim choice from a slot that never had one.
            Worktree.default_title,
            Task.title.label("task_title"),
        )
        .select_from(Worktree)
        .outerjoin(Task, Task.id == Worktree.task_id)
        .where(
            # `node_ref` keeps the resolution in-statement: a value naming no node uuid reads as "no such slot"
            # (`slot_now` is None and `title_for_declaration_fail_open` leaves the title alone).
            Worktree.node_id == node_ref(resolved_node),
            Worktree.project_id == project_id,
            Worktree.number == body.number,
        )
    ).first()
    _derived_name = slot_title.default_name(project_row.name, body.number)
    title, title_rejected = slot_title.title_for_declaration_fail_open(
        state=slot_now.state if slot_now else None,
        # The row's own CURRENT binding (`slot_state.lease()` is the only writer). `prior_task_id` is what
        # the STORED TITLE claims; the two differ once per lease, on the first call after a fresh dispatch.
        declared_task_id=slot_now.task_id if slot_now else None,
        prior_task_id=_prior_task_id_in_title(slot_now.name if slot_now else None),
        task_title=slot_now.task_title if slot_now else None,
        supplied_title=body.title,
        # The half `prior_task_id` cannot carry: an operator rename erases the stored id, so the id test
        # alone would re-stamp the composite on the next ~5s pass and eat the rename.
        operator_named=slot_title.operator_named(
            slot_now.name if slot_now else None,
            default_title=slot_now.default_title if slot_now else None,
            default_name=_derived_name,
        ),
    )
    # `SYSTEM_ACTOR_ID`: this is a node-daemon route and `updated_by_id` is provenance.
    # `name_to_store` keeps a clear off the NOT NULL column.
    mutated = slot_mutate.apply_mutation(
        session,
        resolved_node,
        project_id,
        body.number,
        actor_id=entity_shape.SYSTEM_ACTOR_ID,
        last_used=body.last_used,
        name=slot_title.name_to_store(title, default_name=_derived_name),
    )
    # The observation pass cannot emit for this route (it compares `slot_columns.SENSED`). Unconditional.
    # Tagged with the caller's proven org (`routes/stream.py::_may_see_collection_sentinel`).
    emit_console_event(
        session, entity="worktree", id=0, action="upserted", org_id=caller.org_id,
        commit=True,
    )
    return SlotHolderOut(
        project_id=project_id,
        project=project_row.name,
        number=body.number,
        mutated=mutated,
        title_rejected=title_rejected,
    )
