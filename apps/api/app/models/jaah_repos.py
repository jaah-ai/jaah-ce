"""A repository Jaah made for a project in its own GitHub org (`app/jaah_repos.py`)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import _in_check, Base, EntityMixin, org_unique, OrgScoped

#: `web_app`: an Easy Mode hosted app's repo (its `hosted_apps` row); `plain`: a private repo with no hosting.
JAAH_REPO_KINDS = ("web_app", "plain")
#: `active` serves a write login; `migrating`/`move_failed` a read-only one while its project moves to a repo of its
#: own; `retiring`/`archived` none.
JAAH_REPO_STATES = ("active", "migrating", "move_failed", "retiring", "archived")


class JaahRepo(EntityMixin, OrgScoped, Base):
    """One Jaah-made repository. `name` is its `owner/repo` in the Easy Mode GitHub org; `project_id` goes NULL when the
    project is deleted (the row stays the repo's record). At most one row that is not `archived` per project."""

    __tablename__ = "jaah_repos"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", JAAH_REPO_KINDS, "ck_jaah_repos_kind"),
        _in_check("state", JAAH_REPO_STATES, "ck_jaah_repos_state"),
        Index("uq_jaah_repos_project_id", "project_id", unique=True, postgresql_where=text("state <> 'archived'")),
    )

    project_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    repo_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    migration_task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    integration_branch: Mapped[str] = mapped_column(Text, nullable=False)
    source_head_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Failed `retiring` passes in a row (`app/jaah_repo_retire.py`): the operator is paged once at its limit.
    retire_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
