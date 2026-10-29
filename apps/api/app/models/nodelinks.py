"""NodeLinks: an org's shared storages and the NodeLinks nodes and node groups attach."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Text, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.crons import in_domain_sql
from app.models.hosting import CLOUD_PROVIDERS
from app.models.base import _actor_column, Base, EntityMixin, org_unique, OrgScoped, timestamp_column


# ── The org's shared-accounts store (0268) ───────────────────────────────────────────────────────────

#: The provisioning states of an org's store. `provisioning` = the file system is still being created
#: and the next provision run continues; `failed` carries `last_error`; `deleting` = its cloud objects are being
#: deleted (revision 202610221200), the row going once they are gone (`org_shared_store.delete_requested`).
ORG_SHARED_STORE_STATUSES = ("pending", "provisioning", "ready", "failed", "deleting")


#: Where a Jaah-managed store serves nodes (`OrgSharedStore.managed_location`): the node locations
#: `app/node_allocation.py::Location` names.
NODELINK_MANAGED_LOCATIONS = ("hosting_vpc", "main_vpc")


class OrgSharedStore(EntityMixin, OrgScoped, Base):
    """One EFS file system an org's NodeLinks live on (`app/org_shared_store.py`); an org has any number,
    each named (`uq_org_shared_stores_org_name`), chosen per NodeLink in its editor's Storage tab.

    Created by the control plane in the org's own AWS account, or an existing file system (`adopted`).
    Keyed off its AWS credential like `CloudConnection` (CASCADE). A store has no access point of its own:
    `access_point_id`/`access_point_arn` are legacy columns nothing reads or writes (each NodeLink carries
    its own). A store left with no NodeLink is deleted, the row only (`app/nodelinks.py`), unless Jaah made its cloud
    object and it bills on its own: the admin deletes that one (`routes/org_shared_store.py`). No `slug`.

    `managed_location` set: the operator plane's Jaah-managed store for that node location, on which tenant
    NodeLinks of other orgs live (`app/nodelinks.py` § Jaah-managed storage); at most one per location, never
    dropped with its last NodeLink."""

    __tablename__ = "org_shared_stores"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_org_shared_stores_org_name"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in ORG_SHARED_STORE_STATUSES) + ")",
            name="ck_org_shared_stores_status",
        ),
        # A store the CP provisions needs the network its security group admits; an adopted one has none.
        CheckConstraint("adopted OR ingress_cidr IS NOT NULL", name="ck_org_shared_stores_adopted_or_cidr"),
        CheckConstraint(
            "managed_location IS NULL OR managed_location IN ("
            + ", ".join(f"'{s}'" for s in NODELINK_MANAGED_LOCATIONS) + ")",
            name="ck_org_shared_stores_managed_location",
        ),
        CheckConstraint(in_domain_sql("provider", CLOUD_PROVIDERS), name="ck_org_shared_stores_provider"),
        Index("ix_org_shared_stores_credential_id", "credential_id"),
        Index(
            "uq_org_shared_stores_managed_location", "managed_location", unique=True,
            postgresql_where=text("managed_location IS NOT NULL"),
        ),
    )

    credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credentials.id", ondelete="CASCADE"),
        nullable=False,
    )
    region: Mapped[str] = mapped_column(Text, nullable=False)
    subnet_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The one IPv4 network the store's security group admits NFS from — the nodes' route in. NULL
    # only on an adopted store, whose security group is not the CP's.
    ingress_cidr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "Use existing": a file system the CP records rather than provisions (the fleet one terraform owns,
    # or the org's own), picked from AWS (`routes/org_shared_store.py`). Provisioning refuses it
    # (`app/org_shared_store.py::AdoptedStoreRefused`).
    adopted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # NULL until the provisioner has created (or found) the object — each is recorded as soon as known.
    file_system_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_system_arn: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    security_group_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mount_target_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mount_target_ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Legacy (the store's own access point, before each NodeLink chose its own): never read or written.
    access_point_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    access_point_arn: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # `hosting_vpc` / `main_vpc`: this operator-plane store is that location's Jaah-managed storage.
    managed_location: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The last policy Jaah wrote (`nodelinks.write_policy`): when AWS accepted it and the access-point ARNs it
    # admits. Kept even when the request that wrote it then fails, so the console says the policy changed.
    policy_written_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    policy_access_point_arns: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # The AWS account its credential belongs to, read with STS `GetCallerIdentity` when the operator saves a
    # Jaah-managed location (`routes/nodelink_locations.py`); NULL until then, or when AWS did not say.
    aws_account_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The cloud the store lives on (`app/clouds/base.py::ProviderKey`) and that provider's own ids for it beyond
    # the AWS columns above (revision 202610211437); on any cloud also its recorded `tier` and `capacity_gib`, and a
    # reclaimed EFS store's `creation_token` (`app/org_shared_store.py`).
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="aws", server_default="aws")
    provider_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


#: A legacy column's domain (`nodelinks.kind`): rows made before every NodeLink was ordinary say `accounts`
#: for the one at `{jaah-home}/auth/.shared`. Nothing reads it; new rows are written `general`.
NODELINK_KINDS = ("accounts", "general")


#: A NodeLink's lifecycle. `provisioning` = its access point or policy is not settled yet (re-run);
#: `deleting` = the two-phase delete is waiting for nodes to unmount, or its AWS step failed (Retry).
NODELINK_STATUSES = ("pending", "provisioning", "ready", "failed", "deleting")


class NodeLink(EntityMixin, OrgScoped, Base):
    """One shared directory that nodes mount: an EFS access point on one of the org's stores, rooted at its
    own directory (`app/nodelinks.py` is the single writer and holds every rule).

    `store_id` is NULL until the admin picks its storage. `mount_path` is a TEMPLATE beginning
    `{jaah-home}/`, resolved per node. `root_directory` is `/nodelinks/<id>` for an access point Jaah
    creates, so a NodeLink recreated under the same name never sees an earlier one's files, or an adopted
    access point's own root (`access_point_adopted`: never deleted in AWS). `in_policy`: the last
    file-system policy written to the store admits this access point (`ready` requires it).
    `pending_unmount_node_ids` (no FKs) are the nodes a delete is still waiting on, since `deleting_since`.
    `kind` is legacy (`NODELINK_KINDS`)."""

    __tablename__ = "nodelinks"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_nodelinks_org_slug"),
        UniqueConstraint("org_id", "name", name="uq_nodelinks_org_name"),
        CheckConstraint(in_domain_sql("kind", NODELINK_KINDS), name="ck_nodelinks_kind"),
        CheckConstraint(in_domain_sql("status", NODELINK_STATUSES), name="ck_nodelinks_status"),
        CheckConstraint("mount_path LIKE '{jaah-home}/%'", name="ck_nodelinks_mount_path"),
        # Legacy: at most one `accounts` row per org (harmless; new rows are `general`).
        Index("uq_nodelinks_org_accounts", "org_id", unique=True, postgresql_where=text("kind = 'accounts'")),
        Index("ix_nodelinks_store_id", "store_id"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    store_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("org_shared_stores.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="general", server_default="general")
    mount_path: Mapped[str] = mapped_column(Text, nullable=False)
    root_directory: Mapped[str] = mapped_column(Text, nullable=False)
    access_point_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    access_point_arn: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The access point is one the admin picked on the store's file system, not one Jaah created: a delete
    # drops it from the policy and leaves it in AWS.
    access_point_adopted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    in_policy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    pending_unmount_node_ids: Mapped[Optional[list[UUID]]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=True
    )
    deleting_since: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The store's provider's own ids for this NodeLink beyond the AWS access-point columns (revision 202610211437).
    provider_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class NodeNodeLink(OrgScoped, Base):
    """Node `node_id` mounts NodeLink `nodelink_id`. The pair IS the row (exempt in
    `tests/test_entity_shape.py`); both FKs CASCADE, and the composite org keys hold both in one org."""

    __tablename__ = "node_nodelinks"
    __table_args__ = (Index("ix_node_nodelinks_nodelink_id", "nodelink_id"),)

    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    nodelink_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodelinks.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = _actor_column()


class NodeGroupNodeLink(OrgScoped, Base):
    """Every node of group `node_group_id` mounts NodeLink `nodelink_id` (applied at reconcile). The pair
    IS the row, as `NodeNodeLink`."""

    __tablename__ = "node_group_nodelinks"
    __table_args__ = (Index("ix_node_group_nodelinks_nodelink_id", "nodelink_id"),)

    node_group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("node_groups.id", ondelete="CASCADE"), primary_key=True
    )
    nodelink_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodelinks.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = _actor_column()
