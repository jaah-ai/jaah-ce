"""`user_mfa_factors.step_up_failures` + `step_up_last_failure_at`: a signed-in step-up's bad codes, counted apart

Wrong codes at a step-up (Delete account, Compliance Delete, the org switch — `app/mfa.py::verify_current_code`) no
longer spend the sign-in's tries. Additive with a server default: an older release neither writes nor reads them.

Revision ID: 202610280200
Revises: 202610280100
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610280200"
down_revision: Union[str, None] = "202610280100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_mfa_factors", sa.Column("step_up_failures", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("user_mfa_factors", sa.Column("step_up_last_failure_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("user_mfa_factors", "step_up_last_failure_at")
    op.drop_column("user_mfa_factors", "step_up_failures")
