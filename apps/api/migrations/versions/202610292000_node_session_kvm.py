"""`nodes.session_kvm`: a co-located node's session user may open the host's `/dev/kvm`

The host's owner turns it on per node on a Multi-Node Host (`PUT /api/nodes/{id}`); the host helper reads it from
`GET /api/agent/vnode-host/desired`. Additive: an older release neither writes nor reads the column (the default
covers its inserts).

Revision ID: 202610292000
Revises: 202610291800
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610292000"
down_revision: Union[str, None] = "202610291800"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "nodes", sa.Column("session_kvm", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )


def downgrade() -> None:
    op.drop_column("nodes", "session_kvm")
