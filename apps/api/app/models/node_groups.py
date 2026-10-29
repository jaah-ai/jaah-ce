"""Dynamic node groups, the projects a node or group serves, and GitHub Actions runner pools."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Text, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.crons import in_domain_sql
from app.models.base import Base, EntityMixin, org_unique, OrgScoped, StampedMixin, timestamp_column, UuidPkMixin


# ── Dynamic node groups ──────────────────────────────────────────────────────────────


# The desired-count ceiling; shared by revision 0169's CHECK and the route's 422 so a typo is never a 500.
MAX_NODE_GROUP_DESIRED = 100


# The `node_groups` columns the reconcile pass writes; the `updated_at` trigger fires on the complement
# (revision 0169), so a 60s stamp never looks like an operator edit. The live home a drift guard compares
# the installed trigger against; a new column must be classified here (precedent:
# `app/crons.py::CRON_MACHINERY_COLUMNS`).
NODE_GROUP_MACHINERY_COLUMNS: tuple[str, ...] = ("last_reconciled_at", "last_error")


class NodeGroup(EntityMixin, OrgScoped, Base):
    """A standing instruction to hold a capacity: the CP launches and terminates nodes until the fleet
    matches this group's capacity lines.

    The CP is the scaler; there is no AWS ASG: an ASG would launch instances the CP never
    sees, forcing a group-wide bootstrap secret instead of one minted token and tailnet key per node.
    It reuses `app/cloud_node_launch.py`. Given up: capacity-optimized spot, AZ spread,
    rebalance hints. Capacity is fixed, not demand-driven: the reconcile pass holds exactly what the
    operator set.

    `enabled` defaults false (unlike `Node.enabled`): this row creates billing machines, so a saved form
    must not start converging mid-edit. `last_reconciled_at`/`last_error` are the operator's feedback and
    excluded from the `updated_at` trigger (see `NODE_GROUP_MACHINERY_COLUMNS`); `last_error` is `''`
    when fine.

    A standard entity (revision 0169). No `slug`: reached by `id`; the name only seeds generated node names, and
    `nodes.name` is the durable handle.
    """

    __tablename__ = "node_groups"
    __table_args__ = (
        org_unique(__tablename__),
    )

    # The cloud connection every node of the group launches from (`cloud_node_launch.connection_for`;
    # missing: reported in `last_error`). RESTRICT: running machines hang off a group. Nullable in DDL
    # only for the release overlap (revision 0404 backfilled every row); a write always sets it.
    connection_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cloud_node_defaults.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # The connection's credential, DERIVED on write from `connection_id` and kept because termination
    # proves itself by the credential that signed each launch (`node_group_reconcile.py`). RESTRICT,
    # unlike the connection's CASCADE: running machines hang off a group.
    credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credentials.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_reconciled_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # True: the group's nodes are born with no NodeLinks. Its own column because no `node_group_nodelinks` rows
    # means "never named" (the Accounts default); set only while the group has no rows (`routes/node_groups.py`).
    nodelinks_none: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # No `relationship()`s, per this file's convention: explicit `select()`s, cascades in DDL (0169).


class NodeGroupCapacity(UuidPkMixin, OrgScoped, Base):
    """One line of a group's desired capacity: how many nodes of one instance type, in one market.

    A row, not four group columns, so a group can mix types per market and reconcile is one loop. Not an
    entity (`tests/test_entity_shape.py::_EXEMPT_TABLES`) but has an `id`, which
    `Node.node_group_capacity_id` points at.

    `desired = 0` winds a line down (free nodes terminated, row kept). Deleting a row is refused while a
    node points at it (RESTRICT), so zero-then-delete is the only order, and no path can orphan a billing
    machine.
    """

    __tablename__ = "node_group_capacity"
    __table_args__ = (
        # The vocabulary in DDL, as `ck_worktrees_state`: a third value would fall through the
        # `InstanceMarketOptions` branch.
        CheckConstraint("market IN ('on_demand', 'spot')", name="ck_node_group_capacity_market"),
        # The cost control in DDL: a UI dropdown bounds a browser, not a request.
        CheckConstraint(
            f"desired >= 0 AND desired <= {MAX_NODE_GROUP_DESIRED}",
            name="ck_node_group_capacity_desired",
        ),
        # One line per (group, market, instance_type), so counts are what the operator wrote and nodes
        # match one line.
        UniqueConstraint(
            "group_id", "market", "instance_type", name="uq_node_group_capacity_group_market_type"
        ),
    )

    # CASCADE; running instances are protected by the RESTRICT on `nodes`, which blocks this cascade.
    group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("node_groups.id", ondelete="CASCADE"), nullable=False
    )
    market: Mapped[str] = mapped_column(Text, nullable=False)
    instance_type: Mapped[str] = mapped_column(Text, nullable=False)
    desired: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # No `relationship()` back to the group — see `NodeGroup` for why this file declares none.


class NodeGroupProject(UuidPkMixin, OrgScoped, Base):
    """Which project gets worktrees on every node of a group: a line is (group, project).

    Per node, since slot numbers are scoped to `(node, project)` (`uq_worktrees_node_project_number`).
    An assignment of the project to every node of the group, resolved at read time
    (`app/slot_allocation.py::effective_assignments`) and never materialised per node; the slots it causes
    are converged by `slot_allocation.converge`. Convergent: a fresh node has no `jaah_home` yet, so its
    slots are declared on a later pass.

    Not an entity. It carries no static count: a node's static slots come from its own `NodeProject` row.
    The retired `worktrees_per_node` column (and its CHECK) stay in
    the DB until dropped (`migrations/env.py::_RETIRING_COLUMNS`); the ORM does not map them.
    """

    __tablename__ = "node_group_projects"
    __table_args__ = (
        UniqueConstraint("group_id", "project_id", name="uq_node_group_projects_group_project"),
    )

    group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("node_groups.id", ondelete="CASCADE"), nullable=False
    )
    # CASCADE: the produced `worktrees` rows cascade via `worktrees.project_id`; the node survives.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # No `relationship()` back to the group — see `NodeGroup` for why this file declares none.


class NodeProject(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """An assignment: `project` may hold worktrees on `node`. Placement, not a preference — a
    (node, project) pair with no assignment (here or through the node's group) holds no worktree at all.

    `static_slots` is how many static slots the pair holds; NULL means none (0) — there is no group or
    project default to fall back to. The rules live in
    `app/slot_allocation.py`, the single home; nothing else re-derives them.

    Not an entity (a join, like `NodeGroupProject`), but it carries the actor stamps: the pool clone
    authenticates as the human who assigned the project (`created_by_id`, as `routes/pool_slots.py`'s
    declarations do).

    No `set_updated_at` trigger (a join, `tests/test_entity_shape.py`): `slot_allocation.pin_to_declared`, the
    writer, stamps `updated_at` itself.
    """

    __tablename__ = "node_projects"
    __table_args__ = (
        CheckConstraint(
            "static_slots IS NULL OR static_slots >= 0", name="ck_node_projects_static_slots"
        ),
        UniqueConstraint("node_id", "project_id", name="uq_node_projects_node_project"),
    )

    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    static_slots: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


# The runner-count ceiling, shared by revision 0244's CHECK and the route's 422. 32 is far above any
# real host (each slot gets a home, unit and docker root) and below exhaustion.
MAX_GHA_RUNNERS_PER_NODE = 32


# How many slots one report may name, deliberately above the ceiling: a node reports what is installed,
# which can exceed what the pool declares (the surplus a lowered count leaves). 100 is the whole
# `GHA_RUNNER_SLOT_RE` language (`gha-0 … gha-99`), so it bounds without refusing a legitimate host.
MAX_GHA_REPORTED_SLOTS = 100


# What an operator may ask for. `absent` is a desired state: the row tells the node to remove runners
# until it reports done, then the report route deletes it (`app/routes/agent/gha_runners.py`). Tuples so
# `in_domain_sql` renders the CHECK.
GHA_RUNNER_POOL_STATES: tuple[str, ...] = ("running", "paused", "absent")


# Where the node is relative to that: "not there yet" (self-resolving) must differ from "failed".
GHA_RUNNER_POOL_PHASES: tuple[str, ...] = ("pending", "converged", "error")


# A runner-name prefix must match this before it is stored: `install.py` anchors names on the host's
# `socket.gethostname()`, and the CP interpolates it when matching GitHub's roster. DNS-label alphabet
# plus dot/underscore, 1..64 chars.
GHA_RUNNER_PREFIX_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


# The whole slot-name language (`install.py`'s `gha-1 … gha-N`, N ≤ 2 digits). The CP derives slots from
# GitHub runner names (`routes/agent/gha_runners.py::_gha_slot_of`) and serves them as an allowlist, so it must
# validate like the node-side guards do.
GHA_RUNNER_SLOT_RE = r"^gha-[0-9]{1,2}$"


# Cap on node-posted failure text: re-served on every render and posted each minute, so uncapped it is a
# write amplifier for any node token holder.
GHA_RUNNER_ERROR_MAX_CHARS = 2000


class GhaRunnerPool(EntityMixin, OrgScoped, Base):
    """What self-hosted GitHub Actions runners one node holds: the desired state and the node's report.

    Declarative: the console writes this row and pokes the node (`gha_runners_apply`, no arguments); the
    node re-derives everything from `GET /api/agent/gha-runners/desired` over its own credential,
    converges, and reports. A lost message costs a minute (the 60s pass re-reads). `NodeGroup` is the
    precedent.

    One pool per node (`uq_gha_runner_pools_node_id`), so one repo per node: the host's GitHub Actions
    runner installer owns one `RUNNERS_ROOT` of `gha-1 … gha-N` slots. `node_id`
    cascades; `project_id`/`credential_id` restrict (as `NodeGroup.credential_id`): delete the pool first.

    `last_error` is `''`, never NULL, and is the only feedback path (the converge runs as root, out of
    request reach). `observed` is a node-owned JSONB array of
    `{"slot","name","unit","active","sub","registered"}`, replaced wholesale per report and shaped by
    `install.py --status`, so not a child table. `[]` (looked, nothing) differs from `observed_at IS NULL`
    (never reported).

    A standard entity. No `slug`: reached through its node. `name` is `"<node name>/<project name>"`, a
    label only.
    """

    __tablename__ = "gha_runner_pools"
    __table_args__ = (
        # The 0244 CHECKs, rendered from the same constants (documentation: alembic ignores CHECKs; see
        # `Cron.__table_args__`).
        CheckConstraint(
            in_domain_sql("desired_state", GHA_RUNNER_POOL_STATES),
            name="ck_gha_runner_pools_desired_state",
        ),
        CheckConstraint(
            in_domain_sql("phase", GHA_RUNNER_POOL_PHASES), name="ck_gha_runner_pools_phase"
        ),
        # The cost control in DDL (as `ck_node_group_capacity_desired`). No zero: that means delete.
        CheckConstraint(
            f"desired_count >= 1 AND desired_count <= {MAX_GHA_RUNNERS_PER_NODE}",
            name="ck_gha_runner_pools_desired_count",
        ),
        # One pool per node; named so a route can classify a concurrent create's IntegrityError.
        UniqueConstraint("node_id", name="uq_gha_runner_pools_node_id"),
    )

    # The host. CASCADE. The composite `(node_id, org_id)` key is the derivation, generated by
    # `app/tenancy/keys.py`. No `index=True`: the unique already indexes it.
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    # The repo, via `projects.repo_url`. A NULL `repo_url` is a 422 at the route (a CHECK cannot see
    # another table). A second tenant-owned reference, in the composite-key test's gap
    # register like `worktrees.project_id`.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Whose GitHub token mints registration/remove tokens: a `github` credential, checked at the route
    # (`app/routes/gha_runners.py`) against `app/credentials/registry.py`. RESTRICT.
    credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    desired_count: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_state: Mapped[str] = mapped_column(Text, nullable=False)
    # Born `pending`: every write is an instruction the node has not seen.
    phase: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    observed: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # When the node last reported (see the class docstring); never operator-written.
    observed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The hostname every runner name here is anchored on (`<gethostname()>-gha-N`), reported by the node:
    # it is neither `nodes.name` nor `nodes.id`, and matching GitHub's roster (the busy/idle split a drain
    # needs) requires it. `''` means not yet reported, so the desired-document route acts on nothing
    # rather than guess and stop another host's runner. Written only by the report route.
    runner_prefix: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    # No `relationship()`s (see `NodeGroup`).
