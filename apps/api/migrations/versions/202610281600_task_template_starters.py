"""Seed the 20 starter task templates (`app/task_template_starters.py`), published, each with its built-in image

`ON CONFLICT DO NOTHING`, on either key: a re-run inserts nothing, a starter the operator edited keeps the edit, and an
operator's own global template that already holds a starter's slug keeps it (that starter is skipped).

Additive: data only, into a table an older release does not read.

Revision ID: 202610281600
Revises: 202610281500
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "202610281600"
down_revision: Union[str, None] = "202610281500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.task_template_starters import STARTERS

    conn = op.get_bind()
    for starter in STARTERS:
        conn.execute(
            text(
                "INSERT INTO task_templates "
                "(id, name, slug, summary, purpose, title, body, persona, autonomous, image_key, published) "
                "VALUES (:id, :name, :slug, :summary, :purpose, :title, :body, :persona, :autonomous, :image_key, true) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": starter.id,
                "name": starter.name,
                "slug": starter.slug,
                "summary": starter.summary,
                "purpose": starter.purpose,
                "title": starter.title,
                "body": starter.body,
                "persona": starter.persona,
                "autonomous": starter.autonomous,
                "image_key": f"builtin:{starter.slug}",
            },
        )


def downgrade() -> None:
    from app.task_template_starters import STARTERS

    op.get_bind().execute(
        text("DELETE FROM task_templates WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": [s.id for s in STARTERS]}
    )
