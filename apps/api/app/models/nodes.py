"""Nodes: the node row, its bus socket, host metrics and disk events, MicroVMs and node orders."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    REAL,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
# Leaf import (stdlib-only): `Node.shared_accounts_root` defaults to its constant, so the column is a
# per-node override, not a second home for the path.
from app.shared_storage import LEGACY_SHARED_ACCOUNTS_ROOT
from app.models.base import (
    Base,
    EntityMixin,
    _in_check,
    org_unique,
    OrgScoped,
    StampedMixin,
    timestamp_column,
    UuidPkMixin,
)


class Node(EntityMixin, OrgScoped, Base):
    """Persisted fleet roster — one durable row per jaahd host.

    The DB is the roster; the bus is liveness. Membership is declared (`POST /api/nodes` is the sole
    creator); online/offline is always derived from presence — the node's `node_bus_sockets` row joined to
    a live `cp_peers` row (`app/node_presence.py`); `last_seen` is cosmetic.

    A standard entity since 0106 (see its docstring): uuid `id` with real FKs onto it; `name` replaced
    `node_id`/`display_name`, NOT NULL, not unique (a label, never an identity), freely renameable
    (node.env holds the uuid). No `slug`; the uuid is every address (URLs included), the name a display handle. `first_seen` stays: it is
    the registered/unregistered discriminator in `GET /api/nodes`.

    `id` is the wire identity, `name` the human and tailnet handle (`app/node_identity.py`).
    `config_snapshot` is the daemon-published, allowlist-redacted effective config, NULL until reported."""

    __tablename__ = "nodes"
    # Named: `ON CONFLICT constraint=` clauses reach constraints by name.
    __table_args__ = (
        org_unique(__tablename__),
        # Draining qualifies `enabled`: `(enabled=f, draining=t)` means nothing (a disabled node has
        # finished winding down). In SQL so every writer meets it; the route's 422 is the readable half.
        CheckConstraint(
            "NOT (draining AND NOT enabled)", name="ck_nodes_draining_implies_enabled"
        ),
        # The `app/node_platform.py` vocabularies (0326).
        CheckConstraint("os IN ('ubuntu', 'macos', 'windows')", name="ck_nodes_os"),
        CheckConstraint(
            "os_reported IN ('linux', 'macos', 'windows')", name="ck_nodes_os_reported"
        ),
        # The `app/node_disk_events.py` vocabulary (202610111800).
        CheckConstraint("disk_gate IN ('open', 'paused')", name="ck_nodes_disk_gate"),
        # A node token authenticates exactly one node.
        Index(
            "uq_nodes_node_token_hash", "node_token_hash", unique=True,
            postgresql_where=text("node_token_hash IS NOT NULL"),
        ),
        # And a rotation's pending one, which authenticates beside it until confirmed (202610221637).
        Index(
            "uq_nodes_pending_node_token_hash", "pending_node_token_hash", unique=True,
            postgresql_where=text("pending_node_token_hash IS NOT NULL"),
        ),
        Index(
            "ix_nodes_node_group_capacity_id", "node_group_capacity_id",
            postgresql_where=text("node_group_capacity_id IS NOT NULL"),
        ),
    )

    # The OS this node was CREATED as (0326): declared, never changed, never inferred from the report.
    # What it lets the node do is `app/node_platform.py`'s table. Every pre-0326 row is Ubuntu.
    os: Mapped[str] = mapped_column(
        Text, nullable=False, default="ubuntu", server_default=text("'ubuntu'")
    )
    # Sensed by `app/fleet_observe.py::node_stamp_statement` from the pass's `platform` block (0326);
    # NULL = never reported. `arch` is one of `node_platform.ARCH_VALUES`; `os_version` free text.
    arch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    os_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The OS family the machine itself reports (`linux`/`macos`/`windows`), kept only so a contradiction
    # with `os` is a node blocker (`node_platform.platform_blocker`) rather than a re-label.
    os_reported: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Also sensed from the `platform` block; NULL = never reported. `virtualization` is the hypervisor
    # the machine runs under (`none` = physical, `amazon`, `kvm`, …); `kvm` whether `/dev/kvm` exists there.
    virtualization: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kvm: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Also from the `platform` block; NULL = never reported. `hypervisor` is the one MicroVMs would run
    # on (`node_platform.HYPERVISOR_VALUES`); `microvm_prereq` the first unmet MicroVM prerequisite a Windows or macOS
    # node names (`microvm_mode.PREREQ_SENTENCES`), NULL when none is.
    hypervisor: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    microvm_prereq: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The launch kinds this node's jaahd can enact (`["agent"]`), sensed from the `platform` block
    # like `arch`; NULL = never reported, which placement reads as "cannot run SDK agents" (`app/agents.py`).
    launch_kinds: Mapped[Optional[list[str]]] = mapped_column(ARRAY(Text), nullable=True)
    # A Linux node's release (`/etc/os-release` `ID`/`VERSION_ID`) and its probed host features
    # (`{feature: bool}`, `node_platform.FEATURE_VALUES`), sensed from the `platform` block; NULL = never reported,
    # and a feature key absent = not probed. `node_platform.node_reason` turns them into refusals.
    distro_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    distro_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    host_features: Mapped[Optional[dict[str, bool]]] = mapped_column(JSONB, nullable=True)

    tags: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # The operator's Drain. `enabled=t, draining=t`: no new work (`pool_state.live_nodes_for_project` and
    # `placement_grants._placeable_slot_clause` both exclude it); held work stays (`live_nodes` keeps it,
    # `routes/fleet.py::owns_candidate`'s pin; a relaunch reuses the same tree). Ends itself:
    # `lease_drain._disable_drained_nodes` flips both off once no `state='task'` worktree remains (user
    # slots do not count). A boolean, not an enum replacing `enabled`, so it stayed additive.
    draining: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The disk gate (202610111800): `paused` while this node's disk went critical and has not yet dropped
    # under warn — no runner launches on it and its CI runners take no new job. Machine state, never an
    # operator's: `app/node_disk_events.py` is its one writer and holds the hysteresis rule.
    disk_gate: Mapped[str] = mapped_column(Text, nullable=False, default="open", server_default="open")
    disk_gate_changed_at: Mapped[datetime] = timestamp_column()
    # MicroVM mode (revision 202610111900): every task of this node's projects runs in its own microVM. Only
    # `app/microvm_mode.py` decides whether it may flip. The three sizes are the node override, which wins
    # over every other tier (`app/microvm_sizing.py`); NULL = none.
    microvm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    microvm_vcpus: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    microvm_ram_mib: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    microvm_disk_gib: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # KVM for sessions (revision 202610292000): a co-located node's session user may open the host's `/dev/kvm`.
    # Written by `PUT /api/nodes` (the host's owner only); read by `vnodes.desired` for the host helper.
    session_kvm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Unread leftover of the retired per-node shared-accounts switch;
    # new nodes are born false. Drop in a later contract migration.
    shared_accounts_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Where this host mounts that store (the switch says whether). The share is one trust domain,
    # so it must become per-org (spec §4.2), which needs a per-node path. Defaults to
    # `app/shared_storage.py::LEGACY_SHARED_ACCOUNTS_ROOT`, which the node's `apps/jaahd/shared_storage.py`
    # also carries. NOT NULL, not nullable-means-default. The node resolves its root itself (its
    # `{jaah_home}/auth/.shared`
    # NodeLink at that path, else the legacy literal; `app/vnodes.py::permitted_accounts_roots`), so the console
    # API no longer accepts it from a body (`routes/nodes/update.py::NodeUpdateIn`).
    shared_accounts_root: Mapped[str] = mapped_column(
        Text, nullable=False,
        default=LEGACY_SHARED_ACCOUNTS_ROOT, server_default=LEGACY_SHARED_ACCOUNTS_ROOT,
    )
    # Account removals this node still owes (`app/account_removal.py`): each `{verb, args, actor}` an account Delete
    # could not land here (offline, or the node failed), sent again when the node next connects.
    owed_account_removals: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    role: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Every project's clone path derives from this plus the repo's org/repo (`app/node_clone.py`). Acted
    # on; NULL means no home declared, so no project resolves (no fallback).
    jaah_home: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # No key this release reads or writes: its last one, Max runners, was declarative and is retired (revision
    # 202610241600). The column is left for a later contraction to drop.
    capacity: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    infra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    config_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    config_reported_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # This node's own credential as `sha256(plaintext)`, never the token (0162); rules live in
    # `app/node_credentials.py`. Unique, so one credential names one node. Nullable only for rows
    # predating 0162 (`POST /api/nodes` mints in the same transaction). Secret material: `cp_frame_proof`
    # is computed from it, so reading this column lets one forge the CP's half of the node-frame
    # double guard (`node_frame_auth`). Treat a dump of `nodes` as a credential dump.
    node_token_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    node_token_issued_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # A rotation's next credential, minted but not yet confirmed by the node (`node_credentials.rotate`); it
    # authenticates beside `node_token_hash` until the confirm promotes it. Same secret class as that column.
    pending_node_token_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When this credential last authenticated, at most once a minute
    # (`node_credentials._USED_AT_THROTTLE_SECONDS`): is the credential about to be replaced in use? NULL
    # after every issue.
    node_token_used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Until when an operator's re-enrol (`POST /api/nodes/{node_id}/tailnet-reenrol`) lets this node's
    # bootstrap mint ONE tailnet key despite `last_seen` (0272). NULL = not armed; the mint clears it
    # (`routes/agent_bootstrap.py::_may_mint_a_tailnet_key`).
    tailnet_reenrol_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # `{ENV_KEY: desired value}` this node's `config_snapshot` must match; only present keys are asserted
    # (`{}` = nothing declared). Checked by `app/node_liveness.py`'s CONFIG_DRIFT verdict.
    config_desired: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    # This node's own auto-update override of the Settings knob `node_autoupdate_enabled`; NULL = follow
    # Settings. On a Multi-Node Host's row it is the host's override, governing every node it carries. The
    # resolution is single-homed in `app/node_autoupdate.py`.
    autoupdate_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Coarse bookkeeping, refreshed at most once/node/60s by the presence sweep; never the liveness source.
    first_seen: Mapped[datetime] = timestamp_column()
    last_seen: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The node's own stamp for its last observation pass: the liveness clock (`config_reported_at` only
    # moves when content changes).
    observed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    runners_updated_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Which runner CLI binaries uid `jaah` has on this host (a subset of `app/agent_providers.py`'s
    # `binary` values), sensed by the node and written by `app/fleet_observe.py::node_stamp_statement`:
    # the signal for the provider dispatch refusal. Node-level (one install per host). Sensed, never
    # operator-written. NULL (never attested: older daemons) is not `[]` (attested, none); no default.
    runner_clis: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)
    # The node's daemon senses ACP adapters (`agent_providers.<provider>.acp_binary`) in `runner_clis`, stamped with
    # it. False = a daemon that never said so (an older one), whose `runner_clis` cannot show a missing
    # adapter, so `routes/fleet.py::_acp_adapter_missing` does not judge it. Sensed, never operator-written.
    senses_acp_adapters: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Each catalog-resolved provider's live model list as this host's CLI reports it, keyed by provider:
    # `{<provider>: {"observed_at": ..., "models": [{"slug", "visibility", "priority"}, ...]}}`. Sensed by
    # `apps/jaahd/provider_models.py`, merged per provider by `app/fleet_observe.py::node_stamp_statement`
    # and read at dispatch by `dispatch_compose.compose._node_model_catalog` (`app/model_catalog.py` owns the
    # rule). NULL = never reported; no default. Never operator-written.
    model_catalogs: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Recent daemon trouble the node's own shipped journal shows (a hung NodeLink store, a wedged daemon
    # step), keyed by kind: `{<kind>: {"first_at", "last_at", "detail"}}`. Written by `POST /api/agent/
    # node-logs` and read by the Health tab; `app/node_log_findings.py` owns the rule. Sensed, never
    # operator-written; `{}` = nothing seen.
    log_findings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # What the node's shipped journal says happened, folded at the same ingest by `app/node_log_history.py`
    # (which owns their shapes and caps). `update_history`: the updater's outcomes, newest first, at most 20.
    # `gha_recent_jobs`: per host GHA runner unit, its last 5 jobs. `recent_errors`: the newest warning-or-worse
    # journal lines, served beside a session that ended unexplained (`node_log_history.end_lines`). Sensed,
    # never operator-written; NULL = nothing seen yet (no default; added expand-only).
    update_history: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSONB, nullable=True)
    gha_recent_jobs: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    recent_errors: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSONB, nullable=True)

    # The dynamic-node-group capacity line this node was launched for (0169), else NULL; per
    # line because reconcile counts per line. RESTRICT: no path can delete a line (and orphan a billing
    # machine) while an instance launched for it runs.
    node_group_capacity_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("node_group_capacity.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Set by the operator's org transfer of this node (`app/tenancy/transfer_node.py`) and cleared once the node
    # has confirmed the old org's account dirs erased (`app/durable/node_transfer.py`): until then it is in no
    # placement floor (`placement_grants.org_floor`), so no task of the new org runs beside the old org's tokens.
    org_transfer_cleanup_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # The operator allocated this machine to its tenant (`app/node_allocation.py`): set when the operator moves it
    # out of the operator plane, flipped by the operator alone. The tenant uses it and places projects on it, but
    # changes only its labels and service switches and never deletes it.
    operator_allocated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # The pool-cap freshness clock, separate from `observed_at`: a pass that could not enumerate slots
    # proves liveness but not occupancy, and treating it as enumerated fails open (`app/pool_state.py`).
    # Advanced only when `worktrees_complete`.
    worktrees_updated_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The node's public IPv4 as its own tailnet client sees it (`tailscale netcheck` GlobalV4), reported by
    # jaahd to `POST /api/agent/nodes/egress` (0273). NULL = never reported or no tailnet. The headscale
    # emergency allowlist is built from it (`app/headscale_allowlist.py`). Sensed, never operator-written.
    egress_ipv4: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    egress_ipv4_reported_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The node's NodeVault identity key (NIK, 0377): the PUBLIC half (base64 SPKI DER) the daemon minted and
    # posted to `POST /api/agent/nodevault/identity`, and its fingerprint `hex(sha256(SPKI DER))` — always
    # computed here, never taken from the node. Set once: a different key is a 409 until an admin resets
    # the identity. `nodevault_capable` is the daemon's crypto probe, re-reported on every post (stamped in
    # `nodevault_capable_at`, 202610251400; NULL = not reported since); a node that is not capable is never
    # granted a key.
    nodevault_identity_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    nodevault_identity_fp: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    nodevault_capable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    nodevault_identity_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    nodevault_capable_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # `created_at`/`updated_at` come from `EntityMixin`; no `onupdate`: the `trg_nodes_set_updated_at`
    # trigger owns `updated_at` (0106).


class NodeBusSocket(Base):
    """Which peer holds a node's live node-bus WebSocket — routing AND presence in one row
    (`app/node_presence.py`).

    Keyed on the bus machine key the node presented (text), not a `nodes` FK: an unregistered node is
    still present on the bus and still keyed by the value it dialled with. A node redialling onto
    any peer displaces the row; a peer's death removes its rows by cascade."""

    __tablename__ = "node_bus_sockets"

    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    peer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_peers.id", ondelete="CASCADE"), nullable=False
    )
    # The holder's per-connection id (`app/node_bus.py::NodeConnection.socket_id`): a teardown deletes
    # only the row its own socket wrote, so a redial onto the SAME peer cannot be evicted by the old
    # socket's late close.
    socket_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    connected_at: Mapped[datetime] = timestamp_column(server_now=False)
    last_seen_at: Mapped[datetime] = timestamp_column(server_now=False)


class NodeHostMetric(OrgScoped, Base):
    """One node's CPU/RAM/disk/network reading for one minute — the series behind the Nodes Load column
    and the node Metrics tab.

    `bucket_at` is the minute: the node posts a windowed mean every ~5s tick (`apps/jaahd/
    host_metrics.py`) and the ingest truncates `observed_at` to 60s in SQL, so ~12 posts land on one row.
    `uq_node_host_metrics_node_bucket` is both the write throttle and the idempotency for the observe
    route's deadlock retries. `app/node_host_metrics.py` owns every rule over these columns (bucket width,
    7-day retention, thresholds, levels, staleness, statement builders); never re-spell them.

    CASCADE, unlike `CommandAudit.node_id`: a metric series attributed to nobody is noise."""

    __tablename__ = "node_host_metrics"
    __table_args__ = (
        # One unique index for both the upsert arbiter and `latest_map`'s `DISTINCT ON ... bucket_at
        # DESC` (btrees scan backwards at forward cost; a DESC index would be pure write amplification).
        Index("uq_node_host_metrics_node_bucket", "node_id", "bucket_at", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    bucket_at: Mapped[datetime] = timestamp_column(server_now=False)
    # `double precision`: means of sampled counters. NaN/Inf would store, so the wire model rejects them
    # and `node_host_metrics.classify` calls one `unknown`.
    cpu_pct: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    mem_pct: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    # Static facts for labels ("of 16 cores"); nullable, since absence degrades only the label.
    cpu_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mem_total_kb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # The observed span, not the target (a 40s-old daemon reports ~40.0). NOT NULL: the node omits the
    # whole block rather than send a partial one.
    window_secs: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    # Free and total disk where clones live, in kB (as `mem_total_kb`). The latest value, never a
    # mean: free disk is a level (`apps/jaahd/host_metrics.py`).
    disk_free_kb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    disk_total_kb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Free and total inodes of the node's /tmp, latest like disk. NULL means the daemon said nothing.
    tmp_inodes_free: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    tmp_inodes_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Bytes per second received / sent over the node's device-backed NICs: a windowed mean like
    # `cpu_pct` — traffic flowing, not link capacity. NULL means the daemon said nothing.
    net_rx_bps: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    net_tx_bps: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    # OS patch state, SOC2 G-35 evidence: Ubuntu's reboot-required flag (nodes never auto-reboot) and the
    # last unattended-upgrades run. NULL means the daemon said nothing, as above.
    reboot_required: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_upgrade_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Pressure signals, each the latest value like disk; NULL means the daemon said nothing.
    # Load average (`/proc/loadavg`), swap in kB (`/proc/meminfo`), and PSI `some avg60` percentages.
    load_1: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    load_5: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    load_15: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    swap_total_kb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    swap_free_kb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    psi_cpu_pct: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    psi_mem_pct: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    psi_io_pct: Mapped[Optional[float]] = mapped_column(Float(precision=53), nullable=True)
    # The busiest processes `[{name, user, cpu_pct, rss_kb}]`: the process NAME (`comm`), never argv, which
    # holds secrets. `none_as_null`, so "said nothing" is SQL NULL, not JSON `null`.
    top_processes: Mapped[Optional[list]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # The CP's clock (when the CP last heard about this minute; `bucket_at` is the node's). No
    # `onupdate`: writes are raw-SQL upserts, so `upsert_statement` sets it explicitly.
    updated_at: Mapped[datetime] = timestamp_column()


class NodeSessionUsage(OrgScoped, Base):
    """One runner session's CPU/RAM on one node for one minute — the host's live Sessions table and a task's
    per-session chart. `app/node_session_usage.py` owns every rule; the grain, retention and freshness are
    `node_host_metrics`'s, never re-spelled.

    Written under the NODE's org, never the task's: `task_id` is node-attested, so readers join `tasks` only
    where the task's org equals the row's (a node cannot plant usage on another org's task). No FK on
    `task_id`: a deleted task's minutes age out with the rest. CASCADE on the node, as `NodeHostMetric`."""

    __tablename__ = "node_session_usage"
    __table_args__ = (
        # The upsert arbiter.
        Index(
            "uq_node_session_usage_key_bucket", "node_id", "task_id", "session_id", "bucket_at", unique=True
        ),
        Index("ix_node_session_usage_task_bucket", "task_id", "bucket_at"),
        # The prune's and the live read's per-node range (the unique index leads with task and session).
        Index("ix_node_session_usage_node_bucket", "node_id", "bucket_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The runner's session id; "" until the runner has one (a fresh launch).
    session_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    bucket_at: Mapped[datetime] = timestamp_column(server_now=False)
    # Percent of ONE core (top-style; 200 = two cores busy), over the process tree rooted at the pane pid.
    cpu_pct: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    rss_kb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    procs: Mapped[int] = mapped_column(Integer, nullable=False)
    # The pane pid the tree is rooted at; the latest minute's.
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = timestamp_column()


class TaskSessionUsagePeak(OrgScoped, Base):
    """One runner session's peaks over its whole life — run telemetry, kept with no retention.

    `app/node_session_usage.py` owns the write (a `GREATEST` upsert per figure) and the reads. Written under
    the node's org like `NodeSessionUsage`, and read only joined to a task of the same org. No FK on either
    id: the peak outlives a deleted node, and a deleted task's row is inert."""

    __tablename__ = "task_session_usage_peaks"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("task_id", "node_id", "session_id", name="uq_task_session_usage_peaks_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    peak_rss_kb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    peak_cpu_pct: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    # Cumulative CPU time; monotone per session on the node, so the max is the latest.
    cpu_seconds: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    first_seen_at: Mapped[datetime] = timestamp_column()
    last_seen_at: Mapped[datetime] = timestamp_column()


class NodeDiskEvent(OrgScoped, Base):
    """One thing that happened to a node's disk: a reclaim the node reported, or a pause/resume the disk gate
    decided. The history behind the node page's Disk list; `app/node_disk_events.py` owns every rule (the
    vocabularies, the 30-day retention, the gate). Append-only, pruned on ingest.

    `reclaim_id` is the node's idempotency key for a reclaim report (a replay is a no-op); NULL on gate rows.
    CASCADE like `NodeHostMetric`: a disk history attributed to nobody is noise."""

    __tablename__ = "node_disk_events"
    __table_args__ = (
        UniqueConstraint("reclaim_id", name="uq_node_disk_events_reclaim_id"),
        CheckConstraint("kind IN ('reclaim', 'pause', 'resume')", name="ck_node_disk_events_kind"),
        CheckConstraint(
            "trigger IS NULL OR trigger IN ('scheduled', 'warn', 'converge')", name="ck_node_disk_events_trigger"
        ),
        Index("ix_node_disk_events_node_occurred", "node_id", text("occurred_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = timestamp_column()
    # What started a reclaim; NULL on gate rows.
    trigger: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A reclaim's usage after it ran; a gate row's usage when it decided.
    used_pct: Mapped[Optional[float]] = mapped_column(REAL, nullable=True)
    # `{category: bytes freed}` over the closed `node_disk_events.FREED_KEYS`.
    freed: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # The rest of a reclaim report (usage before, per-engine sizes, removed users, remaining sizes).
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    reclaim_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


#: The states that are over: a row in one holds no VM and no task's place (`uq_microvms_live_task`).
MICROVM_ENDED: tuple[str, ...] = ("destroyed", "failed")


class Microvm(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """One Cloud Hypervisor microVM: a task's (`kind = 'task'`, `task_id`) or a console session's (`'console'`,
    `console_session_id`), on one node, sized and allowlisted at creation (`app/microvm_sizing.py`,
    `app/microvm_egress.py`). At most one live VM per task (`uq_microvms_live_task`). Derives its org through
    its node, like `NodeDiskEvent`; CASCADE with the node and the task, since a VM attributed to nothing is
    noise the node destroys anyway. `wait_reason` is one sentence on why it is not running yet."""

    __tablename__ = "microvms"
    __table_args__ = (
        CheckConstraint("kind IN ('task', 'console')", name="ck_microvms_kind"),
        CheckConstraint(
            "state IN ('provisioning', 'running', 'retained', 'destroying', 'destroyed', 'failed')",
            name="ck_microvms_state",
        ),
        CheckConstraint(
            "(kind = 'task' AND task_id IS NOT NULL AND console_session_id IS NULL) OR "
            "(kind = 'console' AND console_session_id IS NOT NULL AND task_id IS NULL)",
            name="ck_microvms_owner",
        ),
        Index(
            "uq_microvms_live_task", "task_id", unique=True,
            postgresql_where=text("state NOT IN ('destroyed', 'failed')"),
        ),
        Index("ix_microvms_node_state", "node_id", "state"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    console_session_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="provisioning", server_default="provisioning")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    vcpus: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_gib: Mapped[int] = mapped_column(Integer, nullable=False)
    allowlist: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    wait_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When a node report last showed this VM `running` (`app/microvm_lifecycle.py`), on the CP's clock: the
    # VM run's heartbeat for the stall detector (`task_pipeline_cp.list_silent_active_tasks`).
    last_sighted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class MicrovmIntent(OrgScoped, Base):
    """One CP-decided order about one MicroVM (`provision`/`launch`/`destroy`, `app/microvm_rail.py`), for the node
    to execute — `slot_intents`' sibling on the same mechanism (`app/intent_rail.py`).

    `intent_seq` is the supersede key, strictly increasing per VM. `vm_id` is NOT a foreign key on purpose: a
    task delete cascades its `microvms` row, and the `destroy` minted in that transaction must outlive it. The
    node answers through `POST /api/agent/microvms/{vm_id}/facts`, which retires the intent it names."""

    __tablename__ = "microvm_intents"
    __table_args__ = (
        UniqueConstraint("vm_id", "intent_seq", name="uq_microvm_intents_vm_seq"),
        CheckConstraint("verb IN ('provision', 'launch', 'destroy')", name="ck_microvm_intents_verb"),
        CheckConstraint("intent_seq >= 1", name="ck_microvm_intents_seq"),
        Index(
            "ix_microvm_intents_pending", "node_id", "created_at",
            postgresql_where=text("outcome IS DISTINCT FROM 'done'"),
        ),
        Index("ix_microvm_intents_vm_id", "vm_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    vm_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    intent_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    verb: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    acked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The retry bound, `slot_intents`' columns unchanged (`intent_rail.supersede` carries them forward).
    refusals: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    refusal_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


#: `node_orders.path` (`ck_node_orders_path`): how an Add-node submission makes its node (`app/node_orders.py`).
NODE_ORDER_PATHS = (
    "rent_dedicated", "rent_shared", "cloud", "self_hosted", "co_locate", "bundle_keep",
)


#: `node_orders.stage` (`ck_node_orders_stage`). Each path walks its own sequence of these
#: (`app/node_orders.py::SEQUENCES`); `ready` and `failed` are terminal.
NODE_ORDER_STAGES = ("paying", "launching", "joining", "placing", "waiting_for_machine", "ready", "failed")


class NodeOrder(EntityMixin, OrgScoped, Base):
    """One Add-node submission and where it stands (0352). `name` is the first node's name — and the new host's
    display name on a machine path. Written only by `app/node_orders.py`, the single writer of `stage`.

    `host_node_id` is the host the node goes on: the one made for a machine path, the chosen one for co_locate
    (an operator host for a Shared hosting, so it is a plain FK, never a same-org key, and never served to a
    tenant). Both node references are `SET NULL`: a failed order leaves no `nodes` row behind, and its own row
    must survive that delete to say why. `rental_id` names the hosting paths' hosting (`node_rentals`, 0354)."""

    __tablename__ = "node_orders"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("path", NODE_ORDER_PATHS, "ck_node_orders_path"),
        _in_check("stage", NODE_ORDER_STAGES, "ck_node_orders_stage"),
        _in_check("os", ("ubuntu", "macos", "windows"), "ck_node_orders_os"),
        Index("ix_node_orders_org_id_created_at", "org_id", "created_at"),
        # The two hooks' lookups: an open order waiting on its host, or on its node.
        Index(
            "ix_node_orders_open_host", "host_node_id",
            postgresql_where=text("stage NOT IN ('ready', 'failed')"),
        ),
        Index(
            "ix_node_orders_open_node", "node_id",
            postgresql_where=text("stage NOT IN ('ready', 'failed')"),
        ),
    )

    path: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    # A plain-language sentence for the person who ordered; NULL unless `failed`.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The stage a `failed` order failed in, which a retry re-runs. NULL unless `failed`.
    failed_stage: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Project ids, applied to `node_projects` when the node row is made.
    project_ids: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # The path's own inputs a retry needs again (the cloud path's credential id and instance type). Never a
    # secret.
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    host_node_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", name="fk_node_orders_host_node_id"), nullable=True
    )
    node_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", name="fk_node_orders_node_id"), nullable=True
    )
    rental_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("node_rentals.id", ondelete="SET NULL", name="fk_node_orders_rental_id"), nullable=True
    )
    # The machine's OS (self-hosted, cloud and rented paths; NULL on an older or a co-locate order = Ubuntu),
    # which decides its sequence (`node_orders.stages_for`).
    os: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When the person dismissed this `failed` order from the hosts list (`POST /api/node-orders/{id}/dismiss`);
    # a dismissed order is no longer read there. NULL = shown.
    dismissed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
