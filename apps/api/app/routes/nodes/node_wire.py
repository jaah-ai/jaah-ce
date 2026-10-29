"""The node wire shared by every node route: roster schemas, the row→wire mapper, tenant narrowing, error mapping."""
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, Optional, TypeVar
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel, PrivateAttr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from app import (
    agent_providers,
    edition_features,
    node_autoupdate,
    node_disk_events,
    node_host_metrics,
    node_liveness,
    node_log_history,
    node_platform,
    platform_release,
    jaah_discovery,
    node_allocation,
    node_config,
    secret_redact,
)
from app.actors import actor_names as resolve_actor_names
from app.authz.principal import Principal
from app.entity_shape import from_row, iso_or_none
from app.models.identity import AdminUser
from app.models.nodes import Microvm, MicrovmIntent, Node
from app.models.orgs import Org
from app.shared_storage import LEGACY_SHARED_ACCOUNTS_ROOT
from app.tenancy.ids import is_operator_plane, is_tenant_org
from app.tenancy.labels import OrgRef, org_ref_of, org_refs

if TYPE_CHECKING:
    from app import vnodes


class NodeCounts(BaseModel):
    worktrees: int
    # The slots a task can lease (`Worktree.number >= 1`): `worktrees` less the main clone. 0 beside a non-zero
    # `worktrees` is a node with only its main clone, which runs no task.
    task_slots: int = 0
    # The slots being destroyed (`retiring_at` set), counted in `worktrees`: the page shows them apart, and a node
    # delete does not count them as its own.
    retiring_slots: int = 0
    runners: int
    # Enabled accounts with a usable login on this node; not current use (`runners`) and not a dispatch
    # oracle (quota exclusions are not tested). Defined in `_counts_map`.
    accounts: int


class NodeTopProcessOut(BaseModel):
    """One of the host's busiest processes: name (`comm`, never argv), user, CPU in percent of one core, RSS."""

    name: str
    user: str
    cpu_pct: float
    rss_kb: int


class NodeHostMetricsOut(BaseModel):
    """One node's newest CPU/RAM/disk/network reading, already judged: the Load column's whole input.

    `null` on `NodeOut.host_metrics` means never reported; a `0.0` row would falsely assert an idle host
    (`app/node_host_metrics.py`).

    The levels are computed server-side, read off `node_host_metrics.LatestReading`'s properties; the
    client never compares a percentage to a threshold (as with `generation_placement`), so badge and chart
    cannot disagree and the stale⇒`unknown` collapse cannot be bypassed. `level` is the worse of CPU and
    memory. Every field is derived per request from one row plus the DB's `now()`."""

    # Null only for a tenant reading its node on another org's host, which is told its own disk and nothing
    # of the host's CPU, memory or network (`_own_disk_only`).
    cpu_pct: Optional[float]
    mem_pct: Optional[float]
    # The four-value vocabulary is `node_host_metrics`'s; `str`, not a Literal, to avoid a second home.
    cpu_level: str
    mem_level: str
    disk_level: str
    level: str
    # Static labelling facts ("12.3% of 16 cores"). Optional: the node may not read them, and ingest nulls a
    # non-positive one rather than dropping a real percentage (`fleet_observe.HostMetricsIn`).
    cpu_count: Optional[int] = None
    mem_total_kb: Optional[int] = None
    # The span the mean actually covers, so a fresh daemon does not claim five minutes of history.
    window_secs: float
    samples: int
    # The bucket stamp (`bucket_at`), not the sample instant.
    sampled_at: str
    # Measured by the database in the same statement (see `HealthClock`): this age decides `stale` and so
    # the levels, so a skewed clock would change the verdict.
    age_seconds: float
    # Whether the figure still describes the present; on the wire so a reader knows why a node is `unknown`.
    stale: bool
    # The bound `stale` was decided against: `max(window_secs, FRESH_BOUND_SECONDS)`
    # (`node_host_metrics.LatestReading.stale_bound_seconds`), served so no surface re-derives it
    # (`window_secs` alone is wrong whenever the floor governs).
    stale_bound_seconds: float
    # Used percentage of the clone filesystem (`node_host_metrics.disk_used_pct`) and the pair behind it;
    # null when the node never reported disk. Latest, not windowed.
    disk_pct: Optional[float] = None
    disk_free_kb: Optional[int] = None
    disk_total_kb: Optional[int] = None
    # Bytes per second received / sent, windowed like `cpu_pct`; null when the node never reported them.
    net_rx_bps: Optional[float] = None
    net_tx_bps: Optional[float] = None
    # Used percentage of /tmp's inode table and its own level (`warn` above
    # `node_host_metrics.TMP_INODE_WARN_PCT`); not folded into `level`. Null / `unknown` when unreported.
    tmp_inode_pct: Optional[float] = None
    tmp_inode_level: str = node_host_metrics.LEVEL_UNKNOWN
    # Pressure signals, latest and judged by nothing; null when unreported, and the host's, so
    # blanked on another org's host (`_own_disk_only`). `swap_pct` is the used share, null for no swap.
    load_1: Optional[float] = None
    load_5: Optional[float] = None
    load_15: Optional[float] = None
    swap_total_kb: Optional[int] = None
    swap_free_kb: Optional[int] = None
    swap_pct: Optional[float] = None
    psi_cpu_pct: Optional[float] = None
    psi_mem_pct: Optional[float] = None
    psi_io_pct: Optional[float] = None
    # Only on `GET /api/nodes/{id}/metrics`'s `latest`: the roster never reads the JSONB
    # (`node_host_metrics.latest_map(with_processes=True)`), so it is null there.
    top_processes: Optional[list[NodeTopProcessOut]] = None


class NodeDiskGateOut(BaseModel):
    """The node's disk gate (`app/node_disk_events.py`): `paused` stops its runner and CI dispatch until its
    disk drops under warn. `since` is when the gate last changed; `used_pct` the latest FRESH reading's
    usage, null when there is none."""

    state: Literal["open", "paused"]
    since: str
    used_pct: Optional[float] = None


class CloudChangeStepOut(BaseModel):
    step: str
    at: str
    detail: Optional[str] = None


class CloudChangeOut(BaseModel):
    """A machine-type change on a Your Cloud node (`cloud_lifecycle.begin_change`); the record's other keys
    (`id`, `was_stopped`, `started_at`) are the workflow's and not served."""

    from_type: str
    to_type: str
    #: `stuck`: still `running` past `cloud_lifecycle.CHANGE_STUCK_AFTER` (`change_state`), blocking nothing.
    state: Literal["running", "done", "failed", "stuck"]
    #: The cloud's own refusal, when it ended `failed`.
    error: Optional[str] = None
    steps: list[CloudChangeStepOut] = []


class RootGrowOut(BaseModel):
    """A disk grown that the node has not yet grown its root filesystem into: `pending` while it is asked, `failed`
    once it was asked too long (`error` says so)."""

    size_gb: int
    state: Literal["pending", "failed"]
    error: Optional[str] = None
class BootReportOut(BaseModel):
    """Why a launched machine is still silent, as the boot watch last read it (`app/cloud_boot_watch.py`). No
    console tail: that is the operator's (`app/hosts.py::_boot_log`)."""

    reason: str
    at: str
    #: True when `reason` is a cause (the boot cannot finish), False while it is progress.
    stalled: bool = False


class CloudOut(BaseModel):
    """The Your Cloud instance a node runs on (`_cloud_out`), from what its launch recorded on `infra`."""

    provider: str
    provider_label: str
    #: What that cloud calls the account the instance runs in (account, project, subscription).
    account_noun: str
    instance_id: str
    region: str
    zone: Optional[str]
    instance_type: Optional[str]
    disk_gb: Optional[int]
    #: The instance's last known state (`cloud_lifecycle.state_of`, what Stop/Start recorded); no cloud call is made
    #: to serve a read. None on a machine Jaah did not launch.
    state: Optional[Literal["running", "stopping", "stopped", "starting"]]
    #: What the console may offer on the instance (`cloud_lifecycle.actions_for`), decided here; [] for a reader
    #: who may not act on it (`_narrow_for_tenant`).
    actions: list[str]
    #: The machine types Change machine type may pick (the allowed types that can run the node,
    #: `cloud_lifecycle.compatible_types`); [] when it is not offered.
    instance_types: list[str] = []
    #: The machine-type change in flight or last ended (`app/durable/cloud_change.py`); None when there was none.
    change: Optional[CloudChangeOut] = None
    #: The root-filesystem grow the node owes after Grow disk (`cloud_lifecycle.root_grow_of`); None when none.
    root_grow: Optional[RootGrowOut] = None
    #: Why the machine is still silent; None once it has reported in, or when nothing was read.
    boot_report: Optional[BootReportOut] = None


class NodeOut(BaseModel):
    # The row's uuid, the stable key clients address a node by. Additive; `node_id` and `name` keep their
    # meaning. A string, round-tripped exactly. `None` for a presence-only host with no row, which no
    # access reaches (`authz.visible_node_clause`); nothing mints a synthetic id.
    id: Optional[str] = None
    # The three legacy fields, byte-compatible with the presence-only endpoint; `useNodeStatus` reads only
    # these.
    node_id: str
    status: str
    last_seen: Optional[str]
    # Additive record fields (defaults for a presence-only node not yet persisted).
    #
    # `name` is the same string as `node_id`: `node_id` is the pinned legacy contract, `name` the
    # entity-shape field. For a presence-only node it falls back to the presence key.
    name: str
    # NOT NULL (default `''`), so "no description" is an empty string, never null.
    description: str = ""
    tags: list[str] = []
    enabled: bool = True
    # Is this node winding down: a qualifier on `enabled`, so the pair reads as three states
    # (`app/models/nodes.py::Node.draining`). On the list model: "which nodes still take new work" is a roster
    # question.
    draining: bool = False
    # NodeLink: does this host mount the shared credential store (`nodes.shared_accounts_enabled`), the
    # per-node half of the conjunction with an account's `nodelink`. On the list model (a fleet question).
    # Defaults false for a presence-only host: nothing could have declared a mount, and the fail-closed
    # answer is no.
    nodelink_enabled: bool = False
    # Does this node report a NodeLink at the accounts token folder mounted (`nodelinks.accounts_nodelink_nodes`):
    # a login on it reaches every node on that NodeLink, so New account folds its Node pick away. On the list model.
    # False for a presence-only host.
    accounts_nodelink: bool = False
    # MicroVM mode (`nodes.microvm`, `app/microvm_mode.py`): every task of this node's projects runs in its own
    # microVM. On the list model: which nodes run VMs is a roster question. False for a presence-only host.
    microvm: bool = False
    # KVM for sessions (`nodes.session_kvm`): this co-located node's session user may open the host's `/dev/kvm`.
    # False on another org's host for a tenant (`_narrow_for_tenant`) and for a presence-only host.
    session_kvm: bool = False
    # The OS this node was created as (`Node.os`, never changed) and what the machine reports about itself
    # (`Node.arch`/`os_version`, NULL until it does). A presence-only host has no row: Ubuntu, unknown.
    os: Literal["ubuntu", "macos", "windows"] = "ubuntu"
    arch: Optional[str] = None
    os_version: Optional[str] = None
    # A Linux node's release as the console names it (`node_platform.distro_label`: `Ubuntu 22.04`, an unlisted one
    # as reported, `Linux` before the first report); None off Linux, whose `os` label stands.
    distro_label: Optional[str] = None
    # `{capability: one-line reason}` for what this node cannot do (`node_platform.node_unsupported`: its OS's row,
    # its reported host facts, a MicroVM node's sessions asked of its guest); `{}` for a capable Linux node. Served so the console disables, never re-derives.
    unsupported: dict[str, str] = {}
    # The machine reports a different OS family than it was created as (`node_platform.platform_blocker`).
    platform_blocker: Optional[str] = None
    # Which agent CLIs this host has attested (sensed; `Node.runner_clis` is the home). `None` is not `[]`:
    # None = never attested (an older daemon), `[]` = attested none. Served as the column with no coalesce.
    # On the list model for the Add Account node picker (`Accounts.tsx::addLoginNodes`).
    runner_clis: Optional[list[str]] = None
    # The companions (`AgentProvider.companions`) of every provider whose `binary` is in `runner_clis` that
    # `runner_clis` lacks, sorted: a node that would take the provider's runs, which then die when the binary
    # spawns the missing one. `[]` when `runner_clis` is None. A daemon that predates companion sensing never
    # reports them, so it shows its companions as missing until it updates: an upgrade clears it, and no
    # version marker can tell the two apart (the version shipping this is not knowable when it is written).
    missing_companions: list[str] = []
    role: Optional[str] = None
    # The node's jaah-home override: the root of its jaah-managed repos. NULL = no override, no
    # implicit default. Acted on, unlike `infra`.
    jaah_home: Optional[str] = None
    infra: dict[str, Any] = {}
    first_seen: Optional[str] = None
    # When a never-connected node's install command fetched its bootstrap (the first use of its credential,
    # `nodes.node_token_used_at`), which the console reads as "deploying" (`web/src/lib/nodeStatus.ts`). None once it
    # has connected: the column then only stamps the daemon's ongoing use.
    install_started_at: Optional[str] = None
    config_reported_at: Optional[str] = None
    counts: NodeCounts
    # The generation block: what code this node runs, its placement against the desired release, and
    # whether an update could work. Derived per request from `config_snapshot` plus one read of the desired
    # release (`_published_release`). The placement is computed here by `node_liveness.place_generation`
    # (the single home, including its zero-padding), never client-side.
    generation: Optional[str] = None            # config_snapshot->>'version'; None when unreported
    # The constant, not a literal: the vocabulary lives in `node_liveness`. `str` for the pinned contract.
    generation_placement: str = node_liveness.GENERATION_UNKNOWN
    release_desired: Optional[str] = None       # the published version placement was computed against
    # The CP release with no bundle for this node's platform (macOS/Windows: `app/platform_release.py`), so
    # "behind" reads as a publish gap, not a node fault; `release_desired` is then the newest published one.
    release_missing_bundle: Optional[str] = None
    # Auto-update, resolved by `app/node_autoupdate.py`: whether this node installs new releases on its own,
    # where that came from (`node` / `host` / `settings`), the node's own override (null = none), and the
    # value it would have with none (Settings) so the page can word "Default". On the list model: a fleet
    # question. A presence-only host has no row and follows Settings.
    autoupdate_enabled: bool = True
    autoupdate_source: str = node_autoupdate.SOURCE_SETTINGS
    autoupdate_override: Optional[bool] = None
    autoupdate_default: bool = True
    # CPU/RAM pressure; `null` = never reported (see `NodeHostMetricsOut`). On the list model for the Load
    # column. Resolved once per page in `list_nodes` and passed to `_node_out`, like `counts`.
    host_metrics: Optional[NodeHostMetricsOut] = None
    # The disk gate; `null` for a presence-only node (no row, so no gate). On the list model: "which nodes
    # are paused for disk" is a roster question.
    disk_gate: Optional[NodeDiskGateOut] = None
    # Provenance renders the actor's name, never the uuid. `System` is the normal case (the
    # presence sweep stamps rows). None for a presence-only node.
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    # The org the node belongs to (`tenancy/labels.py`). None for a presence-only node.
    org: Optional[OrgRef] = None
    # How the machine is run: `aws` (an EC2 instance), `self_hosted` (the owner's own machine),
    # `multi_node_host` (a Multi-Node Host, running no work), `co_located` (carried by the host named in
    # `hosted_on`). None for a presence-only node, and for a tenant reader's node on a host the reader may
    # not see (an operator host): blank, never the host's name or kind (`_narrow_for_tenant`). The blank itself
    # hints "not an ordinary node" — an accepted trade-off by owner decision, as is the vnode's
    # slot-rooted `jaah_home`/`shared_accounts_root` a tenant superadmin reads.
    hosting: Optional[Literal["aws", "self_hosted", "co_located", "multi_node_host"]] = None
    # The carrying host's name, only when `hosting == "co_located"`.
    hosted_on: Optional[str] = None
    # The node's NodeVault identity-key fingerprint (`Node.nodevault_identity_fp`: hex sha256 of the SPKI DER,
    # CP-computed). A public fact, so no narrowing. The Nodes page shows its first 12 characters for an admin
    # to compare with `jaah nodevault fingerprint` on the node. None for a presence-only node or one that has
    # not registered an identity.
    nodevault_identity_fp: Optional[str] = None
    # Whether the node can hold NodeVault keys now (`Node.nodevault_capable`, the daemon's probe, re-posted on
    # every identity post) and when it last said so (`nodevault_capable_at`, ISO; None = not reported since
    # 202610251400). A fingerprint alone does not prove it: a registered key stays when a later post says
    # not capable. Public, like the fingerprint. False/None for a presence-only node.
    nodevault_capable: bool = False
    nodevault_capable_at: Optional[str] = None
    # The operator allocated this machine to its tenant (`app/node_allocation.py`): the tenant uses it and changes
    # only its labels and service switches. False for a presence-only host.
    operator_allocated: bool = False
    # A Jaah Cloud machine: a live hosting names it (`node_allocation.jaah_cloud`), so, like an allocated node, a
    # tenant changes only its labels and service switches. False for a presence-only host.
    jaah_cloud: bool = False
    # On a shared machine the operator runs that is not CP-launched (`node_allocation.placement`): the console words
    # its lock as the operator's shared machine, not Jaah Cloud. False for a presence-only host.
    shared_machine: bool = False
    # The tenant's own cloud instance (`_cloud_out`); None for a self-hosted node, a launch not yet placed, or a
    # Jaah Cloud machine (whose instance is the operator's).
    cloud: Optional[CloudOut] = None
    # The carrying host's org, for `_narrow_for_tenant`'s verdict only; never serialized.
    _host_org_id: Optional[UUID] = PrivateAttr(default=None)


class NodeMicrovmOut(BaseModel):
    """One `microvms` row as the node page lists it."""

    vm_id: str
    kind: str
    task_id: Optional[int] = None
    console_session_id: Optional[str] = None
    state: str
    wait_reason: Optional[str] = None
    # The newest order's `microvm_intents.reason`: why a destroyed or retained row ended where it did.
    reason: Optional[str] = None
    created_at: Optional[str] = None


class NodeUpdateOut(BaseModel):
    """One updater outcome off the node's shipped journal (`app/node_log_history.py`)."""

    at: str
    version: Optional[str] = None
    outcome: Literal["converged", "failed"]
    detail: str


def _updates_out(history: Optional[list], *, tenant: bool) -> list[NodeUpdateOut]:
    """`nodes.update_history`, newest first, capped, malformed items skipped. A tenant reader gets the outcome
    and time but not the version, which `_narrow_for_tenant` withholds from every tenant as `generation`, nor
    the detail, which spells it."""
    out: list[NodeUpdateOut] = []
    for item in history or []:
        if not isinstance(item, dict) or item.get("outcome") not in ("converged", "failed") or not item.get("at"):
            continue
        out.append(NodeUpdateOut(
            at=str(item["at"]),
            version=None if tenant or item.get("version") is None else str(item["version"]),
            outcome=item["outcome"],
            detail=item["outcome"] if tenant else str(item.get("detail") or ""),
        ))
    return out[:node_log_history.MAX_UPDATES]


class NodeDetailOut(NodeOut):
    # Where this node mounts the NodeLink store (`nodes.shared_accounts_root`). Detail model only (list
    # surfaces would carry it for nothing); served because `PUT /api/nodes/{id}` accepts it, so a writer
    # can read it back.
    nodelink_root: str = LEGACY_SHARED_ACCOUNTS_ROOT
    config_snapshot: Optional[dict[str, Any]] = None
    # The declaration beside the report: `{ENV_KEY: desired value}` the `config_snapshot` must match
    # (`node_liveness`'s config-drift verdict). Detail model only.
    config_desired: dict[str, Any] = {}
    # Fenced by an org transfer until it confirms the old org's cleanup (`app/tenancy/transfer_node.py`): it takes
    # no task meanwhile. Lifted by the node's confirm, or by the operator (`POST .../lift-transfer-fence`).
    org_transfer_cleanup_pending: bool = False
    # The updater's newest outcome (`nodes.update_history[0]`, `_updates_out`'s shape): beside the Version, it says
    # why a node is behind. None until the node shipped one; the operator's (blanked for every tenant, as
    # `generation`). Detail model only.
    last_update: Optional[NodeUpdateOut] = None
    # ── MicroVM (architecture §4.9) ─────────────────────────────────────────────────────────────────
    # The one sentence refusing a flip away from the current mark (`microvm_mode.flip_blocker`), in either
    # direction; None = the flip would go ahead (or there is no row).
    microvm_blocker: Optional[str] = None
    # May THIS reader flip the mark and set the node's VM size: false for a tenant admin on a shared node
    # (`microvm_mode.editable`), so the page hides the control rather than offering a 403.
    microvm_editable: bool = False
    # May THIS reader flip `session_kvm` (`_session_kvm_editable`): the host's owner, on a co-located node only.
    session_kvm_editable: bool = False
    # `{vcpus|ram_mib|disk_gib: {value, source}, override: {...}}` (`microvm_sizing.sizing_out`): what a VM here
    # gets before any project value, and the node's own override. None for a presence-only host.
    microvm_sizing: Optional[dict[str, Any]] = None
    # The node's own MicroVM report (`config_snapshot['microvm']`, architecture §4.3), served as sent; None until
    # it reports one.
    microvm_facts: Optional[dict[str, Any]] = None
    # The node's VMs, newest first (`NODE_MICROVMS_SHOWN` at most).
    microvms: list[NodeMicrovmOut] = []
    # Every one of the node's VM rows counted by state, not just the `microvms` slice: the page's "in VM records"
    # count and its retained-folder marks read these, so an old retained VM past the cap still counts.
    microvm_counts: dict[str, int] = {}
    # The ids of every `retained` VM row (kept on purpose for inspection), so its folder is not flagged leftover.
    microvm_retained_ids: list[str] = []


#: How many of a node's VMs its detail lists, newest first: every live one fits, with recent history.
NODE_MICROVMS_SHOWN = 50


def _status_and_last_seen(
    node_id: str, row: Optional[Node], presence: dict[str, dict[str, Optional[str]]]
) -> tuple[str, Optional[str]]:
    """The `{status, last_seen}` contract for one row: `jaah_discovery.status_and_last_seen` over this
    node's snapshot entry (if any) and the row's persisted `last_seen`.

    A row's entry is read by its uuid (`jaah_discovery.presence_map`'s key) whatever `node_id` the caller passed; `node_id`
    addresses only a presence-only host, which has no row."""
    entry = presence.get(str(row.id)) if row is not None else presence.get(node_id)
    return jaah_discovery.status_and_last_seen(entry, row.last_seen if row is not None else None)


def _reported_generation(row: Optional[Node]) -> Optional[str]:
    """The node's own statement of which code it runs: `config_snapshot->>'version'`, the installed
    `jaah` dpkg version (`apps/jaahd/main.py::build_config_snapshot`). None when unreported. A
    dev-tree daemon reports `VERSION` instead, placed like any other label."""
    snapshot = row.config_snapshot if row is not None else None
    if not snapshot:
        return None
    version = snapshot.get("version")
    return version if isinstance(version, str) and version else None


def _host_metrics_out(
    reading: Optional[node_host_metrics.LatestReading],
) -> Optional[NodeHostMetricsOut]:
    """One `LatestReading` as the wire shape, or `None` ("never reported").

    The one place this translation happens, so list, detail and metrics routes agree. Levels and `stale`
    are read off the reading's properties (`app/node_host_metrics.py` owns thresholds and the stale
    collapse). `None` in, `None` out: a zeroed reading would assert an idle host."""
    if reading is None:
        return None
    return from_row(
        NodeHostMetricsOut, reading,
        sampled_at=reading.bucket_at.isoformat(),
        # Stored entries were cleaned at ingest; one that no longer fits the shape is skipped, not a 500.
        top_processes=None if reading.top_processes is None else [
            NodeTopProcessOut(**entry) for entry in reading.top_processes
            if isinstance(entry, dict) and set(entry) == set(NodeTopProcessOut.model_fields)
        ],
    )


def _node_out(
    session: Session,
    node_id: str,
    row: Optional[Node],
    presence: dict[str, dict[str, Optional[str]]],
    counts: NodeCounts,
    *,
    detail: bool,
    published: Optional[str] = None,
    actor_names: Optional[dict[UUID, str]] = None,
    host_metrics: Optional[dict[UUID, node_host_metrics.LatestReading]] = None,
    hosting: Optional[dict[UUID, vnodes.Hosting]] = None,
    orgs: Optional[dict[UUID, OrgRef]] = None,
    jaah_cloud: Optional[set[UUID]] = None,
    placement: Optional[dict[UUID, node_allocation.Placement]] = None,
    accounts_nodelink: Optional[set[UUID]] = None,
) -> NodeOut:
    """One node's wire shape. `node_id` is the presence key, read only when `row` is None.

    `actor_names`, `host_metrics`, `hosting`, `orgs`, `jaah_cloud`, `placement` and `accounts_nodelink` are
    pre-resolved per page by `list_nodes` (one query each, avoiding an N+1); a single-row caller passes nothing and
    pays for its own read. `is not None`, not truthiness, picks the arm: an empty map is a legitimate answer.

    `hosting`/`hosted_on` are the whole truth here; `_narrow_for_tenant` blanks them for a reader who may
    not see the host."""
    status, last_seen = _status_and_last_seen(node_id, row, presence)
    generation = _reported_generation(row)
    release = platform_release.for_os(row.os if row else None, published)
    if actor_names is not None:
        names = actor_names
    elif row is not None:
        names = resolve_actor_names(session, (row.created_by_id, row.updated_by_id))
    else:
        names = {}
    if row is None:
        # A presence-only host has no uuid, so no reading can exist for it: `null`.
        reading = None
    elif host_metrics is not None:
        reading = host_metrics.get(row.id)
    else:
        reading = node_host_metrics.latest_map(session, [row.id]).get(row.id)
    if row is not None and hosting is None:
        hosting = _hosting_facts(session, [row.id])
    if row is not None and orgs is None:
        orgs = org_refs(session, [row.org_id])
    if row is not None and jaah_cloud is None:
        jaah_cloud = node_allocation.jaah_cloud([row.id])
    if row is not None and placement is None:
        placement = node_allocation.placement([row.id])
    if row is not None and accounts_nodelink is None:
        accounts_nodelink = _accounts_nodelink_nodes(session, [row])
    fact = hosting.get(row.id) if row is not None else None
    placed = (placement or {}).get(row.id) if row is not None else None
    kind, hosted_on, host_org_id = _hosting_of(row, fact)
    autoupdate_default = node_autoupdate.settings_default()
    autoupdate = node_autoupdate.resolve(row.autoupdate_override if row else None, fact, autoupdate_default)
    common = dict(
        id=str(row.id) if row else None,
        # The row's name (display only); a presence-only node's is its presence key. Never blank.
        node_id=row.name if row else node_id,
        status=status,
        last_seen=last_seen,
        name=row.name if row else node_id,
        description=row.description if row else "",
        tags=list(row.tags) if row and row.tags else [],
        enabled=row.enabled if row else True,
        # `False` for a presence-only host: a drain is an operator command, and an unregistered host was never
        # given one.
        draining=bool(row.draining) if row else False,
        # `False` for a presence-only host: with no row it cannot have declared a mount (see `NodeOut`).
        nodelink_enabled=row.shared_accounts_enabled if row else False,
        accounts_nodelink=row is not None and row.id in (accounts_nodelink or ()),
        microvm=bool(row.microvm) if row else False,
        session_kvm=bool(row.session_kvm) if row else False,
        os=row.os if row else node_platform.OS_UBUNTU,
        arch=row.arch if row else None,
        os_version=row.os_version if row else None,
        distro_label=node_platform.distro_label(row) if row else node_platform.label(node_platform.OS_UBUNTU),
        unsupported=node_platform.node_unsupported(row) if row else {},
        platform_blocker=node_platform.platform_blocker(row.os, row.os_reported) if row else None,
        # A presence-only host has never attested: `None`, not `[]`.
        runner_clis=list(row.runner_clis) if row and row.runner_clis is not None else None,
        missing_companions=_missing_companions(row.runner_clis if row else None),
        role=row.role if row else None,
        jaah_home=row.jaah_home if row else None,
        infra=dict(row.infra) if row and row.infra else {},
        # Never before the row existed: a stamp older than `created_at` reads as `created_at`.
        first_seen=iso_or_none(max(row.first_seen, row.created_at)) if row else None,
        install_started_at=iso_or_none(row.node_token_used_at) if row and row.last_seen is None else None,
        config_reported_at=iso_or_none(row.config_reported_at) if row else None,
        counts=counts,
        generation=generation,
        generation_placement=node_liveness.place_generation(generation, release.version),
        release_desired=release.version,
        release_missing_bundle=release.missing,
        autoupdate_enabled=autoupdate.enabled,
        autoupdate_source=autoupdate.source,
        autoupdate_override=row.autoupdate_override if row else None,
        autoupdate_default=autoupdate_default,
        host_metrics=_host_metrics_out(reading),
        disk_gate=(
            NodeDiskGateOut(state=row.disk_gate, since=iso_or_none(row.disk_gate_changed_at),
                            used_pct=node_disk_events.fresh_pct(reading))
            if row is not None else None
        ),
        created_by=names.get(row.created_by_id) if row else None,
        updated_by=names.get(row.updated_by_id) if row else None,
        org=org_ref_of(orgs, row.org_id) if row is not None else None,
        hosting=kind,
        hosted_on=hosted_on,
        nodevault_identity_fp=row.nodevault_identity_fp if row else None,
        nodevault_capable=bool(row.nodevault_capable) if row else False,
        nodevault_capable_at=iso_or_none(row.nodevault_capable_at) if row else None,
        operator_allocated=bool(row.operator_allocated) if row else False,
        jaah_cloud=row is not None and row.id in (jaah_cloud or ()),
        shared_machine=placed is not None and placed.shared_machine,
        cloud=_cloud_out(row) if row is not None and row.id not in (jaah_cloud or ()) else None,
    )
    if detail:
        out: NodeOut = NodeDetailOut(
            # Served from the row so a writer can read it back; omitted, it fell to the model default and every
            # detail response reported the fleet constant. A presence-only host has only the fleet's root.
            nodelink_root=row.shared_accounts_root if row else LEGACY_SHARED_ACCOUNTS_ROOT,
            config_snapshot=row.config_snapshot if row else None,
            config_desired=dict(row.config_desired) if row and row.config_desired else {},
            org_transfer_cleanup_pending=bool(row.org_transfer_cleanup_pending) if row else False,
            last_update=next(iter(_updates_out(row.update_history, tenant=False)), None) if row else None,
            **_microvm_detail(session, row),
            **common,
        )
    else:
        out = NodeOut(**common)
    out._host_org_id = host_org_id
    return out


def _microvm_detail(session: Session, row: Optional[Node]) -> dict[str, Any]:
    """The MicroVM half of a node's detail (architecture §4.9), all but `microvm_editable`, which is the
    reader's (`_microvm_editable`, set by the route). Empty for a presence-only host, and where MicroVM is not
    shipped (the community edition has no MicroVMs): the model defaults."""
    if row is None:
        return {}
    if not edition_features.enabled("microvm"):
        return {}
    from app import microvm_lifecycle, microvm_mode, microvm_placement, microvm_sizing

    vms = session.scalars(
        select(Microvm).where(Microvm.node_id == row.id)
        .order_by(Microvm.created_at.desc()).limit(NODE_MICROVMS_SHOWN)
    ).all()
    # One read for every shown row: each VM's newest order and its reason.
    reasons = dict(session.execute(
        select(MicrovmIntent.vm_id, MicrovmIntent.reason)
        .where(MicrovmIntent.vm_id.in_([vm.id for vm in vms]))
        .order_by(MicrovmIntent.vm_id, MicrovmIntent.intent_seq.desc())
        .distinct(MicrovmIntent.vm_id)
    ).all()) if vms else {}
    counts = dict(session.execute(
        select(Microvm.state, func.count()).where(Microvm.node_id == row.id).group_by(Microvm.state)
    ).all())
    retained = session.scalars(
        select(Microvm.id).where(Microvm.node_id == row.id, Microvm.state == microvm_placement.RETAINED)
    ).all()
    facts = (row.config_snapshot or {}).get(node_config.MICROVM_SNAPSHOT_KEY)
    return dict(
        microvm_counts={str(state): int(n) for state, n in counts.items()},
        microvm_retained_ids=[str(vm_id) for vm_id in retained],
        microvm_blocker=microvm_mode.flip_blocker(session, row),
        # No project: a node's page shows what any VM here gets before a project's own value.
        microvm_sizing=microvm_sizing.sizing_out(
            row, None, session.scalar(select(Org).where(Org.id == row.org_id)), own=row
        ),
        microvm_facts=facts if isinstance(facts, dict) else None,
        microvms=[
            NodeMicrovmOut(
                vm_id=str(vm.id), kind=vm.kind, task_id=vm.task_id,
                console_session_id=str(vm.console_session_id) if vm.console_session_id else None,
                state=vm.state, reason=reasons.get(vm.id),
                # A row stored before `operator_sentence` worded it is reworded on the way out; redacted as
                # `microvm_lifecycle.failure_of` redacts it, node text bound for a browser.
                wait_reason=(secret_redact.redact(microvm_lifecycle.operator_sentence(vm.wait_reason))[0]
                             if vm.wait_reason else None),
                created_at=iso_or_none(vm.created_at),
            )
            for vm in vms
        ],
    )


def _microvm_editable(session: Session, row: Node, actor: AdminUser, principal: Principal | None) -> bool:
    """May `actor` flip this node's MicroVM mark and set its VM size? The operator plane on any node; an Org
    Owner/Admin on its own org's node, unless that node is shared (`microvm_mode.is_shared_node`, I7)."""
    if not edition_features.enabled("microvm"):
        return False  # The community edition has no MicroVMs.
    from app import microvm_mode

    if is_operator_plane(actor.org_id, actor.is_superadmin):
        return True
    return (
        not node_allocation.locked_for(row, actor)
        and actor.org_id == row.org_id and _administers_own_hosts(actor, principal)
        and microvm_mode.editable(session, row, operator=False)
    )


def _session_kvm_editable(session: Session, row: Node, actor: AdminUser, principal: Principal | None) -> bool:
    """May `actor` flip this node's KVM for sessions? Only on a co-located node, and only the host's owner decides:
    `/dev/kvm` widens the host kernel's attack surface for every node it carries. The operator plane on any such
    node; an Org Owner/Admin on its own org's node on its own org's host, unless the node is locked for it."""
    fact = _hosting_facts(session, [row.id]).get(row.id)
    if fact is None or fact.is_host:
        return False
    if is_operator_plane(actor.org_id, actor.is_superadmin):
        return True
    return (
        not node_allocation.locked_for(row, actor)
        and actor.org_id == row.org_id and _administers_own_hosts(actor, principal)
        and fact.host_org_id == actor.org_id
    )


def _missing_companions(attested: Optional[list[str]]) -> list[str]:
    """The companions `NodeOut.missing_companions` names for an attested `runner_clis`; `[]` when None."""
    if attested is None:
        # Never attested: nothing to say about any provider, so nothing about its companions either.
        return []
    have = set(attested)
    return sorted({
        name
        for provider in agent_providers.PROVIDERS if provider.binary in have
        for name in provider.companions if name not in have
    })


def _hosting_facts(session: Session, node_ids: list[UUID]) -> dict[UUID, vnodes.Hosting]:
    """`vnodes.hosting_facts`; empty in the community edition, which has no Multi-Node Hosts."""
    if not edition_features.enabled("multi_node_hosts"):
        return {}
    from app import vnodes

    return vnodes.hosting_facts(session, node_ids)


def _disk_usage_of(node_id: Optional[UUID]) -> Optional[vnodes.DiskUsage]:
    """`vnodes.disk_usage_of`; None in the community edition, where no node is co-located."""
    if not edition_features.enabled("multi_node_hosts"):
        return None
    from app import vnodes

    return vnodes.disk_usage_of(node_id)


def _accounts_nodelink_nodes(session: Session, rows: list[Node]) -> set[UUID]:
    """`nodelinks.accounts_nodelink_nodes`; empty in the community edition, which has no NodeLink."""
    if not edition_features.enabled("nodelink"):
        return set()
    from app import nodelinks

    return nodelinks.accounts_nodelink_nodes(session, rows)


def _hosting_of(
    row: Optional[Node], fact: Optional[vnodes.Hosting]
) -> tuple[Optional[str], Optional[str], Optional[UUID]]:
    """`(hosting, hosted_on, host org)` for one node, before any reader's narrowing (see `NodeOut`)."""
    if row is None:
        return None, None, None
    if fact is not None and fact.is_host:
        return "multi_node_host", None, None
    if fact is not None:
        return "co_located", fact.host_name, fact.host_org_id
    infra = row.infra or {}
    if infra.get("instance_id") or infra.get("launched_by") == "control-plane":
        return "aws", None, None
    return "self_hosted", None, None


def _cloud_out(row: Node) -> Optional[CloudOut]:
    """`row`'s Your Cloud instance as `infra` records it, or None when it records none. An unregistered provider
    reads under its key."""
    if not edition_features.enabled("cloud_nodes"):
        return None  # The community edition has no cloud nodes.
    from app import cloud_boot_watch, cloud_lifecycle, clouds

    infra = row.infra or {}
    try:
        ref = clouds.instance_ref_of(infra)
    except ValueError:  # an unknown provider key: nothing this console can act on
        return None
    if ref is None:
        return None
    provider = clouds.load_adapters().get(ref.provider)
    root_gb = infra.get("root_volume_gb")
    session = object_session(row)
    switch_on = provider is not None and session is not None and cloud_lifecycle.switch_on(
        session, row.org_id, ref.provider
    )
    actions = cloud_lifecycle.actions_for(infra, switch_on=switch_on, in_group=row.node_group_capacity_id is not None)
    change = infra.get("cloud_change")
    if isinstance(change, dict):
        change = {**change, "state": cloud_lifecycle.change_state(infra)}
    grow = cloud_lifecycle.root_grow_of(infra)
    return CloudOut(
        provider=ref.provider,
        provider_label=provider.label if provider is not None else ref.provider,
        account_noun=provider.account_noun if provider is not None else ref.provider,
        instance_id=ref.instance_id, region=ref.region, zone=ref.zone,
        instance_type=infra.get("instance_type"),
        disk_gb=int(root_gb) if root_gb else None,
        state=cloud_lifecycle.state_of(infra) if cloud_lifecycle.actionable(infra) else None,
        actions=actions,
        instance_types=(
            cloud_lifecycle.compatible_types(session, row, ref.provider)
            if "change_type" in actions and session is not None else []
        ),
        change=_change_out(change),
        root_grow=(
            RootGrowOut(size_gb=grow[0], state=grow[1],
                        error=cloud_lifecycle.ROOT_GROW_TIMED_OUT if grow[1] == "failed" else None)
            if grow is not None else None
        ),
        boot_report=_boot_report_out(infra.get(cloud_boot_watch.BOOT_REPORT_KEY)) if row.last_seen is None else None,
    )


def _boot_report_out(report: Any) -> Optional[BootReportOut]:
    """The boot watch's report without its tail, or None for one that does not read as one; a report from before
    `stalled` existed reads as progress."""
    if not isinstance(report, dict):
        return None
    try:
        return BootReportOut.model_validate({key: report[key] for key in ("reason", "at", "stalled") if key in report})
    except ValidationError:
        return None


def _change_out(change: Any) -> Optional[CloudChangeOut]:
    """The node's recorded type change, or None — also for one that does not read as one: a single row never fails
    a page of nodes."""
    if not isinstance(change, dict):
        return None
    try:
        return CloudChangeOut.model_validate(change)
    except ValidationError:
        return None


_N = TypeVar("_N", bound=NodeOut)


# The `infra` keys no tenant reader is served: references to the credential that signed the launch or
# names the shared store, and to the connection the launch read — which for a node the operator launched
# (a hosted host) are the operator's credential and rental connection. Carried through a tenant's `infra`
# write by `_merge_a_non_superadmins_infra`, so withholding them loses nothing. `account_id` is the AWS
# account the instance runs in, which for a hosted machine is Jaah's own.
_TENANT_WITHHELD_INFRA_KEYS: tuple[str, ...] = ("credential_id", "connection_id", "account_id")
if edition_features.enabled("cloud_nodes"):
    from app.cloud_boot_watch import BOOT_REPORT_KEY
    from app.cloud_node_launch import SHARED_STORE_CREDENTIAL_KEY

    # The boot watch's console tail is the operator's; a tenant reads its reason on `cloud.boot_report`.
    _TENANT_WITHHELD_INFRA_KEYS += (SHARED_STORE_CREDENTIAL_KEY, BOOT_REPORT_KEY)


def _administers_own_hosts(actor: AdminUser, principal: Principal | None) -> bool:
    """May `actor` administer its own org's hosts? In a tenant org, the org-admin rule
    (`Principal.is_org_admin`: an Org Owner/Admin); on the operator plane, superadmin, as before (its hosts
    are operator-plane work). The one test the tenant-admin node guards (`update.py`) ask."""
    if is_tenant_org(actor.org_id):
        return principal is not None and principal.is_org_admin
    return bool(actor.is_superadmin)


def _on_a_foreign_host(reader: AdminUser, hosting: Optional[str], host_org_id: Optional[UUID]) -> bool:
    """Is this node, as `reader` sees it, a tenant's node co-located on ANOTHER org's host (an operator host)?
    The one test `_narrow_for_tenant` and `get_node_metrics` narrow on; `hosting`/`host_org_id` are
    `_hosting_of`'s."""
    return is_tenant_org(reader.org_id) and hosting == "co_located" and host_org_id != reader.org_id


def _own_disk_only(
    metrics: Optional[NodeHostMetricsOut], usage: Optional[vnodes.DiskUsage]
) -> Optional[NodeHostMetricsOut]:
    """`metrics` as a tenant is told it for its node on another org's host: CPU, memory, network, /tmp, load, swap,
    pressure and the top processes are the host's, so blanked; disk is the node's own — total = its effective cap, free = cap - use (null until
    the host has measured it). The levels follow: CPU and memory `unknown`, the badge the disk's."""
    if metrics is None:
        return None
    total = usage.cap_kb if usage is not None else None
    free = (
        max(0, usage.cap_kb - usage.used_kb) if usage is not None and usage.used_kb is not None else None
    )
    disk_pct = node_host_metrics.disk_used_pct(free, total)
    disk_level = (
        node_host_metrics.LEVEL_UNKNOWN if metrics.stale else node_host_metrics.classify(disk_pct)
    )
    return metrics.model_copy(update={
        "cpu_pct": None, "mem_pct": None, "cpu_count": None, "mem_total_kb": None,
        "net_rx_bps": None, "net_tx_bps": None,
        "load_1": None, "load_5": None, "load_15": None,
        "swap_total_kb": None, "swap_free_kb": None, "swap_pct": None,
        "psi_cpu_pct": None, "psi_mem_pct": None, "psi_io_pct": None, "top_processes": None,
        "tmp_inode_pct": None, "tmp_inode_level": node_host_metrics.LEVEL_UNKNOWN,
        "cpu_level": node_host_metrics.LEVEL_UNKNOWN, "mem_level": node_host_metrics.LEVEL_UNKNOWN,
        "disk_pct": disk_pct, "disk_free_kb": free, "disk_total_kb": total,
        "disk_level": disk_level, "level": disk_level,
    })


def _narrow_for_tenant(out: _N, reader: AdminUser, principal: Principal | None) -> _N:
    """The read model a tenant-org viewer is served. Identity for a non-tenant reader
    (`tenancy/ids.py::is_tenant_org`), but for `cloud.actions`: every reader is offered only the actions it may
    perform.

    Every tenant: the release trio `release_desired`, `generation` and `generation_placement` is blanked
    (the placement would reveal a pending operator release; it becomes `GENERATION_UNKNOWN`, which an
    unreported node already carries), and with it the detail's `last_update`, as are `_TENANT_WITHHELD_INFRA_KEYS`.

    A tenant ADMINISTRATOR (`_administers_own_hosts`) keeps every field they may write on their own node (`update_node`): the rest of
    `infra`, `jaah_home` and the config trio, so a form round-trip never wipes them,
    and reads `nodelink_root` (node-reported). Any other tenant reader gets those blanked too.

    Kept for all: `role`, `status`/`last_seen`/`counts`, `enabled`/`draining`, the `autoupdate_*`
    fields (see the co-located arm) and `org`; `host_metrics` too, except on another org's host. `hosting`/
    `hosted_on` are kept for a node on a host in the reader's own org, and BOTH blanked for one on another
    org's host (an operator host): the tenant must not be told which host carries it — that an operator machine
    does is told by `shared_machine` alone (a self-hosted one). On such a host
    `host_metrics` keeps only the node's OWN disk (its cap and use) and blanks CPU, memory and network, which
    are the host's (`_own_disk_only`).

    Blanked to each field's empty value, never dropped (pinned wire shape). Applied by every session route
    returning `_node_out` (`list_nodes`, `get_node`, `update_node`, the three registration routes);
    `get_node_health` applies the same split to its own config fields.
    `model_copy` keeps the subclass (hence the TypeVar)."""
    if out.cloud is not None and out.cloud.actions:
        # Offered only what the doors would accept (`routes/nodes/lifecycle.py`, `delete.py`): an administrator of
        # the node's hosts, and Delete with `nodes:delete` too. Write access is not probed per row: a reader who
        # administers its hosts writes every node it can read.
        may_act = _administers_own_hosts(reader, principal)
        may_delete = may_act and (principal.has("nodes:delete") if principal is not None else reader.is_superadmin)
        # A tenant uses an operator-allocated machine but does not manage it (`lifecycle.ALLOCATED_MACHINE`).
        may_manage = may_act and not (out.operator_allocated and is_tenant_org(reader.org_id))
        offered = [action for action in out.cloud.actions
                   if (may_delete if action == "delete" else may_manage)]
        out = out.model_copy(update={"cloud": out.cloud.model_copy(update={
            "actions": offered, "instance_types": out.cloud.instance_types if "change_type" in offered else [],
        })})
    if not is_tenant_org(reader.org_id):
        return out
    blanked: dict[str, Any] = {
        "release_desired": None,
        "release_missing_bundle": None,
        "generation": None,
        "generation_placement": node_liveness.GENERATION_UNKNOWN,
    }
    if isinstance(out, NodeDetailOut):
        blanked["last_update"] = None
    if _on_a_foreign_host(reader, out.hosting, out._host_org_id):
        # Auto-update too, as an ordinary node's: "follows its host", or the host's value, would tell.
        ordinary = node_autoupdate.resolve(out.autoupdate_override, None, out.autoupdate_default)
        blanked.update(
            hosting=None, hosted_on=None, autoupdate_enabled=ordinary.enabled, autoupdate_source=ordinary.source,
            # Only a co-located node can carry it: served, it would tell.
            session_kvm=False,
        )
        if out.host_metrics is not None and out.id is not None:
            blanked["host_metrics"] = _own_disk_only(out.host_metrics, _disk_usage_of(UUID(out.id)))
        if out.disk_gate is not None:
            # The gate's figure is the shared filesystem's, i.e. the host's: the state stays (the node really
            # is paused), the figure goes.
            blanked["disk_gate"] = out.disk_gate.model_copy(update={"used_pct": None})
        if isinstance(out, NodeDetailOut):
            # The MicroVM report measures the host (free RAM and disk, taps, processes); the VM rows, the
            # tenant's own tasks, stay.
            blanked["microvm_facts"] = None
    if _administers_own_hosts(reader, principal):
        blanked["infra"] = {
            key: value for key, value in (out.infra or {}).items()
            if key not in _TENANT_WITHHELD_INFRA_KEYS
        }
        return out.model_copy(update=blanked)
    # `cloud` is read off `infra`, so it goes with it.
    blanked.update(infra={}, config_reported_at=None, jaah_home=None, cloud=None)
    if isinstance(out, NodeDetailOut):
        blanked.update(
            config_snapshot=None,
            microvm_facts=None,
            config_desired={},
            # `""` rather than the default, which is the operator's layout.
            nodelink_root="",
        )
    return out.model_copy(update=blanked)


@contextmanager
def _cloud_node_errors():
    """The single home of the status mapping for `app/cloud_node_launch.py`'s two errors.

    `CloudNodeError` is 422 (the operator's request or configuration; the message names the fault).
    `CloudNodeUpstreamError` is 502 (AWS or Tailscale refused or was unreachable). Messages reach the
    operator verbatim; they are secret-free by that module's rule.

    `routes/agent_bootstrap.py` reuses this, but its `curl -fsS | sudo bash` client sees only the status,
    so refusals with real remediation are raised at registration.

    Matches by type: DBOS wraps an exhausted retry ladder, so `app/durable/cloud_node.py::_step_cause`
    unwraps it before it leaves that module (once, for all three readers, including
    `node_group_reconcile`)."""
    if not edition_features.enabled("cloud_nodes"):
        yield  # The community edition launches no cloud node, so nothing raises these.
        return
    from app.cloud_node_launch import CloudNodeError, CloudNodeUpstreamError

    try:
        yield
    except CloudNodeUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except CloudNodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _request_origin(request: Request) -> str:
    """The `scheme://host[:port]` this request arrived on: `cloud_node_launch.resolve_cp_base_url`'s fallback
    when the knob is empty, usually right since the operator is talking to a control plane."""
    return f"{request.url.scheme}://{request.url.netloc}"
