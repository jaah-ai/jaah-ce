"""Allow the approval rules' deploy-notice kinds

Widens `ck_release_notices_kind` to the model's `NOTICE_KINDS` (`app/models/releases.py`): the approval reminder,
the approval timeout and the rejection a step's approval rules send (`app/releases/notify.py`). Additive: the
previous release writes only kinds the check still allows. The downgrade deletes those notices and narrows it again.

Revision ID: 202611131400
Revises: 202611131330
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611131400"
down_revision: Union[str, Sequence[str], None] = "202611131330"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAME = "ck_release_notices_kind"
# Frozen copies: a migration never imports the live model.
_KINDS = ("release_approval", "release_live", "release_failed", "release_rolled_back")
_ADDED = ("release_approval_timeout", "release_approval_reminder", "release_rejected")


def _check(kinds: Sequence[str]) -> None:
    op.drop_constraint(_NAME, "release_notices", type_="check")
    op.create_check_constraint(_NAME, "release_notices", "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")")


def upgrade() -> None:
    _check((*_KINDS, *_ADDED))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM release_notices WHERE kind = ANY(:kinds)").bindparams(kinds=list(_ADDED)))
    _check(_KINDS)
