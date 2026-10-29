"""`vnode_hosts.desired_polled_at`: when a host's converge loop last asked for its nodes

A host whose converge pass is stuck in one node's setup still heartbeats; this stamp is what tells the host page so.
Additive and nullable: an older release neither writes nor reads it.

Revision ID: 202610280000
Revises: 202610272300
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610280000"
down_revision: Union[str, None] = "202610272300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vnode_hosts", sa.Column("desired_polled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("vnode_hosts", "desired_polled_at")
