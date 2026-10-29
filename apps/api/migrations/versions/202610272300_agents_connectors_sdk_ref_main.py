"""Agents registered from the connectors or SDK repository on `develop` or `v1` follow `main`

`jaah-ai/agent-connectors` and `jaah-ai/agent-sdk` now publish to `main` alone: their `develop` branch and `v1` tag
no longer move, so an agent still pinned to either would never see a new release. Each such row's `ref` becomes
`main`; its `resolved_commit` and bundle stay and keep serving until the next refresh resolves `main`. The repository
is matched however its URL was entered (https or ssh, with or without `.git` or a trailing slash, any case).
Idempotent: a rewritten row's `ref` is `main` and matches nothing. Downgrade is a no-op: the old ref is a data choice
this revision does not record, and neither old ref would move again.

Revision ID: 202610272300
Revises: 202610272100
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610272300"
down_revision: Union[str, None] = "202610272100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres ARE, case-insensitive (`~*`): the bare `owner/repo`, or it after a GitHub host and `/` or `:`.
REPO_PATTERN = r"^(?:(?:https?://|ssh://)?(?:git@)?(?:www\.)?github\.com[/:])?jaah-ai/agent-(?:connectors|sdk)(?:\.git)?/?$"
OLD_REFS = ("develop", "v1")


def upgrade() -> None:
    # The pattern is bound, not inlined: `text()` would read its `(?:https` as a bind parameter. Typed, so an offline
    # (`--sql`) run renders it as a literal.
    refs = ", ".join(f"'{r}'" for r in OLD_REFS)
    op.execute(sa.text(f"UPDATE agents SET ref = 'main' WHERE ref IN ({refs}) AND btrim(repo_url) ~* :pattern")
               .bindparams(sa.bindparam("pattern", REPO_PATTERN, type_=sa.String)))


def downgrade() -> None:
    pass
