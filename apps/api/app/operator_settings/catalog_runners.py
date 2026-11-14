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
        help="Unfinished attempts past this mark the task System failed.",
        more="Attempts lost to a node failure are refunded, up to this same number, so a task can run up to "
             "twice it. Lowering it reaches tasks already running: one past the new limit fails at its next "
             "ended attempt, and can be restarted.",
        docs="using-jaah/tasks/how-tasks-work",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    Knob(
        key="fleet_epic_fanout_cap",
        tier="org",
        label="Fan-out attempts",
        help="Failed subtask starts past this hold the epic for triage.",
        more="Lowering it reaches running epics: one already past the new limit is held at its next failed "
             "start.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    Knob(
        key="fleet_max_observation_age_secs",
        tier="operator",
        label="Observation freshness",
        help="Older node reports stop counting toward dispatch occupancy limits.",
        more="Seconds since a node's last pool report. Wider trusts a dead node's reported occupancy longer; "
             "narrower makes slow nodes drop out of dispatch intermittently. Node liveness is a separate "
             "clock.",
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
        help="Nodes with less free disk get no runners; tasks wait.",
        more="0 switches the check off. 5 GB is measured: a runner on a host with under 1 GB free never wrote "
             "its first transcript and was relaunched three times.",
        # 5 GB, measured: a runner on a host with 0.9 GB free never reached its first transcript
        # write and was relaunched three times. A clone is per task, so size it for one repo. 0 switches the
        # gate off; the ceiling is a typo guard. Read per drain pass by `launch_gates.wait_reason`.
        kind="float", env=None, default=5.0, minimum=0.0, maximum=4096.0, unit="GB",
    ),
    Knob(
        key="abandoned_dispatch_fail_minutes",
        # Operator tier: the CP's own dispatch behaviour, one scheduler.
        tier="operator",
        label="Abandoned after",
        # "Since the order was issued": the sweep measures from the intent's `created_at` (see
        # `lease_drain._fail_abandoned_dispatches`).
        help="Unstarted dispatches older than this fail and free their slot.",
        more="Measured from when the dispatch order was issued, for a ready task still holding its slot. The "
             "ceiling is 55 minutes because the evidence is purged at 60.",
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
        help="Similarly loaded nodes tie; fewer runners per core wins.",
        more="Nodes whose CPU load is within this many percentage points count as equally busy. 1 is the plain "
             "least-busy rule; 100 lets the runner count decide outright. Widen it if work lands unevenly "
             "across equally idle nodes.",
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
        help="Slot stacks nobody is using are stopped after this long.",
        # 720h = 30 days; past that, stop running the reaper instead.
        kind="float", env="POOL_IDLE_STOP_HOURS", default=24.0, minimum=1.0, maximum=720.0, unit="h",
    ),
    Knob(
        key="pool_held_idle_stop_hours",
        # Operator tier, as `pool_idle_stop_hours`: the same org-less drain reads it.
        tier="operator",
        label="Idle stop, stopped tasks",
        help="Stacks of stopped or queued tasks are stopped after this long.",
        # 0.25h floor: shorter than one drain cadence plus a node round trip buys nothing.
        kind="float", env="POOL_HELD_IDLE_STOP_HOURS", default=1.0, minimum=0.25, maximum=720.0, unit="h",
    ),
    _Section("Runners", "Behaviour"),
    # ── The four runner-behaviour kill switches ──
    # One switch per behaviour, no master: they fail independently. Default ON (the intended behaviour);
    # the node's flag reader fails open to the default. Operator tier, forced: a node-facing route reads
    # them with no org, which `_refuse_unscoped_read` would refuse for an org-tier knob. `env=None`.
    # Flip latency (single home): `_CACHE_TTL_SECONDS` 3s + `runner_flags.CACHE_TTL_SECONDS` 60s <= 63s; a node
    # whose daemon is down with no cached copy keeps its last answer. Each one's More says so, in a minute's words.
    Knob(
        key="runner_no_poll_gate",
        tier="operator",
        label="Foreground PR watch",
        help="Runners watch PRs in the foreground; gates wait for verdicts.",
        more="Off restores the earlier behaviour. A flip reaches every node within about a minute; a node "
             "whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_read_once",
        tier="operator",
        label="Read-once files",
        help="Runners are blocked from re-reading an unchanged whole file.",
        more="Runners are also steered to the Read tool. Off restores the earlier behaviour. A flip reaches "
             "every node within about a minute; a node whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_batch_recon",
        tier="operator",
        label="Batched recon",
        help="Runners batch tool calls and run recon as parallel subagents.",
        more="Findings are carried into the task's recon map. Off restores the earlier behaviour. A flip "
             "reaches every node within about a minute; a node whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_hook_dedupe",
        tier="operator",
        label="Router dedupe",
        help="Routed documents are injected once per session, again on change.",
        more="Off restores the earlier behaviour. A flip reaches every node within about a minute; a node "
             "whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="runner_project_memory",
        tier="operator",
        label="Project memory",
        help="Runners receive and contribute project memory.",
        more="Off restores the earlier behaviour for every task, whatever its own setting. A flip reaches "
             "every node within about a minute; a node whose daemon is down keeps its last answer.",
        kind="bool", env=None, default=True,
    ),
    _Section("Runners", "Project memory"),
    # ── "Runners" > "Project memory": the comparison holdout and the feature's model; the kill switch above
    # outranks both, and `project_memory_default` (Tasks > Automation) is the default a task inherits.
    Knob(
        key="project_memory_holdout_percent",
        tier="operator",
        label="Holdout",
        help="This share of tasks runs without memory, for comparison.",
        more="Counts epics and standalone tasks, and applies only where nothing chose memory on or off. 0 runs "
             "no holdout.",
        kind="int", env=None, default=0, minimum=0, maximum=50, unit="%",
    ),
    Knob(
        key="memory_llm_credential_id",
        tier="operator",
        label="Model credential",
        help="Memory's secret and contradiction checks call this key; blank disables.",
        more="The same key distils lessons from finished transcripts, each prompt redacted before it is sent. "
             "Blank turns every memory model check off, with no fallback key.",
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
        help="New nodes dial home to this HTTPS address.",
        more="Hosting requires it. Otherwise empty derives it from the console that created the node.",
        # The empty default is load-bearing (derive from the request), so no length floor — except for hosting,
        # which never trusts a request's origin (`hosting_provisioning.cp_base_url`). 200 is a typo guard.
        kind="str", env="CLOUD_NODE_CP_BASE_URL", default="", maximum=200,
    ),
    Knob(
        key="cloud_node_tailscale_credential_id",
        tier="operator",
        label="Tailnet credential",
        help="Mints each new node's single-use tailnet enrolment key.",
        more="Empty enrols nodes only from inside the VPC.",
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
        help="Enrolled nodes may reach only what these ACL tags allow.",
        more="Comma-separated; applied to each minted enrolment key.",
        # The refusal is the mint's, not a length floor: a floor would silently re-enrol with the default
        # tags when the operator meant "no tags", an authority question. 500 bounds a typo.
        kind="str", env="CLOUD_NODE_TAILSCALE_TAGS", default="tag:jaah-node", maximum=500,
    ),
    Knob(
        key="cloud_node_apt_bucket",
        tier="operator",
        label="Apt bucket",
        help="Nodes install jaah packages from this S3 bucket's signed repository.",
        more="The control plane re-serves it to each node over its node token. Empty falls back to the "
             "deployment's configured bucket.",
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
        help="Orgs may choose only these regions to keep their data.",
        more="Each region carries its transcript bucket. The control-plane region comes first; empty means it "
             "alone, and no org can choose.",
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
        help="Cross-account storage inside this network is flagged unreachable.",
        more="The VPC network nodes reach over the bastion route, e.g. 10.60.0.0/16. A file system whose mount "
             "targets sit inside it, in another AWS account, can never be routed to. Empty here or in the "
             "account turns the check off.",
        docs="using-jaah/nodelink/set-up-the-network-for-nodelink",
        kind="str", env=None, default="", minimum=0, maximum=18,
    ),
    Knob(
        key="nodelink_routed_account",
        tier="operator",
        label="Routed VPC account",
        help="Storage outside this AWS account in that network is flagged.",
        more="The 12-digit account the routed VPC is in.",
        docs="using-jaah/nodelink/set-up-the-network-for-nodelink",
        kind="str", env=None, default="", minimum=0, maximum=12,
    ),
    # Read by `app/nodelinks.py::served_locations`: the operator's word that the Main VPC store's network admits
    # NFS from the hosting VPC (peering + its security group), so one Jaah-managed NodeLink spans both kinds of
    # node. Default off: nothing proves the route, and a node told a store it can't reach hangs its mount.
    Knob(
        key="nodelink_main_serves_hosting",
        tier="operator",
        label="Main VPC storage serves hosting nodes",
        help="On: one NodeLink can mix hosting and Main VPC nodes.",
        more="Correct only when the Main VPC storage admits NFS (port 2049) from the hosting VPC, through "
             "peering and its security group. A node told about storage it cannot reach hangs its mount.",
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
        help="Caps each MicroVM's vCPUs; org, project or node values win.",
        kind="int", env=None, default=4, minimum=MICROVM_BOUNDS["vcpus"][0],
        maximum=MICROVM_BOUNDS["vcpus"][1], unit="vCPUs",
    ),
    Knob(
        key="microvm_ram_mib",
        tier="operator",
        feature="microvm",
        label="RAM",
        help="Caps each MicroVM's memory; org, project or node values win.",
        kind="int", env=None, default=8192, minimum=MICROVM_BOUNDS["ram_mib"][0],
        maximum=MICROVM_BOUNDS["ram_mib"][1], unit="MiB",
    ),
    Knob(
        key="microvm_disk_gib",
        tier="operator",
        feature="microvm",
        label="Disk",
        help="Caps each MicroVM's disk; org, project or node values win.",
        kind="int", env=None, default=64, minimum=MICROVM_BOUNDS["disk_gib"][0],
        maximum=MICROVM_BOUNDS["disk_gib"][1], unit="GiB",
    ),
    # A Whole-node VM's default size is its host less these (`app/node_vm_sizing.py`): room for the host's own
    # jaahd, helper, tailscaled and page cache, and clear of the host disk gate's critical line.
    Knob(
        key="node_vm_host_reserve_ram_mib",
        tier="operator",
        feature="microvm",
        label="Whole-node VM host RAM reserve",
        help="A Whole-node VM gets its host's memory less this, at most three quarters of it.",
        more="Applies when the node's VM is next built.",
        kind="int", env=None, default=2048, minimum=0, maximum=1_048_576, unit="MiB",
    ),
    Knob(
        key="node_vm_host_reserve_disk_gib",
        tier="operator",
        feature="microvm",
        label="Whole-node VM host disk reserve",
        help="A Whole-node VM gets its host's free disk less this.",
        more="Applies when the node's VM is next built.",
        kind="int", env=None, default=20, minimum=0, maximum=65_536, unit="GiB",
    ),
    Knob(
        key="microvm_retention_hours",
        tier="operator",
        feature="microvm",
        label="Keep failed for",
        help="A failed task's MicroVM is kept this long for inspection.",
        more="Then its disk is reclaimed; 0 reclaims at once. The VM holds disk on a shared node, so the "
             "maximum is a week. Edits apply to the next failure.",
        # Holds disk on a shared node, so bounded both ends: 24h default (next-morning inspection), 0
        # reclaims immediately, a week max. Read per sweep via `get_float`, so edits apply to the next failure.
        # Was `dynamic_env_retention_hours`; revision 202610111900 carried a stored value onto this key.
        kind="float", env=None, default=24.0, minimum=0.0, maximum=168.0, unit="h",
    ),
    _Section("Infrastructure", "Ports"),
    # ── "Infrastructure" > "Ports": the range a node picks each slot's ports from ──
    # Shipped on the pool tick's `grid` block and `GET /api/agent/port-grid` (`app/port_grid.py`), so an edit
    # applies to the next lease with no restart. The retired block-size, stride and window keys are
    # ignored on load if still stored.
    Knob(
        key="auto_port_floor",
        tier="org",
        label="Auto-assign floor",
        # The flat range the node walks to give each Port variable a free port at lease
        # (`apps/jaahd/worktree_mechanics/ports.py::apply_auto_ports`). The bounds are the non-privileged
        # port space; an inverted pair falls back whole to the node's defaults.
        help="Slot ports are assigned from here upward.",
        more="A node gives each Port variable a free port from this range when it leases a slot.",
        kind="int", env="POOL_AUTO_PORT_FLOOR", default=20800, minimum=1024, maximum=65535,
    ),
    Knob(
        key="auto_port_ceiling",
        tier="org",
        label="Auto-assign ceiling",
        # 32767 is the last port below the kernel's ephemeral floor, where docker's dynamic ports also come
        # from; a service bound above it eventually loses its port. The help states the fact, not an order a
        # read-only tenant cannot act on. Enforced by the node's `RESERVED_PORT_RANGES` check.
        help="Slot ports are assigned no higher than this.",
        more="Above 32767 the host assigns ports dynamically, and a service bound there eventually loses its "
             "port, so nodes refuse to allocate above it.",
        # Same bounds as the floor. 32767 is deliberately not a range bound: the node's
        # `RESERVED_PORT_RANGES` check refuses allocation above it and can name the reservation.
        kind="int", env="POOL_AUTO_PORT_CEILING", default=32767, minimum=1024, maximum=65535,
    ),
    _Section("Infrastructure", "Console"),
    Knob(
        key="direct_console_token_ttl_seconds",
        tier="operator",
        label="Token lifetime",
        help="Direct-console browser credentials are re-issued after this long.",
        kind="int", env="DIRECT_CONSOLE_TOKEN_TTL_SECONDS", default=86400, minimum=300,
        maximum=86400, unit="s",
    ),
    Knob(
        key="direct_console_max_node_age_seconds",
        tier="operator",
        label="Freshness",
        help="Nodes silent longer than this disappear from the direct console.",
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
        help="Nodes install each new release on their own.",
        more="A host or node can override this on its page.",
        kind="bool", env=None, default=True,
    ),
    # The GitHub Actions runner release every host pool installs at its next converge, instead of the node
    # package's own pin (the GHA runner installer's RUNNER_VERSION), which GitHub retires on its own
    # schedule: a refused version leaves every slot restarting with no job. Served on `GET /api/agent/gha-runners/
    # desired` (`routes/agent/gha_runners.py::_gha_runner_pin`) only while the version is set; the checksums are
    # the release body's `BEGIN SHA linux-x64` / `linux-arm64` lines, which `install.py` verifies the download
    # against. Blank = the package's pin.
    Knob(
        key="gha_runner_version",
        tier="operator",
        label="GitHub runner version",
        help="Runner release nodes install; blank uses the package's.",
        more="Nodes install it at their next converge. GitHub stops serving old runner versions; set the "
             "release's two checksums with it.",
        kind="str", env=None, default="", maximum=32,
        pattern=(r"[0-9]+\.[0-9]+\.[0-9]+", "a runner version like 2.338.2"),
        feature="gha_runners",
    ),
    Knob(
        key="gha_runner_sha256_x64",
        tier="operator",
        label="Runner SHA-256 (x64)",
        help="The release's linux-x64 checksum.",
        kind="str", env=None, default="", maximum=64,
        pattern=(r"[0-9a-f]{64}", "a SHA-256 checksum (64 lowercase hex characters)"),
        feature="gha_runners",
    ),
    Knob(
        key="gha_runner_sha256_arm64",
        tier="operator",
        label="Runner SHA-256 (arm64)",
        help="The release's linux-arm64 checksum.",
        kind="str", env=None, default="", maximum=64,
        pattern=(r"[0-9a-f]{64}", "a SHA-256 checksum (64 lowercase hex characters)"),
        feature="gha_runners",
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
        help="Nodes re-read each account's login and quota this often.",
        more="The ceiling is 900 seconds because dispatch refuses a quota reading older than 30 minutes.",
        # Floor 120s, twice the node's slowest probe bound (`collector._DELEGATED_PROBE_TIMEOUT`, 60s). Ceiling
        # 900s: dispatch refuses a quota reading older than `account_fold.max_quota_age_secs` (1800s), so two
        # intervals must fit inside it.
        kind="int", env=None, default=600, minimum=120, maximum=900, unit="s",
    ),
    Knob(
        key="node_agents_leased_probe_secs",
        tier="operator",
        label="Busy account check",
        help="Dead runners on busy accounts are noticed within this long.",
        more="Each check forks one process per busy slot.",
        # 5s matches the observation pass (smaller buys nothing); one fork per busy slot per check.
        kind="int", env=None, default=5, minimum=1, maximum=300, unit="s",
    ),
    Knob(
        key="node_agents_failed_retry_secs",
        tier="operator",
        label="Failed check retry",
        help="A failed session check is retried after this long.",
        # Same default as the busy check, which it used to follow; bounded by the failure clearing.
        kind="int", env=None, default=5, minimum=1, maximum=300, unit="s",
    ),
    Knob(
        key="node_agents_dirty_watch_secs",
        tier="operator",
        label="Launch watch",
        help="Busy-rate checks continue this long after a runner launches.",
        more="For this long after launching a runner. The floor outlasts the 90-second launch settle, or a "
             "slow-starting runner is read dead before the node sees it.",
        # The floor must outlast the CP's `runner_liveness.LAUNCH_SETTLE_SECONDS` (90s), or a slow-starting
        # runner is read dead before the node sights it; pinned in `test_operator_settings.py`.
        kind="int", env=None, default=300, minimum=240, maximum=3600, unit="s",
    ),
    Knob(
        key="node_agents_full_scan_secs",
        tier="operator",
        label="Full scan",
        help="Idle accounts are re-listed this often, finding untracked sessions.",
        kind="int", env=None, default=3600, minimum=60, maximum=86400, unit="s",
    ),
    Knob(
        key="node_agents_probe_max_parallel",
        tier="operator",
        label="Parallel checks",
        help="Caps simultaneous session checks; each costs about 70 MB.",
        more="Every account is still checked, in batches. Unbounded checks on a 903 MB node killed a login.",
        # 2: unbounded on a 903 MB node OOM-killed a login. Every account is still checked, in batches.
        kind="int", env=None, default=2, minimum=1, maximum=16, unit="checks",
    ),
    Knob(
        key="node_clone_timeout_secs",
        tier="operator",
        label="Clone timeout",
        help="A project's first clone running longer than this is abandoned.",
        # Floor 300s, well above `clone_identity._WAIT_TIMEOUT_S` (60s), which must stay well under it.
        kind="int", env=None, default=900, minimum=300, maximum=7200, unit="s",
    ),
    _Section("Infrastructure", "Orders"),
    # ── "Infrastructure" > "Orders": how long an Add-node order may wait before the sweep ends it ──
    # Read by `app/node_orders.py::sweep` (the `node-order-sweep` cron) on every pass.
    Knob(
        key="self_hosted_join_timeout_days",
        tier="operator",
        label="Self-hosted join timeout",
        help="Self-hosted orders end if the machine hasn't joined by then.",
        more="The join command's token dies with the order.",
        # 7 days: long enough to get to the machine over a weekend; the command's token dies with the order.
        kind="int", env=None, default=7, minimum=1, maximum=90, unit="days",
    ),
    Knob(
        key="node_order_payment_timeout_minutes",
        tier="operator",
        label="Payment timeout",
        help="Orders still unpaid after this long are ended.",
        more="30 minutes covers a 3-D Secure challenge taken on another device.",
        # 30 minutes covers a 3-D Secure challenge taken on another device.
        kind="int", env=None, default=30, minimum=5, maximum=1440, unit="minutes",
    ),
    Knob(
        key="node_order_start_timeout_hours",
        tier="operator",
        label="Start timeout",
        help="Orders whose node never comes online end after this.",
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
        help="Co-located nodes using more disk are flagged Over cap.",
        more="A warning only: dispatch carries on. A Shared tier's disk wins, and each node can override it.",
        kind="int", env=None, default=50, minimum=1, maximum=65536, unit="GB",
    ),
    Knob(
        key="vnode_inode_cap",
        tier="operator",
        label="File cap",
        help="Co-located nodes holding more files are flagged Over cap.",
        more="A warning only: dispatch carries on, and each node can override it. The default is what ext4 "
             "gives a 50 GB filesystem.",
        # 3,276,800 = what ext4's default ratio (one inode per 16 KiB) gives a 50 GB filesystem.
        kind="int", env=None, default=3_276_800, minimum=1000, maximum=2_000_000_000, unit="files",
    ),
    Knob(
        key="vnode_conntrack_limit",
        tier="operator",
        label="Connection cap",
        help="Co-located nodes' new connections past this are dropped.",
        more="Enforced per node by the host's firewall table.",
        # Enforced by the host's nft table per node (`apps/jaahd/vnodehelper`); the bounds are the helper's.
        kind="int", env=None, default=16384, minimum=256, maximum=1_000_000, unit="connections",
    ),
)
