"""node_host_metrics.py — the single home of every rule over a node's CPU/RAM/disk/network series.

The bucket width, retention, warn/critical thresholds, level vocabulary and staleness bound are spelled
here and nowhere else; `app/models/nodes.py::NodeHostMetric` is the shape and argues the storage design. The
thresholds ride the `/api/nodes/{id}/metrics` payload so the browser draws the server's numbers.

Disk is a level (latest free/total, judged as a used percentage against the same thresholds); network is
bytes per second, windowed like CPU, and judged by nothing — throughput is not pressure. Load average, swap,
PSI and the top processes are latest values, also judged by nothing: signals for the operator, never
an input to `level` or placement. Per-session usage is `app/node_session_usage.py`'s, on this module's grain.

Each node posts its rolling 300s windowed mean as an optional `host_metrics` key on
`POST /api/agent/fleet/observe` (`apps/jaahd/host_metrics.py`); `app/fleet_observe.py` runs the two
statement builders only when a block is present. Absence is not a state: an absent block writes nothing,
and a node with no row serves `null`, never 0%. A stale reading's levels are `unknown`, never `ok`.
Ages are the database's clock, in the same statement as the rows (`routes/nodes/health.py::HealthClock`), and
so is the chart's x-axis (`window_bounds`). Every read is one statement for the whole page.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import Float, case, cast, delete, extract, func, literal, null, select, true
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.agent_sessions import STATUS_RUNNING
from app.clock import as_utc
from app.entity_shape import float_or_none
from app.models.agent_sessions import AgentSession
from app.models.nodes import Node, NodeHostMetric
from app.models.worktrees import VirtualNode

# ── The storage grain ─────────────────────────────────────────────────────────────
# One row per node per minute: ~12 observation posts (every ~5s) collapse into one row via
# `uq_node_host_metrics_node_bucket`, which is the write throttle, the idempotency the observe route's
# `40P01`/`40001` retry needs, and a uniformly spaced series. The storage design itself is argued at
# `app/models/nodes.py::NodeHostMetric`.
BUCKET_SECS = 60.0

# The longest range the Metrics tab offers. Enforced by `prune_statement` on the ingest path, since the
# writer is the only actor that reliably touches a node's rows.
RETENTION_DAYS = 7.0
RETENTION_SECONDS = RETENTION_DAYS * 24.0 * 60.0 * 60.0

# The node reports disk in kB (`disk_free_kb`); the gates that read it compare in GB.
KB_PER_GB = 1024 * 1024

# ── The thresholds ────────────────────────────────────────────────────────────────
# Inclusive on both: exactly 75.0 is `warn`, exactly 90.0 is `critical`. Sent to the browser for its
# guide lines.
WARN_PCT = 75.0
CRITICAL_PCT = 90.0

# /tmp inode use above this is `warn` (strictly greater). Its own level, outside `level` and every launch
# gate: a flag for the operator, since a full inode table breaks every `mktemp` with disk to spare.
TMP_INODE_WARN_PCT = 80.0

# ── The level vocabulary ──────────────────────────────────────────────────────────
# A closed set; only the CP computes one. The browser paints `level` and never compares a percentage.
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"
# A positive statement that we do not know (stale or unreadable), not an absence.
LEVEL_UNKNOWN = "unknown"

# `unknown` ranks lowest so a mixed pair keeps the known half; ranking it highest would let one
# unreadable figure erase a genuine `critical`.
LEVEL_RANK = {LEVEL_UNKNOWN: 0, LEVEL_OK: 1, LEVEL_WARN: 2, LEVEL_CRITICAL: 3}

# The floor under the per-reading staleness bound (`LatestReading.stale_bound_seconds`,
# `max(window_secs, FLOOR)`): a reading older than its own window describes an elapsed period. The floor
# keeps a young daemon (short window) out of `unknown`; it equals the sampler's default window, and a
# node configured higher is governed by its row. Tighter than `node_liveness.STALE_AFTER_SECONDS`
# (different question). `bucket_at` is the minute floor, so the bound bites at 240..300s of silence.
FRESH_BOUND_SECONDS = 300.0

# The ceiling on that bound: `window_secs` is node-attested (only non-finite/non-positive are refused at
# ingest), so without it one post with a huge window would never go stale. Capped here, where the value is used, like `MAX_PLACEMENT_CORES`. A
# node legitimately configured above it just goes stale an hour after it stops talking.
MAX_STALE_BOUND_SECONDS = 3600.0

# The most points a chart is served; `bucket_secs_for` re-buckets to fit (7d at 60s would be 10,080).
MAX_POINTS = 240


# ── Pure classification (no DB, no clock, hermetically tested) ────────────────────

def classify(pct: Optional[float]) -> str:
    """One percentage's level: `unknown` / `ok` / `warn` / `critical`.

    `None` and non-finite figures are `unknown`: `NaN >= x` is False, so an unguarded chain would answer
    `ok` (the columns are `double precision`). Both boundaries are inclusive. Pure."""
    if pct is None or not math.isfinite(pct):
        return LEVEL_UNKNOWN
    if pct >= CRITICAL_PCT:
        return LEVEL_CRITICAL
    if pct >= WARN_PCT:
        return LEVEL_WARN
    return LEVEL_OK


def disk_used_pct(free_kb: Optional[int], total_kb: Optional[int]) -> Optional[float]:
    """`100 * (total - free) / total`, clamped to 0..100, or None when the pair is not a measurement
    (either half absent, or a non-positive total). Pure."""
    if free_kb is None or total_kb is None or total_kb <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total_kb - free_kb) / total_kb))


def tmp_inode_used_pct(free: Optional[int], total: Optional[int]) -> Optional[float]:
    """`100 * (total - free) / total`, clamped to 0..100, or None when the pair is not a measurement
    (either half absent, or a non-positive total — a filesystem with no fixed inode table). Pure."""
    if free is None or total is None or total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total - free) / total))


def classify_tmp_inodes(pct: Optional[float]) -> str:
    """`warn` above `TMP_INODE_WARN_PCT`, `ok` at or below, `unknown` for no figure. Pure."""
    if pct is None or not math.isfinite(pct):
        return LEVEL_UNKNOWN
    return LEVEL_WARN if pct > TMP_INODE_WARN_PCT else LEVEL_OK


def worse(a: str, b: str) -> str:
    """The more severe of two levels by `LEVEL_RANK` — the one figure the list badge paints.

    An unrecognised level sorts as `unknown` rather than raising on a serving path."""
    return a if LEVEL_RANK.get(a, 0) >= LEVEL_RANK.get(b, 0) else b


def bucket_secs_for(range_secs: float) -> float:
    """The bucket width a range of `range_secs` should be served at — never finer than the storage
    grain, never more than `MAX_POINTS` points.

    Rounded up to a whole minute, since stored rows sit on 60s boundaries and uneven buckets would make the
    line wobble. 1h → 60s, 6h → 120s, 24h → 360s, 7d → 2520s. Pure."""
    if range_secs <= 0:
        return BUCKET_SECS
    per_point = range_secs / float(MAX_POINTS)
    return max(BUCKET_SECS, math.ceil(per_point / 60.0) * 60.0)


# ── The read shapes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LatestReading:
    """One node's newest bucket, with the age the database measured and the levels that age earns.

    Levels are properties, so the stale⇒unknown rule cannot be bypassed. "Never reported" is the absence
    of a `LatestReading`, not a zeroed one."""

    node_id: UUID
    bucket_at: datetime
    cpu_pct: float
    mem_pct: float
    cpu_count: Optional[int]
    mem_total_kb: Optional[int]
    window_secs: float
    samples: int
    age_seconds: float
    # Optional because a daemon may predate them. `None` is "said nothing", never "no".
    disk_free_kb: Optional[int] = None
    disk_total_kb: Optional[int] = None
    net_rx_bps: Optional[float] = None
    net_tx_bps: Optional[float] = None
    tmp_inodes_free: Optional[int] = None
    tmp_inodes_total: Optional[int] = None
    # Pressure signals, latest like disk; `None` is "said nothing". `top_processes` is read only
    # when asked for (`latest_map(with_processes=True)`), so the roster and placement never pay for the JSONB.
    load_1: Optional[float] = None
    load_5: Optional[float] = None
    load_15: Optional[float] = None
    swap_total_kb: Optional[int] = None
    swap_free_kb: Optional[int] = None
    psi_cpu_pct: Optional[float] = None
    psi_mem_pct: Optional[float] = None
    psi_io_pct: Optional[float] = None
    top_processes: Optional[list] = None

    @property
    def stale_bound_seconds(self) -> float:
        """The age at which this reading stops describing the present:
        `min(max(window_secs, FRESH_BOUND_SECONDS), MAX_STALE_BOUND_SECONDS)`.

        Per reading, because the honest bound is the reading's own window; the floor keeps a freshly restarted
        daemon out of `unknown`, the ceiling stops a node-attested window from buying permanent freshness. On
        the wire so the stale banner can name the bound."""
        return min(max(self.window_secs, FRESH_BOUND_SECONDS), MAX_STALE_BOUND_SECONDS)

    @property
    def stale(self) -> bool:
        """Whether this reading has outlived `stale_bound_seconds` (strictly greater, like
        `node_liveness.is_stale`)."""
        return self.age_seconds > self.stale_bound_seconds

    @property
    def cpu_level(self) -> str:
        """`unknown` while stale, whatever the percentage says."""
        return LEVEL_UNKNOWN if self.stale else classify(self.cpu_pct)

    @property
    def mem_level(self) -> str:
        return LEVEL_UNKNOWN if self.stale else classify(self.mem_pct)

    @property
    def disk_pct(self) -> Optional[float]:
        """Used percentage of the clone filesystem — `disk_used_pct` over this reading's pair."""
        return disk_used_pct(self.disk_free_kb, self.disk_total_kb)

    @property
    def disk_level(self) -> str:
        return LEVEL_UNKNOWN if self.stale else classify(self.disk_pct)

    @property
    def swap_pct(self) -> Optional[float]:
        """Used percentage of swap — `disk_used_pct`'s arithmetic over the swap pair; None for no swap (a zero
        total), as for an unreported pair. Judged by nothing: swap in use is a signal, not a level."""
        return disk_used_pct(self.swap_free_kb, self.swap_total_kb)

    @property
    def tmp_inode_pct(self) -> Optional[float]:
        """Used percentage of /tmp's inode table — `tmp_inode_used_pct` over this reading's pair."""
        return tmp_inode_used_pct(self.tmp_inodes_free, self.tmp_inodes_total)

    @property
    def tmp_inode_level(self) -> str:
        """Not folded into `level`: a flag, not a load figure."""
        return LEVEL_UNKNOWN if self.stale else classify_tmp_inodes(self.tmp_inode_pct)

    @property
    def level(self) -> str:
        """The single figure the list badge paints — the worst of CPU, memory and disk. An unreported disk
        is `unknown`, which ranks lowest, so it never hides the CPU/RAM verdict. Placement does not read
        this (`busyness`)."""
        return worse(worse(self.cpu_level, self.mem_level), self.disk_level)

# ── Busyness: the ONE figure node PLACEMENT ranks on ─────────────────────────────
# Placement needs an order over nodes, which three levels cannot supply, so the same pair folds a second
# way here; `app/pool_state.py::assign_nodes` consumes it. The fold is `max(cpu, mem)`, `worse()`'s rule.
# A pair `(rank, pct)`: unknown nodes (no or stale reading) sort behind every known one. A rank flag,
# not a sentinel percentage: `assign_nodes` adds to the percentage as it places, so a finite sentinel
# could drift below a real reading and `inf` could not spread unknowns among themselves.
Busyness = tuple[int, float]

BUSY_KNOWN = 0
BUSY_UNKNOWN = 1

# The rank does the ordering, so the percentage can start at zero and spread among unknowns.
BUSIEST: Busyness = (BUSY_UNKNOWN, 0.0)


def busyness(reading: Optional[LatestReading]) -> Busyness:
    """How loaded this node is, as the pair `assign_nodes` orders on — `BUSIEST` for no usable reading.

    The fold is `max(cpu, mem)`. Absent, stale and non-finite readings share one arm: load
    unknown, ranked last. The non-finite check covers both columns: `NaN` would read as a good reading,
    cannot be ordered, and makes `band` raise; `max(cpu, nan)` silently drops it."""
    if reading is None or reading.stale:
        return BUSIEST
    if not (math.isfinite(reading.cpu_pct) and math.isfinite(reading.mem_pct)):
        return BUSIEST
    return (BUSY_KNOWN, max(reading.cpu_pct, reading.mem_pct))


def free_cores(reading: Optional[LatestReading]) -> Optional[float]:
    """Idle CPU in cores — `(100 - cpu%) / 100 x cores` — the figure a MERGE-LANE run is placed on
    (`app/pool_state.py::assign_nodes`). `None` for no usable reading: absent, stale, a
    non-finite percentage, or a core count the node never reported (a guessed `1` would rank a silent big
    host as tiny). A percentage outside 0-100 is clamped. Pure.

    Absolute, not a percentage: a full gate wants cores, so 30% idle of 32 beats 60% idle of 8."""
    if reading is None or reading.stale or reading.cpu_count is None:
        return None
    if not math.isfinite(reading.cpu_pct):
        return None
    idle = (100.0 - min(100.0, max(0.0, float(reading.cpu_pct)))) / 100.0
    return idle * cores(reading)


# ── The two rules the RUNNER COUNT needs, over the same reading ──────────────────
# Load alone misses waiting runners (polling, blocked on a question) that cost little CPU but occupy a
# slot, so placement also counts runners against power. Power is the reported core count from `/proc`,
# not the declared slot count (an operator policy).


# A security bound: `cpu_count` is node-reported and ingested up to INT32_MAX (a storage limit), but it
# is now a divisor under placement's runner density, so a huge value would absorb the whole queue every
# pass. 512 is a plausibility ceiling, above any real instance. Not enforced at ingest, since the
# console caption may show whatever a node says. A node lying within the bound still attracts more than
# its share — the observe route's own trust gap, which this bound does not close.
MAX_PLACEMENT_CORES = 512


def cores(reading: Optional[LatestReading]) -> int:
    """This node's usable core count for placement — its power, never zero, never `None`, never above
    `MAX_PLACEMENT_CORES`.

    Staleness does not collapse it (hardware, not load). An absent or unusable count is `1`, the
    pessimistic answer in both uses: maximally dense per core and full weight per placement."""
    return cores_or_default(None if reading is None else reading.cpu_count)


def runners_per_core(runners: int, core_count: Optional[int]) -> float:
    """How densely loaded with runners this node is — `runners / cores`, the key `assign_nodes`
    breaks a CPU tie on (equal runners per core across the fleet).

    Takes the raw `core_count`, `None` included, so the floor and ceiling stay in this module. Pure."""
    return float(runners) / float(cores_or_default(core_count))


def cores_or_default(core_count: Optional[int]) -> int:
    """`core_count` as placement will believe it — the arithmetic half of `cores`, for a bare number.

    The one home of the floor (`None` → 1) and the `MAX_PLACEMENT_CORES` ceiling."""
    if core_count is None:
        return 1
    return max(1, min(MAX_PLACEMENT_CORES, int(core_count)))


# ── The tie BAND: how close two loads have to be for the runner count to decide ───
# "CPU leads, the runner count breaks ties" needs a tie: loads are quantised into bands of `band_pct`
# points, and nodes in one band tie. The width is an operator knob (`placement_cpu_tie_band_pct`, read
# in `app/pool_state.py::cpu_tie_band_pct`); `band` stays pure. Bands are fixed offsets, not centred
# windows (which would be non-transitive), so 79.9 and 80.1 do not tie at the default width.


def band(load: Busyness, band_pct: float) -> tuple[int, int]:
    """`load` quantised to its tie band — the first key `assign_nodes` orders on, so CPU leads.

    The rank travels with it, so an unknown node (`BUSIEST`, 0.0) never bands to the front. `band_pct` is
    clamped at 1.0 (no ZeroDivisionError on dispatch). A non-finite percentage folds to the unknown band
    (`int(floor(nan))` raises, and `assign_nodes` accepts caller-supplied maps). A negative percentage
    floors at band 0 rather than outranking idle nodes. The asymmetry (`-inf` last, `-0.1` first-equal) is
    intentional and pinned by tests. Pure."""
    rank, pct = load
    width = max(1.0, float(band_pct))
    value = float(pct)
    if not math.isfinite(value):
        return (BUSY_UNKNOWN, 0)
    return (rank, int(math.floor(max(0.0, value) / width)))


@dataclass(frozen=True)
class SeriesPoint:
    """One plotted point: a bucket stamp (the bucket's start, so points are evenly spaced) and the mean
    CPU/RAM/disk-used/network over it. The last three are None for a bucket whose rows never carried them
    (an older daemon)."""

    at: datetime
    cpu_pct: float
    mem_pct: float
    disk_pct: Optional[float] = None
    net_rx_bps: Optional[float] = None
    net_tx_bps: Optional[float] = None
    # Used share of /tmp's inode table; None for a bucket whose rows never carried the pair.
    tmp_inode_pct: Optional[float] = None
    # Bucket means of the pressure signals; None for a bucket whose rows never carried them.
    load_1: Optional[float] = None
    swap_pct: Optional[float] = None
    psi_cpu_pct: Optional[float] = None
    psi_mem_pct: Optional[float] = None
    psi_io_pct: Optional[float] = None


# ── SQL fragments ────────────────────────────────────────────────────────────────

def _seconds(value: float):
    """A width or a horizon as a bound float8 parameter, never inlined into the SQL text
    (`app/account_fold.py`'s rule)."""
    return literal(value, Float(precision=53))


def _bucket_expr(moment: Any, width_secs: float):
    """`to_timestamp(floor(extract(epoch from <moment>) / w) * w)` — the one spelling of the truncation.

    In SQL, never Python: a second implementation could differ by a microsecond from the `ON CONFLICT`
    arbiter, yielding duplicate-key errors or two rows per minute. The epoch is cast to `float8` (extract
    returns `numeric`). Serves both the ingest (`now()`) and the chart's re-bucketing of `bucket_at`."""
    epoch = cast(extract("epoch", moment), Float(precision=53))
    width = _seconds(width_secs)
    return func.to_timestamp(func.floor(epoch / width) * width)


def _db_now_minus(seconds: float):
    """`now() - <seconds>` on the database's clock, the same clock that wrote the stamps; spelled with
    `to_timestamp` so the constant stays a bound parameter."""
    return func.to_timestamp(
        cast(extract("epoch", func.now()), Float(precision=53)) - _seconds(seconds)
    )


# ── The two writers (statements only — the caller's transaction executes them) ────

# The scalar pressure columns, written straight off the block; named once for both arms.
_SIGNAL_COLUMNS = (
    "load_1", "load_5", "load_15", "swap_total_kb", "swap_free_kb", "psi_cpu_pct", "psi_mem_pct", "psi_io_pct",
)


def _own_processes(node_uuid: UUID, processes: Optional[list]):
    """`processes`, or NULL when this node is co-located on a Multi-Node Host (a `virtual_nodes` row names it):
    its daemon is ProtectProc-exempt and sees every neighbour's processes, which are not its own to report. In
    SQL, inside the upsert, so a hostile daemon cannot bypass it and the pass pays no extra round trip."""
    if processes is None:
        return None
    co_located = select(VirtualNode.id).where(VirtualNode.node_id == node_uuid).exists()
    return case((co_located, null()), else_=literal(processes, NodeHostMetric.top_processes.type))


def _entries_json(entries: Any) -> Optional[list]:
    """A validated entry list (pydantic models or plain dicts from duck-typed callers) as JSON-able dicts;
    `None` stays `None` ("said nothing")."""
    if entries is None:
        return None
    return [entry.model_dump() if hasattr(entry, "model_dump") else dict(entry) for entry in entries]


def upsert_statement(node_uuid: UUID, block: Any):
    """The minute's row: INSERT … ON CONFLICT (node_id, bucket_at) DO UPDATE.

    `block` is the validated wire block, duck-typed (`fleet_observe.HostMetricsIn`). No session argument,
    so it stays compile-testable; execute only when a block was posted. The conflict arm overwrites rather
    than averages: each post is a mean over the whole overlapping window, so last write wins.
    `index_elements`, since the arbiter is a unique index, not a named constraint.

    The bucket is the database's `now()`, not the node's `observed_at` — a security boundary: `observed_at`
    is clamped only forward, and here the stamp is the primary key, so a slow or hostile node clock could
    overwrite arbitrary past minutes. Receipt vs sample time differ by far less than a bucket."""
    # Range-checked at the wire (`fleet_observe._EPOCH_MAX`); re-checked here because a raise would roll
    # back the caller's whole pass, and duck-typed callers bypass the wire model.
    epoch = getattr(block, "last_upgrade_epoch", None)
    if not (isinstance(epoch, int) and not isinstance(epoch, bool) and 0 < epoch < 10**11):
        epoch = None
    insert = pg_insert(NodeHostMetric).values(
        node_id=node_uuid,
        # Named here: a `pg_insert` is invisible to the write seam's `stamp_org`
        # (`fleet_verdicts._persist_intents` has the argument). A subquery keeps the builder
        # session-free; the `(node_id, org_id)` FK accepts only the node's own value.
        org_id=select(Node.org_id).where(Node.id == node_uuid).scalar_subquery(),
        bucket_at=_bucket_expr(func.now(), BUCKET_SECS),
        cpu_pct=block.cpu_pct,
        mem_pct=block.mem_pct,
        cpu_count=block.cpu_count,
        mem_total_kb=block.mem_total_kb,
        window_secs=block.window_secs,
        samples=block.samples,
        # `getattr` with a `None` fallback: duck-typed callers (fixtures, replays) may lack the newest
        # fields, and absent means "said nothing".
        disk_free_kb=getattr(block, "disk_free_kb", None),
        disk_total_kb=getattr(block, "disk_total_kb", None),
        tmp_inodes_free=getattr(block, "tmp_inodes_free", None),
        tmp_inodes_total=getattr(block, "tmp_inodes_total", None),
        net_rx_bps=getattr(block, "net_rx_bps", None),
        net_tx_bps=getattr(block, "net_tx_bps", None),
        reboot_required=getattr(block, "reboot_required", None),
        last_upgrade_at=None if epoch is None else datetime.fromtimestamp(epoch, tz=timezone.utc),
        **{key: getattr(block, key, None) for key in _SIGNAL_COLUMNS},
        top_processes=_own_processes(node_uuid, _entries_json(getattr(block, "top_processes", None))),
        updated_at=func.now(),
    )
    return insert.on_conflict_do_update(
        index_elements=[NodeHostMetric.node_id, NodeHostMetric.bucket_at],
        set_={
            "cpu_pct": insert.excluded.cpu_pct,
            "mem_pct": insert.excluded.mem_pct,
            "cpu_count": insert.excluded.cpu_count,
            "mem_total_kb": insert.excluded.mem_total_kb,
            "window_secs": insert.excluded.window_secs,
            "samples": insert.excluded.samples,
            "disk_free_kb": insert.excluded.disk_free_kb,
            "disk_total_kb": insert.excluded.disk_total_kb,
            "tmp_inodes_free": insert.excluded.tmp_inodes_free,
            "tmp_inodes_total": insert.excluded.tmp_inodes_total,
            "net_rx_bps": insert.excluded.net_rx_bps,
            "net_tx_bps": insert.excluded.net_tx_bps,
            "reboot_required": insert.excluded.reboot_required,
            "last_upgrade_at": insert.excluded.last_upgrade_at,
            **{key: getattr(insert.excluded, key) for key in _SIGNAL_COLUMNS + ("top_processes",)},
            # Set explicitly: raw SQL bypasses the ORM's `onupdate=`.
            "updated_at": func.now(),
            # Coalesced, never re-stamped: a node's org is immutable (`app/tenancy/node_org.py`), so
            # this only repairs a NULL row.
            "org_id": func.coalesce(NodeHostMetric.org_id, insert.excluded.org_id),
        },
    )


def prune_statement(node_uuid: UUID):
    """Drop this node's buckets older than `RETENTION_DAYS`.

    Scoped to one node (a pass may not touch another's rows), and cheap down the unique index. On the ingest
    path, so it needs no schedule; a silent node's last 7 days (~10k rows) stay until it reports again or
    is deleted (CASCADE)."""
    return delete(NodeHostMetric).where(
        NodeHostMetric.node_id == node_uuid,
        NodeHostMetric.bucket_at < _db_now_minus(RETENTION_SECONDS),
    )


# ── The two readers ──────────────────────────────────────────────────────────────

def latest_map(
    session: Session, node_uuids: Iterable[UUID], *, with_processes: bool = False
) -> dict[UUID, LatestReading]:
    """`{node_id: LatestReading}` for the newest bucket of each node — one statement for a whole page.

    One `LATERAL … ORDER BY bucket_at DESC LIMIT 1` per wanted id reads the unique index backwards: one
    seek per node. Not `DISTINCT ON (node_id)`, which Postgres has no skip scan for — it read every bucket
    in retention (~10k rows a node) to keep one, ~1000x slower at a full week. The age is measured in the
    same statement, on the database's clock, because it decides `stale`; every node is dated against the
    same instant. A node with no row is absent from the map. `with_processes` also reads `top_processes`
    (the Metrics tab); every other caller leaves the JSONB unread."""
    wanted = list(node_uuids)
    if not wanted:
        return {}
    ids = func.unnest(array(wanted, type_=PG_UUID(as_uuid=True))).table_valued("node_id").render_derived(
        name="wanted"
    )
    newest = (
        select(
            NodeHostMetric.node_id,
            NodeHostMetric.bucket_at,
            NodeHostMetric.cpu_pct,
            NodeHostMetric.mem_pct,
            NodeHostMetric.cpu_count,
            NodeHostMetric.mem_total_kb,
            NodeHostMetric.window_secs,
            NodeHostMetric.samples,
            NodeHostMetric.disk_free_kb,
            NodeHostMetric.disk_total_kb,
            NodeHostMetric.tmp_inodes_free,
            NodeHostMetric.tmp_inodes_total,
            NodeHostMetric.net_rx_bps,
            NodeHostMetric.net_tx_bps,
            *(getattr(NodeHostMetric, key) for key in _SIGNAL_COLUMNS),
            (NodeHostMetric.top_processes if with_processes else null()).label("top_processes"),
            cast(
                extract("epoch", func.now() - NodeHostMetric.bucket_at), Float(precision=53)
            ).label("age_seconds"),
        )
        .where(NodeHostMetric.node_id == ids.c.node_id)
        .order_by(NodeHostMetric.bucket_at.desc())
        .limit(1)
        .lateral("newest")
    )
    rows = session.execute(select(newest).select_from(ids).join(newest, true())).all()
    return {
        row.node_id: LatestReading(
            node_id=row.node_id,
            bucket_at=row.bucket_at,
            cpu_pct=row.cpu_pct,
            mem_pct=row.mem_pct,
            cpu_count=row.cpu_count,
            mem_total_kb=row.mem_total_kb,
            window_secs=row.window_secs,
            samples=row.samples,
            age_seconds=row.age_seconds,
            disk_free_kb=row.disk_free_kb,
            disk_total_kb=row.disk_total_kb,
            net_rx_bps=row.net_rx_bps,
            net_tx_bps=row.net_tx_bps,
            tmp_inodes_free=row.tmp_inodes_free,
            tmp_inodes_total=row.tmp_inodes_total,
            **{key: getattr(row, key) for key in _SIGNAL_COLUMNS},
            top_processes=row.top_processes,
        )
        for row in rows
    }


def window_bounds(session: Session, *, hours: float) -> tuple[datetime, datetime]:
    """The `(start, end)` a chart of the last `hours` spans — the axis, read off the database's clock.

    The x-axis is a property of the request, not the data: anchoring on the last point drew a stale node's
    recent gap on the left. `end` is the same transaction-constant `now()` `series` filters on, so both
    horizons are one instant when read in one session. Exception: `series` floors its buckets, so the
    oldest point may precede `window_start` by up to `bucket_secs - BUCKET_SECS`; the client clamps it.
    The client supplies no domain and does no clock arithmetic. A node with no rows still gets real bounds."""
    row = session.execute(
        select(
            _db_now_minus(hours * 60.0 * 60.0).label("window_start"),
            func.now().label("window_end"),
        )
    ).one()
    return row.window_start, row.window_end


def series(session: Session, node_uuid: UUID, *, hours: float) -> list[SeriesPoint]:
    """One node's chart series over the last `hours`, re-bucketed to at most `MAX_POINTS`, oldest-first.

    Uses the ingest's `_bucket_expr`, so buckets are uniform. `avg()`, not max, so the shape matches the
    `latest` figure; a mean of non-overlapping minute means is a legitimate fold. The horizon is the
    database's `now()`. No rows in range returns `[]`."""
    width = bucket_secs_for(hours * 60.0 * 60.0)
    bucket = _bucket_expr(NodeHostMetric.bucket_at, width).label("at")
    # `disk_used_pct` in SQL: NULL for a row without a usable pair, and `avg()` skips NULLs, so a bucket of
    # older-daemon rows is NULL rather than a percentage. The CASE is load-bearing: Postgres' GREATEST/LEAST
    # IGNORE a NULL argument, so a bare clamp would turn "no disk reported" into 100%.
    total = cast(NodeHostMetric.disk_total_kb, Float(precision=53))
    disk_used = case(
        (
            (NodeHostMetric.disk_free_kb.is_not(None)) & (NodeHostMetric.disk_total_kb > 0),
            func.greatest(
                0.0, func.least(100.0, 100.0 * (total - NodeHostMetric.disk_free_kb) / total)
            ),
        ),
        else_=None,
    )
    # `tmp_inode_used_pct` in SQL, the same NULL-preserving CASE as `disk_used`.
    inodes_total = cast(NodeHostMetric.tmp_inodes_total, Float(precision=53))
    tmp_inode_used = case(
        (
            (NodeHostMetric.tmp_inodes_free.is_not(None)) & (NodeHostMetric.tmp_inodes_total > 0),
            func.greatest(
                0.0, func.least(100.0, 100.0 * (inodes_total - NodeHostMetric.tmp_inodes_free) / inodes_total)
            ),
        ),
        else_=None,
    )
    # Used share of swap, the same NULL-preserving CASE (a zero total — no swap — is NULL, not 100%).
    swap_total = cast(NodeHostMetric.swap_total_kb, Float(precision=53))
    swap_used = case(
        (
            (NodeHostMetric.swap_free_kb.is_not(None)) & (NodeHostMetric.swap_total_kb > 0),
            func.greatest(
                0.0, func.least(100.0, 100.0 * (swap_total - NodeHostMetric.swap_free_kb) / swap_total)
            ),
        ),
        else_=None,
    )
    rows = session.execute(
        select(
            bucket,
            func.avg(NodeHostMetric.cpu_pct).label("cpu_pct"),
            func.avg(NodeHostMetric.mem_pct).label("mem_pct"),
            func.avg(disk_used).label("disk_pct"),
            func.avg(NodeHostMetric.net_rx_bps).label("net_rx_bps"),
            func.avg(NodeHostMetric.net_tx_bps).label("net_tx_bps"),
            func.avg(tmp_inode_used).label("tmp_inode_pct"),
            func.avg(NodeHostMetric.load_1).label("load_1"),
            func.avg(swap_used).label("swap_pct"),
            func.avg(NodeHostMetric.psi_cpu_pct).label("psi_cpu_pct"),
            func.avg(NodeHostMetric.psi_mem_pct).label("psi_mem_pct"),
            func.avg(NodeHostMetric.psi_io_pct).label("psi_io_pct"),
        )
        .where(
            NodeHostMetric.node_id == node_uuid,
            NodeHostMetric.bucket_at >= _db_now_minus(hours * 60.0 * 60.0),
        )
        # Grouped and ordered by the same expression object as the label, so they cannot diverge.
        .group_by(bucket)
        .order_by(bucket)
    ).all()
    return [
        SeriesPoint(
            at=row.at,
            cpu_pct=float(row.cpu_pct),
            mem_pct=float(row.mem_pct),
            disk_pct=float_or_none(row.disk_pct),
            net_rx_bps=float_or_none(row.net_rx_bps),
            net_tx_bps=float_or_none(row.net_tx_bps),
            tmp_inode_pct=float_or_none(row.tmp_inode_pct),
            load_1=float_or_none(row.load_1),
            swap_pct=float_or_none(row.swap_pct),
            psi_cpu_pct=float_or_none(row.psi_cpu_pct),
            psi_mem_pct=float_or_none(row.psi_mem_pct),
            psi_io_pct=float_or_none(row.psi_io_pct),
        )
        for row in rows
    ]


# ── Concurrent runners: the node's own sessions, beside the host series ─────────────────────────


def concurrent_counts(
    intervals: Iterable[tuple[datetime, Optional[datetime]]], starts: Iterable[datetime], width_secs: float,
) -> list[int]:
    """Per bucket `[start, start + width)`, how many `[started, ended)` intervals overlap it. `ended=None` is
    still running (open to the right). Pure."""
    width = timedelta(seconds=width_secs)
    spans = [(as_utc(a), as_utc(b) if b is not None else None) for a, b in intervals]
    out: list[int] = []
    for start in starts:
        lo = as_utc(start)
        hi = lo + width
        out.append(sum(1 for a, b in spans if a < hi and (b is None or b > lo)))
    return out


def runner_counts(session: Session, node_uuid: UUID, starts: list[datetime], width_secs: float) -> list[int]:
    """`concurrent_counts` over the node's sessions overlapping the buckets at `starts` (oldest first). A
    session with no `ended_at` counts only while `running`: an `unknown` one never closed would count forever."""
    if not starts:
        return []
    lo = as_utc(starts[0])
    hi = as_utc(starts[-1]) + timedelta(seconds=width_secs)
    rows = session.execute(
        select(AgentSession.started_at, AgentSession.ended_at).where(
            AgentSession.node_id == node_uuid,
            AgentSession.started_at < hi,
            (AgentSession.ended_at > lo)
            | (AgentSession.ended_at.is_(None) & (AgentSession.status == STATUS_RUNNING)),
        )
    ).all()
    return concurrent_counts(rows, starts, width_secs)
