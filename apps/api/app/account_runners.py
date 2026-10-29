"""Single home for "how many fleet runners does this credential account currently carry?" — live ones
and ones the control plane has just handed it but cannot observe yet.

Account selection (`app/jaah_scheduler.py`) caps on this and breaks ties on it. A `task`-held slot
counts when a live session is freshly observed (`pool_state.max_observation_age_secs()`; unbounded, a
silent node would wedge the cap) or its launch is in flight (`dispatched_at` younger than
`fleet_decide.UNACKED_REDISPATCH_DEADLINE_SECONDS`, not narrowed by `acked_at`, or its own order still
queued unanswered on its node within `fleet_decide.UNANSWERED_LEASE_MAX_WAIT_SECONDS` — the redispatch
waits on that order, so the hold must stay charged while it does).

Accepted limitation: `session_status` is NULL for both "no session" and "probe failed", so a blind
probe under-counts, failing permissive; a fix needs an observedness flag.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import Float, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from app import edition_features, fleet_verdicts, pool_state, slot_state
from app.fleet_decide import UNACKED_REDISPATCH_DEADLINE_SECONDS, UNANSWERED_LEASE_MAX_WAIT_SECONDS
from app.models.worktrees import Worktree

__all__ = ["fleet_assigned_runner_counts"]


def fleet_assigned_runner_counts(session: Session) -> dict[UUID, int]:
    """`{accounts.id: assigned fleet runner count}` — one grouped read, never N+1.

    Counts task-held slots with a resolved account and either a fresh live session (any
    `session_status` value, idle included) or an in-flight launch — see the module docstring. The
    clock is the database's (`func.now()`), because `dispatched_at` is DB-stamped.

    Accounts with no runner are absent; callers use `.get(account_id, 0)`.
    """
    # The sensed account when present, else the one the CP leased the slot to. Required: an unobserved
    # in-flight launch has no sensed account yet (an API-key runner's is sensed off its node-derived home). Sensed wins whenever it exists.
    _slot_account = func.coalesce(Worktree.account_id, Worktree.leased_account_id)
    # Arm 1: live session with a fresh observation. NULL `observed_at` fails the comparison
    # (fail-closed for the count).
    _observation_fresh = (
        func.extract("epoch", func.now() - Worktree.observed_at)
        < pool_state.max_observation_age_secs()
    )
    _live_session = and_(
        Worktree.session_status.isnot(None),
        Worktree.session_status != "",
        _observation_fresh,
    )
    # Arm 2: launch in flight. NULL `dispatched_at` fails the comparison, so it under-counts rather
    # than inventing an allocation.
    _launch_in_flight = or_(
        cast(func.extract("epoch", func.now() - Worktree.dispatched_at), Float)
        < UNACKED_REDISPATCH_DEADLINE_SECONDS,
        # The redispatch waits on the hold's own unanswered order, so its launch is still coming.
        cast(func.extract("epoch", func.now() - fleet_verdicts.own_unanswered_lease_since()), Float)
        < UNANSWERED_LEASE_MAX_WAIT_SECONDS,
    )
    rows = session.execute(
        select(_slot_account, func.count())
        .where(
            # `== TASK`, not `!= FREE`: a `user`-held slot earns no fleet occupancy. Must agree with
            # `routes/portfolio.py`'s tally — both feed the same selection gate.
            Worktree.state == slot_state.TASK,
            # A slot neither sensed nor leased has no account and must not become a bucket.
            _slot_account.isnot(None),
            or_(_live_session, _launch_in_flight),
        )
        .group_by(_slot_account)
    ).all()
    counts = {account_id: count for account_id, count in rows}
    if not edition_features.enabled("microvm"):
        return counts  # The community edition has no MicroVMs.
    # A MicroVM runner holds its account from admission until its VM is retained or ends (D21), so the cap
    # binds VMs like slots.
    from app.microvm_placement import LIVE
    from app.models.nodes import Microvm

    for account_id, count in session.execute(
        select(Microvm.account_id, func.count())
        .where(Microvm.account_id.isnot(None), Microvm.kind == "task", Microvm.state.in_(LIVE))
        .group_by(Microvm.account_id)
    ).all():
        counts[account_id] = counts.get(account_id, 0) + count
    return counts
