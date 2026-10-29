"""The idle-stop decision: which pool slots' compose stacks come down because nobody is in them.

The decision lives here, CP-side; the node keeps only the mechanics (`stack_ops.stop_stack`). Every
refusal and every mint is recorded into the pass's `DecisionLog`."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from app import fleet_verdicts, project_identity, slot_state
from app.operator_settings import reads as settings_reads
from app.models.projects import Project
from app.models.worktrees import SlotIntent, Worktree
from app.node_identity import names_by_id

if TYPE_CHECKING:
    # Type-check-time only: `lease_drain` imports this module, so a runtime import would be a cycle.
    from app.lease_drain import DecisionLog

log = logging.getLogger("lease-drain")


# A call-time accessor, so the `pool_idle_stop_hours` override takes effect without a restart.
def idle_stop_hours() -> float:
    return settings_reads.get_float("pool_idle_stop_hours")


def stop_idle_stacks(session, journal: "DecisionLog") -> int:
    """Decide which slots' compose stacks come down; returns how many `stop_stack` intents were minted.

    Every predicate arm is affirmative and every unknown refuses (the open-direction mistake is `./dc down`
    under somebody's feet): not the main clone; `stack_up is True`; `session is False` (a failed probe
    arrives None and refuses); `console == not_attached`; quiet past the window; and an `observed_at`
    stamp. The stamp is required: an intent with no observation has all-None preconditions, the node's
    guards fail closed on absence, and `refused_stale` does not retire, so it is re-refused forever.

    A held slot is never stopped (a launch may be in flight before any session exists). Re-minting is
    suppressed while one is pending for the current stamp only; an older stamp is permanently
    `refused_stale`, and suppressing on it would wedge the idle-stop.

    Every refusal and mint is journaled under the `idle-stop` point against the slot's subject; the
    return value counts only stacks that came down."""
    hours = idle_stop_hours()
    threshold_secs = int(hours * 3600)
    candidates = session.scalars(
        select(Worktree).where(
            Worktree.number != 0,
            Worktree.stack_up.is_(True),
            Worktree.session.is_(False),
            Worktree.console == fleet_verdicts.CONSOLE_NOT_ATTACHED,
            Worktree.age_last_used_secs >= threshold_secs,
            Worktree.observed_at.is_not(None),
        )
    ).all()
    if not candidates:
        return 0
    # The suppression key is the intent's own identity tuple on ids (a display name breaks on rename).
    # Do not `str()` either side: both are `nodes.id` uuids and `str(UUID) != UUID`, so the tuple would
    # match nothing and every pass would re-mint.
    pending: set[tuple[UUID, int, int, Any]] = {
        (node_id, int(project_id), int(number), stamp)
        for node_id, project_id, number, stamp in session.execute(
            select(
                SlotIntent.node_id, SlotIntent.project_id, SlotIntent.number,
                SlotIntent.decided_on_observed_at,
            ).where(
                SlotIntent.verb == fleet_verdicts.VERB_STOP_STACK,
                # The shared predicate, not a local copy: suppression and supersession must agree on "pending".
                fleet_verdicts.still_pending(),
            )
        ).all()
    }
    minted = 0
    # One query for every candidate's display name: the identity decides, the name talks.
    names = project_identity.names_by_id(session, [tree.project_id for tree in candidates])
    # Same split for the node: one batched resolve for the log line.
    node_names = names_by_id(session, [tree.node_id for tree in candidates])
    # One batched query for every candidate project's down command. The intent must name the command; a
    # project without one is skipped below rather than handed an intent it could only refuse forever.
    down_cmds: dict[int, str] = {
        int(project_id): str(cmd or "").strip()
        for project_id, cmd in session.execute(
            select(Project.id, Project.stack_down_cmd).where(
                Project.id.in_({tree.project_id for tree in candidates})
            )
        ).all()
    }
    for tree in candidates:
        # Hoisted above the refusals: every journal entry names its slot.
        name = names.get(tree.project_id, "")
        subject = f"{name or tree.project_id}#{tree.number}"
        if (tree.node_id, tree.project_id, tree.number, tree.observed_at) in pending:
            journal.record(
                "idle-stop", "NOT STOPPED",
                "a stop_stack decided on this same observation is already pending on the node",
                subject=subject,
            )
            continue
        # A fresh read, not `tree.state`: the candidate SELECT never filtered on state, so this catches a hold
        # taken since that scan.
        hold = slot_state.read(session, worktree_id=tree.id)
        if hold is not None and hold.state != slot_state.FREE:
            journal.record(
                "idle-stop", "NOT STOPPED",
                f"the slot was {hold.state} when re-read, and a held slot is never stopped",
                subject=subject,
            )
            continue
        down_cmd = down_cmds.get(tree.project_id, "")
        if not down_cmd:
            # Not minted: the node fails closed on an intent naming no command, every tick, without retiring it.
            # The fix is to set `projects.stack_down_cmd`.
            journal.record(
                "idle-stop", "NOT STOPPED",
                "the project row carries no stack_down_cmd, so the node could only refuse the intent",
                subject=subject,
            )
            log.warning(
                "lease-drain: idle-stop — no stack_down_cmd for %s#%s; not minting a stop_stack the node "
                "could only refuse", name or tree.project_id, tree.number,
            )
            continue
        # One home for the numbers, read by both the intent and the ledger entry.
        idle_note = (
            f"idle {tree.age_last_used_secs / 3600.0:.1f}h with no session (window {hours:g}h)"
        )
        intent = fleet_verdicts.build_stop_stack_intent(
            tree.project_id, name, tree.number,
            stack_down_cmd=down_cmd,
            reason=f"{name}#{tree.number} has been {idle_note} — stopping its stack",
            observation={
                "observed_at": tree.observed_at,
                "session": tree.session,
                "console": tree.console,
                "stack_up": tree.stack_up,
            },
        )
        # `issue_intents` resolves through `node_ref` (name or uuid), so the uuid goes through unchanged; the
        # log takes the name.
        served = fleet_verdicts.issue_intents(session, tree.node_id, [intent])
        if not served.newly:
            # Decided to mint, but `uq_slot_intents_slot_verb_observed_at` absorbed it as a structural no-op.
            # Journaled so it does not read as an idle slot nobody looked at.
            journal.record(
                "idle-stop", "NOT STOPPED",
                "an equivalent intent is already recorded, so the re-mint was a structural no-op "
                "(`issue_intents` reported nothing new)",
                subject=subject,
            )
        for recorded in served.newly:
            minted += 1
            journal.record("idle-stop", "STOPPED", idle_note, subject=subject)
            log.info("lease-drain: idle-stop — stop_stack minted for %s#%s on node %s",
                     recorded["project"], recorded["number"], node_names.get(tree.node_id, "unknown"))
    return minted
