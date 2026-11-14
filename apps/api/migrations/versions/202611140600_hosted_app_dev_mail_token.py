"""`hosted_apps.dev_mail_token_hash`: the dev stage's mail token, registered so its mail is relayed

An app's dev stage (`<slug>--dev`) had its own `JAAH_MAIL_TOKEN` that the control plane never recorded, so every
email it sent (a sign-up confirmation included) was dropped while the app told the person it was sent. The column
holds that token's SHA-256, as `mail_token_hash` does live's. Additive and nullable: the hourly converge fills it in
for each existing dev stage (`app/hosted_apps/service.py::converge_mail_env`), new dev stages get it when made.

Revision ID: 202611140600
Revises: 202611140500
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140600"
down_revision: Union[str, Sequence[str], None] = "202611140500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hosted_apps", sa.Column("dev_mail_token_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosted_apps", "dev_mail_token_hash")
