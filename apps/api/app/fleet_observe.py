"""The one-transaction observation — `POST /api/agent/fleet/observe`.

One `BEGIN … COMMIT` writes every observed row, so a partial rebuild is unrepresentable. The node must
report every slot: `unobserved` (cannot judge), `unmaterialised` (not built), `gone` (positively
absent). Absence from a payload means nothing, and the three completeness flags are required.

The observation never creates a slot (`slot_allocation.converge` declares them from the assignments), so
`slot_update_statement` is an UPDATE on the natural key and an unmatched slot is skipped with a
warning. Nor does it destroy one: a `gone` or `unmaterialised` slot's row stands, and the node's
convergence pass (`apps/jaahd/reconcile/orchestrate.py::_unmaterialised_candidates`) rebuilds it. A vanished tree
with a hold raises an alert (`vanished_hold_statement`).
"""

# Deadlock is the one hazard this design adds (multi-row statements can lock in opposite orders):
#   L1 — every multi-row statement orders by the table's full natural key. `node_id` is constant within
#        a statement, so it decides no order; do not move it onto the name (a join every 15s/node).
#   L2 — `nodes → worktrees → runners → account_observations → accounts`.

# L3 — the quota-mirror write to `accounts` runs in its own transaction after this one commits, since
#      deprovision takes those tables in the opposite order. L4 — retry once on `40P01` and once on
#      `40001`, then fail.
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field, PrivateAttr, ValidationError, field_validator, model_validator
from sqlalchemy import (
    case, cast, column, delete, func, literal, null, or_, select, tuple_, update, values,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert

from app import (
    alert_record,
    carryover,
    edition_features,
    fleet_verdicts,
    node_clone,
    node_disk_events,
    node_host_metrics,
    node_session_usage,
    node_tmp_inode_alarm,
    node_platform,
    project_identity,
    slot_columns,
    slot_state,
)
from app.db import SessionLocal
# The two rules ingest and upsert share (what makes a block a dated reading, how a date parses),
# imported so there is one spelling. `account_fold` must never import from `app` at module level.
from app.account_fold import QUOTA_PERCENT_KEYS, asserts_a_percentage, reading_observed_at
from app.models.agent_sessions import FetchStamp
from app.models.credentials import Account, AccountObservation
from app.models.nodes import Node, NodeHostMetric, NodeSessionUsage, TaskSessionUsagePeak
from app.models.notifications import Alert
from app.models.projects import Project, Runner
from app.models.worktrees import Worktree
from app.node_identity import node_id_or_none, node_matches, warn_unregistered_node

if edition_features.enabled("cloud_nodes"):
    from app import cloud_lifecycle
if edition_features.enabled("microvm"):
    from app import microvm_mode

log = logging.getLogger("fleet-observe")

# The per-element verdict:
#   present        — the path is a directory on disk. Its declared row is updated (never created).
#   gone           — absent and the container is readable.
#   unobserved     — the node could not judge. The row survives, but nothing is written for it.
#   unmaterialised — absent, container readable, and this CP has never observed the slot present.

# `unobserved` writes nothing, since every sensed field is None and would blank the row.
# `unmaterialised` is distinct from it: one means the node could not look, the other that it looked
# and nothing is there yet.
SLOT_PRESENT = "present"
SLOT_GONE = "gone"
SLOT_UNOBSERVED = "unobserved"
SLOT_UNMATERIALISED = "unmaterialised"
SLOT_STATES = (SLOT_PRESENT, SLOT_GONE, SLOT_UNOBSERVED, SLOT_UNMATERIALISED)

# No "reported states" set: there is no absence-driven DELETE any more. Consumers ask
# `present?` for the sensed update and `gone?` for the vanished-hold read; `unmaterialised` writes nothing.

# Columns that move every tick by construction, excluded so the change gate (and SSE fan-out) fires
# only on real change; jaahd re-POSTs identical values each tick.
_CHANGE_GATE_EXCLUDED = frozenset(
    {
        "node_id",
        # The row's identity, matched by the update, so it cannot differ.
        "project_id",
        "number",
        "observed_at",
        "age_last_used_secs",
        "age_last_commit_secs",
        "age_index_mtime_secs",
        # Moves with every transcript write; the stall detector reads it, nothing displays a change of it.
        "transcript_written_at",
    }
)
_CHANGE_GATE_COLUMNS = tuple(sorted(slot_columns.SENSED - _CHANGE_GATE_EXCLUDED))

# The same gate for `runners` (`last_seen`/`observed_at` move every tick). The two identity columns
# are in the gate: a stored pid whose `proc_started_at` differs is a different process on a recycled pid.

# Stored column names (`account_id`, not the wire's `account_dir`): `_changed_runners` resolves the path
# first, or every runner would compare changed.
_RUNNER_CHANGE_GATE_COLUMNS = (
    "account_id", "branch", "cwd", "kind", "proc_started_at", "status", "worktree_path",
)

# The `accounts` gate's exclusions, declared in code: a test asserts every compiled SET key is either
# here or ruled in `_changed_accounts`. `observed_at`/`attested_at` move every tick.

# `account_id` and `org_id` are excluded, not ruled: each moves at most once (NULL -> value) and is
# rendered nowhere.
_ACCOUNT_GATE_EXCLUDED = frozenset(
    {"observed_at", "attested_at", "account_id", "org_id"}
)

# The columns `_changed_accounts` rules; with the set above, exactly the upsert's SET keys (asserted).
_ACCOUNT_GATE_RULED = frozenset(
    {"config_dir", "display_name", "email", "enabled", "extra", "quota"}
)


# ── Runner identity: one spelling for a process start instant ─────────────────────────────────────────

# UTC, whole seconds, `Z`, normalised on the way in because it feeds an equality test against the
# opaque `leases.pid_start_time`; two spellings of one instant never compare equal.

# Normalised here on ingest and on the ack (`routes/agent/slot_intents.py::SlotIntentAckIn.pid_start_time`), since
# node and CP deploy independently. The node's `""` becomes NULL (unknown), which never compares equal
# on a fail-closed path.
_PROC_START_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def normalise_proc_start(value: object) -> Optional[str]:
    """Any ISO-8601 spelling of a process start instant → `_PROC_START_FORMAT`; None for anything unusable.

    None means unknown: the destructive arm ("not my runner, the hold is dead") must refuse to fire on
    it. Sub-second precision is dropped: it is a clock-tick artefact, not identity.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            log.warning(
                "fleet-observe: process start time %r is not ISO-8601 — recorded as unknown, which "
                "refuses an identity match rather than inventing one", value,
            )
            return None
    if parsed.tzinfo is None:
        # A naive stamp means UTC (both node encoders build from an epoch).
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime(_PROC_START_FORMAT)


# ── Wire models ───────────────────────────────────────────────────────────────────────────────────────


# A slot's manifest (`apps/jaahd`'s multi-repo converge), stored whole on `worktrees.repos` for the drawer. Bounded so
# a runaway node cannot grow a row without limit; `ObserveSlotIn._an_unbelievable_repos_block_is_none` turns a
# breach into "no manifest", never a 422 that would cost the node its whole pass.
_MANIFEST_MAX_ENTRIES = 200


class ObservedSlotRepo(BaseModel):
    """One repo checked out in the slot. `id` is `project_repos.id`."""

    id: int
    path: str = Field(max_length=4096)
    repo_path: str = Field(max_length=512)
    base: str = Field(max_length=255)
    primary: bool = False


class ObservedKeptRepo(BaseModel):
    """A repo the project dropped that the node kept, because removing it would lose work."""

    id: int
    path: str = Field(max_length=4096)
    repo_path: str = Field(max_length=512)
    # `uncommitted` | `unpushed`; a plain string so a new node reason is shown, not refused.
    reason: str = Field(max_length=64)


class ObservedFailedRepo(BaseModel):
    """A repo the node could not bring into the slot."""

    id: int
    repo_path: str = Field(max_length=512)
    # `credential_refused` | `clone_failed` | `fetch_failed`, open like `ObservedKeptRepo.reason`.
    reason: str = Field(max_length=64)
    detail: str = Field(default="", max_length=500)


class ObservedSlotRepos(BaseModel):
    primary_repo_id: Optional[int] = None
    repos: list[ObservedSlotRepo] = Field(default_factory=list, max_length=_MANIFEST_MAX_ENTRIES)
    kept: list[ObservedKeptRepo] = Field(default_factory=list, max_length=_MANIFEST_MAX_ENTRIES)
    failed: list[ObservedFailedRepo] = Field(default_factory=list, max_length=_MANIFEST_MAX_ENTRIES)


class ObserveSlotIn(BaseModel):
    """One slot's observation. `state` carries the verdict the node's disk test reached."""

    # `project_id` is the identity (C6); `project`, the display name, is what an older node sends alone, and
    # `resolve_project_ids` then resolves it inside the node's own org.
    project: str
    project_id: Optional[int] = None
    number: int
    state: Literal["present", "gone", "unobserved", "unmaterialised"]
    # Why the node could not judge, or why it concluded `gone`. Diagnostic, never an input.
    reason: Optional[str] = None

    # Required in practice for `present`; a pathless record is treated as `unobserved` in
    # `slot_update_rows` rather than by the type, so the node keeps its row.
    path: Optional[str] = None
    branch: Optional[str] = None
    dirty: Optional[bool] = None
    merged: Optional[bool] = None
    ahead: Optional[int] = None
    behind: Optional[int] = None
    head_sha: Optional[str] = None
    session: Optional[bool] = None
    session_id: Optional[str] = None
    session_status: Optional[str] = None
    session_started_at: Optional[datetime] = None
    # ── Runner identity, not merely presence ──────────────────────────────────────────────────────────
    # A pid alone is not an identity (pids recycle, and reboots reuse them quickly).
    # `session_proc_started_at` is the kernel start instant resolved against `/proc/stat` `btime`, so
    # it survives a reboot.

    # A `str`, compared for equality and never subtracted. Optional for version skew: absent means
    # unknown, never a match.
    session_pid: Optional[int] = None
    session_proc_started_at: Optional[str] = None
    # The config dir a session in this slot runs under (feeds `worktrees.account`).
    account: Optional[str] = None
    console: Optional[str] = None
    probes: Optional[dict[str, Any]] = None
    probed_config_dirs: Optional[list[str]] = None
    # Node-computed ages, never absolute timestamps (spec Constraint 6 / P9): only the CP subtracts.
    age_last_used_secs: Optional[int] = None
    age_last_commit_secs: Optional[int] = None
    age_index_mtime_secs: Optional[int] = None
    # Age of the slot's newest session transcript write; None when nothing resolved. Stored as
    # `worktrees.transcript_written_at` (the stall detector's transcript signal).
    age_transcript_write_secs: Optional[int] = None
    tmux_window_id: Optional[str] = None
    tmux_target: Optional[str] = None
    window_name: Optional[str] = None
    compose_project_name: Optional[str] = None
    stack_present: Optional[bool] = None
    stack_up: Optional[bool] = None
    # Maps onto the existing `ports` column; `urls` is derived from it by the decide pass.
    ports_observed: Optional[dict[str, Any]] = None
    default_title: Optional[str] = None
    host_env: Optional[dict[str, Any]] = None
    model: Optional[str] = None
    # Sent only for a slot with a manifest; absent is a single-repo slot and writes NULL.
    repos: Optional[ObservedSlotRepos] = None

    @field_validator("session_proc_started_at", mode="before")
    @classmethod
    def _canonical_session_proc_start(cls, value: object) -> Optional[str]:
        return normalise_proc_start(value)

    @field_validator("repos", mode="wrap")
    @classmethod
    def _an_unbelievable_repos_block_is_none(cls, value: object, handler: Any) -> Optional[ObservedSlotRepos]:
        """A manifest that breaks a bound is dropped, not refused: the slot's other fields still land."""
        try:
            return handler(value)
        except ValidationError as exc:
            log.warning("fleet-observe: dropping a slot's repos manifest the CP cannot believe (%s)",
                        exc.errors()[0].get("msg", "invalid"))
            return None


class ObserveRunnerIn(BaseModel):
    """One runner's observation. `pid` is required, so a pid-less runner is unrepresentable here. The
    column stays nullable until the last ingest path writing NULL pids is gone."""

    pid: int
    cwd: str
    kind: Optional[str] = None
    # Accepted and dropped (migration 0104 removed `runners.project`); removing a wire field is not
    # additive.
    project: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None
    status: str = "active"
    # ── Runner identity ───────────────────────────────────────────────────────────────────────────────
    # See `ObserveSlotIn.session_pid`. Unlike the slot half, this covers every session the node sees,
    # including a runner whose worktree row was deleted.

    # `account_dir` is the credential the process actually runs under, so the CP can check it spends the
    # account it was given. Normalised: `""` must be unknown, not equal to other unknowns.
    proc_started_at: Optional[str] = None
    account_dir: Optional[str] = None

    @field_validator("proc_started_at", mode="before")
    @classmethod
    def _canonical_proc_start(cls, value: object) -> Optional[str]:
        return normalise_proc_start(value)

    @field_validator("account_dir", mode="before")
    @classmethod
    def _blank_account_dir_is_unknown(cls, value: object) -> Optional[str]:
        """`""` / whitespace ⇒ None, for `normalise_proc_start`'s reason."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None


# ── The ingest boundary for a quota block's date ───────────────────────────────────────────────────────

# How far ahead of the CP's clock a node's `observedAt` may sit. Further is clock skew, which
# `quota_is_stale` would read as permanently fresh; 120s is generous for NTP and far below 1800s.
_MAX_STAMP_SKEW_SECS = 120.0

# One warning per (node_id, slug) per process, capped by entry count (the ids arrive before the
# `is_registered_node` gate, so a `NODE_TOKEN` holder could grow the set). Key length is unbounded for
# these older sets (follow-up; `_WARN_KEY_MAX` covers host metrics). Past the cap, `debug`.
_UNDATABLE_WARN_CAP = 512
_undatable_warned: set[tuple[str, str]] = set()


def _reset_undatable_warnings() -> None:
    """Test seam only — every dedupe set here is process-local and never cleared in production."""
    _undatable_warned.clear()
    _skewed_pass_warned.clear()
    _implausible_metrics_warned.clear()


# One warning per node, capped like `_undatable_warned`.
_skewed_pass_warned: set[str] = set()


def _warn_skewed_pass(node_id: str, ahead_secs: float) -> None:
    """One line naming the node, how far ahead its clock is, and what was done."""
    if node_id in _skewed_pass_warned or len(_skewed_pass_warned) >= _UNDATABLE_WARN_CAP:
        log.debug("node %s posted an observation stamped %.0fs in the future; clamped", node_id, ahead_secs)
        return
    _skewed_pass_warned.add(node_id)
    log.warning(
        "node %s posted an observation stamped %.0fs in the FUTURE, beyond the %.0fs clock-skew allowance — "
        "clamping the pass stamp to this control plane's now(). Its readings will age from now instead of "
        "from its own clock; fix NTP on that host. Attestation is unaffected (it is CP-stamped).",
        node_id, ahead_secs, _MAX_STAMP_SKEW_SECS,
    )


def _warn_undatable_quota(node_id: str, slug: str, *, reason: str, action: str, raw: object) -> None:
    """One line naming the node, the slug, why the stamp is unusable and what was done about it."""
    key = (node_id, slug)
    if key in _undatable_warned or len(_undatable_warned) >= _UNDATABLE_WARN_CAP:
        log.debug("undatable quota from node %r slug %r (%s -> %s): %r", node_id, slug, reason, action, raw)
        return
    _undatable_warned.add(key)
    log.warning(
        "node %r sent a quota block for slug %r this control plane cannot date (%s, observedAt=%r) — %s. "
        "An undatable reading can never be aged out by the staleness bound, so it is refused rather than "
        "believed; the rest of the pass is unaffected.",
        node_id, slug, reason, raw, action,
    )


# ── The ingest boundary for the host metrics block ─────────────────────────────────────────────────────

# One warning per node, capped like the sets above; the ~5s cadence makes rate-limiting mandatory.
_implausible_metrics_warned: set[str] = set()

# A separate budget: the set is keyed on `node_id` alone, so sharing it would let the trivial
# optional-field narrowing warning permanently demote a later lost-block warning to `debug`.
_optional_narrowed_warned: set[str] = set()

# Key length bound: the cap counts entries, not bytes, and `node_id` is unbounded before the
# registration gate. The full id stays on the log line.
_WARN_KEY_MAX = 64


def _warn_implausible_host_metrics(node_id: str, problem: str, *, seen: Optional[set] = None) -> None:
    """One line naming the node, what was implausible about its block, and that the pass itself survived."""
    seen = _implausible_metrics_warned if seen is None else seen
    key = node_id[:_WARN_KEY_MAX]
    if key in seen or len(seen) >= _UNDATABLE_WARN_CAP:
        log.debug("implausible host_metrics from node %r (%s); block dropped", node_id, problem)
        return
    seen.add(key)
    log.warning(
        "node %r sent a host_metrics block this control plane cannot believe (%s) — dropping the BLOCK "
        "only; every other family in the pass is applied normally. A metric is the least important thing "
        "on this payload and must never cost the node its worktree/runner/account rows. Check "
        "apps/jaahd/host_metrics.py on that host.",
        node_id, problem,
    )


class TopProcessIn(BaseModel):
    """One of the host's busiest processes: its NAME (`/proc/<pid>/comm`), never argv (argv holds secrets),
    its user, CPU in percent of one core and RSS. Cleaned before pydantic sees it (`_a_top_process_or_none`)."""

    name: str
    user: str
    cpu_pct: float
    rss_kb: int


class SessionUsageIn(BaseModel):
    """One live runner session's process tree, rooted at its pane pid (`app/node_session_usage.py` owns every
    rule). `cpu_pct` is percent of one core; `cpu_seconds` is cumulative and monotone per session. `task_id` is
    node-attested and never trusted for a cross-org read. Cleaned before pydantic sees it
    (`_a_session_or_none`)."""

    task_id: int
    session_id: str = ""
    pid: int
    cpu_pct: float
    rss_kb: int
    procs: int
    cpu_seconds: float


class HostMetricsIn(BaseModel):
    """One node's windowed CPU/RAM mean — the optional `host_metrics` key on the observation payload.

    Produced by `apps/jaahd/host_metrics.py::HostMetricsSampler.reading`, consumed by
    `app/node_host_metrics.py` (the single home of every rule). This is only the wire shape.

    No `Field(ge=…, le=…)` anywhere: a constraint would 422 the whole pass and cost the node its
    worktree, runner and account rows. Plausibility lives in
    `ObservationIn._an_implausible_host_metrics_block_is_no_reading`, whose verdict is "drop the block".

    `cpu_count`/`mem_total_kb` are optional (unreadable sources must not cost the percentage). `cpu_count`
    also feeds placement (`app/pool_state.py::assign_nodes`); its plausibility ceiling is
    `app/node_host_metrics.py::MAX_PLACEMENT_CORES`, not here."""

    cpu_pct: float
    mem_pct: float
    cpu_count: Optional[int] = None
    mem_total_kb: Optional[int] = None
    # The span the mean actually covers, not the configured target, so the UI's "40s average" is honest.
    window_secs: float
    samples: int
    # ── Disk ─────────────────────────────────────────────────────────────────────
    #
    # Optional so older daemons' payloads are unchanged; a required field would 422 every pass. `None`
    # means "said nothing", never "no" (`app/models/nodes.py::NodeHostMetric`). kB like `mem_total_kb`. Range-checked in the validator ("null the pair"), not here.
    disk_free_kb: Optional[int] = None
    disk_total_kb: Optional[int] = None
    # ── /tmp inodes ──────────────────────────────────────────────────────────────
    #
    # Free and total inodes of the node's /tmp, latest like disk; optional for the same reason. A tmpfs
    # /tmp can run out of inodes with gigabytes free. Range-checked in the validator ("null the pair").
    tmp_inodes_free: Optional[int] = None
    tmp_inodes_total: Optional[int] = None
    # ── OS patch state (SOC2 G-35 evidence) ──────────────────────────────────────
    #
    # Optional for the same reason. `last_upgrade_epoch` is the apt upgrade stamp's mtime, epoch seconds.
    reboot_required: Optional[bool] = None
    last_upgrade_epoch: Optional[int] = None
    # ── Network throughput ───────────────────────────────────────────────────────
    #
    # Bytes per second, windowed like `cpu_pct`; optional for the same reason. Range-checked before
    # pydantic (`_NET_KEYS`), dropped one field at a time.
    net_rx_bps: Optional[float] = None
    net_tx_bps: Optional[float] = None
    # ── Pressure signals ─────────────────────────────────────────────
    #
    # Latest values, optional for the same reason. Range-checked before pydantic (`_SIGNAL_RANGES`), dropped one
    # field at a time; the swap pair is nulled together in the validator, as disk.
    load_1: Optional[float] = None
    load_5: Optional[float] = None
    load_15: Optional[float] = None
    swap_total_kb: Optional[int] = None
    swap_free_kb: Optional[int] = None
    psi_cpu_pct: Optional[float] = None
    psi_mem_pct: Optional[float] = None
    psi_io_pct: Optional[float] = None
    # Bounded lists, cleaned entry by entry before pydantic (`_LIST_KEYS`): a malformed entry costs only itself.
    top_processes: Optional[list[TopProcessIn]] = None
    sessions: Optional[list[SessionUsageIn]] = None


# The columns' own ceilings (`integer`/`bigint`, migration 0151): Python ints are unbounded, and an
# oversized value raises `22003` inside the caller's transaction. Storage limits, not domain rules.
_INT32_MAX = 2_147_483_647
_INT64_MAX = 9_223_372_036_854_775_807


# Runner-CLI attestation bounds: names, not paths, bounded so a compromised node cannot park a blob in
# a JSONB column dispatch reads.
_RUNNER_CLI_NAME_MAX = 64
_RUNNER_CLI_COUNT_MAX = 32
# The block's key count: pydantic's `extra="ignore"` would accept a huge junk-key dict, and the
# narrowing below copies it. One `len()` check.
_BLOCK_KEY_MAX = 64
# Name-shaped only: node-attested input landing in a column dispatch will read, so no newline, shell
# metacharacter or `../`.
_RUNNER_CLI_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]*\Z")


#: The keys older daemons omit, named because they are the only fields that may be dropped
#: individually.
_DISK_KEYS = ("disk_free_kb", "disk_total_kb")
_TMP_INODE_KEYS = ("tmp_inodes_free", "tmp_inodes_total")
_PATCH_KEYS = ("reboot_required", "last_upgrade_epoch")
_NET_KEYS = ("net_rx_bps", "net_tx_bps")
# Exclusive ceiling on a byte rate: 1e12 B/s is 8 Tbit/s, far past any NIC, so only a lie reaches it.
_NET_BPS_MAX = 1e12
# Exclusive ceiling on `last_upgrade_epoch` (year ~5138): far past any real stamp, far inside `datetime`'s
# range, so the ingest's conversion can never raise.
_EPOCH_MAX = 10**11
# The pressure scalars' inclusive ranges. A load average past 1e6 is a lie on any host; PSI is a percentage; swap
# is a bigint column.
_SIGNAL_RANGES: dict[str, tuple[float, float]] = {
    "load_1": (0.0, 1e6), "load_5": (0.0, 1e6), "load_15": (0.0, 1e6),
    "psi_cpu_pct": (0.0, 100.0), "psi_mem_pct": (0.0, 100.0), "psi_io_pct": (0.0, 100.0),
    "swap_total_kb": (0, _INT64_MAX), "swap_free_kb": (0, _INT64_MAX),
}
_SWAP_KEYS = ("swap_total_kb", "swap_free_kb")
# The list caps (the daemon sends the union of top 5 by CPU and top 5 by RSS; at most 64 live sessions) and the
# string caps: a process name is `comm` (15 bytes on Linux; 32 leaves room), a user name likewise.
_TOP_PROCESSES_MAX = 10
_SESSIONS_MAX = 64
_PROCESS_TEXT_MAX = 32
_SESSION_ID_MAX = 128
# Exclusive ceiling on a session's cumulative CPU seconds: ~31,700 core-years, so only a lie reaches it.
_CPU_SECONDS_MAX = 1e12


def _the_optional_fields_cost_themselves_and_not_the_reading(block: Any) -> tuple:
    """Drop a wrong-typed optional key out of a `host_metrics` block, before pydantic sees it.

    The whole-block validator would catch it but cost the CPU/RAM reading too; this drops just the bad
    field to absent, as `runner_clis` does.

    Stricter than pydantic's lax mode: `"true"` or `1.0` are refused. `bool` is checked before `int` (a
    `bool` is an `int`)."""
    if not isinstance(block, dict) or len(block) > _BLOCK_KEY_MAX:
        # A block with far too many keys is dropped whole by the caller; this function only judges the
        # optional fields.
        return (block, ())
    bad = []
    for key in _DISK_KEYS + _TMP_INODE_KEYS + _PATCH_KEYS + _NET_KEYS:
        if key not in block or block[key] is None:
            continue
        value = block[key]
        if key == "reboot_required":
            ok = isinstance(value, bool)
        elif key in _NET_KEYS:
            # The range compare alone, never `math.isfinite`: a JSON int of 300+ digits overflows the
            # float conversion and would fail the whole pass. int/float compare is exact; NaN/inf fail it.
            ok = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 <= value < _NET_BPS_MAX
            )
        elif key == "last_upgrade_epoch":
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value < _EPOCH_MAX
        else:
            ok = isinstance(value, int) and not isinstance(value, bool)
        if not ok:
            bad.append(key)
    for key, (low, high) in _SIGNAL_RANGES.items():
        value = block.get(key)
        if value is None:
            continue
        # Swap is a bigint column, so an int only; the rest take either.
        number_ok = isinstance(value, int) if key in _SWAP_KEYS else isinstance(value, (int, float))
        if not _in_range(value, low, high) or not number_ok:
            bad.append(key)
    cleaned: dict[str, list] = {}
    narrowed_lists: list[str] = []
    ceiling = _cpu_pct_ceiling(block.get("cpu_count"))
    for key, cap, clean in _LIST_KEYS:
        raw = block.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            bad.append(key)
            continue
        # Truncated first, so a huge list costs a bounded walk; then each malformed entry is dropped alone.
        entries = [clean(item, ceiling) for item in raw[:cap]]
        # Always the cleaned copy (strings stripped, numbers as floats); named for the warning only when the node
        # sent something the clean changed.
        cleaned[key] = [entry for entry in entries if entry is not None]
        if cleaned[key] != raw:
            narrowed_lists.append(key)
    if not bad and not cleaned:
        return (block, ())
    # A copy, never a mutation of the caller's body. The dropped names travel back for the warning.
    narrowed = {k: v for k, v in block.items() if k not in bad}
    narrowed.update(cleaned)
    return (narrowed, tuple(bad) + tuple(narrowed_lists))


def _in_range(value: Any, low: float, high: float) -> bool:
    """A JSON number within `[low, high]` — never a bool (an `int`), never NaN (fails every compare). The compare
    alone, never `math.isfinite`, for `_NET_KEYS`' reason: a 300-digit int would overflow the float conversion."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


def _cpu_pct_ceiling(cpu_count: Any) -> float:
    """The most percent-of-one-core a process or session can burn: `100 x cores`, with an unusable core count
    read as `node_host_metrics.MAX_PLACEMENT_CORES` (generous: a bad label must not cost the entries)."""
    plausible = (
        isinstance(cpu_count, int) and not isinstance(cpu_count, bool)
        and 0 < cpu_count <= node_host_metrics.MAX_PLACEMENT_CORES
    )
    return 100.0 * (cpu_count if plausible else node_host_metrics.MAX_PLACEMENT_CORES)


# How far past `100 x cores` a CPU figure may read and still be clamped rather than dropped: two scans' clock
# jitter can put a pegged tree a few percent over, which is a real reading.
_CPU_PCT_SLACK = 1.1


def _cpu_pct_or_none(value: Any, ceiling: float) -> Optional[float]:
    """A CPU percentage clamped to `ceiling`, or None when it is not a number in `[0, ceiling x _CPU_PCT_SLACK]`."""
    if not _in_range(value, 0.0, ceiling * _CPU_PCT_SLACK):
        return None
    return min(float(value), ceiling)


def _clean_text(value: Any, cap: int) -> Optional[str]:
    """A node-attested string with only its printable characters kept, truncated to `cap`, or None for a non-string.

    `str.isprintable`, not a control-character class: it also drops the format characters (bidi overrides,
    zero-width joiners) that make a rendered process name read as something it is not."""
    if not isinstance(value, str):
        return None
    return "".join(ch for ch in value[: cap * 4] if ch.isprintable())[:cap]


def _a_top_process_or_none(item: Any, ceiling: float) -> Optional[dict]:
    """One `top_processes` entry as `TopProcessIn` will accept it, or None when it is malformed. A nameless
    process names nothing, so it is dropped; an empty user is kept."""
    if not isinstance(item, dict):
        return None
    name = _clean_text(item.get("name"), _PROCESS_TEXT_MAX)
    user = _clean_text(item.get("user", ""), _PROCESS_TEXT_MAX)
    cpu = _cpu_pct_or_none(item.get("cpu_pct"), ceiling)
    rss: Any = item.get("rss_kb")
    if not name or user is None or cpu is None:
        return None
    if not isinstance(rss, int) or not _in_range(rss, 0, _INT64_MAX):
        return None
    return {"name": name, "user": user, "cpu_pct": cpu, "rss_kb": rss}


def _a_session_or_none(item: Any, ceiling: float) -> Optional[dict]:
    """One `sessions` entry as `SessionUsageIn` will accept it, or None when it is malformed. `task_id` must name
    a possible task (`tasks.id` is an integer); it is still never trusted across orgs
    (`app/node_session_usage.py`)."""
    if not isinstance(item, dict):
        return None
    session_id = _clean_text(item.get("session_id") or "", _SESSION_ID_MAX)
    ints: list[Any] = [item.get(key) for key in ("task_id", "pid", "rss_kb", "procs")]
    if session_id is None or not all(isinstance(value, int) and not isinstance(value, bool) for value in ints):
        return None
    task_id, pid, rss, procs = ints
    if not (0 < task_id <= _INT32_MAX and 0 < pid <= _INT32_MAX and 0 <= rss <= _INT64_MAX
            and 0 <= procs <= _INT32_MAX):
        return None
    cpu = _cpu_pct_or_none(item.get("cpu_pct"), ceiling)
    cpu_seconds: Any = item.get("cpu_seconds")
    if cpu is None or not _in_range(cpu_seconds, 0.0, _CPU_SECONDS_MAX):
        return None
    return {
        "task_id": task_id, "session_id": session_id, "pid": pid, "cpu_pct": cpu, "rss_kb": rss,
        "procs": procs, "cpu_seconds": float(cpu_seconds),
    }


#: The bounded lists in a `host_metrics` block: key, cap, entry cleaner.
_LIST_KEYS = (
    ("top_processes", _TOP_PROCESSES_MAX, _a_top_process_or_none),
    ("sessions", _SESSIONS_MAX, _a_session_or_none),
)


def _a_runner_cli_list_this_cp_cannot_parse_is_no_attestation(data: dict[str, Any]) -> dict[str, Any]:
    """Drop a `runner_clis` value that is not a bounded list of plausible names, before pydantic can 422
    the pass over it.

    The pass carries the fleet's occupancy truth; this field is a dispatch nicety, so it may cost only
    itself. Believable: a bounded list of distinct, name-shaped strings (the node derives it from a set).
    Dropped whole, not filtered, so no claim the node never made is stored. Absent and `None` pass."""
    raw = data.get("runner_clis")
    if raw is None:
        return data
    ok = (
        isinstance(raw, list)
        and len(raw) <= _RUNNER_CLI_COUNT_MAX
        and all(
            isinstance(n, str) and len(n) <= _RUNNER_CLI_NAME_MAX and _RUNNER_CLI_NAME.match(n)
            for n in raw
        )
        and len(set(raw)) == len(raw)
    )
    if ok:
        return data
    log.warning(
        "node %r sent a runner_clis value this control plane cannot believe (%s) — dropping the FIELD "
        "and keeping the rest of the pass; `nodes.runner_clis` holds its last good attestation. Check "
        "apps/jaahd/runner_clis.py on that host.",
        str(data.get("node_id") or "<unnamed>"), type(raw).__name__,
    )
    # A dict copy, never a mutation of the caller's own.
    return {**data, "runner_clis": None}


class PlatformIn(BaseModel):
    """What the machine says it is — the optional `platform` key (`app/node_platform.py` owns the
    vocabularies). Each field may be absent (unknown); a value outside its vocabulary is dropped at ingest by
    `_a_platform_block_this_cp_cannot_believe_is_partly_unknown`, never a 422 of the pass."""

    os: Optional[str] = None
    arch: Optional[str] = None
    os_version: Optional[str] = None
    # `systemd-detect-virt`'s answer (`none` = bare metal) and whether `/dev/kvm` exists — what the host's
    # hardware panel shows (`app/hosts.py`). Linux-only facts; another OS leaves both unknown.
    virtualization: Optional[str] = None
    kvm: Optional[bool] = None
    # The hypervisor MicroVMs run on here (`node_platform.HYPERVISOR_VALUES`) and, off Linux, the first unmet
    # MicroVM prerequisite (`microvm_mode.PREREQ_SENTENCES`'s codes). `kvm` stays the usable verdict on every OS.
    hypervisor: Optional[str] = None
    microvm_prereq: Optional[str] = None
    # Which `launch.kind`s jaahd enacts (`apps/jaahd/agent_process.py::launch_kinds`), narrowed to the kinds
    # this CP knows; `agent_resolution.sdk_capable_nodes` places an SDK agent run only where "agent" is listed
    # (and a manifest 2 one only where "agent_acp" is too).
    launch_kinds: Optional[list[str]] = None
    # A Linux node's release, `/etc/os-release`'s `{"id": ID, "version_id": VERSION_ID}`, and its probed host
    # features (`{feature: bool}`, `node_platform.FEATURE_VALUES`; an absent key = not probed). Another OS sends
    # neither.
    distro: Optional[dict[str, str]] = None
    features: Optional[dict[str, bool]] = None


#: The `launch.kind` vocabulary this CP believes (`fleet_verdicts`); a kind a newer node reports is dropped.
_LAUNCH_KINDS = frozenset({fleet_verdicts.LAUNCH_KIND_AGENT, fleet_verdicts.LAUNCH_KIND_AGENT_ACP})

#: `systemd-detect-virt`'s vocabulary is open-ended (`kvm`, `amazon`, `vmware`, `docker`…), so it is bounded
#: by shape rather than listed: one lowercase token, never display-hostile text.
_VIRTUALIZATION = re.compile(r"[a-z0-9_-]{1,32}")

#: `os_version` is display text; a bound keeps a hostile node from parking a megabyte on its row.
_OS_VERSION_MAX = 128
# Printable ASCII only: it is rendered as display text, so no control, ANSI or bidi characters.
_OS_VERSION = re.compile(r"[\x20-\x7E]{1,%d}" % _OS_VERSION_MAX)
#: `/etc/os-release` `ID` is a lowercase token by its spec; `VERSION_ID` is display text, bounded and printable.
_DISTRO_ID = re.compile(r"[a-z0-9._-]{1,32}")
_DISTRO_VERSION = re.compile(r"[\x20-\x7E]{1,32}")


def _believable_distro(node: str, raw: Any) -> Optional[dict[str, str]]:
    """`platform.distro` when its `id` is an os-release token and its `version_id` (optional) printable text, else
    None — dropped whole and logged, so the node's release stays unknown rather than wrong."""
    if isinstance(raw, dict):
        distro_id, version = raw.get("id"), raw.get("version_id")
        if (isinstance(distro_id, str) and _DISTRO_ID.fullmatch(distro_id)
                and (version is None or (isinstance(version, str) and _DISTRO_VERSION.fullmatch(version)))):
            return {"id": distro_id, **({"version_id": version} if version is not None else {})}
    log.warning("node %r reported platform.distro=%r, which this control plane does not accept — dropping that "
                "field (it stays unknown)", node, str(raw)[:64])
    return None


def _believable_features(node: str, raw: Any) -> Optional[dict[str, bool]]:
    """`platform.features` narrowed to `node_platform.FEATURE_VALUES` keys with bool values; each other entry is
    dropped and logged (that feature stays unprobed), a non-object dropped whole."""
    if not isinstance(raw, dict):
        log.warning("node %r reported platform.features=%r, which is not an object — dropping that field",
                    node, str(raw)[:64])
        return None
    kept = {k: v for k, v in raw.items() if k in node_platform.FEATURE_VALUES and isinstance(v, bool)}
    if len(kept) < len(raw):
        log.warning("node %r reported platform.features keys %r this control plane does not accept — dropping them",
                    node, sorted(str(k)[:32] for k in raw if k not in kept)[:8])
    return kept


def _a_platform_block_this_cp_cannot_believe_is_partly_unknown(data: dict[str, Any]) -> dict[str, Any]:
    """Narrow `platform` to what this CP can believe, before pydantic can 422 the pass over it.

    Like `runner_clis`, the block may cost only itself: a non-object is dropped whole, and each field
    outside its vocabulary (`node_platform.REPORTED_OS_VALUES`, `ARCH_VALUES`, a bounded string) is dropped
    and logged, leaving that fact unknown rather than wrong."""
    raw = data.get("platform")
    if raw is None or isinstance(raw, PlatformIn):
        return data
    node = str(data.get("node_id") or "<unnamed>")
    if not isinstance(raw, dict):
        log.warning("node %r sent a platform block that is not an object (%s) — dropping it", node,
                    type(raw).__name__)
        return {**data, "platform": None}
    kept: dict[str, Any] = {}
    # The type is checked before the vocabulary, and strictly: pydantic would coerce `"yes"` or `1` into a
    # `kvm` bool, which is a guess, not a report.
    for key, kind, ok in (
        ("os", str, lambda v: v in node_platform.REPORTED_OS_VALUES),
        ("arch", str, lambda v: v in node_platform.ARCH_VALUES),
        ("os_version", str, lambda v: _OS_VERSION.fullmatch(v) is not None),
        ("virtualization", str, lambda v: _VIRTUALIZATION.fullmatch(v) is not None),
        ("kvm", bool, lambda v: True),
        ("hypervisor", str, lambda v: v in node_platform.HYPERVISOR_VALUES),
        # The community edition has no MicroVMs: the key is not read there.
        *((("microvm_prereq", str, lambda v: v in microvm_mode.PREREQ_SENTENCES),)
          if edition_features.enabled("microvm") else ()),
    ):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, kind) and ok(value):
            kept[key] = value
        else:
            log.warning("node %r reported platform.%s=%r, which this control plane does not accept — "
                        "dropping that field (it stays unknown)", node, key, str(value)[:64])
    for key, believe in (("distro", _believable_distro), ("features", _believable_features)):
        if raw.get(key) is not None:
            value = believe(node, raw[key])
            if value is not None:
                kept[key] = value
    kinds = raw.get("launch_kinds")
    if isinstance(kinds, list):
        kept["launch_kinds"] = sorted({k for k in kinds if isinstance(k, str) and k in _LAUNCH_KINDS})
    elif kinds is not None:
        log.warning("node %r reported platform.launch_kinds=%r, which is not a list — dropping that field",
                    node, str(kinds)[:64])
    return {**data, "platform": kept}


def _implausible_host_metrics(block: HostMetricsIn) -> Optional[str]:
    """Why this block cannot be believed, as a phrase for the log — or None when it can.

    Pure and total. The refusals: a percentage outside 0..100 or non-finite (breaks every bar beside
    it); `window_secs <= 0`; `samples <= 0`; `samples > _INT32_MAX` (would raise `22003` and roll back the
    whole pass). The node clamps too, but it is untrusted input. `cpu_count`/`mem_total_kb` are not judged
    here: the validator nulls a bad label rather than dropping the reading."""
    for name, pct in (("cpu_pct", block.cpu_pct), ("mem_pct", block.mem_pct)):
        if not math.isfinite(pct):
            return f"{name}={pct!r} is not a finite number"
        if pct < 0.0 or pct > 100.0:
            return f"{name}={pct!r} is outside 0..100"
    if not math.isfinite(block.window_secs) or block.window_secs <= 0.0:
        return f"window_secs={block.window_secs!r} claims a mean over no elapsed time"
    if block.samples <= 0:
        return f"samples={block.samples!r} claims a mean over no samples"
    if block.samples > _INT32_MAX:
        return f"samples={block.samples!r} is larger than the column can hold"
    return None


class ObserveAccountIn(BaseModel):
    """One node's observation of one credential account.

    `extra` is a deliberate pass-through bag, so new identity fields reach the page automatically."""

    slug: str
    config_dir: Optional[str] = None
    enabled: Optional[bool] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    # Sent only from a first-hand probe this pass; `None` means "no reading from me", never "fine" or
    # "clear". A block asserting a percentage must carry `observedAt` and replaces what is stored; one
    # asserting none merges (`_readings_the_cp_cannot_date_are_no_reading`, `account_upsert_statement`).
    quota: Optional[dict[str, Any]] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ObservationIn(BaseModel):
    """One pass's whole observation. The three completeness flags have no defaults (module docstring)."""

    node_id: str
    # One node-clock reading for the whole pass, so every row shares a stamp.
    observed_at: datetime
    # false ⇒ SKIP the worktrees DELETE entirely.
    worktrees_complete: bool
    # = the node's own `agents_ok`; false ⇒ skip the runners DELETE.
    runners_complete: bool
    # = roster_ok; false ⇒ skip the accounts DELETE.
    accounts_complete: bool
    worktrees: list[ObserveSlotIn] = Field(default_factory=list)
    runners: list[ObserveRunnerIn] = Field(default_factory=list)
    accounts: list[ObserveAccountIn] = Field(default_factory=list)
    # Projects this pass `git fetch`ed — the write half of the fetch stamp. By id; the names are an older
    # node's spelling, resolved inside its org (`resolve_project_ids`).
    fetched_project_ids: list[int] = Field(default_factory=list)
    fetched_projects: list[str] = Field(default_factory=list)
    # This node's CPU/RAM mean, the one optional top-level block (`app/node_host_metrics.py`).
    # Defaults to None for backward compatibility (nodes update independently) and because a node may
    # omit it. A pass without a block writes nothing for it: silence is not idleness.
    host_metrics: Optional[HostMetricsIn] = None
    # Which runner CLI binaries uid `jaah` has on this host, sensed by the node
    # (`app/models/nodes.py::Node.runner_clis` defines NULL vs `[]` vs absent). Defaults to None for
    # `host_metrics`' reason; the validators extend that from absence to malformedness.
    runner_clis: Optional[list[str]] = None
    # The node's daemon senses ACP adapters in `runner_clis` (`app/models/nodes.py::Node.senses_acp_adapters`).
    # Read only beside `runner_clis`: a list without it (an older daemon) stamps False.
    senses_acp_adapters: Optional[bool] = None
    # What the machine says it runs (`PlatformIn`). Absent = unknown: nothing on the row is touched.
    platform: Optional[PlatformIn] = None
    # The size of the node's root filesystem in kB, which ends a pending disk grow (`cloud_lifecycle.root_grow_wanted`).
    # Absent from an older daemon and a virtual node: nothing is cleared.
    root_total_kb: Optional[int] = Field(default=None, ge=0)
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _a_host_metrics_block_this_cp_cannot_parse_is_no_reading(cls, data: Any) -> Any:
        """Drop a `host_metrics` block that is not even the right shape, before pydantic can 422 the pass.

        The structural half of the rule; it must run `mode="before"`, since a bad shape raises before an
        `after` validator runs. The sole exception to "malformed refuses the pass", because its loss costs
        nothing. On the model, not the field, so the warning can name the node; `runner_clis` is handled
        here too (two `before` validators have no defined order)."""
        if not isinstance(data, dict):
            return data
        data = _a_runner_cli_list_this_cp_cannot_parse_is_no_attestation(data)
        data = _a_platform_block_this_cp_cannot_believe_is_partly_unknown(data)
        if data.get("host_metrics") is None:
            return data
        raw = data["host_metrics"]
        # Already parsed (in-process construction): the `after` sibling still checks values.
        if isinstance(raw, HostMetricsIn):
            return data
        # A junk-key block would skip the narrowing below and carry unchecked optional fields to the
        # ingest (an out-of-range epoch raises there and rolls back the pass), so it is dropped whole.
        if isinstance(raw, dict) and len(raw) > _BLOCK_KEY_MAX:
            _warn_implausible_host_metrics(
                str(data.get("node_id") or "<unnamed>"),
                f"the block has {len(raw)} keys (> {_BLOCK_KEY_MAX})",
            )
            return {**data, "host_metrics": None}
        # Narrowed first, so a bad optional key costs only itself.
        narrowed, dropped = _the_optional_fields_cost_themselves_and_not_the_reading(raw)
        # A cleaned list is a new block even when the node sent nothing wrong (stripped strings, float casts).
        if narrowed is not raw:
            data = {**data, "host_metrics": narrowed}
            raw = narrowed
        if dropped:
            _warn_implausible_host_metrics(
                str(data.get("node_id") or "<unnamed>"),
                # Field names, not a count, so the operator reads one field instead of four.
                f"unusable optional field(s) {sorted(dropped)}; dropping those only",
                seen=_optional_narrowed_warned,
            )
        try:
            HostMetricsIn.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError, or a TypeError off a non-mapping
            # A dict copy, never a mutation of the caller's body.
            data = {**data, "host_metrics": None}
            _warn_implausible_host_metrics(
                str(data.get("node_id") or "<unnamed>"),
                f"the block is not the expected shape ({type(exc).__name__})",
            )
        return data

    @model_validator(mode="after")
    def _an_implausible_host_metrics_block_is_no_reading(self) -> "ObservationIn":
        """Enforce the plausibility floor on a well-shaped block — the value half of the rule above.

        Never 422s the pass: the verdict is `self.host_metrics = None` plus one rate-limited warning
        naming the node. The CP checks because the node's clamp is a courtesy; `_implausible_host_metrics`
        carries the refusals.

        The two static labels are nulled, not refused, when non-positive or above their column ceiling:
        a bad label must not cost the measurement. A nulled `cpu_count` reads as one core for placement
        (pessimistic); the placement ceiling is `app/node_host_metrics.py::MAX_PLACEMENT_CORES`."""
        block = self.host_metrics
        if block is None:
            return self
        problem = _implausible_host_metrics(block)
        if problem is not None:
            self.host_metrics = None
            _warn_implausible_host_metrics(self.node_id, problem)
            return self
        if block.cpu_count is not None and not 0 < block.cpu_count <= _INT32_MAX:
            block.cpu_count = None
        if block.mem_total_kb is not None and not 0 < block.mem_total_kb <= _INT64_MAX:
            block.mem_total_kb = None
        # The disk pair is nulled or kept together: "free" is meaningless without "total".
        # `disk_free_kb` may be zero (a full disk is a real reading); `disk_total_kb` must be positive.
        free, total = block.disk_free_kb, block.disk_total_kb
        if free is not None or total is not None:
            unusable = (
                free is None or total is None
                or free < 0 or free > _INT64_MAX
                or not 0 < total <= _INT64_MAX
                or free > total
            )
            if unusable:
                block.disk_free_kb = None
                block.disk_total_kb = None
        # The /tmp inode pair, the same rule: zero free is a full table, a zero total is no table.
        free, total = block.tmp_inodes_free, block.tmp_inodes_total
        if free is not None or total is not None:
            unusable = (
                free is None or total is None
                or free < 0
                or not 0 < total <= _INT64_MAX
                or free > total
            )
            if unusable:
                block.tmp_inodes_free = None
                block.tmp_inodes_total = None
        # The swap pair, the same rule, except a zero total is a real reading (no swap configured).
        free, total = block.swap_free_kb, block.swap_total_kb
        if (free is None) != (total is None) or (free is not None and total is not None and free > total):
            block.swap_free_kb = None
            block.swap_total_kb = None
        return self

    @model_validator(mode="after")
    def _readings_the_cp_cannot_date_are_no_reading(self) -> "ObservationIn":
        """Enforce the dating half of the ingest rule, per account, at the boundary.

        Asserts a percentage with an unusable stamp -> the whole block becomes `None` (cannot be aged).
        Asserts none with an unusable stamp -> keep the block, drop only `observedAt` (else the previous
        reading looks fresh). No stamp and no percentage -> untouched. Unusable means missing, malformed,
        or beyond `_MAX_STAMP_SKEW_SECS` in the future. Never 422s the pass; a `model_validator` so the
        warning is rate-limited per (node_id, slug)."""
        now = datetime.now(timezone.utc)
        for account in self.accounts:
            block = account.quota
            if block is None:
                continue
            stamp = reading_observed_at(block)
            if stamp is not None and (stamp - now).total_seconds() <= _MAX_STAMP_SKEW_SECS:
                continue
            reason = (
                "stamp is %.0fs in the future, beyond the %.0fs clock-skew allowance"
                % ((stamp - now).total_seconds(), _MAX_STAMP_SKEW_SECS)
                if stamp is not None
                else "no parseable observedAt"
            )
            if asserts_a_percentage(block):
                account.quota = None
                _warn_undatable_quota(
                    self.node_id, account.slug, reason=reason, raw=block.get("observedAt"),
                    action="dropping the block: it asserts percentages nothing can age",
                )
            elif "observedAt" in block:
                account.quota = {k: v for k, v in block.items() if k != "observedAt"}
                _warn_undatable_quota(
                    self.node_id, account.slug, reason=reason, raw=block.get("observedAt"),
                    action="keeping the verdict and dropping only its stamp — a verdict needs no date",
                )
        return self

    @model_validator(mode="after")
    def _a_pass_cannot_be_stamped_in_the_future(self) -> "ObservationIn":
        """Clamp a forward-skewed pass stamp to the CP's own clock — the pass-level twin of the quota
        block's `_MAX_STAMP_SKEW_SECS` bound.

        A future stamp reads as permanently fresh. Clamped rather than 422 or nulled (`pool_state` reads
        NULL as "no complete enumeration"). Backward skew is not clamped: it only makes a reading look
        older, the safe direction (dispatch freshness reads the CP-stamped `attested_at`)."""
        now = datetime.now(timezone.utc)
        stamped = (
            self.observed_at
            if self.observed_at.tzinfo
            else self.observed_at.replace(tzinfo=timezone.utc)
        )
        if (stamped - now).total_seconds() > _MAX_STAMP_SKEW_SECS:
            _warn_skewed_pass(self.node_id, (stamped - now).total_seconds())
            self.observed_at = now
        else:
            self.observed_at = stamped
        return self


class ObserveFamilyResult(BaseModel):
    """How many rows one family's write touched, and how many its complement removed.

    `written`, not `upserted`: worktrees are reached by a plain UPDATE (`slot_update_statement`), not an
    upsert. The node discards the response, so the audience is a human reading it."""

    written: int
    deleted: int


class ObservationOut(BaseModel):
    worktrees: ObserveFamilyResult
    runners: ObserveFamilyResult
    accounts: ObserveFamilyResult
    # Drives one console event per pass. `(display name, number)` for readability; the gate compares on
    # `project_id`.
    changed_slots: list[tuple[str, int]]
    # The `runners` twin; the families move independently.
    changed_runners: list[int] = []
    # The `accounts` twin (slugs); the probe runs at 600s, the pass at ~5s.
    changed_accounts: list[str] = []
    # The unattended task-held subset of this pass's `gone` report (`{"project", "number", "task_id"}`),
    # read inside the retryable transaction so the route alerts on it only after commit
    # (`routes/agent/fleet_observe.py::fleet_observe_route` → `emit_vanished_hold_alerts`). The row survives,
    # but the work in the tree is gone.
    vanished_task_holds: list[dict[str, Any]] = []
    # Task ids whose runner just disappeared (session true → not-true while task-held), visible only
    # inside the transaction. The route wakes `task-pipeline` after commit (`app/task_wake.py`), since a
    # dying runner is an observation change, not a task transition.
    runner_lost_task_ids: list[int] = []
    # The disk percentage at which the node reclaims on its own, served so the node holds no second copy of
    # the thresholds (`app/node_host_metrics.py` is their one home).
    disk_warn_pct: float = node_host_metrics.WARN_PCT
    # Grow the root filesystem into its disk (a Your Cloud disk grown, `app/cloud_lifecycle.py`), until the node reports
    # it grown (`root_total_kb`) or `cloud_lifecycle.ROOT_GROW_GIVE_UP` passes (the console then shows it failed).
    root_grow: bool = False
    # This pass's disk-gate decision, for the route's post-commit effects (`node_disk_events.after_commit`).
    # Private: never on the wire.
    _disk_transition: Optional[node_disk_events.GateTransition] = PrivateAttr(default=None)
    # This pass's /tmp inode alarm crossing, the same way (`node_tmp_inode_alarm.after_commit`).
    _tmp_inode_crossing: Optional[node_tmp_inode_alarm.Crossing] = PrivateAttr(default=None)
    # This pass's project resolution, for the route's post-commit sighting pass: one resolution per request,
    # not two. None when the pass resolved nothing (an unregistered reporter), so the route resolves itself.
    _project_ids: Optional[dict[Any, int]] = PrivateAttr(default=None)
    # The observing node's org, read off the stamp, for the route's console events. None as above.
    _node_org_id: Optional[UUID] = PrivateAttr(default=None)


# ── The ingest boundary for the project's identity ─────────────────────────────────────────────────────


# A project as one pass reports it: its `projects.id` when the node sent one, else (an older node) its name.
ProjectKey = Union[int, str]


def project_key(slot: ObserveSlotIn) -> ProjectKey:
    """The key `resolve_project_ids`' map holds this slot's project under: the id when sent, else the name."""
    return slot.project_id if slot.project_id is not None else slot.project


def resolve_project_ids(session, payload: ObservationIn) -> dict[ProjectKey, int]:
    """`{project key: projects.id}` for every distinct project this pass names — one resolution per pass.

    The identity boundary of the whole ingest: past here every key is `project_id`. A reported id
    stands only for a project of the node's own org or one it runs a carried-over slot of
    (`app/carryover.py`); an older node's bare name resolves inside the node's org
    (`project_identity.legacy_project_id`, C6) — names are unique per org only — unless a carried-over slot's
    project answers to it, which wins (the node's org may since have reused the name). Anything else (e.g. a
    rename in flight, or an id of another org) is absent from the map and its rows are skipped, fail-closed and
    self-healing; one warning per such key per pass."""
    org_id = session.scalar(select(Node.org_id).where(Node.id == node_id_or_none(session, payload.node_id)))
    return _resolve_project_ids(session, payload, org_id)


def _resolve_project_ids(session, payload: ObservationIn, org_id: Optional[UUID]) -> dict[ProjectKey, int]:
    """`resolve_project_ids` for a caller that has already read the node's org (`apply_observation`, off its
    stamp) — the same resolution without re-reading it."""
    # No truthiness filter: `""` must resolve to nothing and get the warning, not skip silently.
    keys: set[ProjectKey] = {project_key(slot) for slot in payload.worktrees}
    keys |= set(payload.fetched_project_ids)
    keys |= set(payload.fetched_projects)
    resolved: dict[ProjectKey, int] = {}
    # A carried-over slot's project moved out of this node's org (`app/carryover.py`): resolved by the carry-over.
    carried = carryover.carried_project_ids(session, payload.node_id)
    reported_ids = {key for key in keys if isinstance(key, int)}
    own_ids = set(session.scalars(
        select(Project.id).where(Project.id.in_(reported_ids), Project.org_id == org_id)
    ).all()) if reported_ids and org_id is not None else set()
    own_ids |= reported_ids & set(carried)
    carried_by_name: dict[str, list[int]] = {}
    for carried_id, carried_name in carried.items():
        carried_by_name.setdefault(carried_name, []).append(carried_id)
    for name in sorted(keys, key=str):
        if isinstance(name, int):
            project_id = name if name in own_ids else None
        elif name in carried_by_name:
            # The carry-over first: once its project leaves, the node's org may create a new project of the same
            # name, and the older daemon's report of the carried slot must not land on it. Two carried projects
            # of one name cannot be told apart by name: refused.
            matches = carried_by_name[name]
            project_id = matches[0] if len(matches) == 1 else None
        else:
            project_id = project_identity.legacy_project_id(session, name, org_id)
        if project_id is None:
            log.warning(
                "fleet-observe: %s reported project %r, which no `projects` row answers to — every row "
                "naming it is SKIPPED this pass (slot update, `gone` read, fetch stamp). "
                "A rename in flight looks exactly like this and clears itself on the node's next roster; "
                "anything else means the project was deleted while a node still holds its slots.",
                payload.node_id, name,
            )
            continue
        resolved[name] = project_id
    return resolved


# ── Statement builders (R-W1: Core statements with an explicit column list, and nothing else) ──────────


def gone_slot_keys(payload: ObservationIn, project_ids: dict[ProjectKey, int]) -> list[tuple[int, int]]:
    """The slots this pass positively observed absent, ordered by the full natural key (L1).

    Feeds the vanished-hold read; the row stands. A target list,
    never a complement, so silence (or a slot declared after the tick's snapshot) acts on nothing. An
    unresolved project name drops out."""
    return sorted(
        (project_ids[project_key(slot)], slot.number)
        for slot in payload.worktrees
        if slot.state == SLOT_GONE and project_key(slot) in project_ids
    )


# The oldest transcript age read as a reading; past it (or negative) the value is dropped, never a 422 of the pass: a
# negative age would stamp the future and blind the stall detector, a huge one overflows `timedelta`.
_MAX_TRANSCRIPT_AGE_SECS = 400 * 86400


def _transcript_stamp(received: datetime, age: Optional[int]) -> Optional[datetime]:
    """`worktrees.transcript_written_at` from the node's age, or None (keep the stored stamp) for no valid reading."""
    if age is None or not 0 <= age <= _MAX_TRANSCRIPT_AGE_SECS:
        return None
    return received - timedelta(seconds=age)


def slot_update_rows(
    session, payload: ObservationIn, project_ids: dict[ProjectKey, int], node_uuid: UUID,
) -> list[dict[str, Any]]:
    """The observation UPDATE's values, ordered by the full natural key (L1).

    A row here is a candidate, not a write: a slot with no `worktrees` row is dropped by
    `apply_observation` (against `_stored_slot_shapes`) and logged. Only `present` slots are written;
    every other state has all-None sensed fields, and writing them would blank a live session and let
    the cap over-dispatch. An unobserved slot's `observed_at` therefore does not advance. Unresolved
    projects and pathless slots are skipped.

    `node_uuid` arrives resolved and credential dirs are batch-resolved onto `worktrees.account_id`
    (no per-row work in the 15s sweep, R-5)."""
    # One lookup for the payload, keyed on the node's own spelling.
    account_ids = node_clone.account_ids_for_dirs(session, [slot.account for slot in payload.worktrees])
    # The CP's clock: ages are subtracted only here, never from the node's own stamp.
    received = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for slot in payload.worktrees:
        if project_key(slot) not in project_ids:
            continue
        if not slot.path:
            log.warning(
                "fleet-observe: %s %s.%s reported %s with no path — skipping (a pathless row is "
                "unusable downstream)", payload.node_id, slot.project, slot.number, slot.state,
            )
            continue
        # A positive test against `present`, so a new state can never blank a live row.
        if slot.state != SLOT_PRESENT:
            continue
        rows.append(
            {
                "node_id": node_uuid,
                "project_id": project_ids[project_key(slot)],
                "number": slot.number,
                "path": slot.path,
                "branch": slot.branch,
                "dirty": slot.dirty,
                "merged": slot.merged,
                "ahead": slot.ahead,
                "behind": slot.behind,
                "head_sha": slot.head_sha,
                "session": slot.session,
                "session_id": slot.session_id,
                "session_status": slot.session_status,
                "session_started_at": slot.session_started_at,
                "session_pid": slot.session_pid,
                "session_proc_started_at": slot.session_proc_started_at,
                # Fails open: an unknown dir lands NULL rather than failing the whole pass.
                # `resolve_auth_dir`'s fail-closed `""` is a dispatch rule; both polarities are deliberate.
                "account_id": account_ids.get(slot.account),
                "console": slot.console,
                "probes": slot.probes,
                "probed_config_dirs": slot.probed_config_dirs,
                "observed_at": payload.observed_at,
                "age_last_used_secs": slot.age_last_used_secs,
                "age_last_commit_secs": slot.age_last_commit_secs,
                "age_index_mtime_secs": slot.age_index_mtime_secs,
                # The one sensed column the node ships as an age and the CP stores as a stamp on its own clock;
                # None keeps the stored value (`slot_update_statement`).
                "transcript_written_at": _transcript_stamp(received, slot.age_transcript_write_secs),
                "tmux_window_id": slot.tmux_window_id,
                "tmux_target": slot.tmux_target,
                "window_name": slot.window_name,
                "compose_project_name": slot.compose_project_name,
                "stack_present": slot.stack_present,
                "stack_up": slot.stack_up,
                "ports": slot.ports_observed,
                "default_title": slot.default_title,
                "host_env": slot.host_env,
                "model": slot.model,
                "repos": slot.repos.model_dump() if slot.repos is not None else None,
                # No `name` key: it is a MUTATED column, and this statement names none.
            }
        )
    rows.sort(key=lambda row: (row["node_id"], row["project_id"], row["number"]))
    return rows


def sighted_slot_keys(payload: ObservationIn, project_ids: dict[ProjectKey, int]) -> list[tuple[int, int]]:
    """The `(project_id, number)` pairs this pass reported a live session in — `slot_state.mark_seen_live`'s
    input.

    `worktrees.last_seen_live` still has a reader (`app/task_pipeline_job.py`) and `OCCUPANCY` requires a
    writer. `slot.session is True` on the literal: `None` (unknown) and `False` must never count, since
    `mark_seen_live` only extends a hold's life."""
    return sorted(
        (project_ids[project_key(slot)], slot.number)
        for slot in payload.worktrees
        if slot.session is True and project_key(slot) in project_ids
    )


def mark_sighted_slots(session, node_id: str, payload: ObservationIn, project_ids: dict[ProjectKey, int]) -> int:
    """Advance `worktrees.last_seen_live` for every slot this pass sighted a live session in. Returns rows
    touched.

    Called post-commit in its own transaction, never inside `apply_observation`:
    `slot_state.mark_seen_live` commits internally, which would end the pass's single transaction and
    defeat its deadlock retry (L4). `mark_seen_live` is the column's only writer; this only shapes input."""
    pairs = sighted_slot_keys(payload, project_ids)
    if not pairs:
        return 0
    return slot_state.mark_seen_live(session, node_id, pairs)


# The sensed columns a NULL reading leaves alone rather than clears: an evidence stamp, never a current state.
_KEPT_WHEN_ABSENT = frozenset({"transcript_written_at"})


def slot_update_statement(node_uuid: UUID, rows: list[dict[str, Any]]):
    """``UPDATE worktrees SET <SENSED only> FROM (VALUES …) AS incoming`` — matched on the natural key.

    It cannot create a row: a slot exists because an operator declared it. Rows minted by an older
    upsert are attributed to the system actor and cannot be migrated (no one knows who would have
    declared them); `git_access.resolve_for_worktree_clone` keeps refusing them until re-declared.

    One multi-row statement per pass (R-5), joined on `(project_id, number)` under this node. The SET
    list is `slot_columns.SENSED_UPDATABLE` (test-asserted), disjoint from DECIDED and `OCCUPANCY` by
    construction (`tests/test_slot_two_writers.py`). `ORDER BY … FOR UPDATE` in a subquery imposes L1,
    since a join is planner-ordered. Every `incoming` column is cast to its column's type (from
    `Worktree.__table__`): an all-NULL `VALUES` column infers as `text`."""
    table = Worktree.__table__
    sensed = sorted(slot_columns.SENSED_UPDATABLE)
    # The natural key rides the VALUES for the join; `SENSED_UPDATABLE` excludes `IDENTITY` from SET.
    carried = ("project_id", "number", *sensed)
    incoming = values(
        *[column(name, table.c[name].type) for name in carried], name="incoming"
    ).data([tuple(row[name] for name in carried) for row in rows])

    def sent(name: str):
        return cast(incoming.c[name], table.c[name].type)

    def setting(name: str):
        # A pass with no transcript reading (an old node, nothing resolved) keeps the last one.
        return func.coalesce(sent(name), table.c[name]) if name in _KEPT_WHEN_ABSENT else sent(name)

    locked = (
        select(Worktree.id)
        .where(
            Worktree.node_id == node_uuid,
            tuple_(Worktree.project_id, Worktree.number).in_(
                sorted((row["project_id"], row["number"]) for row in rows)
            ),
        )
        .order_by(Worktree.node_id, Worktree.project_id, Worktree.number)
        .with_for_update()
    )
    return (
        update(Worktree)
        .where(
            Worktree.node_id == node_uuid,
            Worktree.id.in_(locked),
            Worktree.project_id == sent("project_id"),
            Worktree.number == sent("number"),
        )
        .values({name: setting(name) for name in sensed})
    )


def vanished_hold_statement(node_uuid: UUID, gone: list[tuple[int, int]]):
    """The slots the node reported `gone`, with their occupancy — a SELECT that replaced a DELETE.

    A sensor may not retract a human's declaration: the row stands and the
    node rebuilds the slot (`apps/jaahd/reconcile/orchestrate.py::_unmaterialised_candidates`). A `task`-held slot
    with no live session held unlanded work, so it earns an `alerts` row; one with a live session is a
    log line. `ORDER BY … FOR UPDATE` on the full natural key (L1); the Python tuples and `tuple_()`
    columns must move together or `.in_()` matches nothing.
    """
    return (
        select(Worktree.project_id, Worktree.number, Worktree.state, Worktree.task_id, Worktree.session)
        .where(
            Worktree.node_id == node_uuid,
            tuple_(Worktree.project_id, Worktree.number).in_(
                [(project_id, number) for project_id, number in gone]
            ),
        )
        .order_by(Worktree.node_id, Worktree.project_id, Worktree.number)
        .with_for_update()
    )


def _vanished_hold_alert_identity(entry: dict[str, Any]) -> str:
    """The per-slot identity in the event's `issue` field (like `fleet_verdicts.reap_notify_identity`),
    so `uq_alerts_dedup` does not collapse one pass's vanished holds into one alert per 300s window."""
    return f"{entry['project']}-{entry['number']}"


def build_vanished_hold_alert_event(node_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """The `alert_record.record_alert` kwargs for one unattended `task`-held row whose tree a node just
    reported gone — `fleet_verdicts.build_reap_notify_event`'s sibling. Kind `self-heal`.

    The row survives but the alert is about the lost work, which rebuilding does not recover."""
    identity = _vanished_hold_alert_identity(entry)
    task_id = entry.get("task_id")
    return {
        "kind": "self-heal",
        "issue": identity,
        "node_id": node_id,
        "task_id": task_id,
        "reason": (
            f"worktree slot {identity} was held for task {task_id} with no runner attached"
            + f" when node {node_id} positively reported its tree gone — the declaration and the hold "
            "survive and the slot will be rebuilt EMPTY, but any unlanded work that was in that tree is "
            "gone and there is nothing left to inspect"
        ),
    }


def emit_vanished_hold_alerts(node_id: str, vanished: list[dict[str, Any]]) -> int:
    """One durable `alerts` row per unattended `task`-held slot whose tree a `gone` report just found
    missing. Returns how many the store accepted. `fleet_verdicts.emit_reap_notifies`'s sibling.

    A fresh session per alert (`record_alert` commits). Best effort, after the observation committed.
    Dedup on the per-slot `issue` absorbs deadlock retries and a never-rebuildable slot re-reported
    every pass, one per window."""
    accepted = 0
    for entry in vanished:
        event = build_vanished_hold_alert_event(node_id, entry)
        session = SessionLocal()
        try:
            outcome = alert_record.record_alert(session, **event)
            accepted += 1
            if outcome.relay_failed:
                log.warning(
                    "fleet-observe: the vanished-task-hold alert for slot %s was NOT delivered "
                    "to any operator channel (relay failed; the dedup row was withdrawn so a retry is not "
                    "suppressed)",
                    event.get("issue"),
                )
        # `Exception`, not `BaseException`: the suite's alert boundary is a `BaseException`, and must
        # not be swallowed (see `emit_reap_notifies`).
        except Exception as exc:  # noqa: BLE001 — an alert must never break the pass it describes
            log.warning(
                "fleet-observe: could not record the vanished-task-hold alert for slot %s: %s "
                "— the deletion itself already committed and is in the log line above it",
                event.get("issue"), exc,
            )
        finally:
            session.close()
    return accepted


# How far ahead of a Claude login's hard expiry the operator is told. The console's badge window
# (`apps/web/src/pages/credentials/Accounts.tsx`, `LOGIN_EXPIRY_WARN_MS`) is the same three days.
LOGIN_EXPIRY_WARN_SECONDS = 3 * 86_400
LOGIN_EXPIRY_ALERT_KIND = "login-expiring"


def _login_expiry(account: ObserveAccountIn) -> Optional[datetime]:
    """The account's reported refresh-token expiry (`extra.refresh_expires_at`, jaahd
    `collector._refresh_expires_iso`) as an aware UTC datetime, or None when absent or unparseable."""
    raw = (account.extra or {}).get("refresh_expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


# Per-process memo of `(org, issue)` → its expiry, for an alert this process has seen settled (a row exists or
# it just recorded one). Every node re-reports the same in-window login every pass (~5s), so without it each
# pass would re-probe `alerts`; entries drop once their expiry passes. A cache, never the dedup: a fresh
# process re-probes once and refills it.
_LOGIN_EXPIRY_SETTLED: dict[tuple[Any, str], datetime] = {}
# `(org, issue)` → when a relay-failed alert may be tried again: the failed row is withdrawn so a retry gets
# through, and this holds that retry to once per alert window rather than once per pass per node.
_LOGIN_EXPIRY_RETRY_AT: dict[tuple[Any, str], datetime] = {}


def emit_login_expiry_alerts(
    node_id: str, accounts: list[ObserveAccountIn], *, now: Optional[datetime] = None,
) -> int:
    """ONE alert per (account, login) whose refresh token dies within `LOGIN_EXPIRY_WARN_SECONDS`: no
    refresh extends that lifetime, so an unwarned account just stops (acct13/acct14, 2026-10). Returns how
    many were recorded.

    Once per expiry, not per window: `issue` names the slug and the expiry, and an existing row of that
    kind+issue in this org (any window) skips it; `uq_alerts_dedup` covers two nodes racing one window. A
    re-login moves the expiry, so the next one alerts afresh. Only a slug that is an ENABLED account of the
    node's own org is alerted: the body is node-supplied, so an invented slug must not reach the operator
    channels, and nothing runs on a disabled account. Best effort after the observation committed; never
    raises."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(seconds=LOGIN_EXPIRY_WARN_SECONDS)
    for memo in (_LOGIN_EXPIRY_SETTLED, _LOGIN_EXPIRY_RETRY_AT):
        for key in [k for k, at in memo.items() if at <= now]:
            del memo[key]
    due: dict[str, tuple[datetime, ObserveAccountIn]] = {}
    for account in accounts:
        expiry = _login_expiry(account)
        if expiry is not None and account.enabled is not False and now < expiry <= horizon:
            due[account.slug] = (expiry, account)
    if not due:
        return 0  # the steady state: no database touched
    recorded = 0
    session = SessionLocal()
    try:
        node_org = session.scalar(select(Node.org_id).where(Node.id == node_id_or_none(session, node_id)))
        if node_org is None:
            return 0  # an unregistered node: `record_alert` would refuse it too (no org)
        known = set(session.scalars(select(Account.slug).where(
            Account.org_id == node_org, Account.enabled.is_(True), Account.slug.in_(list(due)),
        )))
        session.rollback()  # `record_alert` needs a session with no open work
        for slug in sorted(known):
            expiry, account = due[slug]
            when = expiry.strftime("%Y-%m-%d %H:%M UTC")
            issue = f"{slug} login expires {when}"
            key = (node_org, issue)
            if key in _LOGIN_EXPIRY_SETTLED or key in _LOGIN_EXPIRY_RETRY_AT:
                continue
            try:
                # `repo = ''` leads `uq_alerts_dedup`, so this is an index probe. Org-scoped: slugs are per tenant.
                seen = session.scalar(select(Alert.id).where(
                    Alert.repo == "", Alert.issue == issue, Alert.kind == LOGIN_EXPIRY_ALERT_KIND,
                    Alert.org_id == node_org,
                ).limit(1))
                session.rollback()
                if seen is not None:
                    _LOGIN_EXPIRY_SETTLED[key] = expiry
                    continue
                # The org's Owners and Admins hear of an account's expiry once per warning horizon, not once per expiry
                # the node reports: the expiry is node-supplied, so a node could otherwise mint a fresh alert to
                # them every minute it names. The operator channel keeps the per-expiry alert.
                told_recently = session.scalar(select(Alert.id).where(
                    Alert.repo == "", Alert.kind == LOGIN_EXPIRY_ALERT_KIND, Alert.org_id == node_org,
                    Alert.issue.startswith(f"{slug} login expires ", autoescape=True),
                    Alert.created_at > now - timedelta(seconds=LOGIN_EXPIRY_WARN_SECONDS),
                ).limit(1)) is not None
                session.rollback()
                outcome = alert_record.record_alert(
                    session, kind=LOGIN_EXPIRY_ALERT_KIND, node_id=node_id, issue=issue,
                    reason=(
                        f"the Claude login for account {slug} expires {when} (seen on node {node_id}); "
                        "its runners stop then. No refresh extends it — re-login from Directory > Agents > Agents"
                    ),
                    # Not in `reason`: the relay redacts a bare domain there, so `a@x.com` would read
                    # `a@[link removed]`.
                    payload={"email": account.email} if account.email else None,
                    tell_org_admins=not told_recently,
                )
                if outcome.relay_failed:
                    _LOGIN_EXPIRY_RETRY_AT[key] = now + timedelta(seconds=alert_record.ALERT_WINDOW_SECONDS)
                    log.warning(
                        "fleet-observe: the login-expiry alert for %s was NOT delivered to any operator "
                        "channel (relay failed; retried once the alert window turns)", slug,
                    )
                    continue
                _LOGIN_EXPIRY_SETTLED[key] = expiry
                if not outcome.deduped:
                    recorded += 1
            # `Exception`, not `BaseException` — as `emit_vanished_hold_alerts`.
            except Exception as exc:  # noqa: BLE001 — an alert must never break the observation
                session.rollback()
                log.warning("fleet-observe: could not record the login-expiry alert for %s: %s", slug, exc)
    except Exception as exc:  # noqa: BLE001 — as above, for the org/account read
        log.warning("fleet-observe: login-expiry alerts skipped for node %s: %s", node_id, exc)
    finally:
        session.close()
    return recorded


def present_slot_keys(rows: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """The slots this pass actually wrote, ordered by the full natural key (L1) — the session-run stamp's
    scope.

    Derived from the sensed UPDATE's rows, not the payload, so an `unobserved` slot keeps its stamp."""
    return sorted((row["project_id"], row["number"]) for row in rows)


def _session_run_start():
    """`session_since`'s next value, as SQL over the row the sensed UPDATE already wrote this pass.

    `COALESCE` keeps a continuing run's original start. The `null()` arm covers false and unknown alike."""
    return case(
        (Worktree.session.is_(True), func.coalesce(Worktree.session_since, func.now())),
        else_=null(),
    )


def session_since_stamp_statement(node_uuid: UUID, keys: list[tuple[int, int]]):
    """``UPDATE worktrees SET session_since = <run start>`` for the slots this pass wrote.

    Separate and after the sensed UPDATE: the value depends on the stored row against the value just
    written. `IS DISTINCT FROM` limits writes to rows that change. `ORDER BY … FOR UPDATE` (L1)."""
    start = _session_run_start()
    inner = (
        select(Worktree.id)
        .where(
            Worktree.node_id == node_uuid,
            tuple_(Worktree.project_id, Worktree.number).in_(
                [(project_id, number) for project_id, number in keys]
            ),
            Worktree.session_since.is_distinct_from(start),
        )
        .order_by(Worktree.node_id, Worktree.project_id, Worktree.number)
        .with_for_update()
    )
    return (
        update(Worktree)
        .where(Worktree.node_id == node_uuid, Worktree.id.in_(inner))
        .values(session_since=start)
    )


def _node_org(node_uuid: UUID):
    """This node's `org_id`, as a subquery, for the three Core upserts below.

    `write_seam.stamp_org` only sees ORM instances, and these families land via `pg_insert` (see
    `fleet_verdicts._persist_intents`); each is `Derived(parent=Node)`. A subquery, not a resolved value:
    revision 0181's composite FK accepts only the parent's own org, so a read would be pure per-pass cost
    (R-5). It cannot be NULL (`nodes.org_id` is NOT NULL, node resolved first). On conflict it is
    COALESCEd, never overwritten: the org is immutable, and this repairs rows an older release left NULL."""
    return select(Node.org_id).where(Node.id == node_uuid).scalar_subquery()


def runner_upsert_statement(session, node_uuid: UUID, payload: ObservationIn):
    """``INSERT … ON CONFLICT (node_id, pid) DO UPDATE`` over the reported runner set.

    `proc_started_at`/`account_id` are overwritten, not carried forward: a NULL here is an older node or
    a failed `/proc` read, and carrying forward would pin a dead runner's identity onto a recycled pid."""
    # Batched as in `slot_update_rows`; same fail-open polarity for an unknown credential dir.
    account_ids = node_clone.account_ids_for_dirs(
        session, [runner.account_dir for runner in payload.runners]
    )
    rows = sorted(
        (
            {
                "node_id": node_uuid,
                # Named: a Core insert is invisible to the write seam (`_node_org`).
                "org_id": _node_org(node_uuid),
                "pid": runner.pid,
                "cwd": runner.cwd,
                "kind": runner.kind,
                "worktree_path": runner.worktree_path,
                "branch": runner.branch,
                "status": runner.status,
                "proc_started_at": runner.proc_started_at,
                "account_id": account_ids.get(runner.account_dir),
                "last_seen": payload.observed_at,
                "observed_at": payload.observed_at,
            }
            for runner in payload.runners
        ),
        key=lambda row: (row["node_id"], row["pid"]),
    )
    if not rows:
        return None
    insert = pg_insert(Runner).values(rows)
    return insert.on_conflict_do_update(
        constraint="uq_runners_node_pid",
        set_={
            **{
                name: insert.excluded[name]
                for name in (
                    "cwd", "kind", "worktree_path", "branch", "status",
                    "proc_started_at", "account_id", "last_seen", "observed_at",
                )
            },
            # COALESCEd rather than replaced, repairing only pre-contraction NULLs (`_node_org`).
            "org_id": func.coalesce(Runner.org_id, insert.excluded.org_id),
        },
    )


def runner_delete_statement(node_uuid: UUID, pids: list[int]):
    inner = (
        select(Runner.id)
        .where(Runner.node_id == node_uuid)
        .order_by(Runner.node_id, Runner.pid)
        .with_for_update()
    )
    if pids:
        inner = inner.where(Runner.pid.notin_(pids))
    return delete(Runner).where(Runner.node_id == node_uuid, Runner.id.in_(inner))


# `'{}'::jsonb` as a bound value: `::` collides with SQLAlchemy's bind-parameter parser.
_EMPTY_JSONB = cast(literal("{}"), JSONB)


def _asserts_a_percentage_sql(quota_column):
    """`account_fold.asserts_a_percentage` in SQL, over `QUOTA_PERCENT_KEYS`, for the upsert's quota arm.

    Exactly equivalent: `->> 'key' IS NOT NULL` is true iff present and not jsonb `null`. Keys are bound
    parameters, never inlined."""
    return or_(*[quota_column[key].astext.isnot(None) for key in QUOTA_PERCENT_KEYS])


def account_upsert_statement(session, node_uuid: UUID, payload: ObservationIn):
    """The account observation upsert, where the node's carry-forward floor becomes `COALESCE`.

    `enabled` is roster-owned and never COALESCEd (a roster disable must take effect); `extra` merges
    (`||`). `quota` has three branches:
      1. `excluded.quota IS NULL` -> keep the stored block (nearly every pass; never "fine" or "clear").
      2. incoming asserts a percentage -> replace; the only way a verdict can be cleared.
      3. otherwise -> `COALESCE(stored, '{}') || incoming`; without the COALESCE, `NULL || block` is NULL."""
    # Resolved from the slug, which an observation holds (two nodes spell one account's dir differently).
    # One query per pass.
    slugs = {account.slug for account in payload.accounts}
    ids_by_slug: dict[str, UUID] = {}
    if slugs:
        ids_by_slug = {
            slug: account_id
            for account_id, slug in session.execute(
                select(Account.id, Account.slug).where(Account.slug.in_(slugs))
            ).all()
        }
    rows = sorted(
        (
            {
                "node_id": node_uuid,
                # Named: a Core insert is invisible to the write seam (`_node_org`).
                "org_id": _node_org(node_uuid),
                "slug": account.slug,
                # Fails open like the slot and runner arms: refusing an unknown slug would stop
                # `attested_at` advancing for every account on the node.
                "account_id": ids_by_slug.get(account.slug),
                "config_dir": account.config_dir,
                "enabled": account.enabled,
                "email": account.email,
                "display_name": account.display_name,
                "quota": account.quota,
                "extra": account.extra,
                "observed_at": payload.observed_at,
            }
            for account in payload.accounts
        ),
        key=lambda row: (row["node_id"], row["slug"]),
    )
    if not rows:
        return None
    insert = pg_insert(AccountObservation).values(rows)
    excluded = insert.excluded
    table = AccountObservation
    return insert.on_conflict_do_update(
        constraint="uq_account_observations_node_slug",
        set_={
            # Must be in `set_`: a slug reported before the operator adds the account resolves NULL first.
            # Plain `excluded`, so a roster deletion reads back NULL (fails open both ways).
            "account_id": excluded.account_id,
            "config_dir": func.coalesce(excluded.config_dir, table.config_dir),
            "enabled": excluded.enabled,
            "email": func.coalesce(excluded.email, table.email),
            "display_name": func.coalesce(excluded.display_name, table.display_name),
            "quota": case(
                (excluded.quota.is_(None), table.quota),
                (_asserts_a_percentage_sql(excluded.quota), excluded.quota),
                else_=func.coalesce(table.quota, _EMPTY_JSONB).op("||")(excluded.quota),
            ),
            "extra": table.extra.op("||")(excluded.extra),
            # COALESCEd (`_node_org`); excluded from the change gate (moves once, rendered nowhere).
            "org_id": func.coalesce(table.org_id, excluded.org_id),
            "observed_at": excluded.observed_at,
            # CP-stamped, and must be in `set_` or it keeps its first-insert value; the dispatch freshness
            # window reads it, so a stalled stamp slowly stops the fleet.
            "attested_at": func.now(),
        },
    )


def account_delete_statement(node_uuid: UUID, slugs: list[str]):
    inner = (
        select(AccountObservation.id)
        .where(AccountObservation.node_id == node_uuid)
        .order_by(AccountObservation.node_id, AccountObservation.slug)
        .with_for_update()
    )
    if slugs:
        inner = inner.where(AccountObservation.slug.notin_(slugs))
    return delete(AccountObservation).where(
        AccountObservation.node_id == node_uuid, AccountObservation.id.in_(inner)
    )


def fetch_stamp_upsert_statement(
    node_uuid: UUID, payload: ObservationIn, project_ids: dict[ProjectKey, int]
):
    """``INSERT … ON CONFLICT (node_id, project_id) DO UPDATE`` — when this node last fetched each base.

    An unresolvable key is dropped (no key to write); the cost is one stale fetch age, which reads as
    "fetch it". The `constraint=` target is the primary key, whose name survived migration 0104."""
    rows = sorted(
        (
            {
                "node_id": node_uuid,
                # Named: a Core insert is invisible to the write seam (`_node_org`).
                "org_id": _node_org(node_uuid),
                "project_id": project_id,
                "fetched_at": payload.observed_at,
            }
            # A set of ids: an id and its legacy name in one pass are one stamp.
            for project_id in {
                project_ids[key] for key in (*payload.fetched_project_ids, *payload.fetched_projects)
                if key in project_ids
            }
        ),
        key=lambda row: (row["node_id"], row["project_id"]),
    )
    if not rows:
        return None
    insert = pg_insert(FetchStamp).values(rows)
    return insert.on_conflict_do_update(
        constraint="pk_fetch_stamps",
        set_={
            "fetched_at": insert.excluded.fetched_at,
            # COALESCEd (`_node_org`), and in the SET list because these rows are only ever updated after
            # the first insert.
            "org_id": func.coalesce(FetchStamp.org_id, insert.excluded.org_id),
        },
    )


def node_stamp_statement(node_uuid: UUID, payload: ObservationIn):
    """`nodes.observed_at` / `worktrees_updated_at` / `runners_updated_at` / `runner_clis` — the node
    row's write.

    Three clocks: `observed_at` (liveness) always advances; `worktrees_updated_at` and
    `runners_updated_at` (occupancy freshness) advance only on a complete enumeration, else a blind pass
    would look idle and over-dispatch. An incomplete enumeration leaves its column out of SET. `last_seen`
    is not written here (HTTP proves nothing about the bus). The freshness stamps are the CP's clock: the
    node stamps before its 10-12s probe loop, and a cross-clock difference fails open. `runner_clis` is
    omitted when the key is absent, so silence never erases an attestation."""
    # A plain UPDATE: `apply_observation` refuses unregistered reporters
    # (`node_identity.is_registered_node`), and a node deleted mid-pass stamps nothing.
    values: dict[str, Any] = {"observed_at": payload.observed_at}
    # Omitting the column keeps the last good stamp without binding a bare NULL into a COALESCE.
    if payload.worktrees_complete:
        values["worktrees_updated_at"] = func.now()
    if payload.runners_complete:
        values["runners_updated_at"] = func.now()
    # `runner_clis` is omitted on the same argument, gated on the payload key.
    # The adapter-sensing flag describes that list, so it is stamped with it: a list without the flag comes from a
    # daemon that does not sense adapters (an older or downgraded release).
    if payload.runner_clis is not None:
        values["runner_clis"] = payload.runner_clis
        values["senses_acp_adapters"] = bool(payload.senses_acp_adapters)
    # Each reported platform fact is stamped on its own; an absent one leaves the column alone. The family
    # lands on `os_reported`, never on the declared `os` (`node_platform.platform_blocker`).
    if payload.platform is not None:
        for field, column in (("os", "os_reported"), ("arch", "arch"), ("os_version", "os_version"),
                              ("virtualization", "virtualization"), ("kvm", "kvm"),
                              ("launch_kinds", "launch_kinds")):
            value = getattr(payload.platform, field)
            if value is not None:
                values[column] = value
        # The release rides as one fact: a pass reporting `distro` without a `version_id` has none. The features
        # map is replaced whole, so a probe that stops being sent is unprobed again rather than stale.
        if payload.platform.distro is not None:
            values["distro_id"] = payload.platform.distro["id"]
            values["distro_version"] = payload.platform.distro.get("version_id")
        if payload.platform.features is not None:
            values["host_features"] = payload.platform.features
        # The hypervisor and the prerequisite ride the `kvm` verdict they explain: a pass reporting `kvm` without
        # them has none, so a stale value clears rather than lingering.
        if payload.platform.kvm is not None:
            values["hypervisor"] = payload.platform.hypervisor
            values["microvm_prereq"] = payload.platform.microvm_prereq
    return update(Node).where(Node.id == node_uuid).values(**values)


# ── The transaction ───────────────────────────────────────────────────────────────────────────────────


def _runner_lost_task_ids(session, node_uuid: UUID, rows: list[dict[str, Any]]) -> list[int]:
    """Task ids whose slot is losing its live session this pass — read before the write, since the edge
    is a comparison against the stored row.

    The wake edge for a dead runner (a sensed change, not a task transition, so `app/task_wake.py` never
    sees it). Narrow: `state = 'task'`, a bound `task_id`, stored `session` literally TRUE going to
    not-true, so a steady dead slot never re-fires. Not-true includes unknown: the reclaimers carry the
    three-valued verdict (`task_pipeline_job.runner_verdict`) and hold on unknown."""
    if not rows:
        return []
    incoming = {(row["project_id"], row["number"]): row for row in rows}
    stored = session.execute(
        select(Worktree.project_id, Worktree.number, Worktree.task_id).where(
            Worktree.node_id == node_uuid,
            tuple_(Worktree.project_id, Worktree.number).in_(list(incoming)),
            Worktree.state == "task",
            Worktree.task_id.isnot(None),
            Worktree.session.is_(True),
        )
    ).all()
    return [
        row.task_id
        for row in stored
        if incoming.get((row.project_id, row.number), {}).get("session") is not True
    ]


def _stored_slot_shapes(session, node_uuid: UUID, rows: list[dict[str, Any]]):
    """`{(project_id, number): <shape columns>}` for the declared slots among the ones this pass reports.

    One read before the write: membership says whether a reported slot is declared (else dropped with a
    warning), and the shape columns feed `_changed_slots`. Exactly `_CHANGE_GATE_COLUMNS` plus the key;
    a superset would fire the gate every tick."""
    if not rows:
        return {}
    return {
        (row.project_id, row.number): row
        for row in session.execute(
            select(
                Worktree.project_id,
                Worktree.number,
                *[getattr(Worktree, name) for name in _CHANGE_GATE_COLUMNS],
            ).where(
                Worktree.node_id == node_uuid,
                tuple_(Worktree.project_id, Worktree.number).in_(
                    sorted((row["project_id"], row["number"]) for row in rows)
                ),
            )
        ).all()
    }


def _changed_slots(rows: list[dict[str, Any]], stored) -> list[tuple[int, int]]:
    """Which slots' shape moved this pass.

    Load-bearing: the node re-POSTs identical values every tick. A comparison rather than a `WHERE
    <shape changed>` on the write, which would freeze `observed_at` and the ages. `rows` is already the
    declared subset, so there is no "new slot" arm. Keyed on `(project_id, number)` so a rename cannot
    make every slot read as new."""
    return sorted(
        (row["project_id"], row["number"])
        for row in rows
        if any(
            getattr(stored[(row["project_id"], row["number"])], name) != row[name]
            for name in _CHANGE_GATE_COLUMNS
        )
    )


def _warn_undeclared_slots(
    node_id: str, names: dict[int, str], keys: list[tuple[int, int]]
) -> None:
    """One warning per undeclared slot per pass — a tree exists on the node that nobody declared.

    Not rate-limited: the node reports only slots from its roster (`routes/agent/pool_tick.py::build_pool_roster`),
    so this means genuine divergence and the steady state emits nothing. The slot is dropped; the rest of
    the pass is written."""
    for project_id, number in keys:
        log.warning(
            "fleet-observe: %s reported worktree %s#%s, which no declared slot answers to — SKIPPED, "
            "nothing was created. The observation senses slots, it never creates them: a slot exists "
            "because the project is assigned to the node (its static slots, or a dynamic one a lease made). "
            "Either assign the project to this node so the control plane owns it, or remove the directory "
            "from the node.",
            node_id, names.get(project_id, project_id), number,
        )


def _changed_runners(session, node_uuid: UUID, payload: ObservationIn) -> list[int]:
    """Which runners' shape moved this pass — the `runners` twin of `_changed_slots`.

    Returns the pids. A pid with no stored row is a runner appearing, so it counts as changed."""
    incoming = {runner.pid: runner for runner in payload.runners}
    if not incoming:
        return []
    # Resolve incoming dirs to stored ids before comparing (else every runner changes every pass). A
    # second batched lookup, since this runs before the write.
    account_ids = node_clone.account_ids_for_dirs(
        session, [runner.account_dir for runner in payload.runners]
    )
    stored = {
        row.pid: row
        for row in session.execute(
            select(
                Runner.pid,
                *[getattr(Runner, name) for name in _RUNNER_CHANGE_GATE_COLUMNS],
            ).where(Runner.node_id == node_uuid, Runner.pid.in_(list(incoming)))
        ).all()
    }
    changed: list[int] = []
    for pid, runner in incoming.items():
        prior = stored.get(pid)
        if prior is None:
            changed.append(pid)
            continue
        if any(
            getattr(prior, name)
            != (
                account_ids.get(runner.account_dir)
                if name == "account_id"
                else getattr(runner, name)
            )
            for name in _RUNNER_CHANGE_GATE_COLUMNS
        ):
            changed.append(pid)
    return sorted(changed)


def _changed_accounts(session, node_uuid: UUID, payload: ObservationIn) -> list[str]:
    """Which accounts' quota/identity moved this pass — the `accounts` twin of `_changed_slots`.

    Compares the effective post-write value, not the raw incoming one: the upsert COALESCEs three columns
    and branches `quota`, so a raw compare would fire every 5s. `extra` compares the merge. Exclusions are
    declared in `_ACCOUNT_GATE_EXCLUDED` and asserted against the compiled SET clause."""
    incoming = {account.slug: account for account in payload.accounts}
    if not incoming:
        return []
    stored = {
        row.slug: row
        for row in session.execute(
            select(
                AccountObservation.slug,
                AccountObservation.config_dir,
                AccountObservation.enabled,
                AccountObservation.email,
                AccountObservation.display_name,
                AccountObservation.quota,
                AccountObservation.extra,
            ).where(
                AccountObservation.node_id == node_uuid,
                AccountObservation.slug.in_(list(incoming)),
            )
        ).all()
    }
    changed: list[str] = []
    for slug, account in incoming.items():
        prior = stored.get(slug)
        if prior is None:
            changed.append(slug)
            continue
        # Mirror each arm of the upsert's `set_` exactly.
        coalesced = (
            (account.config_dir, prior.config_dir),
            (account.email, prior.email),
            (account.display_name, prior.display_name),
        )
        if any(
            (incoming_value if incoming_value is not None else stored_value) != stored_value
            for incoming_value, stored_value in coalesced
        ):
            changed.append(slug)
            continue
        if account.enabled != prior.enabled:
            changed.append(slug)
            continue
        # Mirrors the upsert's three-branch quota arm; SQL `||` is `{**stored, **incoming}`.
        if account.quota is None:
            effective_quota = prior.quota
        elif asserts_a_percentage(account.quota):
            effective_quota = account.quota
        else:
            effective_quota = {**(prior.quota or {}), **account.quota}
        if effective_quota != prior.quota:
            changed.append(slug)
            continue
        # `extra`'s rule is the merge (both operands are guaranteed objects). jsonb `||` never removes a
        # key, so a field the node stops sending keeps its value; fixing that needs a different write.
        stored_extra = prior.extra or {}
        if {**stored_extra, **(account.extra or {})} != stored_extra:
            changed.append(slug)
    return sorted(changed)


def _root_grow(session, node_uuid: UUID, infra: Optional[dict[str, Any]], root_total_kb: Optional[int]) -> bool:
    """Whether the node is still asked to grow its root (`cloud_lifecycle.root_grow_wanted`: bounded in time, decided on
    read, so nothing is written until a report shows it grown — which clears the ask, under the stamp's lock)."""
    if not edition_features.enabled("cloud_nodes"):
        return False  # The community edition has no cloud disk to grow.
    cleared = cloud_lifecycle.note_root_reported(infra, root_total_kb)
    if cleared is not None:
        session.execute(update(Node).where(Node.id == node_uuid).values(infra=cleared))
        return False
    return cloud_lifecycle.root_grow_wanted(infra)


def apply_observation(session, payload: ObservationIn) -> ObservationOut:
    """The whole pass, in one transaction. The caller commits (and retries per L4).

    Statement order follows L2 — `nodes → worktrees → runners → account_observations` — then the leaf
    `fetch_stamps`; `node_host_metrics` follows the `nodes` stamp. Every statement is `WHERE node_id =
    :node_uuid`. Project names are resolved once, up front; `names` is the inverse for display. Three
    builders take the session to batch-resolve `account_id`."""
    # `payload.node_id` is the node's name, for log lines only; statements key on `node_uuid`.
    node_id = payload.node_id
    # Registration gate and resolution are one lookup (R-5). An unregistered reporter is dropped whole,
    # or it would populate a pool and runner set feeding the dispatch cap
    # (`node_identity.is_registered_node`). The same read carries the platform columns the blocker log
    # below compares, read before the stamp overwrites `os_reported` — one round trip, not two.
    registered = session.execute(
        select(Node.id, Node.os, Node.os_reported).where(node_matches(node_id))
    ).first()
    node_uuid = registered.id if registered is not None else None
    if node_uuid is None:
        warn_unregistered_node(node_id, "the fleet observation")
        empty = ObserveFamilyResult(written=0, deleted=0)
        return ObservationOut(
            worktrees=empty, runners=empty, accounts=empty,
            changed_slots=[], changed_runners=[], changed_accounts=[], vanished_task_holds=[],
            runner_lost_task_ids=[],
        )
    # Logged once, when the reported family changes, not on every pass (the console shows the blocker).
    if payload.platform is not None and payload.platform.os is not None:
        if registered.os_reported != payload.platform.os:
            blocker = node_platform.platform_blocker(registered.os, payload.platform.os)
            if blocker is not None:
                log.warning("fleet-observe: %s — %s", node_id, blocker)
    # RETURNING the row's other facts this pass reads: the stamp holds the row's lock from here on, so these
    # are exactly what a read after it would see, without three more round trips. No row (the
    # node deleted mid-pass) reads as the reads did: no name, no gate, no org.
    stamped = session.execute(
        node_stamp_statement(node_uuid, payload).returning(
            Node.name, Node.disk_gate, Node.org_id, Node.infra
        )
    ).first()
    node_org_id = stamped.org_id if stamped is not None else None
    root_grow = _root_grow(session, node_uuid, stamped.infra if stamped is not None else None, payload.root_total_kb)

    # ── The host-metrics minute ───────────────────────────────────────────────────────────────────────
    # Only when the node sent a block: absence asserts nothing (`app/node_host_metrics.py`).
    #
    # Placed right after the `nodes` stamp for L2: its INSERT's `FOR KEY SHARE` on `nodes` is already
    # covered by the stamp's exclusive lock. Idempotent under the route's deadlock retry via
    # `ON CONFLICT (node_id, bucket_at)`. The prune rides the write (`prune_statement` says why).
    if payload.host_metrics is not None:
        # Read before the upsert overwrites it: the /tmp inode alarm fires on the crossing only.
        previous_tmp_inode_pct = node_tmp_inode_alarm.previous_pct(session, node_uuid)
        # One round trip for both: the prune rides the upsert as a data-modifying CTE, which Postgres runs to
        # completion whether or not anything reads it. The two touch disjoint rows (this minute vs. buckets
        # past retention), so sharing one snapshot changes neither.
        # The session minutes and peaks ride the same statement (`app/node_session_usage.py`): their prune on
        # every block, their upserts only when the block lists sessions. Disjoint tables, so one snapshot is safe.
        sessions = payload.host_metrics.sessions or []
        ctes = [
            node_host_metrics.prune_statement(node_uuid).returning(NodeHostMetric.id).cte("pruned"),
            node_session_usage.prune_statement(node_uuid).returning(NodeSessionUsage.id).cte("sessions_pruned"),
        ]
        # No org (the node deleted mid-pass) writes no session row: there is no org to write it under.
        session_upsert = None
        if node_org_id is not None:
            session_upsert = node_session_usage.upsert_statement(node_uuid, node_org_id, sessions)
        if session_upsert is not None and node_org_id is not None:
            ctes.append(session_upsert.returning(NodeSessionUsage.id).cte("sessions_written"))
            ctes.append(
                node_session_usage.peaks_statement(node_uuid, node_org_id, sessions)
                .returning(TaskSessionUsagePeak.id).cte("peaks_written")
            )
        session.execute(node_host_metrics.upsert_statement(node_uuid, payload.host_metrics).add_cte(*ctes))
        # The disk gate, from the reading just written (`app/node_disk_events.py`); a block with no disk
        # decides nothing.
        # The alert names the node by its display name; the wire carries only its uuid.
        node_name = (stamped.name if stamped is not None else None) or str(node_uuid)
        disk_transition = node_disk_events.apply_reading(
            session, node_uuid, node_name, payload.host_metrics,
            disk_gate=stamped.disk_gate if stamped is not None else None,
        )
        tmp_inode_crossing = node_tmp_inode_alarm.judge(
            node_uuid, node_name, previous_tmp_inode_pct, payload.host_metrics
        )
    else:
        disk_transition = None
        tmp_inode_crossing = None

    project_ids = _resolve_project_ids(session, payload, node_org_id)

    # The declaration gate: only declared slots are written; the partition rides `_stored_slot_shapes`'
    # single pre-write read.
    reported = slot_update_rows(session, payload, project_ids, node_uuid)
    stored = _stored_slot_shapes(session, node_uuid, reported)
    rows = [row for row in reported if (row["project_id"], row["number"]) in stored]
    undeclared = sorted(
        (row["project_id"], row["number"])
        for row in reported
        if (row["project_id"], row["number"]) not in stored
    )
    changed = _changed_slots(rows, stored)
    # Display only (logs, the vanished-hold alert, `changed_slots`): read back by id, never inverted from the
    # payload's names — and read only when something will be displayed. The steady pass (nothing undeclared,
    # changed or gone) displays nothing, so it skips the read.
    gone = gone_slot_keys(payload, project_ids)
    names = (
        project_identity.names_by_id(session, project_ids.values())
        if undeclared or changed or gone else {}
    )
    _warn_undeclared_slots(node_id, names, undeclared)
    # Before the write: the edge is read from the column the UPDATE is about to overwrite.
    runner_lost = _runner_lost_task_ids(session, node_uuid, rows)
    if rows:
        session.execute(slot_update_statement(node_uuid, rows))
        # After the sensed write: the stamp reads the `session` value just written.
        session.execute(session_since_stamp_statement(node_uuid, present_slot_keys(rows)))
    # `unmaterialised` and `gone` are one verdict: both leave the declared row for the node to rebuild.
    # Computed above (a pure read of the payload); acted on after the sensed UPDATE.
    vanished_task_holds: list[dict[str, Any]] = []
    if not payload.worktrees_complete:
        # An incomplete enumeration cannot be trusted about `gone` either; the next complete pass re-reports.
        if gone:
            log.info(
                "fleet-observe: %s reported worktrees_complete=false — ignoring its `gone` verdicts "
                "(the node could not enumerate its slots, so even a `gone` verdict in it is unreliable)",
                node_id,
            )
        gone = []
    if gone:
        # Under the pass's lock: the alert is about the occupancy this pass found.
        vanished_rows = session.execute(vanished_hold_statement(node_uuid, gone)).all()
        # An unattended task-held slot held unlanded work, so a vanished tree earns a durable `alerts`
        # row. Emitted post-commit by the route (`emit_vanished_hold_alerts`), since a deadlock retry
        # could undo this pass.
        vanished_task_holds = [
            {
                "project_id": project_id,
                "project": names.get(project_id, str(project_id)),
                "number": number,
                "task_id": task_id,
            }
            for project_id, number, state, task_id, has_session in vanished_rows
            if state == slot_state.TASK and not has_session
        ]
        # An audit line naming the slots (`worktrees` has no history; the SSE event is transient).
        log.info(
            "fleet-observe: %s reported %d declared slot(s) gone from disk — the declaration(s) survive "
            "and the node's convergence pass will rebuild them: %s",
            node_id, len(gone),
            ", ".join(f"{names.get(project_id, project_id)}#{n}" for project_id, n in gone),
        )

    # Before the upsert, like `_changed_slots`.
    runners_changed = _changed_runners(session, node_uuid, payload)
    runners_statement = runner_upsert_statement(session, node_uuid, payload)
    if runners_statement is not None:
        session.execute(runners_statement)
    runners_deleted = 0
    if payload.runners_complete:
        runners_deleted = session.execute(
            runner_delete_statement(node_uuid, sorted(r.pid for r in payload.runners))
        ).rowcount or 0

    # Before the upsert, like the two gates above.
    accounts_changed = _changed_accounts(session, node_uuid, payload)
    accounts_statement = account_upsert_statement(session, node_uuid, payload)
    if accounts_statement is not None:
        session.execute(accounts_statement)
    accounts_deleted = 0
    if payload.accounts_complete:
        accounts_deleted = session.execute(
            account_delete_statement(node_uuid, sorted(a.slug for a in payload.accounts))
        ).rowcount or 0

    fetch_statement = fetch_stamp_upsert_statement(node_uuid, payload, project_ids)
    if fetch_statement is not None:
        session.execute(fetch_statement)

    out = ObservationOut(
        # Always 0: the observation cannot delete a `worktrees` row; the shape is shared.
        worktrees=ObserveFamilyResult(written=len(rows), deleted=0),
        runners=ObserveFamilyResult(written=len(payload.runners), deleted=runners_deleted),
        accounts=ObserveFamilyResult(written=len(payload.accounts), deleted=accounts_deleted),
        # Rendered back to the display name.
        changed_slots=[(names.get(project_id, str(project_id)), number) for project_id, number in changed],
        changed_runners=runners_changed,
        changed_accounts=accounts_changed,
        vanished_task_holds=vanished_task_holds,
        runner_lost_task_ids=runner_lost,
        root_grow=root_grow,
    )
    out._disk_transition = disk_transition
    out._tmp_inode_crossing = tmp_inode_crossing
    out._project_ids = project_ids
    out._node_org_id = node_org_id
    return out
