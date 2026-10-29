"""`projects.default_persona`: the persona a project's new tasks start as

NULL = the org's "Persona" default, as before. Additive and nullable: an older release neither writes nor reads it.

Revision ID: 202610280300
Revises: 202610280200
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610280300"
down_revision: Union[str, None] = "202610280200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("default_persona", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "default_persona")
