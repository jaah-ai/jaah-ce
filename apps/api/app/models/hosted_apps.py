"""Easy Mode's hosted apps: one row per sponsored app, and its monthly usage (`app/hosted_apps/__init__.py`)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
    SmallInteger, Text, text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import _in_check, Base, EntityMixin, org_unique, OrgScoped

#: The templates an app is generated from (`app/hosted_apps/catalog.py` holds their settings). Every new app is `web`
#: (`NEW_APP_TEMPLATE`); `node` and `python` stay for the apps made from them, and their Retry.
HOSTED_APP_TEMPLATES = ("node", "python", "web")
NEW_APP_TEMPLATE = "web"
#: The control plane environment an app belongs to: dev apps publish at `<slug>.dev.jaahai.app`.
HOSTED_APP_ENVS = ("dev", "main")
HOSTED_APP_STATES = ("provisioning", "live", "paused", "handed_over", "failed", "deleting")
#: Why a paused app is paused — also the edge's `state` value and the paused page it shows.
HOSTED_APP_PAUSE_REASONS = ("over_limit", "sponsor_ended", "operator", "db_size")
#: A database cell's state: `open` takes new apps while under its capacity, `closed` keeps its apps and takes none.
HOSTING_CELL_STATES = ("open", "closed")
#: A custom domain's lifecycle state (`app/hosted_apps/domains.py` holds the rules).
HOSTED_APP_DOMAIN_STATES = ("waiting_dns", "issuing", "live", "attention", "removing")


class HostingCell(Base):
    """One database cell of an environment: one Postgres instance in the apps account, which every app placed in it
    has its database on, and the ops function that administers it (`app/hosted_apps/cells.py`). Written only by the
    cell sync from the apps account's config (`db_cells`, `app/hosted_apps/config.py`), which is the single home of
    a cell's facts; the row adds the placements that point at it. `id` is the cell's number in
    its environment (cell 1 is the instance every app made before cells has its database on). `db_endpoint` and
    `db_secret_arn` (the instance's master login secret) are null only while the config predates cells and so
    does not publish them."""

    __tablename__ = "hosting_cells"
    __table_args__ = (
        _in_check("env", HOSTED_APP_ENVS, "ck_hosting_cells_env"),
        _in_check("state", HOSTING_CELL_STATES, "ck_hosting_cells_state"),
        CheckConstraint("capacity >= 0", name="ck_hosting_cells_capacity"),
    )

    env: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    db_endpoint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    db_secret_arn: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ops_function: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class HostedApp(EntityMixin, OrgScoped, Base):
    """One Easy Mode app: its project, its repo in Jaah's GitHub org and its AWS resources (all named from `env` and
    `slug`, `app/hosted_apps/names.py`). `slug` is the app's address label, unique per environment among apps not
    deleted, and never changes. `project_id` goes NULL when the project is deleted: the row stays as the record of
    the deleted app (`deleted_at`). `failure_step`/`failure_detail` name a `failed` provisioning's step and why, or a stopped delete's (the app
    stays `deleting`)."""

    __tablename__ = "hosted_apps"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("template", HOSTED_APP_TEMPLATES, "ck_hosted_apps_template"),
        _in_check("env", HOSTED_APP_ENVS, "ck_hosted_apps_env"),
        _in_check("state", HOSTED_APP_STATES, "ck_hosted_apps_state"),
        CheckConstraint(
            "pause_reason IS NULL OR pause_reason IN (" + ", ".join(f"'{v}'" for v in HOSTED_APP_PAUSE_REASONS) + ")",
            name="ck_hosted_apps_pause_reason",
        ),
        Index(
            "uq_hosted_apps_env_slug", "env", "slug", unique=True, postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("uq_hosted_apps_project_id", "project_id", unique=True),
        ForeignKeyConstraint(
            ["env", "cell_id"], ["hosting_cells.env", "hosting_cells.id"], name="fk_hosted_apps_cell",
            ondelete="RESTRICT",
        ),
        Index("ix_hosted_apps_cell", "env", "cell_id"),
    )

    project_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    env: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    repo_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="provisioning")
    pause_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pause_cap: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_step: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_deploy_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_deploy_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_deploy_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    handed_over_to: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The edge shape the app's apps-account resources have: `hosted_apps/aws.py` `ROUTER_EDGE_VERSION` (the router
    #: edge), which provisioning writes; every app is on it since the per-app edge was retired.
    edge_version: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    #: Lowercase hex SHA-256 of the token a `web` app's function carries as `JAAH_MAIL_TOKEN` (`hosted_apps/aws.py`
    #: `ensure_function`); the token itself lives only in that environment. Null for `node`/`python` apps.
    mail_token_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The same for the dev stage's function (its own token, `durable/hosted_app.py` `_dev_stage`; an app given its dev
    #: stage before it was registered gets it from `service.converge_mail_env`). Null while the app has no dev stage.
    dev_mail_token_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The database cell the app's database is on (`HostingCell`); null until provisioning places it.
    cell_id: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    #: When the live admin was seeded on the dev stage (`durable/hosted_app.py` `_dev_admin`); null while a `web` app on
    #: `develop` is still owed one, which the converge pass seeds (`hosted_apps/service.py` `converge_dev_admin`).
    dev_admin_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class HostedAppUsage(OrgScoped, Base):
    """One app's metered use in one month (written by the usage cron): requests, edge egress and database size."""

    __tablename__ = "hosted_app_usage"

    app_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("hosted_apps.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    egress_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    db_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class HostedAppDomain(EntityMixin, OrgScoped, Base):
    """One custom domain connected to an Easy Mode app: `name` is the hostname (lowercase ASCII, punycode for an
    international name), held per environment by at most one row once ownership is proved. Its lifecycle, the
    main-domain rule and the redirect rule live in `app/hosted_apps/domains.py`. Rows are hard-deleted on removal; the history is the audit log."""

    __tablename__ = "hosted_app_domains"
    __table_args__ = (
        _in_check("env", HOSTED_APP_ENVS, "ck_hosted_app_domains_env"),
        _in_check("state", HOSTED_APP_DOMAIN_STATES, "ck_hosted_app_domains_state"),
        # One proved row per name; pending (`waiting_dns`) claims may share it, and a trigger refuses a new one for a
        # name a proved row holds (migration 202611141500, `domain_lifecycle.py` picks the winner).
        Index("uq_hosted_app_domains_env_name", "env", "name", unique=True,
              postgresql_where=text("state <> 'waiting_dns'")),
        Index("uq_hosted_app_domains_main", "hosted_app_id", unique=True, postgresql_where=text("is_main")),
        Index("ix_hosted_app_domains_next_check", "next_check_at"),
    )

    hosted_app_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("hosted_apps.id", ondelete="CASCADE"), nullable=False
    )
    env: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="waiting_dns")
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: The ownership token the TXT record `_jaah-verify.<name>` carries: new per row, so a freed name is claimed again
    #: only by proving ownership anew.
    verify_token: Mapped[str] = mapped_column(Text, nullable=False)
    #: The CloudFront distribution tenant serving it; NULL until ownership and pointing pass.
    tenant_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attention_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attention_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    live_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The hostname the user picked as main when adding a root/www pair; NULL for a single domain.
    main_pick: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class HostedAppDomainMonth(Base):
    """A custom hostname that served an app in a metered month, kept for the whole month so its egress counts after a
    removal (the usage query's `hosts` map). Written by the domain lifecycle; never per org."""

    __tablename__ = "hosted_app_domain_months"
    __table_args__ = (_in_check("env", HOSTED_APP_ENVS, "ck_hosted_app_domain_months_env"),)

    env: Mapped[str] = mapped_column(Text, primary_key=True)
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    hostname: Mapped[str] = mapped_column(Text, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
