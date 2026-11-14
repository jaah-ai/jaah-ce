"""The org's slot-port range — the two knobs a node picks each slot's ports from, as one snapshot.

A node assigns every declared `port_vars` entry a free port from `[auto_port_floor, auto_port_ceiling]` at
lease (`apps/jaahd/worktree_mechanics/ports.py::apply_auto_ports`); the control plane stores no per-project
port and computes none. This module only resolves the two knobs (ordinary precedence: DB > env > default)
into the `grid` block the pool tick and `GET /api/agent/port-grid` ship.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.operator_settings import catalog as settings_catalog, reads as settings_reads
from app.models.control_plane import SystemState

# The two knob keys, spelled once for `scalars()` and `grid()`'s consistency check.
SCALAR_KEYS = ("auto_port_floor", "auto_port_ceiling")


@dataclass(frozen=True)
class Grid:
    """One resolved snapshot of the org's port range. Immutable and DB-free, so one tick sees one range."""

    auto_port_floor: int
    auto_port_ceiling: int

    def wire(self) -> dict:
        """The shape the node and the pool-port checker receive, spelled once."""
        return {"auto_port_floor": self.auto_port_floor, "auto_port_ceiling": self.auto_port_ceiling}


class GridUnavailable(RuntimeError):
    """The knobs could not be resolved consistently with the settings row, so there is no range to serve.

    Raised rather than degraded: a default range served after a swallowed settings-read error would move every
    slot whose port sits outside it. The pool tick calls `grid()` unguarded, so this fails the whole tick."""


def _stored_override_keys(session: Session) -> set[str]:
    """The knob keys the settings row actually holds, read through the caller's own session.

    A second read on purpose: `operator_settings.reads.overrides()` caches a swallowed DB error as `{}` for
    the full TTL, indistinguishable from "nothing overridden"."""
    row = session.get(SystemState, settings_catalog.STATE_KEY)
    stored = row.value if row is not None and isinstance(row.value, dict) else {}
    return {key for key in SCALAR_KEYS if key in stored}


def scalars(org: Optional[UUID]) -> dict[str, int]:
    """The two knobs for one org, through the ordinary knob precedence, read as one snapshot (retried once if
    the resolver's view moved mid-read, so a two-knob save cannot yield a range neither value authorized).

    Both are `tier="org"` knobs, so `None` means "the operator's range"."""
    resolved: dict[str, int] = {}
    for _ in range(2):
        before = settings_reads.overrides()
        # Spelled per key, not looped over `SCALAR_KEYS`: the threaded-reader census finds readers by literal.
        resolved = {
            "auto_port_floor": settings_reads.get_int("auto_port_floor", org=org),
            "auto_port_ceiling": settings_reads.get_int("auto_port_ceiling", org=org),
        }
        if settings_reads.overrides() == before:
            return resolved
    return resolved


def grid(session: Session, org: Optional[UUID]) -> Grid:
    """The one way anything in the control plane obtains one org's port range.

    Raises `GridUnavailable` when the settings row holds an override the resolver did not apply. Presence is
    compared, never values, and only on the operator tier (the org tier lives on another plane)."""
    resolved = scalars(org)
    stored = _stored_override_keys(session)
    missed = sorted(key for key in stored if key not in settings_reads.overrides())
    if missed:
        raise GridUnavailable(
            f"the settings row overrides {missed} but the resolver did not apply them — its cached view "
            f"of the row is stale or was built from a failed read. Refusing to serve a default port range"
        )
    return Grid(**resolved)
