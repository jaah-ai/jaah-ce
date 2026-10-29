"""The deterministic port grid's inputs — the single home, stored in the database.

Range starts are `port_grid_vars` rows an operator CRUDs; the scalars are operator knobs.

* No code fallback for the starts: `SEED_RANGE_STARTS` is read only by the 0114 revision and its test;
  an empty table means "every var is tier 2".
* The scalars keep the ordinary knob precedence (DB > env > default).
* Validation is advisory: `warnings_for` reports what a save breaks; the write goes through anyway.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.operator_settings import catalog as settings_catalog, reads as settings_reads
from app.models.control_plane import SystemState
from app.models.worktrees import PortGridVar

# The five knob keys, spelled once for `scalars()` and `grid()`'s consistency check.
SCALAR_KEYS = (
    "port_block_size", "port_stride", "port_window", "auto_port_floor", "auto_port_ceiling",
)

# ── The seed, and only the seed ───────────────────────────────────────────────────────────────────────
# The seven original starts, exact so migrating moves no port. Read by revision 0114 and
# `tests/test_port_grid.py` (which asserts it trips no warning); not a runtime fallback.
SEED_RANGE_STARTS: tuple[tuple[str, int, str], ...] = (
    ("FRONTEND_PORT", 3000, "The web frontend a project's stack publishes."),
    ("API_PORT", 8000, "The project's HTTP API."),
    ("BACKEND_PORT", 8000, "The same service as API_PORT, spelled the other way — one shared window."),
    ("CONSOLE_AGENT_PORT", 9000, "The per-slot console agent."),
    ("POSTGRES_PORT", 15400, "Postgres, offset +10000 from 5432 so no window contains the host default."),
    ("REDIS_PORT", 16400, "Redis, offset +10000 from 6379 so no window contains the host default."),
    ("CDP_BRIDGE_PORT", 18800, "The Chrome DevTools Protocol bridge."),
)

# Ports a host daemon binds by default; a window over one yields an EADDRINUSE no `.env` scan sees.
# Only daemons this fleet's hosts run: a wider list would warn on the shipped grid (3306 sits inside
# FRONTEND_PORT's window). Add a port when a host starts running that daemon.
WELL_KNOWN_HOST_PORTS: tuple[tuple[int, str], ...] = (
    (5432, "PostgreSQL"),
    (6379, "Redis"),
    (11211, "memcached"),
)

# The hosts' ephemeral floor (`ip_local_port_range` 32768-60999), also docker's dynamic-publish range.
EPHEMERAL_FLOOR = 32768

# Var-name allowlist, also enforced node-side (`worktree_mechanics.ports._PORT_VAR_NAME_RX`): a newline or
# `=` would append a second assignment to a `.env`.
VAR_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

# And it must read as a port var, so a row cannot shadow a key like `NODE_TOKEN` in every slot's
# `.env`. Same heuristic the node allocates by (`worktree_mechanics.ports._env_assigned_ports`).
VAR_NAME_MUST_CONTAIN = "PORT"


@dataclass(frozen=True)
class Grid:
    """One resolved snapshot of every grid input. Immutable and DB-free, so `fleet_decide` stays pure
    and one request/tick sees one consistent grid."""

    range_start_by_var: dict[str, int]
    block_size: int
    stride: int
    window: int
    auto_port_floor: int
    auto_port_ceiling: int

    def wire(self) -> dict:
        """The shape the node and the pool-port checker receive, spelled once.

        Sorted by start, then name: `check_pool_ports.service_expected_for_embedded` resolves a port to
        the first window containing it, so with overlapping windows the lowest start must win."""
        return {
            "range_start_by_var": dict(
                sorted(self.range_start_by_var.items(), key=lambda kv: (kv[1], kv[0]))
            ),
            "block_size": self.block_size,
            "stride": self.stride,
            "window": self.window,
            "auto_port_floor": self.auto_port_floor,
            "auto_port_ceiling": self.auto_port_ceiling,
        }


class GridUnavailable(RuntimeError):
    """The scalars could not be resolved consistently with the rows, so there is no grid to serve.

    Raised rather than degraded: real rows with default scalars (after a swallowed settings-read
    error) would band slots onto another project's ports silently; a refused lease is loud.
    `routes/agent/pool_tick.py`'s pool tick calls `grid()` unguarded, so this fails the whole tick, which is
    intended — every roster band depends on the same scalars."""


def _stored_override_keys(session: Session) -> set[str]:
    """The scalar keys the settings row actually holds, read through the caller's own session.

    A second read on purpose: `operator_settings.reads.overrides()` caches a swallowed DB error as `{}` for
    the full TTL, indistinguishable from "nothing overridden"."""
    row = session.get(SystemState, settings_catalog.STATE_KEY)
    stored = row.value if row is not None and isinstance(row.value, dict) else {}
    return {key for key in SCALAR_KEYS if key in stored}


def scalars(org: Optional[UUID]) -> dict[str, int]:
    """The four-plus-one scalar inputs for one org, through the ordinary knob precedence.

    No session argument: the accessors read their own cached row, so edits apply without a restart.
    `grid()` checks the cache against the DB; call this directly only where a code default is acceptable.

    All five are `tier="org"` knobs, so `None` means "the operator's grid" and stops being answerable
    once any org overrides one. `None` can still reach here from `node_org()` for an unstamped node and
    from `routes/agent/releases.py::get_port_grid` for a credential that proves no node; both raise once the tier
    is in use."""
    # Five reads, one snapshot, retried once if the resolver's view moved: a two-knob `PUT
    # /api/settings` invalidates the cache once after commit, and straddling it yields a band neither
    # grid authorized.
    for _ in range(2):
        before = settings_reads.overrides()
        resolved = {
            "block_size": settings_reads.get_int("port_block_size", org=org),
            "stride": settings_reads.get_int("port_stride", org=org),
            "window": settings_reads.get_int("port_window", org=org),
            "auto_port_floor": settings_reads.get_int("auto_port_floor", org=org),
            "auto_port_ceiling": settings_reads.get_int("auto_port_ceiling", org=org),
        }
        if settings_reads.overrides() == before:
            return resolved
    return resolved


def range_starts(session: Session, org: Optional[UUID]) -> dict[str, int]:
    """`{VAR: start}` for one org's tier-1 vars. Empty means "everything is tier 2", never an error.

    The `org_id` predicate is explicit: this table's readers are unbound (pool tick, drain cron), so the
    read seam would answer them `false()`, indistinguishable from an empty grid. `org=None` renders as
    `IS NULL`, the unstamped rows."""
    return {
        row.name: int(row.range_start)
        for row in session.scalars(
            select(PortGridVar)
            .where(PortGridVar.org_id == org)
            .order_by(PortGridVar.range_start, PortGridVar.name)
        )
    }


def grid(session: Session, org: Optional[UUID]) -> Grid:
    """The one way anything in the control plane obtains one org's grid.

    Raises `GridUnavailable` when the settings row holds a scalar override the resolver did not apply.
    Presence is compared, never values. The check covers the operator tier only (the org tier lives on
    another plane), so a settings-store outage degrades an org to the operator's scalars for up to the
    TTL rather than raising.

    Per org is enough for disjointness: bands collide only on one host, and a host belongs to one org
    for life (`app/tenancy/node_org.py`). Spec §4.4."""
    starts = range_starts(session, org)
    resolved = scalars(org)
    stored = _stored_override_keys(session)
    missed = sorted(key for key in stored if key not in settings_reads.overrides())
    if missed:
        raise GridUnavailable(
            f"the settings row overrides {missed} but the resolver did not apply them — its cached view "
            f"of the row is stale or was built from a failed read. Refusing to serve a grid mixing real "
            f"range starts with default scalars"
        )
    return Grid(range_start_by_var=starts, **resolved)


def index_ceiling(grid_snapshot: Grid) -> int:
    """The highest worktree index one port block holds: slot 0 (the clone) and indices `1..ceiling` each
    take one stride inside `block_size`, so `(block_size - 1) // stride`, never below 0. The one index
    bound every (node, project) pair has."""
    if grid_snapshot.stride <= 0 or grid_snapshot.block_size <= 0:
        return 0
    return max(0, (grid_snapshot.block_size - 1) // grid_snapshot.stride)


def warnings_for(grid_snapshot: Grid) -> list[str]:
    """Every grid invariant this snapshot breaks, as operator-readable sentences. Empty ⇒ consistent.

    Advisory, never a refusal. Each line names the offending value and what it collides with.
    """
    out: list[str] = []
    starts = grid_snapshot.range_start_by_var
    window = grid_snapshot.window

    # 1. Overlapping service windows. Vars sharing a start exactly (API_PORT/BACKEND_PORT) are one
    #    service, so only a non-zero gap under `window` is a collision.
    ordered = sorted(starts.items(), key=lambda kv: (kv[1], kv[0]))
    for (lo_var, lo), (hi_var, hi) in zip(ordered, ordered[1:]):
        if lo != hi and hi - lo < window:
            out.append(
                f"{lo_var} ({lo}) and {hi_var} ({hi}) are {hi - lo} apart but each window is {window} "
                f"wide — their windows overlap, so one project's {lo_var} band can land on another's "
                f"{hi_var}."
            )

    # 2. A window over a port a host daemon binds by default.
    for var, start in ordered:
        for port, daemon in WELL_KNOWN_HOST_PORTS:
            if start <= port < start + window:
                out.append(
                    f"{var}'s window ({start}-{start + window - 1}) contains {port}, the {daemon} "
                    f"default — a project banded onto it collides with a host daemon, which no .env "
                    f"scan can see."
                )

    # 3. A start below 1024 (docker as root would publish over e.g. sshd). A warning, per this module's
    #    contract; `1..65535` is enforced at the route.
    for var, start in ordered:
        if start < 1024:
            out.append(
                f"{var}'s range start ({start}) is in the privileged range (<1024), so its window "
                f"({start}-{start + window - 1}) covers ports host infrastructure owns — 22 for sshd, "
                f"80/443 for a proxy. A project banded there squats them, and docker will publish it."
            )

    # 4. A window that runs off the top of the port space, or up into the kernel's ephemeral range.
    for var, start in ordered:
        last = start + window - 1
        if last > 65535:
            out.append(f"{var}'s window ends at {last}, past the 65535 ceiling — the top of it is unbindable.")
        elif last >= EPHEMERAL_FLOOR:
            out.append(
                f"{var}'s window ends at {last}, inside the kernel's ephemeral range "
                f"({EPHEMERAL_FLOOR}+) — a stack bound there loses the port to an outbound socket "
                f"sooner or later."
            )

    # 5. The tier-2 auto range must sit above every grid window and below the ephemeral floor.
    if grid_snapshot.auto_port_floor > grid_snapshot.auto_port_ceiling:
        out.append(
            f"the tier-2 auto range is inverted ({grid_snapshot.auto_port_floor}-"
            f"{grid_snapshot.auto_port_ceiling}) — no port can be auto-assigned at all."
        )
    for var, start in ordered:
        if grid_snapshot.auto_port_floor < start + window and grid_snapshot.auto_port_ceiling >= start:
            out.append(
                f"the tier-2 auto range ({grid_snapshot.auto_port_floor}-"
                f"{grid_snapshot.auto_port_ceiling}) overlaps {var}'s window ({start}-"
                f"{start + window - 1}) — an auto-assigned port can be handed out on top of a banded one."
            )
    if grid_snapshot.auto_port_ceiling >= EPHEMERAL_FLOOR:
        out.append(
            f"the tier-2 auto ceiling ({grid_snapshot.auto_port_ceiling}) reaches into the kernel's "
            f"ephemeral range ({EPHEMERAL_FLOOR}+) — an auto port up there collides with docker's "
            f"dynamic publishing and with outbound sockets."
        )
    return out


def usage_by_var(port_vars_per_project: Iterable[tuple[str, Iterable[str]]]) -> dict[str, list[str]]:
    """`{VAR: [project, …]}` — who declares each var, so the console can say what a delete demotes to
    tier 2. Presentation only."""
    out: dict[str, list[str]] = {}
    for project, declared in port_vars_per_project:
        for var in declared or ():
            out.setdefault(str(var), []).append(project)
    return {var: sorted(names) for var, names in sorted(out.items())}
