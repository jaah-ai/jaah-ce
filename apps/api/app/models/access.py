"""Authorization: groups, permissions, roles, role bindings, access requests and budgets."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.access_catalog import LEVELS, ONE_SCOPE_SQL, REQUEST_STATES, SCOPE_ORG_IS_OWN_SQL, in_sql
from app.models.base import Base, EntityMixin, org_unique, OrgScoped, StampedMixin, timestamp_column, UuidPkMixin


class Group(EntityMixin, OrgScoped, Base):
    """An RBAC group (role) — a named bundle of permissions. Data, not a hardcoded enum, so a new role
    never requires a code deploy."""

    __tablename__ = "groups"
    __table_args__ = (
        org_unique(__tablename__),
        # A name is unique within its org, case-insensitively: it is typed as input and resolved
        # exact-match-or-refuse within the org (memberships route, `seed.py`, the reserved-name guard in
        # `app/routes/groups.py`). Per org, so an identity provider's common names never collide.
        Index("uq_groups_org_id_lower_name", "org_id", text("lower(name)"), unique=True),
        # At most one Everyone group per org.
        Index(
            "uq_groups_everyone_per_org", "org_id", unique=True, postgresql_where=text("is_everyone")
        ),
    )

    # The org's Everyone group: every user of the org is a member. System-managed membership.
    is_everyone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Created by the org's identity provider over SCIM (`app/scim.py::create_group`), which owns it from then on.
    scim_managed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class Permission(Base):
    """A permission catalog entry — a capability key a role carries (`role_permissions`)."""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class UserGroupMembership(OrgScoped, Base):
    """A user's grant of a group. `resource_type`/`resource_id` are DEAD columns.

    Only `(user_id, group_id)` is read: a member holds the group's role bindings (`app/access.py`).
    `resource_type`/`resource_id` MUST NOT BE READ FOR AUTHORIZATION — scoped access lives in
    `RoleBinding` alone. Retained because `GET /api/auth/me` surfaces them on an additive-only contract."""

    __tablename__ = "user_group_memberships"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "group_id", "resource_type", "resource_id",
            name="uq_user_group_memberships_user_group_resource",
        ),
        Index("ix_user_group_memberships_user_id", "user_id"),
        Index(
            "ix_user_group_memberships_resource",
            "resource_type", "resource_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class Budget(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """A monthly USD budget on ONE project or ONE group (exactly one of `project_id`/`group_id`); `app/budgets.py`
    owns every rule — the spend, the 80% warning, the hold at 100%. `warned_month`/`held_month` are the UTC month
    (its first day) whose notice already went, so each goes once a month. A child record of its project or group,
    reached through it and deleted with it, so not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "budgets"
    __table_args__ = (
        CheckConstraint("(project_id IS NOT NULL) <> (group_id IS NOT NULL)", name="ck_budgets_one_scope"),
        CheckConstraint("monthly_usd_micros > 0", name="ck_budgets_monthly_usd_micros_positive"),
        Index("uq_budgets_project_id", "project_id", unique=True, postgresql_where=text("project_id IS NOT NULL")),
        Index("uq_budgets_group_id", "group_id", unique=True, postgresql_where=text("group_id IS NOT NULL")),
    )

    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE", name="fk_budgets_project_id"), nullable=True
    )
    group_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE", name="fk_budgets_group_id"),
        nullable=True,
    )
    monthly_usd_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    warned_month: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    held_month: Mapped[Optional[date]] = mapped_column(Date, nullable=True)


# ── Roles on the resource hierarchy (revision 0286) ──────────────────────────────────────────────────
# The catalog's contents live in `app/access_catalog.py`; these are its tables and the bindings that name
# them. Authorization reads bindings alone (`app/access.py`).

#: The scope columns, in the order every unique over them lists them.
_SCOPE_COLUMNS = ("scope_org_id", "project_id", "account_id")


class Role(Base):
    """A built-in role — platform data like `Permission`, so NOT `OrgScoped`. Rows come from
    `app/access_catalog.py::ROLES` (revision 0286, converged by `app/seed.py::_seed_roles`); no route
    writes one. `builtin` is always true today: custom roles are out of scope."""

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_roles_slug"),
        CheckConstraint(in_sql("level", LEVELS), name="ck_roles_level"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    level: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class RolePermission(Base):
    """Join row: one key a role expands to. Platform data, cascading with either end."""

    __tablename__ = "role_permissions"

    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class RoleAtScopeMixin:
    """A role at a scope: the four columns a binding holds and a request names (one row's worth of either).
    `role_id` is RESTRICT — a role still bound or asked for is not deletable (built-ins are never deleted
    anyway); exactly one of `_SCOPE_COLUMNS` is set (`ONE_SCOPE_SQL`, each table's own CHECK)."""

    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    scope_org_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    account_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )


class RoleBinding(UuidPkMixin, StampedMixin, OrgScoped, RoleAtScopeMixin, Base):
    """One principal holding one role at one scope. Additive only: no deny row or column.

    Exactly one of `user_id`/`group_id` (`ck_role_bindings_one_principal`) and exactly one of the three
    scope columns (`ck_role_bindings_one_scope`, a SUM: chained `<>` XOR admits three non-nulls). Inheritance is a READ
    rule, not rows: org → every project and account in it. An
    org-scope binding never expires (`ck_role_bindings_org_scope_never_expires`). Every reference is an
    id with an FK; the object columns' same-org keys are generated from `app/tenancy/manifest.py`."""

    __tablename__ = "role_bindings"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL) <> (group_id IS NOT NULL)", name="ck_role_bindings_one_principal"
        ),
        CheckConstraint(ONE_SCOPE_SQL, name="ck_role_bindings_one_scope"),
        CheckConstraint(SCOPE_ORG_IS_OWN_SQL, name="ck_role_bindings_scope_org_is_own_org"),
        CheckConstraint(
            "scope_org_id IS NULL OR expires_at IS NULL",
            name="ck_role_bindings_org_scope_never_expires",
        ),
        # NULLS NOT DISTINCT: three of the five principal/scope columns are NULL on every row.
        UniqueConstraint(
            "user_id", "group_id", "role_id", *_SCOPE_COLUMNS,
            name="uq_role_bindings", postgresql_nulls_not_distinct=True,
        ),
        *(
            Index(
                f"ix_role_bindings_{principal}_{scope}", f"{principal}_id", column,
                postgresql_where=text(f"{column} IS NOT NULL"),
            )
            for principal in ("user", "group")
            for scope, column in (
                ("org", "scope_org_id"), ("project", "project_id"), ("account", "account_id"),
            )
        ),
        Index(
            "ix_role_bindings_expires_at", "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )

    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class AccessRequest(UuidPkMixin, StampedMixin, OrgScoped, RoleAtScopeMixin, Base):
    """One principal asking for a role at a scope, and what came of it.

    The role and scope columns (`RoleAtScopeMixin`) and their CHECKs are `RoleBinding`'s: a request names
    exactly the binding approving it would write. One PENDING request per (requester, role, scope)
    (`uq_access_requests_pending`).
    `binding_id` is SET NULL so the record outlives a later unbind. `state`'s vocabulary is
    `app/access_catalog.py::REQUEST_STATES`."""

    __tablename__ = "access_requests"
    __table_args__ = (
        CheckConstraint(ONE_SCOPE_SQL, name="ck_access_requests_one_scope"),
        CheckConstraint(SCOPE_ORG_IS_OWN_SQL, name="ck_access_requests_scope_org_is_own_org"),
        CheckConstraint(
            in_sql("state", REQUEST_STATES), name="ck_access_requests_state"
        ),
        Index(
            "uq_access_requests_pending", "requester_id", "role_id", *_SCOPE_COLUMNS,
            unique=True, postgresql_nulls_not_distinct=True,
            postgresql_where=text("state = 'pending'"),
        ),
    )

    requester_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    decided_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    binding_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("role_bindings.id", ondelete="SET NULL"), nullable=True
    )
    requester_seen_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    slack_messages: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
