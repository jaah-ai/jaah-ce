"""The audit trail: activity events, their search index, and the command audit."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import ForeignKey, Index, SmallInteger, String, Text, desc, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, org_unique, OrgScoped, timestamp_column


class ActivityEvent(OrgScoped, Base):
    """Generic read/write request log."""

    __tablename__ = "activity_events"
    __table_args__ = (
        org_unique(__tablename__),
        # Prefix (`LIKE 'x%'`) filters on `kind`, and lookups by the subject id inside `data`.
        Index("ix_activity_events_kind_pattern", "kind", postgresql_ops={"kind": "varchar_pattern_ops"}),
        Index("ix_activity_events_data_id", text("(data ->> 'id')")),
        # Covers `app/user_activity.py::_recently_recorded`'s dedup scan:
        # WHERE user_id = ? AND action_type = 'read' AND path = ? ORDER BY created_at DESC LIMIT 1.
        Index(
            "ix_activity_events_dedup",
            "user_id", "action_type", "path", "created_at",
        ),
        # The browse query's default filter plus its ORDER BY. Leading on `action_type` is what lets
        # that column go without its own index.
        Index(
            "ix_activity_events_browse",
            "action_type", desc("created_at"),
        ),
        # `routes/audit.py`'s `user_id` filter and its chip labels: one actor's rows, newest first.
        # `org_id` leads because the read seam puts it in every such WHERE.
        Index(
            "ix_activity_events_actor_newest",
            "org_id", "user_id", desc("created_at"), desc("id"),
        ),
        # `routes/audit.py::browse_audit`'s keyset page under one org (the seam's predicate, or its
        # `org_id` filter): `(created_at, id) < cursor` walks this in order.
        Index(
            "ix_activity_events_org_newest",
            "org_id", desc("created_at"), desc("id"),
        ),
        # Two GIN indexes are declared after the class, beside `activity_search_text()`, which one of
        # them indexes and which needs the mapped columns: `ix_activity_events_search_trgm` and
        # `ix_activity_events_data_path`.
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # An `admin_users.id` with NO FOREIGN KEY, deliberately: the trail OUTLIVES the account that wrote
    # it (`RESTRICT` would block deleting any user who ever acted, `CASCADE` would erase history,
    # `SET NULL` would anonymize it). Adding one changes what `DELETE /api/users/{id}` means.
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    actor_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # No `index=True`: `ix_activity_events_browse` leads on this column.
    action_type: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    method: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    data: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # HTTP status of the captured request. NULL is the PERMANENT, correct value for every row that is not
    # a captured request (reads, `record_write` audits, task transitions/notes) — never backfill it: a
    # `status` filter deliberately EXCLUDES those rows (`=` and `<>` are both false against NULL).
    status: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = timestamp_column(index=True)


def activity_search_text() -> Any:
    """The ONE text a `GET /api/audit?q=` search matches: summary, actor, path and kind, space-joined.

    `ix_activity_events_search_trgm` indexes exactly this expression (revision 202610141500 spells it in
    SQL), and the planner uses that index only for a textually identical expression, so the route builds
    it here and nowhere else. Literals, not binds, for the same reason. Change it only together with a
    migration that rebuilds the index."""
    blank, space = text("''"), text("' '")
    return (
        func.coalesce(ActivityEvent.summary, blank)
        .op("||")(space).op("||")(func.coalesce(ActivityEvent.actor_label, blank))
        .op("||")(space).op("||")(func.coalesce(ActivityEvent.path, blank))
        .op("||")(space).op("||")(ActivityEvent.kind)
    )


#: The record a refused sign-in writes (`app/login_funnel.py::record_login_failure`), the attempt in `data.identifier`.
SIGN_IN_REFUSAL_KIND = "login_failed"


def refusal_identifier_text() -> Any:
    """A refused sign-in's attempted address (`data.identifier`), the second text a `GET /api/audit?q=` search
    matches, AND-ed with `refusal_kind_clause()`. `ix_activity_events_refusal_identifier_trgm` indexes exactly
    this expression under exactly that predicate (revision 202610201300), so literals, not binds — the planner
    proves a partial index only from a literal."""
    return ActivityEvent.data.op("->>")(text("'identifier'"))


def refusal_kind_clause() -> Any:
    """`kind = 'login_failed'` as a literal: the partial index's predicate (`refusal_identifier_text`)."""
    return ActivityEvent.kind == text(f"'{SIGN_IN_REFUSAL_KIND}'")


# Declared, not DDL-only, so `alembic revision --autogenerate` sees them and never emits their drop. They
# render the revision's DDL (the extra parentheses `||` gains here normalise to the same `pg_get_indexdef`).
# The `q` search: an unanchored `ILIKE` over `activity_search_text()` as an index probe, not a scan.
Index(
    "ix_activity_events_search_trgm",
    activity_search_text().label("search_text"),
    postgresql_using="gin",
    postgresql_ops={"search_text": "gin_trgm_ops"},
)


# The `q` search's second arm: a refusal's attempted address, which the search text does not carry. Partial, so
# only refusals pay for it and the trail-wide index above is not rebuilt.
Index(
    "ix_activity_events_refusal_identifier_trgm",
    refusal_identifier_text().label("identifier"),
    postgresql_using="gin",
    postgresql_ops={"identifier": "gin_trgm_ops"},
    postgresql_where=refusal_kind_clause(),
)


# The `subject` filter's JSONB `@>`.
Index(
    "ix_activity_events_data_path",
    ActivityEvent.__table__.c.data,
    postgresql_using="gin",
    postgresql_ops={"data": "jsonb_path_ops"},
)


class CommandAudit(OrgScoped, Base):
    """Command execution audit log — one row per executed command.

    `POST /api/commands` dispatches over the node transport and inserts the row in the request
    handler's own transaction; that insert is the only write, so this table is the record, not a cache."""

    __tablename__ = "command_audit_log"
    __table_args__ = (
        org_unique(__tablename__),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # SET NULL (nullable since 0106): the append-only record must survive node deregistration, losing
    # only attribution. CASCADE would erase the trail; RESTRICT would make nodes undeletable.
    node_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    command_type: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column(index=True)
    completed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
