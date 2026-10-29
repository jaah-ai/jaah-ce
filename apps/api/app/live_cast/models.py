"""Live Cast's tables, registered with `Base` by the import at the foot of
`app/models/`. Tenancy (`app/tenancy/manifest.py`): both per-project tables are `Derived` through `projects` (one hop, the
composite key and org-inherit trigger built by their revision);
the two viewer-activity tables are `Shared` — an anonymous viewer belongs to no org.

* `live_cast_projects` — one project's Live Cast settings: the checkbox, the default and rules, the stable
  `slug`, and what was frozen at publish (`published_at` non-NULL = published, the quota counts these). The
  operator's two: `short_name`, the project's address `live.jaah.ai/<short_name>` in ONE namespace
  across every org, and `featured`, the one project the bare `live.jaah.ai` opens.
* `live_cast_items` — one task's verdict against the frozen rules, and an admin's override. A
  verdict counts only while its two shas are current (`app/live_cast/visibility.py`); an override counts until
  an admin resets it, and nothing automatic ever writes one (NC-6) — save a merge lane run, filed Hidden
  (`app/live_cast/state.py::hide_system_task`).
* `live_viewer_events` and `live_viewer_minutes` — Jaah's own measurement of the viewer
  (`app/live_analytics.py` writes and reads both): one row per page view, named action or console click, and one
  presence row per (minute, visitor, tab). A visitor is a random id the browser keeps; no IP and no raw User-Agent
  is stored. The viewed project is `viewed_org_id`/`project_id`/`task_id` with NO foreign key: a row outlives the
  project it names, and the column is not `org_id` because the row is not the org's (the tenancy census keeps
  `org_id` for tenant-owned tables).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScoped, _actor_column, timestamp_column

DEFAULT_ALL, DEFAULT_NONE = "all", "none"
DEFAULT_MODES = (DEFAULT_ALL, DEFAULT_NONE)
ITEM_TASK = "task"
#: "attachment" is retired: it stays in the constraint until a contract release narrows it.
ITEM_KINDS = (ITEM_TASK, "attachment")
VERDICT_PENDING, VERDICT_PUBLISHED, VERDICT_HIDDEN = "pending", "published", "hidden"
VERDICTS = (VERDICT_PENDING, VERDICT_PUBLISHED, VERDICT_HIDDEN)
OVERRIDES = (VERDICT_PUBLISHED, VERDICT_HIDDEN)
#: The rules list's bounds (AC3.x): at most this many rules, each this long once stripped.
RULES_MAX, RULE_MAX_CHARS = 20, 500


def _in(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


class LiveCastProject(OrgScoped, Base):
    """One project's Live Cast settings (module docstring). Keyed by the project: it IS that project's tab, so
    it has no id or name of its own. `slug` is assigned once (`app/live_cast/state.py::slug_candidates`) and
    never recomputed, so a later collision cannot move a published URL."""

    __tablename__ = "live_cast_projects"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_live_cast_projects_org_slug"),
        _in("default_mode", DEFAULT_MODES, "ck_live_cast_projects_default_mode"),
        CheckConstraint(
            f"jsonb_typeof(rules) = 'array' AND jsonb_array_length(rules) <= {RULES_MAX}",
            name="ck_live_cast_projects_rules",
        ),
        CheckConstraint("published_at IS NULL OR rules_sha IS NOT NULL", name="ck_live_cast_projects_frozen"),
        Index("ix_live_cast_projects_published", "org_id", postgresql_where=text("published_at IS NOT NULL")),
        # Fleet-wide, not per org: both are addresses on the one live host.
        Index("uq_live_cast_projects_short_name", "short_name", unique=True),
        Index("uq_live_cast_projects_featured", "featured", unique=True, postgresql_where=text("featured")),
    )

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    default_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=DEFAULT_ALL)
    rules: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: Frozen at publish (`app/live_cast/state.py::rules_sha`); kept on unpublish.
    rules_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The backfill N frozen at publish (the entitlement's then).
    backfill: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: The operator's short address, unique across every org (`app/live_cast/state.py::SHORT_NAME_RE`); not frozen
    #: at publish — it moves no verdict.
    short_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The project the bare live host opens; at most one row fleet-wide (the partial unique index).
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = timestamp_column()
    #: Written by the code on every save (an exempt table carries no `set_updated_at` trigger).
    updated_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = _actor_column()
    updated_by_id: Mapped[UUID] = _actor_column()


class LiveCastItem(OrgScoped, Base):
    """One task's Live Cast state (module docstring); its attachments follow it and hold none. `task_id` is
    `item_id`. The evaluation columns (`verdict` … `last_error`) are the sweep's (C3); `override*` the admin's.
    `in_scope=false` is a task outside the backfill's last N, hidden unless overridden published."""

    __tablename__ = "live_cast_items"
    __table_args__ = (
        UniqueConstraint("item_kind", "item_id", name="uq_live_cast_items_item"),
        _in("item_kind", ITEM_KINDS, "ck_live_cast_items_item_kind"),
        _in("verdict", VERDICTS, "ck_live_cast_items_verdict"),
        CheckConstraint(
            f"override IS NULL OR override IN ({', '.join(repr(v) for v in OVERRIDES)})",
            name="ck_live_cast_items_override",
        ),
        Index("ix_live_cast_items_project", "project_id", "item_kind"),
        Index("ix_live_cast_items_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    item_kind: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False, server_default=VERDICT_PENDING)
    rules_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evaluated_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    override_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    override_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    in_scope: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


#: What a viewer-activity row counts: a navigation, a named action, a console sign-up/log-in click.
EVENT_KINDS = ("page_view", "action", "console_click")
#: The categories a User-Agent is folded into; the raw string is never stored.
DEVICES = ("desktop", "mobile", "tablet", "other")
BROWSERS = ("Chrome", "Safari", "Firefox", "Edge", "Opera", "Samsung Internet", "Other")
#: How long each table keeps a row (`app/cron_retention.py` sweeps them): events cover the dashboard's longest range
#: (1y), presence only the realtime window and a day's slack.
EVENT_RETENTION_DAYS = 400
MINUTE_RETENTION_DAYS = 2


class LiveViewerEvent(Base):
    """One page view, named action or console click on the public viewer (`app/live_analytics.py`)."""

    __tablename__ = "live_viewer_events"
    __table_args__ = (
        _in("kind", EVENT_KINDS, "ck_live_viewer_events_kind"),
        _in("device", DEVICES, "ck_live_viewer_events_device"),
        _in("browser", BROWSERS, "ck_live_viewer_events_browser"),
        Index("ix_live_viewer_events_at", "at"),
        Index("ix_live_viewer_events_project_id_at", "project_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = timestamp_column()
    visitor_id: Mapped[str] = mapped_column(Text, nullable=False)
    tab_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    viewed_org_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    device: Mapped[str] = mapped_column(Text, nullable=False)
    browser: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LiveViewerMinute(Base):
    """One tab's presence in one minute: upserted by every beacon, so `last_seen` is its latest heartbeat and the
    project/task what it showed then."""

    __tablename__ = "live_viewer_minutes"
    __table_args__ = (
        _in("device", DEVICES, "ck_live_viewer_minutes_device"),
        _in("browser", BROWSERS, "ck_live_viewer_minutes_browser"),
    )

    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    visitor_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tab_id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seen: Mapped[datetime] = timestamp_column(server_now=False)
    viewed_org_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    device: Mapped[str] = mapped_column(Text, nullable=False)
    browser: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
