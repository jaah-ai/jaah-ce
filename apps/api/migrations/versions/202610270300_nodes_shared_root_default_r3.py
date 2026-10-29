"""`nodes.shared_accounts_root` defaults to the accounts share's mount point, `/mnt/jaah-shared/accounts`

The package moved the accounts share's mount point to `/mnt/jaah-shared` on every node; a row registered from now on is
seeded with it (`app/shared_storage.py::LEGACY_SHARED_ACCOUNTS_ROOT`, the model's default). The baseline creates the
column with this default already, so on a database built from it this revision changes nothing.

DEFAULT ONLY, NO DATA REWRITE. `shared_accounts_root`, `nodes.jaah_home` and `worktrees.path` are each the node's own
report (the observe pass and the config snapshot); a row a node wrote before its move is respelled by
`202610271800_nodes_paths_jaah_spelling.py`.

Revision ID: 202610270300
Revises: 202610262100
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610270300"
down_revision: Union[str, None] = "202610262100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW = "/mnt/jaah-shared/accounts"


def upgrade() -> None:
    op.alter_column("nodes", "shared_accounts_root", server_default=sa.text(f"'{_NEW}'::text"))


def downgrade() -> None:
    pass  # the previous revision's default is this one too (the baseline's)
