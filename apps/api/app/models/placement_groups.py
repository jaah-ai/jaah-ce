"""Node groups (code name `placement_groups`): named sets of an org's nodes a project may be allocated to.

A project with no `project_placement_groups` row runs on **All** — every node of its org — which is computed,
never stored (`app/placement_groups.py`, the one reader). Pools keep the code name `node_groups`
(`app/models/node_groups.py`); these are a separate concept.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, OrgScoped, UuidPkMixin, org_unique


class PlacementGroup(EntityMixin, OrgScoped, Base):
    """A named set of the org's nodes. Standard entity shape plus `slug`, unique per org. "All" is built in
    (computed), so no row may take its name."""

    __tablename__ = "placement_groups"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_placement_groups_org_slug"),
        Index("uq_placement_groups_org_lower_name", "org_id", func.lower("name"), unique=True),
        CheckConstraint("lower(name) <> 'all'", name="ck_placement_groups_name_not_all"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)


class PlacementGroupNode(UuidPkMixin, OrgScoped, Base):
    """A node's membership of a group. A join, not an entity."""

    __tablename__ = "placement_group_nodes"
    __table_args__ = (
        UniqueConstraint("group_id", "node_id", name="uq_placement_group_nodes_group_node"),
    )

    group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("placement_groups.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )


class ProjectPlacementGroup(UuidPkMixin, OrgScoped, Base):
    """A project's allocation to a group. A project with none runs on All. A join, not an entity."""

    __tablename__ = "project_placement_groups"
    __table_args__ = (
        UniqueConstraint("project_id", "group_id", name="uq_project_placement_groups_project_group"),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("placement_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
