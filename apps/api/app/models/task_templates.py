"""Task templates: curated task definitions a member starts in one click (`app/routes/task_templates.py`).

Two tables answer one library, the Playbooks split: the operator's global templates (`task_templates`, `Shared`)
and an org's own (`org_task_templates`, `OrgScoped`, behind the "Templates (org)" plan switch). Both carry
`_TemplateColumns` and draw ids from one UUIDv7 space, so `/api/templates/{id}` resolves either kind. A
template stores what a task create stores (title, body, persona, autonomous, model); depth and involvement
are derived from them as the task form derives them (`app/routes/task_templates.py::depth_of`).

Start counts live in per-day counter tables, one per template table, so "Most popular" never reads `tasks`
across orgs: the global counter carries no `org_id` at all. Revision 202610281500 is the DDL.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, OrgScoped, _in_check, org_unique, timestamp_column

#: The purpose groups, in gallery order (PRD §9), each with its label. The order IS the sort order.
PURPOSES: dict[str, str] = {
    "build_fix": "Build & fix",
    "quality": "Quality",
    "security": "Security",
    "maintenance": "Maintenance",
    "docs_release": "Docs & release",
    "research_planning": "Research & planning",
}
#: Field bounds (PRD D7): a card shows the name on two lines and the summary on one.
MAX_NAME = 80
MAX_SUMMARY = 120
#: Card image kinds (`task_template_assets.kind`).
ASSET_KINDS = ("card_image", "body_image")

#: The full-text document: name (A), summary and the purpose's label (B), body (C). The label is a CASE over the
#: closed set so the expression stays IMMUTABLE, as a generated column requires.
_PURPOSE_LABEL_SQL = (
    "CASE purpose " + " ".join(f"WHEN '{key}' THEN '{label}'" for key, label in PURPOSES.items()) + " ELSE '' END"
)
TSV_EXPRESSION = (
    "setweight(to_tsvector('english'::regconfig, name), 'A') || "
    "setweight(to_tsvector('english'::regconfig, summary), 'B') || "
    f"setweight(to_tsvector('english'::regconfig, {_PURPOSE_LABEL_SQL}), 'B') || "
    "setweight(to_tsvector('english'::regconfig, body), 'C')"
)


def _template_checks(table: str) -> tuple[CheckConstraint, ...]:
    return (
        _in_check("purpose", tuple(PURPOSES), f"ck_{table}_purpose"),
        CheckConstraint(f"char_length(name) <= {MAX_NAME}", name=f"ck_{table}_name_length"),
        CheckConstraint(f"char_length(summary) <= {MAX_SUMMARY}", name=f"ck_{table}_summary_length"),
    )


class _TemplateColumns:
    """What a template is, whichever table holds it. `origin` is which table the row lives in. `image_key` is
    `builtin:<slug>` (a static SVG the console serves at `/templates/<slug>.svg`) or `asset:<asset id>` (an upload)."""

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # May hold `attachment:<filename>` image refs; copied onto a task's body on Start.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # A `persona_registry.CREATABLE_PERSONAS` member, checked by the route.
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    autonomous: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # A draft is never in the gallery and never starts.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(TSV_EXPRESSION, persisted=True))


class TaskTemplate(EntityMixin, _TemplateColumns, Base):
    """A global template, one copy every org reads; only the operator writes it."""

    __tablename__ = "task_templates"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_task_templates_slug"),
        *_template_checks(__tablename__),
        Index("ix_task_templates_tsv", "tsv", postgresql_using="gin"),
    )


class OrgTaskTemplate(EntityMixin, OrgScoped, _TemplateColumns, Base):
    """An org's own template: written from scratch or duplicated from a global one, by an org editor
    (`templates:edit`) while the org's plan has "Templates (org)" on. Hidden, never deleted, while it is off."""

    __tablename__ = "org_task_templates"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_org_task_templates_org_slug"),
        *_template_checks(__tablename__),
        Index("ix_org_task_templates_tsv", "tsv", postgresql_using="gin"),
    )


class TaskTemplateAsset(Base):
    """An uploaded image of a GLOBAL template: its card image or one pasted into its body (`filename` is the
    body's `attachment:<filename>` token). An org template's images are `task_attachments` rows on
    `org_template_id` instead. The bytes are S3's, shared by key with every task started from the template."""

    __tablename__ = "task_template_assets"
    __table_args__ = (_in_check("kind", ASSET_KINDS, "ck_task_template_assets_kind"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    s3_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    s3_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    uploaded_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )


class TaskTemplateFavorite(OrgScoped, Base):
    """One member's star on one template, global or their org's. Exactly one of the two ids; deleting the
    template takes the star with it."""

    __tablename__ = "task_template_favorites"
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint("num_nonnulls(template_id, org_template_id) = 1", name="ck_task_template_favorites_one_template"),
        Index(
            "uq_task_template_favorites_user_template", "user_id", "template_id", unique=True,
            postgresql_where=text("template_id IS NOT NULL"),
        ),
        Index(
            "uq_task_template_favorites_user_org_template", "user_id", "org_template_id", unique=True,
            postgresql_where=text("org_template_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    template_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_templates.id", ondelete="CASCADE"), nullable=True, index=True
    )
    org_template_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("org_task_templates.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = timestamp_column()


class TaskTemplateStartDay(Base):
    """How many tasks people started from a global template on one day (PRD D8, I-2: never an automation's). No
    `org_id`: a global template's popularity sums every org without a cross-tenant read."""

    __tablename__ = "task_template_start_days"
    __table_args__ = (PrimaryKeyConstraint("template_id", "day", name="pk_task_template_start_days"),)

    template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_templates.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    starts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class OrgTaskTemplateStartDay(OrgScoped, Base):
    """`TaskTemplateStartDay` for an org's own template. An `id` of its own, as every `Stored` row's composite
    target (`org_unique`); the bump's upsert targets the unique pair."""

    __tablename__ = "org_task_template_start_days"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_template_id", "day", name="uq_org_task_template_start_days_template_day"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("org_task_templates.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    starts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
