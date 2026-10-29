"""A project's repositories: the single writer of `project_repos` and of the four `projects` columns
that project its primary.

A project connects any number of repositories (no cap, no flag); exactly one is primary whenever it has any. The
primary is today's repo: `projects.repo_url/forge/forge_base_url/integration_branch` equal the primary row's, and
`sync_primary` is the ONLY code that writes them (`test_project_repo_projection_sites.py` holds `app/` to that;
`tests/test_project_repos.py` proves the projection over every write path). With no row, `repo_url/forge/forge_base_url` are NULL and `integration_branch` keeps its last value, since
the column is NOT NULL and the Edit form still shows it.

Validation is the route's (`routes/projects.py::_validated_repos`); this module applies an already-validated set.
Nothing here commits: every write lands in the caller's transaction, so a later refusal saves nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import forges
from app.models.projects import Project, ProjectRepo

#: The `projects` columns that project the primary row, in `ProjectOut`'s order.
PROJECTED = ("repo_url", "forge", "forge_base_url", "integration_branch")


@dataclass(frozen=True)
class RepoSpec:
    """One validated row of a whole-set write: `id` None inserts, an id updates that row."""

    id: Optional[int]
    repo_url: str
    forge: Optional[str]
    forge_base_url: Optional[str]
    integration_branch: str
    is_primary: bool


def display_name(repo_url: str, forge: Optional[str], forge_base_url: Optional[str]) -> str:
    """`owner/name` (the forge path) for a URL that parses, else the URL itself, so a row always has a label."""
    try:
        return forges.parse_repo_url(repo_url, forge, forge_base_url).path
    except forges.ForgeUrlError:
        return repo_url.strip()


def url_key(repo_url: str) -> str:
    """What `uq_project_repos_project_url` compares: `lower(btrim(repo_url))`."""
    return repo_url.strip().lower()


def repos_by_project(session: Session, project_ids: Iterable[int]) -> dict[int, list[ProjectRepo]]:
    """Every listed project's rows in display order, in one select (a page of projects has no N+1)."""
    ids = set(project_ids)
    out: dict[int, list[ProjectRepo]] = {project_id: [] for project_id in ids}
    if not ids:
        return out
    rows = session.scalars(
        select(ProjectRepo).where(ProjectRepo.project_id.in_(ids))
        .order_by(ProjectRepo.project_id, ProjectRepo.position, ProjectRepo.id)
    )
    for row in rows:
        out[row.project_id].append(row)
    return out


def repos_of(session: Session, project_id: int) -> list[ProjectRepo]:
    return repos_by_project(session, [project_id])[project_id]


def sync_primary(session: Session, project: Project, *, branch_without_repo: Optional[str] = None) -> None:
    """Write the projection: the four `projects` columns from the primary row, in the caller's transaction.

    With no row, `repo_url/forge/forge_base_url` are cleared and `integration_branch` keeps its value, or takes
    `branch_without_repo` (a legacy write naming only the branch of a project with no repository)."""
    session.flush()
    primary = session.scalar(
        select(ProjectRepo).where(ProjectRepo.project_id == project.id, ProjectRepo.is_primary.is_(True))
    )
    if primary is None:
        project.repo_url = None
        project.forge = None
        project.forge_base_url = None
        if branch_without_repo is not None:
            project.integration_branch = branch_without_repo
        return
    project.repo_url = primary.repo_url
    project.forge = primary.forge
    project.forge_base_url = primary.forge_base_url
    project.integration_branch = primary.integration_branch


def _stamp(row: ProjectRepo, actor_id: Optional[UUID], *, created: bool) -> None:
    """Attribute a write to `actor_id`; None (a seed or migration-time path) leaves the system actor default."""
    if actor_id is None:
        return
    if created:
        row.created_by_id = actor_id
    row.updated_by_id = actor_id


def replace(session: Session, project: Project, specs: list[RepoSpec], actor_id: Optional[UUID]) -> None:
    """Make `specs` the project's whole set: rows named by id are updated, rows without one inserted, every
    other row deleted; then the projection. `specs` is validated (ids belong to this project, one primary when
    non-empty, no duplicate URL).

    Updates run in two flushes so a primary moving between rows, or two rows trading URLs, never passes through
    a state the unique indexes refuse."""
    existing = {row.id: row for row in repos_of(session, project.id)}
    kept = {spec.id for spec in specs if spec.id is not None}
    for row_id, row in existing.items():
        if row_id not in kept:
            session.delete(row)
    for spec in specs:
        if spec.id is None:
            continue
        row = existing[spec.id]
        row.is_primary = False
        if url_key(row.repo_url) != url_key(spec.repo_url):
            # Unique per project and never a real URL: frees the old value for another row in this flush.
            row.repo_url = f"jaah-moving://{row.id}"
    session.flush()
    for position, spec in enumerate(specs):
        values: dict[str, Any] = dict(
            name=display_name(spec.repo_url, spec.forge, spec.forge_base_url),
            repo_url=spec.repo_url, forge=spec.forge, forge_base_url=spec.forge_base_url,
            integration_branch=spec.integration_branch, is_primary=spec.is_primary, position=position,
        )
        if spec.id is None:
            row = ProjectRepo(project_id=project.id, org_id=project.org_id, **values)
            _stamp(row, actor_id, created=True)
            session.add(row)
            continue
        row = existing[spec.id]
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = func.now()
        _stamp(row, actor_id, created=False)
    sync_primary(session, project)


class PrimaryStillNeeded(ValueError):
    """A legacy write cleared the primary's URL while other repositories remain (AS4)."""


def upsert_primary(
    session: Session, project: Project, values: Mapping[str, Any], actor_id: Optional[UUID]
) -> None:
    """A legacy single-repo write (`repo_url`/`forge`/`forge_base_url`/`integration_branch`, no `repos`): applied to
    the PRIMARY row — updated, inserted when the project has none, deleted when the URL is cleared — then the
    projection. `values` holds validated, derived values for the keys the request named.

    Raises `PrimaryStillNeeded` for a cleared URL while other rows remain."""
    unknown = set(values) - set(PROJECTED)
    if unknown:
        raise ValueError(f"upsert_primary takes only {PROJECTED}, not {sorted(unknown)}")
    rows = repos_of(session, project.id)
    primary = next((row for row in rows if row.is_primary), None)
    repo_url = values["repo_url"] if "repo_url" in values else (primary.repo_url if primary else None)
    if not (repo_url or "").strip():
        if primary is not None:
            if len(rows) > 1:
                raise PrimaryStillNeeded("Pick another primary first")
            session.delete(primary)
        sync_primary(session, project, branch_without_repo=values.get("integration_branch"))
        return
    if primary is None:
        primary = ProjectRepo(
            project_id=project.id, org_id=project.org_id, is_primary=True,
            position=max((row.position for row in rows), default=-1) + 1,
            integration_branch=project.integration_branch,
        )
        _stamp(primary, actor_id, created=True)
        session.add(primary)
    else:
        primary.updated_at = func.now()
        _stamp(primary, actor_id, created=False)
    for key, value in values.items():
        setattr(primary, key, value)
    primary.name = display_name(primary.repo_url, primary.forge, primary.forge_base_url)
    sync_primary(session, project)
