"""The CP half of `destroy_slot`'s ack: what the node's report does to the retiring row and to
the task it held. The mint is `app/slot_allocation.py::retire`; the node half is
`apps/jaahd/reconcile/enact_signal.py::_enact_destroy_slot`, whose `report` this reads:

    {"erased": bool, "refused": bool, "killed": [str],
     "unpushed": [{"branch": str, "shas": [str]}] | {"unknown": true, "reason": str},
     "detail": str}

Three arms, on a `done` ack only (a `failed` ack is a partial failure: the intent stays pending with the
node's detail on it and is re-served, every node step tolerating a half-destroyed tree):

* `erased` — a task hold is released and the task requeued (`slot_allocation.requeue_after_destroy`, D9:
  only now, with its runner dead; a `paused` task's hold is released and its pause kept), the row is deleted so its index is free, and the lost work is recorded
  on the task (`dynamic_slot_destroyed`) and in the command audit (AC7.3).
* `refused` — the node will not erase it (its containment checks: the path is not this slot's):
  the row stays retiring with `destroy_error`; nothing re-mints, and an operator Delete is the
  way out. Its runner is already dead, so a held task is requeued all the same — the work stays in the tree.
* neither — treated as no ack. If such failures exhaust the retry budget the intent parks, and
  `apply_park` requeues a held task (its runner died at the first kill); the row stays retiring.

Between the node's kill and this ack the task is still `active` on a retiring held row, so the dead-runner
paths ask `destroy_pending` first and leave such a task to this ack."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional, Union
from uuid import UUID

from typing import Annotated

from pydantic import Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app import agent_session_lifecycle, agent_sessions, fleet_verdicts, intent_rail, slot_allocation, slot_state
from app.entity_shape import StrictModel
from app.events import emit_console_event
from app.models.projects import Project
from app.models.worktrees import SlotIntent, Worktree
from app.node_identity import node_org
from app.user_activity import record_task_note

log = logging.getLogger(__name__)

# The task note's action (`task_note:dynamic_slot_destroyed`) and the audit row's `command_type`.
NOTE_ACTION = "dynamic_slot_destroyed"
AUDIT_COMMAND = "destroy_slot"
# Who the audit row names: the CP's own decision, enacted by the node.
AUDIT_ACTOR = "control-plane"
# The break causes a runner killed by its slot's destroy reports: a death with no transcript verdict. Any other
# cause is a genuine break (its account verdict still lands); `routes/agent/runner_liveness.py::record_runner_break`.
KILL_SIGNATURE_CAUSES = frozenset({"crashed", "vanished", "unknown"})
# What the node's `killed` list may hold (`apps/jaahd/reconcile/enact_signal.py::_enact_destroy_slot` caps each at 200).
_KILLED_MAX = 512
_KILLED_ENTRY_MAX = 256

# The `unpushed` bounds: a report is node-authored, untrusted input landing in JSONB on a shared CP, so every
# field is bounded, roomy against real teardowns (a bound refusing real work leaves a teardown no way to report).
_BRANCH_MAX = 512
_SHAS_PER_BRANCH_MAX = 1_000
_BRANCHES_MAX = 500
_REASON_MAX = 8_000
# An object name and nothing else: hex, 7 to 64 (abbreviated sha1 through full sha256).
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
# Bounds on the note's prose; the full record is on the audit row and in the note's `data`.
_NOTE_BRANCHES = 10
_NOTE_SHAS = 5


class UnpushedBranch(StrictModel):
    """One branch a teardown found with commits that had reached no `origin` ref."""

    branch: str = Field(min_length=1, max_length=_BRANCH_MAX)
    shas: list[str] = Field(max_length=_SHAS_PER_BRANCH_MAX)

    @field_validator("shas")
    @classmethod
    def _are_object_names(cls, value: list[str]) -> list[str]:
        for sha in value:
            if not _SHA_RE.fullmatch(sha):
                # Truncated in the message too: the refusal text reaches a log line.
                raise ValueError(f"not an object name: {sha[:64]!r}")
        return value


class UnpushedUnknown(StrictModel):
    """The alternative to an empty list, and why `unpushed` is a union.

    An empty list claims "I looked, nothing was unpushed". A teardown that could not look must say
    `unknown` with a reason, so a human can tell a clean teardown from a lossy one."""

    unknown: bool
    reason: str = Field(min_length=1, max_length=_REASON_MAX)

    @field_validator("unknown")
    @classmethod
    def _must_be_true(cls, value: bool) -> bool:
        # `{"unknown": false}` is malformed, not a third state: a field whose only meaning is `true` refuses
        # `false`.
        if not value:
            raise ValueError("unknown must be true; report the branches themselves if they are known")
        return value


def unpushed_record(unpushed: Any) -> dict[str, Any]:
    """A report's `unpushed` as the JSONB dict the audit row stores. Wrapped as `{"branches": [...]}` so an
    empty list reads as "looked, found nothing" rather than NULL."""
    if hasattr(unpushed, "model_dump"):  # a validated `UnpushedUnknown`
        unpushed = unpushed.model_dump()
    if isinstance(unpushed, dict):
        return {"unknown": True, "reason": str(unpushed.get("reason") or "not stated")}
    branches = []
    for item in unpushed or []:
        branch = item.get("branch") if isinstance(item, dict) else getattr(item, "branch", None)
        shas = item.get("shas") if isinstance(item, dict) else getattr(item, "shas", None)
        branches.append({"branch": str(branch), "shas": [str(s) for s in (shas or [])]})
    return {"branches": branches}


def lost_work_summary(record: dict[str, Any]) -> Optional[str]:
    """The sentence a human reads on the Activity tab, or None when nothing was lost.

    Names the branch and the shas. An unknown is surfaced too: "nobody could look" must not read the same
    as a clean teardown."""
    if record.get("unknown"):
        return (
            "Dynamic worktree destroyed without a check for unpushed work "
            f"({record.get('reason') or 'reason not stated'}). Anything not pushed to origin is gone."
        )
    branches = [b for b in record.get("branches") or [] if b.get("shas")]
    if not branches:
        return None
    parts = []
    for b in branches[:_NOTE_BRANCHES]:
        shas = b["shas"]
        shown = ", ".join(s[:12] for s in shas[:_NOTE_SHAS])
        more = f" and {len(shas) - _NOTE_SHAS} more" if len(shas) > _NOTE_SHAS else ""
        parts.append(f"{b['branch']} ({len(shas)} commit{'s' if len(shas) != 1 else ''}: {shown}{more})")
    extra = len(branches) - _NOTE_BRANCHES
    tail = f"; and {extra} more branch(es)" if extra > 0 else ""
    return (
        "Dynamic worktree destroyed. Unpushed commits were lost (never reached origin): "
        + "; ".join(parts) + tail + "."
    )


class DestroyReport(StrictModel):
    """The node's `destroy_slot` report — the module docstring's contract. Every field is bounded: each
    lands in a column or a JSONB row a human's console renders."""

    erased: bool = False
    refused: bool = False
    killed: list[Annotated[str, StringConstraints(max_length=_KILLED_ENTRY_MAX)]] = Field(
        default=[], max_length=_KILLED_MAX)
    unpushed: Union[
        Annotated[list[UnpushedBranch], Field(max_length=_BRANCHES_MAX)], UnpushedUnknown,
    ] = []
    detail: str = Field(default="", max_length=8_000)


def _destroy_error_statement(row_id: UUID, detail: str):
    """Record the node's refusal on one row — `DESTROY_ACK`'s one statement (`app/slot_columns.py`)."""
    return update(Worktree).where(Worktree.id == row_id).values(destroy_error=detail)


def _destroyed_row_delete_statement(row_id: UUID):
    """Delete one destroyed row by primary key, freeing its index. Writes no column."""
    return delete(Worktree).where(Worktree.id == row_id)


def destroy_pending(session: Session, task_id: int) -> bool:
    """Is `task_id` held by a slot the CP has ordered destroyed? Then its runner's death is the destroy's
    kill, and its ack — not a dead-runner reclaim — returns the task to `ready`."""
    return session.scalar(
        select(Worktree.id).where(
            Worktree.task_id == task_id,
            Worktree.state == slot_state.TASK,
            Worktree.retiring_at.is_not(None),
        ).limit(1)
    ) is not None


def lock_before_ack(session: Session, intent: SlotIntent) -> None:
    """Take the pair lock for a `destroy_slot` ack before the route writes the intent row — the order every
    writer of the pair keeps (`slot_allocation.lock_pair`)."""
    if intent.verb == fleet_verdicts.VERB_DESTROY_SLOT:
        slot_allocation.lock_pair(session, intent.node_id, intent.project_id)


def _retiring_row(session: Session, intent: SlotIntent, label: str) -> Optional[Worktree]:
    """The row this intent was minted for, locked: still present and still retiring, else None (logged)."""
    slot_allocation.lock_pair(session, intent.node_id, intent.project_id)
    row = session.scalars(
        select(Worktree).where(
            Worktree.node_id == intent.node_id,
            Worktree.project_id == intent.project_id,
            Worktree.number == intent.number,
        ).with_for_update()
    ).first()
    if row is None:
        log.info("destroy_slot ack (intent %s, %s): the row is already gone", intent.id, label)
        return None
    if row.retiring_at is None:
        # Not the row this intent was minted for: a re-declared index must not be deleted by an old ack.
        log.warning("destroy_slot ack (intent %s, %s): the row is not retiring — left as it is",
                    intent.id, label)
        return None
    return row


def apply_park(session: Session, intent: SlotIntent, *, caller_node_id: Optional[UUID],
               already_retired: bool) -> None:
    """A `destroy_slot` whose partial failures spent their retry budget is parked (`routes/agent/slot_intents.py::
    _account_for_refusal`) and never re-served. Its runner died at the first attempt's kill, so a held task is
    requeued now rather than left `active` with no runner; the row stays retiring for the re-mint."""
    if (intent.verb != fleet_verdicts.VERB_DESTROY_SLOT or intent.outcome != fleet_verdicts.OUTCOME_PARKED
            or already_retired or (caller_node_id is not None and caller_node_id != intent.node_id)):
        return
    label = f"project {intent.project_id} #{intent.number}"
    row = _retiring_row(session, intent, label)
    if row is None or row.state != slot_state.TASK or row.task_id is None:
        return
    log.warning("destroy_slot %s PARKED after repeated failures (%s) — requeueing task %s off the retiring row",
                label, intent.reason, row.task_id)
    _end_hold_and_requeue(session, row, row.task_id, label)


def apply_ack(session: Session, intent: SlotIntent, outcome: str, report: Optional[dict[str, Any]], *,
              caller_node_id: Optional[UUID], already_retired: bool) -> None:
    """Apply one `destroy_slot` ack on the caller's transaction (no commit). A no-op for any other verb, a
    non-`done` outcome, a repeat ack of a retired intent, an ack from a node other than the intent's, an
    unreadable report, or a row that is gone or no longer retiring.

    `caller_node_id` None is the control plane's own arm (`cp_self`, or `insecure` locally), which the
    route already admits for every verb; a node's own credential may only report its own destroys."""
    if intent.verb != fleet_verdicts.VERB_DESTROY_SLOT or outcome != fleet_verdicts.OUTCOME_DONE:
        return
    label = f"project {intent.project_id} #{intent.number}"
    if already_retired:
        log.info("destroy_slot ack (intent %s, %s): a repeat of a retired intent — nothing re-applied",
                 intent.id, label)
        return
    if caller_node_id is not None and caller_node_id != intent.node_id:
        log.warning("destroy_slot ack (intent %s, %s): sent by node %s, not the intent's node %s — ignored",
                    intent.id, label, caller_node_id, intent.node_id)
        return
    try:
        parsed = DestroyReport.model_validate(report or {})
    except ValidationError as exc:
        log.warning("destroy_slot ack (intent %s, %s): unreadable report — treated as no ack: %s",
                    intent.id, label, exc)
        return
    if not parsed.erased and not parsed.refused:
        return
    row = _retiring_row(session, intent, label)
    if row is None:
        return
    held_task = row.task_id if row.state == slot_state.TASK else None
    payload = intent.payload if isinstance(intent.payload, dict) else {}
    task_id = held_task if held_task is not None else payload.get("task_id")
    if held_task is not None:
        # The node killed the runner before either verdict (`_enact_destroy_slot`; slot 0, the one refusal
        # before the kill, is never task-held), so the task goes back to the queue whatever became of the tree.
        _end_hold_and_requeue(session, row, held_task, label)
    if not parsed.erased:
        detail = parsed.detail or intent.reason or "the node refused to erase the tree"
        session.execute(_destroy_error_statement(row.id, detail))
        log.warning("destroy_slot %s REFUSED by the node — the row stays retiring: %s", label, detail)
        return
    session.execute(_destroyed_row_delete_statement(row.id))
    _record(session, intent, row, parsed, task_id)
    emit_console_event(session, entity="worktree", id=0, action="deleted", org_id=node_org(session, str(row.node_id)))


def _end_hold_and_requeue(session: Session, row: Worktree, task_id: int, label: str) -> None:
    """Close the killed runner's session, release the hold and requeue its task unless it is paused (D9: only
    now, with the kill enacted)."""
    agent_session_lifecycle.note_end(
        session, agent_session_id=row.launch_session_id or "", end_reason=agent_sessions.END_REASON_ABANDONED,
    )
    slot_state.release(session, worktree_id=row.id, generation=row.generation, reason="slot destroyed",
                       commit=False)
    try:
        slot_allocation.requeue_after_destroy(session, task_id, reason="its dynamic slot was destroyed")
    except ValueError as exc:
        # Settled or deleted since the kill, or paused (stopped): its hold ends, its state is kept.
        log.info("destroy_slot %s: task %s not requeued — %s", label, task_id, exc)


def _record(session: Session, intent: SlotIntent, row: Worktree, report: DestroyReport,
            task_id: Optional[int]) -> None:
    """The lost-work record (AC7.3): a task note when a task rode the slot, and always an audit row."""
    from app.routes.commands import _record_audit  # noqa: PLC0415 — `CommandAudit`'s single writer, a route

    unpushed = unpushed_record(report.unpushed)
    summary = lost_work_summary(unpushed)
    project = session.get(Project, intent.project_id)
    name = project.name if project is not None else str(intent.project_id)
    extra = {
        "project": name, "number": intent.number, "intent": intent.id, "unpushed_refs": unpushed,
        "killed": report.killed,
    }
    if task_id is not None:
        note = summary or "Its dynamic worktree was destroyed; no unpushed work was found."
        record_task_note(session, int(task_id), NOTE_ACTION, f"{name} #{intent.number}: {note}", extra)
    _record_audit(
        session, str(row.node_id), AUDIT_COMMAND, {**extra, "task_id": task_id}, "done",
        {"detail": report.detail}, AUDIT_ACTOR, commit=False,
    )


# ── The payload-addressed erase (`fleet_verdicts.VERB_ERASE_TREE`) ──────────────────────────────────────

def enqueue_erase(
    session: Session,
    *,
    node_id: UUID,
    project_id: int,
    project: str,
    number: int,
    clone_path: str,
    has_repo: bool,
    worktree_id: Optional[UUID],
    label: str,
    compose_project: Optional[str] = None,
) -> None:
    """Order one slot's tree erased on `node_id`, whatever scope the caller runs under (no commit, no push: the
    node's pool tick serves a pending intent).

    For a tree the node's roster no longer names — a carried-over slot whose project moved to another org
    (`app/carryover.py::close`) — so it is addressed by its payload: the node's `worktree_erase` arguments,
    which recompute the target from `clone_path` and `number` and never accept a path. The node's erase
    never refuses for what the tree holds. `worktree_id` lets the ack delete the row.
    No `stack_down_cmd`: the project now belongs to another org, whose command must not run on this node (the
    node's erase skips the stop when none is named).

    The node move passes `compose_project`: the slot's stack goes down WITH its volumes (`purge_volumes`, the
    node's own `docker compose -p <name> down -v`, never a project command), since a stack's volumes and `.env`
    hold the old org's data.

    Inserted straight through the rail with the org derived from the node in the statement
    (`fleet_verdicts._persist_kwargs`): the caller may be bound to the task's NEW org, where neither the node nor
    the slot is readable, and nothing here reads them."""
    intent = fleet_verdicts.build_slot_intent(
        fleet_verdicts.VERB_ERASE_TREE, project_id, project, number,
        reason=f"erase {label}: its project moved to another org",
        payload={
            "project": project,
            "number": int(number),
            "clone_path": clone_path,
            "force": True,
            **({} if has_repo else {"has_repo": False, "project_id": int(project_id)}),
            **({"purge_volumes": True, "compose_project": compose_project} if compose_project else {}),
            "worktree_id": str(worktree_id) if worktree_id is not None else None,
            "label": label,
        },
        precondition_fields=(),
    )
    intent_rail.persist(
        session, intents=[intent], commit=False, **fleet_verdicts._persist_kwargs(node_id),
    )
    log.info("erase_tree ordered on node %s: %s", node_id, label)


def apply_erase_ack(session: Session, intent: SlotIntent, outcome: str, *, caller_node_id: Optional[UUID],
                    already_retired: bool) -> None:
    """A `done` `erase_tree` ack deletes the slot row it was minted for, once that row is free (no commit). A
    row still held is left: its hold's own release frees it, and nothing re-leases a slot whose project left
    the node's org. A no-op for any other verb, outcome, repeat ack, or another node's ack."""
    if intent.verb != fleet_verdicts.VERB_ERASE_TREE or outcome != fleet_verdicts.OUTCOME_DONE or already_retired:
        return
    if caller_node_id is not None and caller_node_id != intent.node_id:
        log.warning("erase_tree ack (intent %s): sent by node %s, not the intent's node %s — ignored",
                    intent.id, caller_node_id, intent.node_id)
        return
    payload = intent.payload if isinstance(intent.payload, dict) else {}
    raw_id = payload.get("worktree_id")
    if not raw_id:
        return
    deleted = session.execute(
        delete(Worktree).where(
            Worktree.id == UUID(str(raw_id)),
            Worktree.node_id == intent.node_id,
            Worktree.state == slot_state.FREE,
        ).execution_options(synchronize_session=False)
    ).rowcount
    if deleted:
        log.info("erase_tree ack (intent %s): %s erased, its row deleted", intent.id, payload.get("label"))
        emit_console_event(session, entity="worktree", id=0, action="deleted",
                           org_id=node_org(session, str(intent.node_id)))
