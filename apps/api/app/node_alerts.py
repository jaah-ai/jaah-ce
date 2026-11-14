"""The tenant node alerts: `node-offline` and `runner-crashing`, one per episode, to the node org's Owners and Admins.

Both are computed by the `node-liveness` cron (`app/node_liveness_job.py::sweep_once`, every 300s) and sent with
`alert_record.record_alert(tell_org_admins=True)`, so every on channel of every Owner and Admin of the node's org
hears it (and the operator channel too). Members never do: both events are admin-only in
`app/notification_catalog.py`. The operator's own liveness alarms (`app/node_liveness.py`) are untouched.

NODE OFFLINE. An enabled node with `node_liveness.VERDICT_SILENT` (no report for `STALE_AFTER_SECONDS`) opens an
episode: `nodes.offline_alerted_at` is stamped and one alert sent. The stamp is cleared once the node reports in
again, so the next silence is a new episode. A disabled node never alerts (`verdicts_for`).

RUNNER KEEPS CRASHING. A node-wide count of `task_note:runner_terminated` rows naming the node (`data.node`; a
row with none is not counted) whose cause is in `CRASH_CAUSES`: `CRASH_THRESHOLD` inside `CRASH_WINDOW` opens an
episode (`nodes.crash_alerted_at`, one alert); a whole `CRASH_WINDOW` with no such row closes it.

Each alert's `issue` carries the node's uuid and the episode's start: names repeat across orgs, and
`uq_alerts_dedup` carries no org, so a name alone would let one org's alert swallow another's. The `issue` is a
dedup identity only and no person reads it: the `reason` is the message, its first line the headline
(`alert_relay._REASON_LEADS`), and `payload["console_url"]` links the node's page, whose Overview lists its
dead runs.

The stamp is claimed by a guarded UPDATE before the alert is sent, so two passes can never both send one; an
alert that could not be recorded gives its claim back for the next pass. An operator relay failure is not
retried: the Owners and Admins are told before the relay runs (`alert_record.record_alert`). Best effort: a
failure is logged, never raised into the sweep.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import alert_record, launch_failures, node_liveness, runner_terminations
from app.config import console_url
from app.entity_shape import optional_uuid
from app.models.audit import ActivityEvent
from app.models.nodes import Node

log = logging.getLogger("cockpit-scheduler")

OFFLINE_KIND = "node-offline"
CRASHING_KIND = "runner-crashing"

#: A runner that failed to start or died: the launch-failure causes and the causes the CP infers from a runner's
#: absence, each imported from its own home. A refused clone started no runner: it is no crash.
CRASH_CAUSES: frozenset[str] = (
    (frozenset(launch_failures.LAUNCH_FAILURE_CAUSES) - {runner_terminations.CAUSE_CLONE_FAILED})
    | runner_terminations.RUNNER_LOST_CAUSES
)
#: Crashes on one node inside `CRASH_WINDOW` that open an episode.
CRASH_THRESHOLD = 3
#: How far back a crash counts, and how long a node must go without one for its episode to close.
CRASH_WINDOW = timedelta(minutes=30)

#: `CRASH_CAUSES` as the alert reads them: what the run did in everyday words, and the one thing to check when it
#: is the commonest. Covers `CRASH_CAUSES` exactly (`tests/test_node_alerts.py`).
CRASH_GROUPS: tuple[tuple[str, frozenset[str], str], ...] = (
    (
        "never started",
        frozenset({runner_terminations.CAUSE_LAUNCH_UNANNOUNCED, runner_terminations.CAUSE_LAUNCH_FAILED}),
        "Check that the machine is on and has free disk space and memory.",
    ),
    (
        "stopped suddenly",
        frozenset({runner_terminations.CAUSE_VANISHED}),
        "Check that the machine is not running out of memory or restarting.",
    ),
    (
        "stopped responding",
        frozenset({runner_terminations.CAUSE_STALLED}),
        "Check the machine's network connection.",
    ),
)


def sweep(session: Session, rows: Iterable[node_liveness.NodeRow]) -> None:
    """One pass of both alerts. `rows` is the liveness sweep's roster. `session` must carry no open work."""
    roster = list(rows)
    for name, step in ((OFFLINE_KIND, lambda: _offline(session, roster)), (CRASHING_KIND, lambda: _crashing(session))):
        try:
            step()
        except Exception:  # noqa: BLE001 — a tenant alert must never break the liveness sweep
            session.rollback()
            log.exception("node-alerts: the %s pass failed", name)


# ── Node offline ─────────────────────────────────────────────────────────────────

def _offline(session: Session, rows: list[node_liveness.NodeRow]) -> None:
    """Close the episodes of nodes reporting again (or disabled, so a re-enabled node that is still down alerts
    afresh), then open one per newly silent enabled node. A node that never reported has not stopped reporting:
    a just-registered or just-launched node is still booting, so it is not alerted."""
    reporting = [uuid for row in rows if (uuid := optional_uuid(row.node_uuid))
                 and (not row.enabled or not node_liveness.is_stale(row.last_seen_age))]
    if reporting:
        session.execute(
            update(Node).where(Node.id.in_(reporting), Node.offline_alerted_at.isnot(None))
            .values(offline_alerted_at=None)
        )
        session.commit()
    for row in rows:
        node_id = optional_uuid(row.node_uuid)
        if node_id is None or row.last_seen_age is None:
            continue
        if node_liveness.VERDICT_SILENT not in node_liveness.verdicts_for(row):
            continue
        _open_episode(
            session, node_id, Node.offline_alerted_at, kind=OFFLINE_KIND,
            issue=lambda name, since, node_id=node_id: f"{name} ({node_id}) offline since {since}",
            reason=lambda name, since, age=row.last_seen_age: offline_message(name, age),
            enabled_only=True,
        )


# ── Runner keeps crashing ────────────────────────────────────────────────────────

def _one_line(name: str) -> str:
    """The node's name on one line: a typed newline would end the headline early and forge a line of the message."""
    return " ".join(name.split())


def offline_message(name: str, age_seconds: float) -> str:
    """The `node-offline` alert's text: the headline, then what it means and what to check."""
    minutes = max(1, int(age_seconds // 60))
    name = _one_line(name)
    return (
        f"Node {name} is offline\n"
        f"It last reported {minutes} minute{'s' if minutes != 1 else ''} ago, so no new tasks start there. "
        "Check that the machine is on and the Jaah service is running on it."
    )


def crash_message(name: str, causes: dict[str, int]) -> str:
    """The `runner-crashing` alert's text: the headline, how many runs failed and how (`CRASH_GROUPS`, commonest
    first), then the commonest group's check (a tie goes to the group listed first)."""
    groups = sorted(
        ((sum(causes.get(cause, 0) for cause in members), label, step) for label, members, step in CRASH_GROUPS),
        key=lambda group: -group[0],
    )
    seen = [(count, label, step) for count, label, step in groups if count]
    total = sum(causes.values())
    how = ", ".join(f"{count} {label}" for count, label, _step in seen)
    step = seen[0][2] if seen else ""
    minutes = int(CRASH_WINDOW.total_seconds() // 60)
    name = _one_line(name)
    return (
        f"Runs keep failing on node {name}\n"
        f"{total} run{'s' if total != 1 else ''} failed there in the last {minutes} minutes: {how}.\n"
        f"{step} The node's page lists each failed run.".lstrip()
    )


def crash_counts(session: Session) -> dict[UUID, dict[str, int]]:
    """Per node uuid, per cause: the `CRASH_CAUSES` terminations naming it inside `CRASH_WINDOW`. A row naming no
    node, or no parseable one, is not counted."""
    node_key = ActivityEvent.data["node"].astext
    cause_key = ActivityEvent.data["cause"].astext
    rows = session.execute(
        select(node_key, cause_key, func.count())
        .where(
            ActivityEvent.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
            cause_key.in_(sorted(CRASH_CAUSES)),
            node_key.isnot(None),
            ActivityEvent.created_at >= func.now() - CRASH_WINDOW,
        )
        .group_by(node_key, cause_key)
    ).all()
    session.rollback()
    counts: dict[UUID, dict[str, int]] = {}
    for raw, cause, count in rows:
        node_id = optional_uuid(raw)
        if node_id is not None:
            per_cause = counts.setdefault(node_id, {})
            per_cause[cause] = per_cause.get(cause, 0) + count
    return counts


def _crashing(session: Session) -> None:
    """Close the episodes of nodes with no crash in the window, then open one per node at the threshold."""
    counts = crash_counts(session)
    quiet = update(Node).where(Node.crash_alerted_at.isnot(None))
    if counts:
        quiet = quiet.where(Node.id.notin_(list(counts)))
    session.execute(quiet.values(crash_alerted_at=None))
    session.commit()
    for node_id, causes in sorted(counts.items(), key=lambda item: str(item[0])):
        if sum(causes.values()) < CRASH_THRESHOLD:
            continue
        _open_episode(
            session, node_id, Node.crash_alerted_at, kind=CRASHING_KIND,
            issue=lambda name, since, node_id=node_id: f"{name} ({node_id}) runners crashing since {since}",
            reason=lambda name, since, causes=causes: crash_message(name, causes),
            enabled_only=False,
        )


# ── The episode claim ────────────────────────────────────────────────────────────

def _open_episode(session: Session, node_id: UUID, stamp, *, kind: str, issue, reason, enabled_only: bool) -> bool:
    """Stamp `stamp` on `node_id` if no episode is open, and send its one alert. True when one was sent.

    The guarded UPDATE is the claim: a node with the stamp already set (an open episode) matches nothing. An alert
    that raises gives the claim back, so the next pass tries again; the fault is logged, never raised, so one node's
    failure never costs another node its alert."""
    claim = update(Node).where(Node.id == node_id, stamp.is_(None))
    if enabled_only:
        claim = claim.where(Node.enabled.is_(True))
    claimed = session.execute(
        claim.values({stamp.key: func.now()}).returning(Node.name, stamp)
    ).first()
    session.commit()
    if claimed is None:
        return False
    name, at = claimed
    since = _since_text(at)
    try:
        alert_record.record_alert(
            session, kind=kind, node_id=str(node_id), issue=issue(name, since), reason=reason(name, since),
            payload={"console_url": console_url(f"/nodes/{node_id}")}, tell_org_admins=True,
        )
    except Exception:  # noqa: BLE001 — see the docstring
        session.rollback()
        log.exception("node-alerts: %s for node %s not recorded — retried next pass", kind, name)
        session.execute(update(Node).where(Node.id == node_id).values({stamp.key: None}))
        session.commit()
        return False
    return True


def _since_text(at: datetime) -> str:
    """The episode's start, as its alert names it."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
