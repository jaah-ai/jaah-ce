"""Which `sessions` rows are stale, and removing them — the single home for both halves.

A row is written before the runner exists (`apps/jaahd/fleet_dispatch.py`), so a spawn that never
writes a `<sid>.jsonl` leaves a row nothing can finish. The operator deletes them from Audit >
Sessions; not a cron (operator decision). Caller-transactional, like `app/cron_retention.py`.

Node-reach scoped in both halves: count and delete take the same `Principal` through
`deletable_node_ref_clause` (`system:edit` is fleet-wide), so the banner's count equals the set the
button removes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, exists, func, select, true
from sqlalchemy.orm import Session

from app.operator_settings import reads as settings_reads

# Vocabularies imported from their owners (rendered into revision 0135's CHECKs), so a rename breaks
# this import rather than silently changing which rows the safety argument covers.
from app.agent_sessions import STATUS_RUNNING, TRANSCRIPT_PENDING
from app.authz.principal import Principal
from app.authz.visibility import node_reach_clause, sees_all_nodes
from app.models.agent_sessions import AgentSession
from app.models.tasks import Task
from app.models.worktrees import Worktree

# `slot_state.FREE`, not a literal: a stale `'free'` would widen this destructive set to every
# slot-held session.
from app.slot_state import FREE as SLOT_FREE

log = logging.getLogger("sessions-cleanup")

# Mirrored as a literal in `app/operator_settings/catalog.py::CATALOG`, which is DB-free and cannot import
# this; `tests/test_sessions_cleanup.py` pins the two spellings and defaults together.
STALE_SESSION_AFTER_HOURS_KEY = "stale_session_after_hours"

# Derived from the retry machine: a legitimately mid-flight row resolves in minutes (a node tick, or
# the 5-attempt ladder one lease TTL apart). Six hours is an order of magnitude past that and within a
# working day. Mirrored in the catalogue; re-tune it there.
DEFAULT_STALE_SESSION_AFTER_HOURS = 6

# Per-call ceiling, like `cron_retention.MAX_ROWS_PER_SWEEP`, so a click cannot hold locks over an
# unbounded set. Smaller than the sweep's because a human waits; truncation means a second click.
MAX_DELETE_PER_CALL = 5000

# Identifiers per log line. The delete destroys its own provenance, so the line names what went (as
# the single-row delete does); bounded, with the elided count stated. The count is always exact.
MAX_DELETE_IDENTIFIERS_LOGGED = 50


def stale_after() -> timedelta:
    """How long a session with no transcript may sit before the page offers it for deletion, as the
    interval the cutoff is measured with. Re-read per call, so a saved edit applies with no restart."""
    return timedelta(hours=settings_reads.get_int(STALE_SESSION_AFTER_HOURS_KEY))


def stale_cutoff(now: datetime | None = None) -> datetime:
    """The instant a row must have started before to count as stale. `now` is injectable for tests."""
    return (now or datetime.now(timezone.utc)) - stale_after()


# ── Who may delete a row, and why it is not the clause that decides who may read one ─────────────────


def deletable_node_ref_clause(principal: Principal | None, column):
    """May `principal` destroy a row whose nullable node reference is `column`? Correlates to the
    enclosing query like `authz.node_reach_clause`; usable only inside a select over `AgentSession`.

    It is `authz.visible_node_ref_clause` without the NULL arm. `sessions.node_id` is `SET NULL` on
    node delete, so retiring a node would otherwise let any `system:edit` holder with no node reach
    erase that node's audit history. A confined principal reaches only rows on nodes it reaches.

    Defined here, not beside its read twin, so the two are not picked interchangeably; move it to
    `authz` when a second deleter needs it. `sees_all_nodes` states the unrestricted case explicitly
    rather than relying on the helper's `true()` arms."""
    if sees_all_nodes(principal):
        return true()
    return node_reach_clause(principal, column)


# ── The staleness predicate ──────────────────────────────────────────────────────────────────────────
# running AND pending AND attempts=0 AND no lease AND started_at < cutoff AND not held live by the CP.
# running+pending: nothing can finish the row (`/work` serves only `ended`; the node probe cannot end
# a never-written file). attempts=0 + no lease is the S3 guard: only an upload ticket makes a key, and
# it stamps the lease, so such a row provably holds no bucket bytes. Never widen to "not uploaded":
# `failed`/`uploading` rows can hold objects, and an unset bucket (kill switch) would match every row.

# Deletion is durable only for never-started rows (no `.jsonl`, so `/observed` never recreates them).
# Age is not a liveness test: a live `advisor` chat or `awaiting_response` runner looks identical and
# may run for hours, hence the live conjunct. Keyed on indexed `started_at`; `ended_at` is unindexed
# and NULL on these rows.


def _cp_holds_it_live():
    """Does the control plane hold this row's session id as live? A correlated pair of EXISTS,
    rendering only inside an enclosing select over `sessions`.

    The same oracle as `routes/agent_sessions.py::_live_sessions` (which refuses to reconstruct a live
    session, as this refuses to delete one); if either moves, move both. Both halves: a non-NULL
    `tasks.runner_session_id`, or `worktrees.launch_session_id` on a non-free slot (console/user-held
    sessions). Kept separate because the delete needs a SQL predicate, not a Python set of ids.

    `state != FREE` rather than `IN (TASK, USER)`, so a new slot state defaults to protecting."""
    return exists(
        select(1)
        .where(Task.runner_session_id == AgentSession.agent_session_id)
        .correlate(AgentSession)
    ) | exists(
        select(1)
        .where(
            Worktree.launch_session_id == AgentSession.agent_session_id,
            Worktree.state != SLOT_FREE,
        )
        .correlate(AgentSession)
    )


def stale_clause(cutoff: datetime):
    """The one staleness predicate. The page's count, the delete and the list's `stale` filter all use
    it, so the shown number and the deleted set cannot diverge.

    Liveness is ANDed in here, not by callers, so no caller can forget it. Node scoping is not here:
    the list route applies its own read clause (which keeps the NULL arm), and folding a reach test in
    would narrow that read too."""
    return (
        (AgentSession.status == STATUS_RUNNING)
        & (AgentSession.transcript_status == TRANSCRIPT_PENDING)
        & (AgentSession.transcript_attempts == 0)
        & (AgentSession.transcript_leased_at.is_(None))
        & (AgentSession.started_at < cutoff)
        & ~_cp_holds_it_live()
    )


# No row-level `is_stale` twin, deliberately: a second Python spelling of a destructive predicate
# drifts. A caller holding an object should ask this clause in a query.


def count_stale(session: Session, principal: Principal | None, now: datetime | None = None) -> int:
    """How many rows the delete would take for this principal. Unbounded by `MAX_DELETE_PER_CALL`, so
    the operator sees the true backlog.

    Uses the same two clauses as `delete_stale`, so the banner never misstates what the button destroys."""
    cutoff = stale_cutoff(now)
    return int(session.execute(
        select(func.count()).select_from(AgentSession)
        .where(stale_clause(cutoff))
        .where(deletable_node_ref_clause(principal, AgentSession.node_id))
    ).scalar_one())


def delete_stale(session: Session, principal: Principal | None, now: datetime | None = None) -> int:
    """Delete the stale rows `principal` may destroy, at most `MAX_DELETE_PER_CALL` of them, and return
    how many went.

    Does not commit. Logs the destroyed identifiers (capped, count exact). Orders before it limits so
    truncated calls consume the oldest rows. Built as an expression so the inner SELECT reuses
    `stale_clause` and the node-reach clause directly.
    """
    cutoff = stale_cutoff(now)
    oldest_stale = (
        select(AgentSession.id)
        .where(stale_clause(cutoff))
        .where(deletable_node_ref_clause(principal, AgentSession.node_id))
        .order_by(AgentSession.started_at)
        .limit(MAX_DELETE_PER_CALL)
    )
    # `synchronize_session=False`: nothing holds these rows in the identity map, and the ORM cannot
    # evaluate `IN (SELECT …)`. RETURNING, not `rowcount`: the identifiers are the only audit trace.
    # The predicate is repeated on the target relation for EvalPlanQual: under READ COMMITTED the
    # subquery keeps the original snapshot, so a concurrent `/end` would otherwise still be deleted.
    # The reach clause stays in the subquery, beside the `select` it was written for.
    rows = session.execute(
        delete(AgentSession)
        .where(AgentSession.id.in_(oldest_stale))
        .where(stale_clause(cutoff))
        .returning(
            AgentSession.agent_session_id, AgentSession.node_id, AgentSession.task_id,
        ),
        execution_options={"synchronize_session": False},
    ).all()
    removed = len(rows)
    if removed:
        shown = rows[:MAX_DELETE_IDENTIFIERS_LOGGED]
        elided = removed - len(shown)
        log.info(
            "sessions cleanup: deleted %d never-ticketed running/pending session row(s) started "
            "before %s: %s%s",
            removed,
            cutoff.isoformat(),
            ", ".join(f"{sid} (node={node_id}, task={task_id})" for sid, node_id, task_id in shown),
            f" and {elided} more" if elided else "",
        )
    return removed
