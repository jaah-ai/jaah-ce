"""Task templates: the global and org libraries, their images, stars and start counters, and the task seam

* `task_templates` — the operator's global templates (`Shared`); `org_task_templates` — an org's own (`Stored`,
  unique `(org_id, slug)`). Both carry `app/models/task_templates.py::_TemplateColumns`, with a generated,
  GIN-indexed `tsv` for the gallery search.
* `task_template_assets` — a global template's images; an org template's are `task_attachments` rows on the new
  subject `org_template_id` (its composite org key cascades, its org-inherit trigger learns the new path).
* `task_template_favorites` — one member's stars; `task_template_start_days` (no org) and
  `org_task_template_start_days` — people's starts per template per day ("Most popular").
* `tasks.template_id` / `tasks.org_template_id` / `tasks.template_name` — the template a task was started from and its
  name, kept after a delete. The org key is `ON DELETE SET NULL (org_template_id)`, so `org_id` is never nulled.
* The "Templates (org)" plan switch (`billing_plans.org_templates_enabled`, OFF; `orgs.org_templates_enabled_override`)
  and the `templates:*` permission keys, every one granted to the two `ALL_KEYS` roles (Org Owner, Org Admin).

Additive: an older release reads none of these and writes nothing they constrain.

Revision ID: 202610281500
Revises: 202610281400
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202610281500"
down_revision: Union[str, None] = "202610281400"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TS = postgresql.TIMESTAMP(timezone=True)
# `app/entity_shape.py::SYSTEM_ACTOR_ID`, frozen here: a revision never reads a live constant.
_SYSTEM_ACTOR = "'01900000-0000-7000-8000-000000000001'::uuid"
# `app/models/task_templates.py::PURPOSES` as it stands today, in gallery order, with labels.
_PURPOSES = (
    ("build_fix", "Build & fix"),
    ("quality", "Quality"),
    ("security", "Security"),
    ("maintenance", "Maintenance"),
    ("docs_release", "Docs & release"),
    ("research_planning", "Research & planning"),
)
_PURPOSE_LABEL = "CASE purpose " + " ".join(f"WHEN '{k}' THEN '{label}'" for k, label in _PURPOSES) + " ELSE '' END"
# `app/models/task_templates.py::TSV_EXPRESSION`.
_TSV = (
    "setweight(to_tsvector('english'::regconfig, name), 'A') || "
    "setweight(to_tsvector('english'::regconfig, summary), 'B') || "
    f"setweight(to_tsvector('english'::regconfig, {_PURPOSE_LABEL}), 'B') || "
    "setweight(to_tsvector('english'::regconfig, body), 'C')"
)
# `app/models/task_templates.py::MAX_NAME` / `MAX_SUMMARY`.
_MAX_NAME, _MAX_SUMMARY = 80, 120
# `authz.permission_description` for the `templates` feature ("task templates").
_PERMISSIONS = (
    ("templates:view", "View task templates."),
    ("templates:create", "Create task templates."),
    ("templates:edit", "Edit task templates."),
    ("templates:delete", "Delete task templates."),
)
# `app/access_catalog.py`: the two `ALL_KEYS` roles (Org Owner, Org Admin) take every key.
_ALL_KEYS_ROLES = ("01900000-0000-7000-8000-000000000020", "01900000-0000-7000-8000-000000000021")
# `task_attachments`' subjects, the new one last (`app/models/task_extras.py::TaskAttachment`).
_ATTACHMENT_SUBJECTS = "num_nonnulls(task_id, ticket_id, conversation_id, automation_id, org_template_id) = 1"
_ATTACHMENT_SUBJECTS_BEFORE = "num_nonnulls(task_id, ticket_id, conversation_id, automation_id) = 1"
# Its org-inherit derivation, in the manifest's order (`app/tenancy/manifest.py::TaskAttachment`).
_ATTACHMENT_INHERIT = (
    ("tasks", "task_id"), ("tickets", "ticket_id"), ("support_conversations", "conversation_id"),
    ("automations", "automation_id"), ("org_task_templates", "org_template_id"),
)


def _template_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", sa.UUID(), server_default=sa.text(_SYSTEM_ACTOR), nullable=False),
        sa.Column("updated_by_id", sa.UUID(), server_default=sa.text(_SYSTEM_ACTOR), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("autonomous", sa.Boolean(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("image_key", sa.Text(), nullable=True),
        sa.Column("featured", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("published", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tsv", postgresql.TSVECTOR(), sa.Computed(_TSV, persisted=True), nullable=True),
    ]


def _template_constraints(table: str) -> list[sa.Constraint]:
    return [
        sa.CheckConstraint(
            "purpose IN (" + ", ".join(f"'{key}'" for key, _label in _PURPOSES) + ")", name=f"ck_{table}_purpose"
        ),
        sa.CheckConstraint(f"char_length(name) <= {_MAX_NAME}", name=f"ck_{table}_name_length"),
        sa.CheckConstraint(f"char_length(summary) <= {_MAX_SUMMARY}", name=f"ck_{table}_summary_length"),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["admin_users.id"], name=f"fk_{table}_created_by_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["admin_users.id"], name=f"fk_{table}_updated_by_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]


def upgrade() -> None:
    from app.entity_shape import set_updated_at_trigger_sql
    from app.tenancy.inherit_trigger import org_inherit_trigger_sql

    op.create_table(
        "task_templates",
        *_template_columns(),
        *_template_constraints("task_templates"),
        sa.UniqueConstraint("slug", name="uq_task_templates_slug"),
    )
    op.create_index("ix_task_templates_tsv", "task_templates", ["tsv"], postgresql_using="gin")
    op.execute(set_updated_at_trigger_sql("task_templates"))

    op.create_table(
        "org_task_templates",
        sa.Column("org_id", sa.UUID(), nullable=False),
        *_template_columns(),
        *_template_constraints("org_task_templates"),
        sa.UniqueConstraint("id", "org_id", name="uq_org_task_templates_id_org_id"),
        sa.UniqueConstraint("org_id", "slug", name="uq_org_task_templates_org_slug"),
    )
    op.create_index("ix_org_task_templates_tsv", "org_task_templates", ["tsv"], postgresql_using="gin")
    op.execute(set_updated_at_trigger_sql("org_task_templates"))

    op.create_table(
        "task_template_assets",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("s3_bucket", sa.Text(), nullable=True),
        sa.Column("uploaded_at", _TS, nullable=True),
        sa.Column("uploaded_by_id", sa.UUID(), nullable=True),
        sa.CheckConstraint("kind IN ('card_image', 'body_image')", name="ck_task_template_assets_kind"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["task_templates.id"], name="fk_task_template_assets_template_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"], ["admin_users.id"], name="fk_task_template_assets_uploaded_by_id", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_template_assets"),
    )
    op.create_index("ix_task_template_assets_template_id", "task_template_assets", ["template_id"])
    op.create_index("ix_task_template_assets_s3_key", "task_template_assets", ["s3_key"])

    op.create_table(
        "task_template_favorites",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=True),
        sa.Column("org_template_id", sa.UUID(), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "num_nonnulls(template_id, org_template_id) = 1", name="ck_task_template_favorites_one_template"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["admin_users.id"], name="fk_task_template_favorites_user_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["task_templates.id"], name="fk_task_template_favorites_template_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_template_id"], ["org_task_templates.id"], name="fk_task_template_favorites_org_template_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_template_favorites"),
        sa.UniqueConstraint("id", "org_id", name="uq_task_template_favorites_id_org_id"),
    )
    op.create_index("ix_task_template_favorites_template_id", "task_template_favorites", ["template_id"])
    op.create_index("ix_task_template_favorites_org_template_id", "task_template_favorites", ["org_template_id"])
    op.create_index(
        "uq_task_template_favorites_user_template", "task_template_favorites", ["user_id", "template_id"],
        unique=True, postgresql_where=sa.text("template_id IS NOT NULL"),
    )
    op.create_index(
        "uq_task_template_favorites_user_org_template", "task_template_favorites", ["user_id", "org_template_id"],
        unique=True, postgresql_where=sa.text("org_template_id IS NOT NULL"),
    )

    op.create_table(
        "task_template_start_days",
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("starts", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"], ["task_templates.id"], name="fk_task_template_start_days_template_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("template_id", "day", name="pk_task_template_start_days"),
    )
    op.create_table(
        "org_task_template_start_days",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_template_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("starts", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["org_template_id"], ["org_task_templates.id"], name="fk_org_task_template_start_days_org_template_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_org_task_template_start_days"),
        sa.UniqueConstraint("id", "org_id", name="uq_org_task_template_start_days_id_org_id"),
        sa.UniqueConstraint("org_template_id", "day", name="uq_org_task_template_start_days_template_day"),
    )

    op.add_column("tasks", sa.Column("template_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("org_template_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("template_name", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_template_id", "tasks", "task_templates", ["template_id"], ["id"], ondelete="SET NULL"
    )
    # The column-list form (Postgres 15+): a template delete nulls the link, never the task's org.
    op.create_foreign_key(
        "fk_tasks_org_template_id", "tasks", "org_task_templates", ["org_template_id", "org_id"], ["id", "org_id"],
        ondelete="SET NULL (org_template_id)",
    )
    op.create_index("ix_tasks_template_id", "tasks", ["template_id"], postgresql_where=sa.text("template_id IS NOT NULL"))
    op.create_index(
        "ix_tasks_org_template_id", "tasks", ["org_template_id"], postgresql_where=sa.text("org_template_id IS NOT NULL")
    )

    op.add_column("task_attachments", sa.Column("org_template_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_task_attachments_org_template_id", "task_attachments", "org_task_templates", ["org_template_id"], ["id"],
        ondelete="CASCADE",
    )
    # `app/tenancy/keys.py::add_sql`/`validate_sql` for the new derivation path, frozen.
    op.execute(
        "ALTER TABLE task_attachments ADD CONSTRAINT fk_task_attachments_org_template_id_org "
        "FOREIGN KEY (org_template_id, org_id) REFERENCES org_task_templates (id, org_id) ON DELETE CASCADE NOT VALID"
    )
    op.execute("ALTER TABLE task_attachments VALIDATE CONSTRAINT fk_task_attachments_org_template_id_org")
    op.create_index("ix_task_attachments_org_template_id", "task_attachments", ["org_template_id"])
    op.drop_constraint("ck_task_attachments_one_subject", "task_attachments", type_="check")
    op.create_check_constraint("ck_task_attachments_one_subject", "task_attachments", _ATTACHMENT_SUBJECTS)
    op.execute(
        org_inherit_trigger_sql("task_attachments", _ATTACHMENT_INHERIT).replace(
            "CREATE TRIGGER", "CREATE OR REPLACE TRIGGER", 1
        )
    )

    op.add_column(
        "billing_plans",
        sa.Column("org_templates_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=True),
    )
    op.add_column("orgs", sa.Column("org_templates_enabled_override", sa.Boolean(), nullable=True))

    conn = op.get_bind()
    for key, description in _PERMISSIONS:
        conn.execute(
            sa.text("INSERT INTO permissions (key, description) VALUES (:key, :description) ON CONFLICT (key) DO NOTHING"),
            {"key": key, "description": description},
        )
    conn.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.id = ANY(CAST(:roles AS uuid[])) AND p.key LIKE 'templates:%' ON CONFLICT DO NOTHING"
        ),
        {"roles": list(_ALL_KEYS_ROLES)},
    )


def downgrade() -> None:
    from app.tenancy.inherit_trigger import org_inherit_trigger_sql

    op.execute("SET lock_timeout = '3s'")
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE key LIKE 'templates:%')"
    )
    op.execute("DELETE FROM permissions WHERE key LIKE 'templates:%'")
    op.drop_column("orgs", "org_templates_enabled_override")
    op.drop_column("billing_plans", "org_templates_enabled")
    op.execute(
        org_inherit_trigger_sql("task_attachments", _ATTACHMENT_INHERIT[:-1]).replace(
            "CREATE TRIGGER", "CREATE OR REPLACE TRIGGER", 1
        )
    )
    op.execute("DELETE FROM task_attachments WHERE org_template_id IS NOT NULL")
    op.drop_constraint("ck_task_attachments_one_subject", "task_attachments", type_="check")
    op.create_check_constraint("ck_task_attachments_one_subject", "task_attachments", _ATTACHMENT_SUBJECTS_BEFORE)
    op.drop_column("task_attachments", "org_template_id")
    op.drop_index("ix_tasks_org_template_id", table_name="tasks")
    op.drop_index("ix_tasks_template_id", table_name="tasks")
    op.drop_constraint("fk_tasks_org_template_id", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_template_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "template_name")
    op.drop_column("tasks", "org_template_id")
    op.drop_column("tasks", "template_id")
    op.drop_table("org_task_template_start_days")
    op.drop_table("task_template_start_days")
    op.drop_table("task_template_favorites")
    op.drop_table("task_template_assets")
    op.drop_table("org_task_templates")
    op.drop_table("task_templates")
