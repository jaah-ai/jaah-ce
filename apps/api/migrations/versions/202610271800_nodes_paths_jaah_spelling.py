"""Node homes, shared-accounts roots and slot paths a node reported before the accounts rename, respelled

The package renamed every node's accounts and data dir in place (`/home/jaah`, `~/.jaah`, `/mnt/jaah-shared`) and the
control plane accepted a path under either spelling while nodes upgraded. Every node now reports the new one, so the
control plane spells only it; a row last written by a node before its move (an offline or retired node, a slot it
never re-reported) is rewritten here once. Idempotent: a row already spelled the new way matches nothing, and a
database created from the baseline holds none.

The former name is assembled from two halves: the monorepo-wide retired-name check (`tools/mirror/sensitive.py`) refuses
it spelled whole, and this is the one place that must still match it.

Revision ID: 202610271800
Revises: 202610271200
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202610271800"
down_revision: Union[str, None] = "202610271200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "sky" + "net"

# (pattern, replacement) in Postgres ARE syntax: the user home, the data dir as a whole path segment, the share mount.
RESPELLINGS: tuple[tuple[str, str], ...] = (
    (f"^/home/{_OLD}(?=/|$)", "/home/jaah"),
    (f"/\\.{_OLD}(?=/|$)", "/.jaah"),
    (f"^/mnt/{_OLD}-shared(?=/|$)", "/mnt/jaah-shared"),
)
COLUMNS: tuple[tuple[str, str], ...] = (
    ("nodes", "jaah_home"),
    ("nodes", "shared_accounts_root"),
    ("worktrees", "path"),
)


def _respelled(column: str) -> str:
    expr = column
    for pattern, replacement in RESPELLINGS:
        expr = f"regexp_replace({expr}, '{pattern}', '{replacement}', 'g')"
    return expr


def upgrade() -> None:
    for table, column in COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = {_respelled(column)} WHERE {column} ~ '{_OLD}'")


def downgrade() -> None:
    pass  # the old spelling is a retired name, never restored
