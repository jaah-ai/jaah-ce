"""`project_repos`: one more pass of the primary-row backfill, closing the overlap window

Until this revision every `project_repos` write first adopted a `projects.repo_url` that had no row
(`app/project_repos.py::adopt_legacy`): the outgoing release of the 2026-10-15 deploy still wrote the columns
without a row, so a project it touched during the overlap would otherwise have no primary. That shim is now
deleted, so the backfill of `202610151400` runs once more for any such project, restated here because
`projects.has_repo` has since gone: a project has a repository exactly when `repo_url` is set. Idempotent
(`ON CONFLICT DO NOTHING`), so on a tree already covered it inserts nothing.

EXPAND ONLY: inserts rows the outgoing release already reads; no schema change.

Revision ID: 202610260530
Revises: 202610252247
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610260530"
down_revision: Union[str, None] = "202610252247"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _label(repo_url: str, forge: str | None, forge_base_url: str | None) -> str:
    """The forge path, as `app/project_repos.py::display_name` derives it; a URL that no longer parses keeps the URL."""
    from app.forges import ForgeUrlError, parse_repo_url

    try:
        return parse_repo_url(repo_url, forge, forge_base_url).path
    except ForgeUrlError:
        return repo_url.strip()


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    connection = op.get_bind()
    projects = connection.execute(sa.text(
        "SELECT id, org_id, repo_url, forge, forge_base_url, integration_branch FROM projects "
        "WHERE repo_url IS NOT NULL AND btrim(repo_url) <> '' AND system_kind IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM project_repos r WHERE r.project_id = projects.id)"
    )).all()
    for row in projects:
        connection.execute(
            sa.text(
                "INSERT INTO project_repos (org_id, project_id, name, repo_url, forge, forge_base_url, "
                "integration_branch, is_primary, position) "
                "VALUES (:org_id, :project_id, :name, :repo_url, :forge, :forge_base_url, :branch, true, 0) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "org_id": row.org_id, "project_id": row.id,
                "name": _label(row.repo_url, row.forge, row.forge_base_url),
                "repo_url": row.repo_url, "forge": row.forge, "forge_base_url": row.forge_base_url,
                "branch": row.integration_branch,
            },
        )


def downgrade() -> None:
    # The rows are what the writer would have adopted on the next write; nothing to take back.
    pass
