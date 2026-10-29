"""One node's health read: `GET /api/nodes/{node_id}/health` (clocks, findings, update history)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import node_liveness, node_log_findings, platform_release, pool_state
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.entity_shape import iso_or_none
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.worktrees import Worktree
from app.tenancy.ids import is_tenant_org
from app.routes.nodes import reads
from app.routes.nodes.node_wire import (
    NodeUpdateOut,
    _administers_own_hosts,
    _hosting_facts,
    _hosting_of,
    _on_a_foreign_host,
    _reported_generation,
    _updates_out,
)
from app.routes.nodes.reads import _readable_node_or_404

router = APIRouter(tags=["nodes"])


# ── Node health: the internal queues and clocks, for a human ───────────────────────────────────────
# The human-facing read of the liveness facts otherwise readable only by a machine (the 300s sweep,
# `GET /api/agent/nodes/liveness`). Read-only and derived per request; every rule is imported
# (`node_liveness.verdicts_for`, `STALE_AFTER_SECONDS`, `config_drift`, `fleet_verdicts`), so this page
# moves with the sweep.


class HealthClock(BaseModel):
    """One reporting clock, with the age the DB itself measured and the verdict that age earns.

    The DB's age, not the client's: a laptop with a skewed clock would otherwise render a healthy node
    stale; the client does no arithmetic."""

    key: str
    label: str
    # The stamp itself. None = never written, a different fact from "old".
    at: Optional[str] = None
    age_seconds: Optional[float] = None
    # `node_liveness.is_stale` verbatim: a NULL stamp is stale, since never reported is worse than late.
    stale: bool
    # The bound this verdict was measured against. `None` for a tenant on the two enumeration clocks only:
    # they are bounded by `fleet_max_observation_age_secs`, an operator-tier knob withheld from tenants
    # (`routes/settings.py::_settings_out`). `stale` itself is never withheld. The other clocks use a code
    # constant, which discloses nothing.
    bound_seconds: Optional[float] = None
    # What a stale reading on this clock means, so an operator knows which timestamp matters.
    meaning: str


class ConfigDriftRow(BaseModel):
    """One declared knob whose live value contradicts the declaration (`node_liveness.config_drift`'s `(key,
    want, got)`). `got` is None when the snapshot lacks the key: unset, or outside
    `CONFIG_SNAPSHOT_ALLOWLIST`."""

    key: str
    want: str
    got: Optional[str] = None


class HealthFinding(BaseModel):
    """One finding off the node's shipped journal or its own host series (`app/node_log_findings.py`): active in
    `NodeHealthOut.findings`, cleared within the day in `recent_findings`."""

    kind: str
    label: str
    detail: str
    first_at: Optional[str] = None
    last_at: str
    # What it means and what to do, one or two sentences.
    meaning: str
    # The tasks a finding is about and how many events it counts (`relaunch_storm`); empty/None for the rest.
    task_ids: list[int] = []
    count: Optional[int] = None


_FINDING_MEANINGS = {
    node_log_findings.CONVERGE_FAILED: (
        "The node's last converge refused its credential layer (the git credential helper or the agent-token "
        "helper), so clones on this node can fail. Fix what the detail names, then re-run the converge (update or "
        "reinstall the jaah package)."
    ),
    node_log_findings.NODELINK_STORE_UNRESPONSIVE: (
        "The daemon's check of the NodeLink shared store did not answer: a hung NFS (EFS) mount. Steps that "
        "touch the store stall until it answers; remount it or reboot the machine."
    ),
    node_log_findings.STEP_WEDGED: (
        "A daemon step has been running far longer than its interval and is skipping its runs; the daemon "
        "keeps heartbeating, so look at what that step waits on."
    ),
    node_log_findings.OOM_KILL: (
        "The machine ran out of memory and the kernel killed a process; the job or service it belonged to "
        "failed. Find what filled memory on the node's runners and stacks."
    ),
    node_log_findings.MEMORY_CRITICAL: (
        "Memory is at or above 90%; no new runners or CI jobs are placed here until it stays below for 10 "
        "minutes. Find the consumer on the node's runners and stacks."
    ),
    node_log_findings.CPU_CRITICAL: (
        "CPU is at or above 90%, so everything on the node runs slowly; placement does not wait on CPU, so "
        "new work still lands here."
    ),
    node_log_findings.RELAUNCH_STORM: (
        f"The same task's runner vanished {node_log_findings.STORM_MIN_SESSIONS} or more times within "
        f"{int(node_log_findings.STORM_SPAN.total_seconds() // 60)} minutes: each relaunch on this node was lost. "
        "Open the task's sessions, and check the node's memory and daemon."
    ),
    node_log_findings.ENGINE_UNREADY: (
        "The node's container engine (its jaah-engine WSL 2 distro) is not answering docker info, so nothing that "
        "needs a container runs here. Usually WSL 2 cannot start its VM: turn on virtualization (on EC2, nested "
        "virtualization on a C8i, M8i or R8i type). The detail names the node's engine log."
    ),
    node_log_findings.FLEET_ANCHOR_UNVERIFIED: (
        "The daemon cannot tie the fleet tmux server to the jaah-runner service, so it refuses every runner on this "
        "Windows node as not_a_runner. It replaces a server left from before a jaah-runner restart by itself; if "
        "this stays, restart jaah-runner, then the node."
    ),
    node_log_findings.TMP_INODES_HIGH: (
        "/tmp is running out of inodes, usually from files left behind by tests or scripts. At 100% every "
        "mktemp on the node fails even with disk free; remove stale entries under /tmp."
    ),
}


class NodeHealthOut(BaseModel):
    """Everything that says whether this node is in trouble, in one read."""

    node_id: str
    enabled: bool
    # The other half of the switch: `enabled` alone renders a draining node as Enabled, and `verdicts` will
    # not say why it lands nothing (a requested wind-down is not an alarm, so `node_liveness` has no drain
    # condition). `app/models/nodes.py::Node.draining` is the home for the meaning.
    draining: bool = False
    # `node_liveness.verdicts_for` verbatim: `["live"]`, `["disabled"]`, or every violated condition. Not
    # a single status, since a node can be silent and unenrolled at once.
    verdicts: list[str] = []
    healthy: bool
    # The verdict bound (`node_liveness.STALE_AFTER_SECONDS`) for the silent/not-observing conditions. The
    # enumeration clocks use the operator-tunable `pool_state.max_observation_age_secs()`, so render each
    # clock's own `bound_seconds`, never this.
    stale_bound_seconds: float
    clocks: list[HealthClock] = []
    # No `intents` block, deliberately: the queue lives whole at `GET /api/nodes/intents`
    # (`IntentPageOut.counts`); a second copy under a different WHERE would disagree.
    config_drift: list[ConfigDriftRow] = []
    # Presence-only nodes answer too: with no row they have no clocks, queue or declaration, and
    # `registered=false` says so.
    registered: bool
    # Active findings off the node's own journal and host series; any one makes `healthy` false. A liveness
    # fact about the reader's box, so served to tenants too — less CPU/memory on another org's host, which
    # are the host's (`_on_a_foreign_host`, as the metrics read).
    findings: list[HealthFinding] = []
    # Journal findings that cleared within the last day (`node_log_findings.recent`): past, so they never make
    # `healthy` false, but what explains an incident the reader arrives after. Disjoint from `findings`.
    recent_findings: list[HealthFinding] = []
    # The updater's recent outcomes, newest first (`_updates_out` says what a tenant reader is not told).
    updates: list[NodeUpdateOut] = []


# ── The health read's own helpers ─────────────────────────────────────────────────────────────────


# The five stamps a node writes, in reading order: on the bus, observing, occupancy trustworthy, runner
# enumeration current, config report current. Each carries its own bound: `last_seen`/`observed_at` take
# `node_liveness.STALE_AFTER_SECONDS` (the sweep's conditions); the enumeration stamps take the
# operator-tunable `pool_state.max_observation_age_secs()`, the real dispatch gate (`app/pool_state.py`);
# `config_reported_at` takes the liveness bound as a reading aid only (it advances on content change).
def _clocks(row: Optional[Node], now: datetime, *, withhold_operator_bound: bool) -> list[HealthClock]:
    """The five reporting clocks with DB-measured ages. Empty for a presence-only node (no row, and five
    "never" rows would misreport it as broken).

    `withhold_operator_bound` blanks the two enumeration clocks' bound on the payload only (see
    `HealthClock.bound_seconds`). Keyword-only with no default, so every caller decides.

    Withheld from the payload, never the comparison: `node_liveness.is_stale(age, bound)` needs the number
    (a `None` bound is a TypeError whenever a stamp is non-null), and the verdict is what a tenant keeps."""
    if row is None:
        return []
    liveness_bound = node_liveness.STALE_AFTER_SECONDS
    # Read once (a DB-backed setting), and always: it decides `stale` for both planes.
    enumeration_bound = pool_state.max_observation_age_secs()
    # The clocks whose bound is an operator knob, named by key (bounds could coincide by value).
    withheld_bound_keys = (
        {"worktrees_updated_at", "runners_updated_at"} if withhold_operator_bound else set()
    )
    spec = (
        (
            # "Presence", not "Presence (bus)": the bus is gone; presence is the node's connection.
            "last_seen", "Presence", row.last_seen, liveness_bound,
            "When this node was last seen connected, written by the presence sweep alone. Stale here is "
            "the SILENT verdict: the node's daemon is not connected, so nothing else on this page can be "
            "trusted.",
        ),
        (
            "observed_at", "Observation pass", row.observed_at, liveness_bound,
            "The node's own stamp for its last observation pass. Stale here while presence is fresh is "
            "NOT-OBSERVING: connected, but contributing nothing that can be acted on.",
        ),
        (
            "worktrees_updated_at", "Pool occupancy", row.worktrees_updated_at, enumeration_bound,
            # Names the node's own consequence, not the scheduler's gating mechanism, which would disclose what the
            # withheld bound governs to a tenant.
            "The last COMPLETE slot enumeration — advanced only when the node vouched for the whole "
            "list. Past this bound its reported occupancy stops being trusted, so no new work is "
            "placed on it even while it looks live.",
        ),
        (
            "runners_updated_at", "Runner enumeration", row.runners_updated_at, enumeration_bound,
            "The last COMPLETE runner enumeration, gated on its own completeness flag exactly as pool "
            "occupancy is. Stale here means the runners this node reports are a partial list.",
        ),
        (
            "config_reported_at", "Config snapshot", row.config_reported_at, liveness_bound,
            "When the daemon last published its effective config. It advances only when the config "
            "CONTENT changes, so age here is NOT by itself a fault — it is the age of the evidence the "
            "drift check is decided on.",
        ),
    )
    clocks: list[HealthClock] = []
    for key, label, stamp, bound, meaning in spec:
        age = (now - stamp).total_seconds() if stamp is not None else None
        clocks.append(
            HealthClock(
                key=key,
                label=label,
                at=iso_or_none(stamp),
                age_seconds=age,
                # `node_liveness.is_stale` (a NULL stamp is stale), computed from the real bound for every reader;
                # the number is dropped afterwards for those who may not have it.
                stale=node_liveness.is_stale(age, bound),
                bound_seconds=None if key in withheld_bound_keys else bound,
                meaning=meaning,
            )
        )
    return clocks



@router.get("/api/nodes/{node_id}/health", response_model=NodeHealthOut)
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def get_node_health(
    node_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> NodeHealthOut:
    """This node's internal queues and reporting clocks: the read that says whether it is in trouble.

    Node-grant scoped through `_readable_node_or_404`, as `get_node` is (D13).

    Read-only: a bare `SELECT` and pure classification; opening a health tab must not nudge a node's queue.
    `UserActivityMiddleware` still writes its universal read-trail row fire-and-forget (so a test counting
    rows would race it); this route's own property is no non-`read` event and every touched row unchanged.

    The clock is the DB's `now()`, read once in the same session as the rows, never the browser's."""
    _presence, row = _readable_node_or_404(session, principal, node_id)
    now = session.scalar(select(func.now()))
    # `is_tenant_org` on the caller's own org: the withholding is about which plane reads.
    clocks = _clocks(row, now, withhold_operator_bound=is_tenant_org(user.org_id))
    drift = node_liveness.config_drift(
        dict(row.config_desired) if row and row.config_desired else {},
        row.config_snapshot if row else None,
    )
    if row is None:
        # A presence-only host: no row, so no clocks, queue, declaration or verdict (a zeroed NodeRow would
        # falsely accuse it). `registered=false` says it is not a member yet.
        return NodeHealthOut(
            # `draining=False`: an unregistered host has never been given the command.
            node_id=node_id, enabled=True, draining=False, verdicts=[], healthy=True,
            stale_bound_seconds=node_liveness.STALE_AFTER_SECONDS,
            clocks=[], config_drift=[], registered=False,
        )
    ages = {c.key: c.age_seconds for c in clocks}
    slots = session.scalar(
        select(func.count()).select_from(Worktree).where(Worktree.node_id == row.id, Worktree.number > 0)
    ) or 0
    generation = _reported_generation(row)
    # Against what this node's platform can install (`app/platform_release.py`), as the Nodes list places it.
    published = platform_release.for_os(row.os, reads._published_release(session, [row])).version
    placement = node_liveness.place_generation(generation, published)
    # The release grace window: a node behind right after the fleet published is unproven, not stuck, and the
    # sweep suppresses STALE-RELEASE there (`node_liveness`); ignoring it would make page and alarm disagree
    # for the first minutes of every rollout. Read only for a behind placement.
    release_grace_active = False
    if placement == node_liveness.GENERATION_BEHIND:
        from app.routes.agent import releases as agent_releases  # deferred: the routes<->routes cycle, as `reads._published_release`

        release_grace_active = node_liveness.release_desired_in_grace(
            getattr(agent_releases.cp_release_desired(), "updated_age", None)
        )
    # The verdicts are `node_liveness`'s, from a NodeRow assembled here. Unneeded fields are still filled,
    # since a half-filled sweep shape is how the two readings drift.
    liveness_row = node_liveness.NodeRow(
        node_id=row.name,
        enabled=bool(row.enabled),
        last_seen_age=ages.get("last_seen"),
        observed_at_age=ages.get("observed_at"),
        worktrees_updated_age=ages.get("worktrees_updated_at"),
        last_seen=iso_or_none(row.last_seen),
        observed_at=iso_or_none(row.observed_at),
        slots=int(slots),
        worktree_rows=int(session.scalar(
            select(func.count()).select_from(Worktree).where(Worktree.node_id == row.id)
        ) or 0),
        generation=generation,
        generation_reported_age=ages.get("config_reported_at"),
        placement=placement,
        drift=drift,
    )
    verdicts = node_liveness.verdicts_for(liveness_row, release_grace_active=release_grace_active)
    # Read below (served verdicts, clocks, drift rows); reuse these names. A tenant administrator keeps the
    # config fields `_narrow_for_tenant` serves them; the release verdict is withheld from every tenant.
    tenant = is_tenant_org(user.org_id)
    config_withheld = tenant and not _administers_own_hosts(user, principal)
    if tenant and node_liveness.VERDICT_STALE_RELEASE in verdicts:
        # STALE-RELEASE is `generation_placement: behind` through another door, which `_narrow_for_tenant`
        # blanks, so it is dropped from the served list for tenants (computed from the full row; an emptied list
        # reads `["live"]`). CONFIG_DRIFT stays: drift is a liveness fact about the tenant's box, a pending
        # release is the operator's rollout.
        verdicts = [v for v in verdicts if v != node_liveness.VERDICT_STALE_RELEASE] or [
            node_liveness.VERDICT_LIVE
        ]
    hosting, _hosted_on, host_org_id = _hosting_of(row, _hosting_facts(session, [row.id]).get(row.id))
    pressure = (
        [] if _on_a_foreign_host(user, hosting, host_org_id) else node_log_findings.pressure(session, row.id, now)
    )
    # A tenant is told only its own org's sessions (task ids are another org's otherwise).
    storm = node_log_findings.relaunch_storm(session, row.id, now, org_id=user.org_id if tenant else None)
    findings = [
        HealthFinding(
            kind=kind, label=node_log_findings.LABELS[kind], detail=str(f.get("detail") or ""),
            first_at=f.get("first_at"), last_at=f["last_at"], meaning=_FINDING_MEANINGS[kind],
            task_ids=f.get("task_ids") or [], count=f.get("count"),
        )
        for kind, f in [*pressure, *storm, *node_log_findings.active(row.log_findings, now)]
    ]
    recent_findings = [
        HealthFinding(
            kind=kind, label=node_log_findings.LABELS[kind], detail=str(f.get("detail") or ""),
            first_at=f.get("first_at"), last_at=f["last_at"], meaning=_FINDING_MEANINGS[kind],
        )
        for kind, f in node_log_findings.recent(row.log_findings, now)
    ]
    return NodeHealthOut(
        node_id=row.name,
        enabled=bool(row.enabled),
        # Straight off the column, like `enabled`; `verdicts` carry no drain condition.
        draining=bool(row.draining),
        verdicts=verdicts,
        # Healthy is the absence of every alarm: `["disabled"]` is never alarmed. Computed from the served list,
        # and no active finding.
        healthy=not findings and verdicts in ([node_liveness.VERDICT_LIVE], [node_liveness.VERDICT_DISABLED]),
        stale_bound_seconds=node_liveness.STALE_AFTER_SECONDS,
        # The drift rows are the two fields `_narrow_for_tenant` blanks for a non-superadmin tenant, side by
        # side; that reader gets the CONFIG_DRIFT verdict but none of the rows, and the `config_reported_at`
        # clock is dropped (not blanked, which would render a permanently stale accusation). Verdicts were
        # computed from the full clocks, so a tenant's verdicts equal the operator's less STALE_RELEASE.
        clocks=(
            [c for c in clocks if c.key != "config_reported_at"]
            if config_withheld
            else clocks
        ),
        config_drift=(
            []
            if config_withheld
            else [ConfigDriftRow(key=k, want=w, got=g) for k, w, g in drift]
        ),
        registered=True,
        findings=findings,
        recent_findings=recent_findings,
        updates=_updates_out(row.update_history, tenant=tenant),
    )
