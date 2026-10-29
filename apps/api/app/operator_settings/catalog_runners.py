"""The "Runners" and "Infrastructure" Settings groups of the Settings catalogue, in render order."""
from __future__ import annotations

from typing import Final, Mapping

from app.operator_settings.knobs import Knob, _Section, _sectioned

#: `{field: (min, max)}` for any tier's MicroVM size (`app/microvm_sizing.py`, which reads it as `BOUNDS`); a ceiling
#: outside these is a typo, never a VM. Here, not there, so the knobs below build where MicroVM is not shipped.
MICROVM_BOUNDS: Final[Mapping[str, tuple[int, int]]] = {
    "vcpus": (1, 256),
    "ram_mib": (512, 1_048_576),
    "disk_gib": (8, 16_384),
}

KNOBS: tuple[Knob, ...] = _sectioned(
    _Section("Runners", "Dispatch"),
    # ── The two retry ceilings that end a task rather than an action ──
    # Read per call (`end_attempt`, `record_fanout_attempt_failed`). `env=None`: nothing sets one. Both are
    # retroactive, and the help says so: a saved 1 fails every task already past it at its next ended attempt
    # (deliberate, and recoverable since `system_failed` can restart). Floor 1 (0 reads as unset), ceiling
    # 20 a typo guard: each attempt is a billable runner.
    Knob(
        key="fleet_dispatch_cap",
        tier="org",
        label="Task attempts",
        # The help is the actionable rule (tenants read it too); mechanics are above. It keeps two facts: the
        # node-fault refund is capped at this same value (`routes/tasks/runner_settle.py::node_fault_refund_cap`, so the worst
        # case is twice it), and lowering it reaches in-flight tasks.
        help="Dispatches a task gets before an unfinished attempt marks it System failed; attempts lost to a "
             "node failure are refunded.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    Knob(
        key="fleet_epic_fanout_cap",
        tier="org",
        label="Fan-out attempts",
        help="How many times an epic's fan-out may fail to start a subtask before the epic is held for triage.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    Knob(
        key="fleet_max_observation_age_secs",
        tier="operator",
        label="Observation freshness",
        help="How recent a node's last pool report must be for its occupancy to count toward capping dispatch.",
        # Ceiling derived: 300s is 10x `jaah_discovery`'s 30s PRESENCE_HEARTBEAT_TTL while still refusing a
        # value that switches the staleness gate off. The 120.0 default is a measurement; its derivation lives in
        # `app/pool_state.py` beside the predicate. Nothing sets the env var in either environment.
        kind="float", env="FLEET_MAX_OBSERVATION_AGE_SECS", default=120.0,
        minimum=1.0, maximum=300.0, unit="s",
    ),
    Knob(
        key="runner_launch_disk_floor_gb",
        # Operator tier: it protects shared hardware, and one tenant zeroing it would launch onto a host other
        # tenants' runners share.
        tier="operator",
        label="Launch disk floor",
        help="Free disk a node needs before a runner is launched on it; below it the task waits.",
        # 5 GB, measured: a runner on a host with 0.9 GB free never reached its first transcript
        # write and was relaunched three times. Static slots reuse a clone, so this is small. 0 switches the
        # gate off; the ceiling is a typo guard. Read per drain pass by `launch_disk.wait_reason`.
        kind="float", env=None, default=5.0, minimum=0.0, maximum=4096.0, unit="GB",
    ),
    Knob(
        key="runner_memory_reservation_gb",
        # Operator tier, as the disk floor above: it protects shared hardware.
        tier="operator",
        label="Runner memory",
        help="Memory each runner session is counted at; a node runs at most its memory divided by this.",
        # 3 GB, measured: a runner session (Claude, LSP servers, its own dev stack, Chromium, gate
        # suites) held ~2.9 GB each on a 14-session node; 22 sessions placed on a 64 GB node in 3 minutes
        # took it from 18% to the OOM killer. 0 switches the ceiling off; the ceiling is a typo guard. Read by
        # `launch_memory.wait_reason`/`ids_blocked`.
        kind="float", env=None, default=3.0, minimum=0.0, maximum=1024.0, unit="GB",
    ),
    Knob(
        key="abandoned_dispatch_fail_minutes",
        # Operator tier: the CP's own dispatch behaviour, one scheduler.
        tier="operator",
        label="Abandoned after",
        # "Since the order was issued": the sweep measures from the intent's `created_at` (see
        # `lease_drain._fail_abandoned_dispatches`).
        help="A ready task still holding its slot this long after its dispatch order fails and frees the slot.",
        # 15 minutes, a measurement: the park lands ~75s after the first refusal and a genuine re-mint is
        # one 10s drain pass, so 15 minutes is ~90 passes past any transient.
        kind="int", env="FLEET_ABANDONED_DISPATCH_FAIL_MINUTES", default=15,
        # Floor 1 (0 reads as unset). The 55-minute ceiling exists because the evidence expires at 60:
        # `fleet_verdicts.purge_unexecutable_intents` deletes a parked intent at
        # `UNEXECUTABLE_INTENT_MAX_AGE_SECS` (3600s), so a window past that could never fire.
        # `test_operator_settings.py` pins the two numbers against each other; residual in
        # `lease_drain._drain_pass`.
        minimum=1, maximum=55, unit="minutes",
    ),
    _Section("Runners", "Placement"),
    # ── Node selection ──
    # The one dial between the two halves of placement; the rest is sized by core count
    # (`app/pool_state.py::assign_nodes`). Read per drain pass. Operator tier, forced: the allocation table
    # is built once per pass fleet-wide, so one band width is in play. `env=None`: nothing sets one.
    Knob(
        key="placement_cpu_tie_band_pct",
        tier="operator",
        label="CPU tie band",
        help="Nodes within this many points of load count as equally busy, and the one with fewer runners per "
             "core wins. Widen it if work lands unevenly across equally idle nodes.",
        # The range is the rule's two degenerate ends: 1pp is the plain least-busy rule unchanged, 100pp lets the
        # count decide outright. 0 is excluded (a division by zero, not "off"; `node_host_metrics.band` clamps too).
        kind="float", env=None, default=10.0, minimum=1.0, maximum=100.0, unit="pp",
    ),
    _Section("Runners", "Worktrees"),
    Knob(
        key="pool_idle_stop_hours",
        # Operator tier: an org-less reader and a deployment-owned subject (see
        # tests/test_operator_settings.py::_TIER_VERDICTS).
        tier="operator",
        label="Idle stop",
        help="A slot's stack with nobody in it is stopped after this long.",
        # 720h = 30 days; past that, stop running the reaper instead.
        kind="float", env="POOL_IDLE_STOP_HOURS", default=24.0, minimum=1.0, maximum=720.0, unit="h",
    ),
    Knob(
        key="pool_user_hold_release_hours",
        # Operator tier: the pool's own occupancy bookkeeping fleet-wide.
        tier="operator",
        label="Console hold release",
        # "With no session in it" is the load-bearing clause: this is how long a provably dead hold may keep
        # a slot, not an idle timer.
        help="A console hold with no live session is released after this long; task holds last until their "
             "task ends.",
        # 24h is the operator's number; below an hour it would race a node's lagging observation. The
        # maximum is not an off switch: at 720h the sweep still fires, only slower. Deliberately no disable
        # value for this one inference in the slot lifecycle.
        kind="float", env="POOL_USER_HOLD_RELEASE_HOURS", default=24.0, minimum=1.0, maximum=720.0,
        unit="h",
        # Beside `pool_idle_stop_hours`: both govern what the drain does to an unattended slot.
    ),
    _Section("Runners", "Behaviour"),
    # ── The four runner-behaviour kill switches ──
    # One switch per behaviour, no master: they fail independently. Default ON (the intended behaviour);
    # the node's flag reader fails open to the default. Operator tier, forced: a node-facing route reads
    # them with no org, which `_refuse_unscoped_read` would refuse for an org-tier knob. `env=None`.
    # Flip latency (single home): `_CACHE_TTL_SECONDS` 3s + `runner_flags.CACHE_TTL_SECONDS` 60s <= 63s; a node
    # whose daemon is down with no cached copy keeps its last answer. Each help says so, in a minute's words.
    Knob(
        key="runner_no_poll_gate",
        tier="operator",
        label="Foreground PR watch",
        help="Runners poll their PR in foreground calls and their gate blocks until it has a verdict; off "
             "restores the earlier behaviour. A flip reaches every node within about a minute, and a node "
             "whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_read_once",
        tier="operator",
        label="Read-once files",
        help="A repeat whole-file read of an unchanged file is skipped, and runners prefer the Read tool; "
             "off restores the earlier behaviour. A flip reaches every node within about a minute, and a "
             "node whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_batch_recon",
        tier="operator",
        label="Batched recon",
        help="Runners batch independent tool calls, send recon to one parallel wave of read-only subagents "
             "and carry the findings into the task's recon map; off restores the earlier behaviour. A flip "
             "reaches every node within about a minute, and a node whose daemon is down keeps its last "
             "answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_hook_dedupe",
        tier="operator",
        label="Router dedupe",
        help="The context router injects each routed document once per session, again only when its text "
             "changes; off restores the earlier behaviour. A flip reaches every node within about a minute, "
             "and a node whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_project_memory",
        tier="operator",
        label="Project memory",
        help="Runners receive and contribute project memory; off restores the earlier behaviour for every "
             "task, whatever its own setting. A flip reaches every node within about a minute, and a node "
             "whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    _Section("Runners", "Project memory"),
    # ── "Runners" > "Project memory": the comparison holdout and the feature's model; the kill switch above
    # outranks both, and `project_memory_default` (Tasks > Automation) is the default a task inherits.
    Knob(
        key="project_memory_holdout_percent",
        tier="operator",
        label="Holdout",
        help="Share of epics and standalone tasks run without memory, for comparison. Applies only where "
             "nothing was chosen.",
        kind="int", env=None, default=0, minimum=0, maximum=50, unit="%",
    ),
    Knob(
        key="memory_llm_credential_id",
        tier="operator",
        label="Model credential",
        help="The API key memory's model checks call. Blank turns those checks off.",
        # A handle, as `support_llm_credential_id`. No env fallback: blank means inactive.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("anthropic_api_key", "deepseek_api_key"),
    ),
    _Section("Infrastructure", "Cloud"),
    # ── "Infrastructure" > "Cloud": what the control plane needs in order to create one ──
    # Dial-home URL, tailnet enrolment, software source; read per create request. Four knobs about one act (the
    # instance-type allow-list is a table, `app/instance_types.py`). All operator-tier: every create route is
    # `@superadmin_only(Tenancy.OPERATOR)` (see `tests/test_operator_settings.py::_TIER_VERDICTS`).
    # Per-AWS-account details (AMI, region, subnet, security group, profile, volume) live per credential in
    # `cloud_node_defaults`: instance data belongs with its instance (`docs/policies/context-architecture.md`).
    Knob(
        key="cloud_node_cp_base_url",
        tier="operator",
        label="Control plane URL",
        help="The HTTPS base a new node reports to. Hosting requires it; otherwise empty derives it from the "
             "console that created the node.",
        # The empty default is load-bearing (derive from the request), so no length floor — except for hosting,
        # which never trusts a request's origin (`hosting_provisioning.cp_base_url`). 200 is a typo guard.
        kind="str", env="CLOUD_NODE_CP_BASE_URL", default="", maximum=200,
    ),
    Knob(
        key="cloud_node_tailscale_credential_id",
        tier="operator",
        label="Tailnet credential",
        help="The headscale credential that mints each new node's single-use enrolment key. Empty enrols nodes "
             "only from inside the VPC.",
        # A handle, never a key, so it is operational rather than secret (`OperatorSettingsError` echoes
        # values). An id, not a name: names are not unique, so a name could not pick a tailnet. `env=None`: an
        # env var could only carry another deployment's UUID. 36 is a canonical UUID's length, a shape check;
        # `cloud_node_launch.tailscale_credential` resolves it. Empty is a real setting, so bounds are 0..36.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("headscale",),
    ),
    Knob(
        key="cloud_node_tailscale_tags",
        tier="operator",
        label="Tailnet tags",
        help="Comma-separated ACL tags on each minted key; they bound what an enrolled node may reach.",
        # The refusal is the mint's, not a length floor: a floor would silently re-enrol with the default
        # tags when the operator meant "no tags", an authority question. 500 bounds a typo.
        kind="str", env="CLOUD_NODE_TAILSCALE_TAGS", default="tag:jaah-node", maximum=500,
    ),
    Knob(
        key="cloud_node_apt_bucket",
        tier="operator",
        label="Apt bucket",
        help="The S3 bucket of the signed jaah apt repository re-served to nodes over their node token.",
        # No real bucket as default: it would embed this fleet's AWS account in every deployment
        # (`docs/policies/context-architecture.md` rule 2), so empty means "not told". Named
        # `JAAH_ARTIFACTS_BUCKET` (the fleet's artifact store); its one setter is the deployment's
        # task definition, pinned by the deployment's launch-contract test. 3..63 is S3's real
        # bucket-name length. The 3 is load-bearing, unlike the empty-is-a-value rule for this section's
        # other knobs: an emptied field stored `""`, which outranked the env var and 503'd every node's
        # apt channel (2026-10-05). Now a write of `""` is refused and a stored one falls through to the
        # env, which is also the bucket the task role's read grant names.
        kind="str", env="JAAH_ARTIFACTS_BUCKET", default="", minimum=3, maximum=63,
    ),
    _Section("Infrastructure", "Data regions"),
    # ── "Infrastructure" > "Data regions": where an org's data may live (`app/data_residency.py`) ──
    # A JSON list of `{region, label, transcript_bucket}`, the control plane's own region first. Edited on its own
    # panel (`routes/data_residency.py`'s operator door, `pages/settings/dataRegions`), never as raw text; a bucket
    # name is not a secret. Empty = the control-plane region alone (no org can choose).
    Knob(
        key="data_regions",
        tier="operator",
        label="Data regions",
        help="Regions an org may keep its data in, each with its bucket. The control-plane region comes first.",
        kind="str", env=None, default="", minimum=0, maximum=4000,
        data_regions=True,
    ),
    # Titled as the NodeLink storage panel's section (`pages/Settings.tsx`), so the routed-VPC knobs and the storage
    # locations they check share one card stack.
    _Section("Infrastructure", "NodeLink storage"),
    # Read by `app/org_shared_store.py::network_warning` on a Storage tab open: a tenant file system whose mount
    # targets sit inside this network, in another account, can never be routed to. Empty (either) = no check.
    # Instance data, so no default (`docs/policies/context-architecture.md` rule 2); 18 is a dotted /nn's length.
    Knob(
        key="nodelink_routed_cidr",
        tier="operator",
        label="Routed VPC network",
        help="The VPC network nodes reach over the bastion route, e.g. 10.60.0.0/16. Storage whose mount targets "
             "fall inside it, in another AWS account, gets a warning.",
        kind="str", env=None, default="", minimum=0, maximum=18,
    ),
    Knob(
        key="nodelink_routed_account",
        tier="operator",
        label="Routed VPC account",
        help="The 12-digit AWS account that VPC is in.",
        kind="str", env=None, default="", minimum=0, maximum=12,
    ),
    # Read by `app/nodelinks.py::served_locations`: the operator's word that the Main VPC store's network admits
    # NFS from the hosting VPC (peering + its security group), so one Jaah-managed NodeLink spans both kinds of
    # node. Default off: nothing proves the route, and a node told a store it can't reach hangs its mount.
    Knob(
        key="nodelink_main_serves_hosting",
        tier="operator",
        label="Main VPC storage serves hosting nodes",
        help="On when the Main VPC storage admits NFS (2049) from the hosting VPC. Then one Jaah-managed "
             "NodeLink can hold hosting and Main VPC nodes together.",
        kind="bool", env=None, default=False,
    ),
    _Section("Infrastructure", "MicroVM"),
    # ── "Infrastructure" > "MicroVM": the system tier of a MicroVM's size (`app/microvm_sizing.py`: an org, project or
    # node value beats it; its `BOUNDS` are every tier's range) and how long a failed task's VM is kept. Read
    # per VM and per sweep. ──
    Knob(
        key="microvm_vcpus",
        tier="operator",
        feature="microvm",
        label="vCPUs",
        help="Each MicroVM's vCPU ceiling, unless an org, project or node sets its own.",
        kind="int", env=None, default=4, minimum=MICROVM_BOUNDS["vcpus"][0],
        maximum=MICROVM_BOUNDS["vcpus"][1], unit="vCPUs",
    ),
    Knob(
        key="microvm_ram_mib",
        tier="operator",
        feature="microvm",
        label="RAM",
        help="Each MicroVM's memory ceiling, unless an org, project or node sets its own.",
        kind="int", env=None, default=8192, minimum=MICROVM_BOUNDS["ram_mib"][0],
        maximum=MICROVM_BOUNDS["ram_mib"][1], unit="MiB",
    ),
    Knob(
        key="microvm_disk_gib",
        tier="operator",
        feature="microvm",
        label="Disk",
        help="Each MicroVM's disk ceiling, unless an org, project or node sets its own.",
        kind="int", env=None, default=64, minimum=MICROVM_BOUNDS["disk_gib"][0],
        maximum=MICROVM_BOUNDS["disk_gib"][1], unit="GiB",
    ),
    Knob(
        key="microvm_retention_hours",
        tier="operator",
        feature="microvm",
        label="Keep failed for",
        help="How long a failed task's MicroVM stays for inspection before its disk is reclaimed; 0 "
             "reclaims at once.",
        # Holds disk on a shared node, so bounded both ends: 24h default (next-morning inspection), 0
        # reclaims immediately, a week max. Read per sweep via `get_float`, so edits apply to the next failure.
        # Was `dynamic_env_retention_hours`; revision 202610111900 carried a stored value onto this key.
        kind="float", env=None, default=24.0, minimum=0.0, maximum=168.0, unit="h",
    ),
    _Section("Infrastructure", "Ports"),
    # ── "Infrastructure" > "Ports": the deterministic port grid's five scalar inputs ──
    # `app/port_grid.py::scalars()` reads them per request, per pool tick and per pool-port checker
    # run, so an edit re-bands the next lease with no restart. Env names and defaults match the constants
    # they replaced, so changing nothing moves no port.
    Knob(
        key="port_block_size",
        tier="org",
        label="Block size",
        # The port-grid helps are written for both consoles (org-tier: tenants read them read-only), with the
        # operator's reasoning kept in these comments. Descriptive, not an instruction: the block, with the
        # stride, IS the worktree index ceiling (`app/port_grid.py::index_ceiling`).
        help="Consecutive ports each project owns per service; with the stride, how many slots a project "
             "can hold on a node.",
        # Floor 1 is the degenerate but coherent grid. Ceiling is `port_window`'s default width (1000): a
        # wider block would start the next project inside the next service's range.
        kind="int", env="POOL_PORT_BLOCK_SIZE", default=10, minimum=1, maximum=1000, unit="ports",
    ),
    Knob(
        key="port_stride",
        tier="org",
        label="Stride",
        help="How far apart consecutive slots sit inside a project's block; 1 packs them.",
        # Ceiling 100 is where the bounds meet: a ten-worktree pool at stride 100 already needs the full
        # 1000-port block.
        kind="int", env="POOL_PORT_STRIDE", default=1, minimum=1, maximum=100, unit="ports",
    ),
    Knob(
        key="port_window",
        tier="org",
        label="Service window",
        # This width also decides whether a port embedded in a URL (FRONTEND_URL=http://localhost:3300)
        # belongs to a managed service and is re-banded with it (`app/port_grid.py`). The help names the
        # consequence, not the range-starts table, which a tenant does not see (`routes/port_grid.py`);
        # `port_grid.warnings_for` checks the relationship properly.
        help="How wide each service's port range is; it also decides whether a port in a URL belongs to that "
             "service.",
        # Floor 10 is the default block size (one project must fit). The ceiling is a typo guard: the whole
        # tier-1 grid is under 18000 ports wide.
        kind="int", env="POOL_PORT_WINDOW", default=1000, minimum=10, maximum=20000, unit="ports",
    ),
    Knob(
        key="auto_port_floor",
        tier="org",
        label="Auto-assign floor",
        # The flat range the node walks (tier 2) to auto-assign a declared port_vars entry with no grid
        # window; a port from it inside a window would look banded. The help names the consequence, not the
        # table a tenant does not see; `port_grid.warnings_for` checks the relationship.
        help="The lowest port a node hands to a service outside the grid, keeping auto-assigned ports clear "
             "of every project's block.",
        # 20800: the first port clear of the highest window (CDP_BRIDGE_PORT 18800..19799) with room for one
        # more. The bounds are just the non-privileged port space; the floor/ceiling relationship is
        # `port_grid.warnings_for`'s (a range bound sees one number at a time).
        kind="int", env="POOL_AUTO_PORT_FLOOR", default=20800, minimum=1024, maximum=65535,
    ),
    Knob(
        key="auto_port_ceiling",
        tier="org",
        label="Auto-assign ceiling",
        # 32767 is the last port below the kernel's ephemeral floor, where docker's dynamic ports also come
        # from; a service bound above it eventually loses its port. The help states the fact, not an order a
        # read-only tenant cannot act on. Enforced by the node's `RESERVED_PORT_RANGES` check.
        help="The highest port a node hands out; below 32768, where the host starts assigning ports "
             "dynamically.",
        # Same bounds as the floor. 32767 is deliberately not a range bound: the node's
        # `RESERVED_PORT_RANGES` check refuses allocation above it and can name the reservation.
        kind="int", env="POOL_AUTO_PORT_CEILING", default=32767, minimum=1024, maximum=65535,
    ),
    _Section("Infrastructure", "Console"),
    Knob(
        key="direct_console_token_ttl_seconds",
        tier="operator",
        label="Token lifetime",
        help="How long a direct-console browser credential stays valid before it is re-issued.",
        kind="int", env="DIRECT_CONSOLE_TOKEN_TTL_SECONDS", default=86400, minimum=300,
        maximum=86400, unit="s",
    ),
    Knob(
        key="direct_console_max_node_age_seconds",
        tier="operator",
        label="Freshness",
        help="How recent a node's last report must be for the console to offer it.",
        kind="float", env="DIRECT_CONSOLE_MAX_NODE_AGE_SECONDS", default=90.0,
        minimum=10.0, maximum=3600.0, unit="s",
    ),
    _Section("Infrastructure", "Updates"),
    # ── "Infrastructure" > "Updates" ──
    Knob(
        key="node_autoupdate_enabled",
        tier="operator",
        label="Auto-update",
        # The fleet default under each node's own override (`nodes.autoupdate_override`); resolved per node by
        # `app/node_autoupdate.py` and served on `GET /api/agent/autoupdate`, which the daemon and the root
        # updater ask before every converge. Default ON: updates are the routine deploy channel.
        help="Nodes install each new release on their own. A host or node can override this on its page.",
        kind="bool", env=None, default=True,
    ),
    _Section("Infrastructure", "Daemon"),
    # ── "Infrastructure" > "Daemon": the node daemon's own cadences and bounds (`NODE_SETTING_KEYS`) ──
    # Served on every `GET /api/agent/pool-tick` as `node_settings` and read by the daemon at use time
    # (`apps/jaahd/node_settings.py`, which mirrors each default and range; a jaahd test pins the pair), so
    # an edit reaches every node on its next pass. Operator tier: the route reads them org-less. `env=None`:
    # they replaced node-side env vars, and a CP env var could not reach a node anyway. Until a node has
    # read a tick, it runs on the same defaults.
    Knob(
        key="node_account_probe_interval_secs",
        tier="operator",
        label="Account probe",
        help="How often a node re-reads each account's login and quota.",
        # Floor 120s, twice the node's slowest probe bound (`collector._DELEGATED_PROBE_TIMEOUT`, 60s). Ceiling
        # 900s: dispatch refuses a quota reading older than `account_fold.max_quota_age_secs` (1800s), so two
        # intervals must fit inside it.
        kind="int", env=None, default=600, minimum=120, maximum=900, unit="s",
    ),
    Knob(
        key="node_agents_leased_probe_secs",
        tier="operator",
        label="Busy account check",
        help="How often a node re-lists the sessions of an account working a task; a dead runner is noticed "
             "no sooner.",
        # 5s matches the observation pass (smaller buys nothing); one fork per busy slot per check.
        kind="int", env=None, default=5, minimum=1, maximum=300, unit="s",
    ),
    Knob(
        key="node_agents_failed_retry_secs",
        tier="operator",
        label="Failed check retry",
        help="How soon a node retries an account whose session check failed.",
        # Same default as the busy check, which it used to follow; bounded by the failure clearing.
        kind="int", env=None, default=5, minimum=1, maximum=300, unit="s",
    ),
    Knob(
        key="node_agents_dirty_watch_secs",
        tier="operator",
        label="Launch watch",
        help="How long a node keeps checking an account at the busy rate after launching a runner on it.",
        # The floor must outlast the CP's `runner_liveness.LAUNCH_SETTLE_SECONDS` (90s), or a slow-starting
        # runner is read dead before the node sights it; pinned in `test_operator_settings.py`.
        kind="int", env=None, default=300, minimum=240, maximum=3600, unit="s",
    ),
    Knob(
        key="node_agents_full_scan_secs",
        tier="operator",
        label="Full scan",
        help="How often a node re-lists every other account, finding sessions no task names.",
        kind="int", env=None, default=3600, minimum=60, maximum=86400, unit="s",
    ),
    Knob(
        key="node_agents_probe_max_parallel",
        tier="operator",
        label="Parallel checks",
        help="Session checks a node runs at once; each costs about 70 MB of memory.",
        # 2: unbounded on a 903 MB node OOM-killed a login. Every account is still checked, in batches.
        kind="int", env=None, default=2, minimum=1, maximum=16, unit="checks",
    ),
    Knob(
        key="node_clone_timeout_secs",
        tier="operator",
        label="Clone timeout",
        help="How long a node lets a project's first clone run before giving up.",
        # Floor 300s, well above `clone_identity._WAIT_TIMEOUT_S` (60s), which must stay well under it.
        kind="int", env=None, default=900, minimum=300, maximum=7200, unit="s",
    ),
    Knob(
        key="node_max_concurrent_commands",
        tier="operator",
        label="Concurrent commands",
        help="Console commands a node runs at once; the rest wait their turn.",
        # Ceiling: the CP's own in-flight cap per node (`node_bus.MAX_PENDING_PER_NODE`, 256), pinned in
        # `test_operator_settings.py`; past it the CP refuses before the node would queue.
        kind="int", env=None, default=64, minimum=1, maximum=256, unit="commands",
    ),
    _Section("Infrastructure", "Orders"),
    # ── "Infrastructure" > "Orders": how long an Add-node order may wait before the sweep ends it ──
    # Read by `app/node_orders.py::sweep` (the `node-order-sweep` cron) on every pass.
    Knob(
        key="self_hosted_join_timeout_days",
        tier="operator",
        label="Self-hosted join timeout",
        help="How long a new self-hosted machine has to run its command and join before the order ends.",
        # 7 days: long enough to get to the machine over a weekend; the command's token dies with the order.
        kind="int", env=None, default=7, minimum=1, maximum=90, unit="days",
    ),
    Knob(
        key="node_order_payment_timeout_minutes",
        tier="operator",
        label="Payment timeout",
        help="How long an order may wait for its payment to finish before it ends.",
        # 30 minutes covers a 3-D Secure challenge taken on another device.
        kind="int", env=None, default=30, minimum=5, maximum=1440, unit="minutes",
    ),
    Knob(
        key="node_order_start_timeout_hours",
        tier="operator",
        label="Start timeout",
        help="How long a new node may take to come online before its order ends.",
        # 6 hours: a launch and its first converge take minutes; past this something needs a person.
        kind="int", env=None, default=6, minimum=1, maximum=168, unit="hours",
    ),
    _Section("Infrastructure", "Hosts"),
    # ── "Infrastructure" > "Hosts": what one co-located node may take of its shared host ──
    # Operator-tier: they protect shared hardware, and their readers (dispatch, the host's converge) are
    # org-less. Disk and inodes are a WARNING — over it the console flags the node "Over cap" and dispatch
    # carries on (`app/vnodes.py::over_cap`); the operator may override both per node.
    Knob(
        key="vnode_disk_cap_gb",
        tier="operator",
        label="Disk cap",
        help="Disk one co-located node may use before it is flagged Over cap. A Shared tier's disk wins.",
        kind="int", env=None, default=50, minimum=1, maximum=65536, unit="GB",
    ),
    Knob(
        key="vnode_inode_cap",
        tier="operator",
        label="File cap",
        help="Files one co-located node may hold before it is flagged Over cap.",
        # 3,276,800 = what ext4's default ratio (one inode per 16 KiB) gives a 50 GB filesystem.
        kind="int", env=None, default=3_276_800, minimum=1000, maximum=2_000_000_000, unit="files",
    ),
    Knob(
        key="vnode_conntrack_limit",
        tier="operator",
        label="Connection cap",
        help="Connections one co-located node may have open at once; past it, new ones are dropped.",
        # Enforced by the host's nft table per node (`apps/jaahd/vnodehelper`); the bounds are the helper's.
        kind="int", env=None, default=16384, minimum=256, maximum=1_000_000, unit="connections",
    ),
)
