"""Nodes: `hypervisor` and `microvm_prereq`, the MicroVM OS dimension

What a node's `platform` block now reports about running MicroVMs off Linux: which hypervisor (`kvm`, `kvm-wsl2`,
`vz`) and, on Windows or macOS, the first unmet prerequisite (`app/microvm_mode.py::PREREQ_SENTENCES`). NULL = never
reported.

EXPAND ONLY: two new nullable columns the outgoing release never reads.

Revision ID: 202610252210
Revises: 202610251700
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610252210"
down_revision: Union[str, None] = "202610251700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("hypervisor", sa.Text(), nullable=True), if_not_exists=True)
    op.add_column("nodes", sa.Column("microvm_prereq", sa.Text(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    op.drop_column("nodes", "microvm_prereq")
    op.drop_column("nodes", "hypervisor")
