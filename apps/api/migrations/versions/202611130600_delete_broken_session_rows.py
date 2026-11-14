"""Delete the broken-session incident rows

The control plane no longer files a hidden `adhoc` Task per broken runner session (the cause rides on the
session row, Audit › Sessions). This deletes the rows it filed: `adhoc` Tasks whose body carries the
`<!-- sysdedup:runner-break:<cause>:<task> -->` marker, hidden or not. Every foreign key into `tasks` cascades or nulls, so
the rows' versions, events and links go with them.

Data only, no schema change. Not reversible: the downgrade is a no-op. A row the outgoing release files while
the two releases overlap survives this delete; that handful is accepted.

Revision ID: 202611130600
Revises: 202611130500
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202611130600"
down_revision: Union[str, None] = "202611130500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM tasks WHERE persona = 'adhoc' AND body LIKE '%<!-- sysdedup:runner-break:%'"
    )


def downgrade() -> None:
    pass
