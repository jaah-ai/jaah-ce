"""`nodes.distro_id`, `nodes.distro_version`, `nodes.host_features`: what a Linux node reports it runs and can do

A Linux node's `/etc/os-release` `ID`/`VERSION_ID` and its probed host features (`{feature: bool}`), sensed from the
observe pass's `platform` block (`app/fleet_observe.py`) and read by `app/node_platform.py`'s host-fact rules. Additive
and nullable: NULL is "never reported", which restricts nothing, and an older release neither writes nor reads them.

Revision ID: 202610281700
Revises: 202610281600
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "202610281700"
down_revision: Union[str, None] = "202610281600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("distro_id", sa.Text(), nullable=True))
    op.add_column("nodes", sa.Column("distro_version", sa.Text(), nullable=True))
    op.add_column("nodes", sa.Column("host_features", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "host_features")
    op.drop_column("nodes", "distro_version")
    op.drop_column("nodes", "distro_id")
