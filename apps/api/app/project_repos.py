"""A project's repositories: the single writer of `project_repos` and of the seven `projects` columns
that project its primary.

A project connects any number of repositories (no cap, no flag); exactly one is primary whenever it has any. The
primary is today's repo: `projects.repo_url/forge/forge_base_url/integration_branch` and its Branch policy
(`POLICY`) equal the primary row's, and `sync_primary` is the ONLY code that writes them
(`test_project_repo_projection_sites.py` holds `app/` to that; `tests/test_project_repos.py` proves the projection
over every write path). With no row, `repo_url/forge/forge_base_url` are NULL and `integration_branch` keeps its
last value, since the column is NOT NULL and the Edit form still shows it.

A repository's Branch policy follows the repository, not the row id: in a whole-set write every row's prior is
the policy of the stored row that names the same repository, whatever its id (`prior_policies`), so dropping and
re-adding, repointing a row onto, or trading URLs with a read-only repository never loosens it. Only a repository
no stored row names inherits the project's values; removing one whose policy differs from that is authoring it
(the route's governance check, `removed_repos`), so a delete-then-re-add across two writes cannot reset it either.

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

#: Each repository's Branch policy, projected from the primary too. With no row the project's own columns stand.
POLICY = ("branch_prefix", "push_enabled", "allow_force_push")


@dataclass(frozen=True)
class RepoSpec:
    """One validated row of a whole-set write: `id` None inserts, an id updates that row."""

    id: Optional[int]
    repo_url: str
    forge: Optional[str]
    forge_base_url: Optional[str]
    integration_branch: str
    is_primary: bool
    # The Branch policy, normalised; None takes `prior_policies`' (the stored policy of the same repository,
    # whichever row held it, else the project's prefix and push flag, force push off).
    branch_prefix: Optional[str] = None
    push_enabled: Optional[bool] = None
    allow_force_push: Optional[bool] = None


def inherited_policy(project: Project) -> dict[str, Any]:
    """What a new row takes for each `POLICY` field it does not name."""
    return {"branch_prefix": project.branch_prefix, "push_enabled": project.push_enabled, "allow_force_push": False}


def display_name(repo_url: str, forge: Optional[str], forge_base_url: Optional[str]) -> str:
    """`owner/name` (the forge path) for a URL that parses, else the URL itself, so a row always has a label."""
    try:
        return forges.parse_repo_url(repo_url, forge, forge_base_url).path
    except forges.ForgeUrlError:
        return repo_url.strip()


def url_key(repo_url: str) -> str:
    """What `uq_project_repos_project_url` compares: `lower(btrim(repo_url))`."""
    return repo_url.strip().lower()


def repo_keys(url: str, forge: Optional[str], forge_base_url: Optional[str]) -> set[str]:
    """What makes two URLs the same repository (AS3): the stored text, case- and space-folded, and the forge root
    plus path when it parses (`.git`, scheme and host case aside)."""
    keys = {url_key(url)}
    try:
        ref = forges.parse_repo_url(url, forge, forge_base_url)
        keys.add(f"{ref.root}/{ref.path}".lower())
    except forges.ForgeUrlError:
        pass
    return keys


def _same_repo(url: str, row: ProjectRepo) -> bool:
    """Whether `url` names `row`'s repository, read under the row's own forge so the answer never turns on what a
    request did or did not declare (the route's authority check and `replace` must agree)."""
    return bool(repo_keys(url, row.forge, row.forge_base_url) & repo_keys(row.repo_url, row.forge, row.forge_base_url))


def prior_policies(
    stored: list[ProjectRepo], rows: list[tuple[Optional[int], str]], inherited: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Each `(id, repo_url)` row's Branch policy before its own `POLICY` values apply: the policy of the stored row
    that names the same repository (the row's own when it still does, else any other), whatever the row's id; a
    repository no stored row names takes `inherited`."""
    by_id = {row.id: row for row in stored}
    out: list[dict[str, Any]] = []
    for row_id, url in rows:
        own = by_id.get(row_id) if row_id is not None else None
        source = own if own is not None and _same_repo(url, own) else next(
            (row for row in stored if _same_repo(url, row)), None,
        )
        out.append({key: getattr(source, key) for key in POLICY} if source is not None else dict(inherited))
    return out


def removed_repos(stored: list[ProjectRepo], urls: list[str]) -> list[ProjectRepo]:
    """The stored rows whose repository no URL of the final set names: what the write removes."""
    return [row for row in stored if not any(_same_repo(url, row) for url in urls)]


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
    """Write the projection: the `PROJECTED` and `POLICY` columns from the primary row, in the caller's transaction.

    With no row, `repo_url/forge/forge_base_url` are cleared, `POLICY` keeps the project's own values, and
    `integration_branch` keeps its value, or takes `branch_without_repo` (a legacy write naming only the branch of a
    project with no repository)."""
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
    for key in POLICY:
        setattr(project, key, getattr(primary, key))


def set_primary_policy(
    session: Session, project: Project, values: Mapping[str, Any], actor_id: Optional[UUID]
) -> None:
    """A project-level `POLICY` write (the legacy `branch_prefix`/`push_enabled` body keys): applied to the PRIMARY
    row, then the projection. With no row the caller's write to the project's own columns stands."""
    primary = session.scalar(
        select(ProjectRepo).where(ProjectRepo.project_id == project.id, ProjectRepo.is_primary.is_(True))
    )
    if primary is None:
        return
    for key, value in values.items():
        setattr(primary, key, value)
    primary.updated_at = func.now()
    _stamp(primary, actor_id, created=False)
    sync_primary(session, project)


def _stamp(row: ProjectRepo, actor_id: Optional[UUID], *, created: bool) -> None:
    """Attribute a write to `actor_id`; None (a seed or migration-time path) leaves the system actor default."""
    if actor_id is None:
        return
    if created:
        row.created_by_id = actor_id
    row.updated_by_id = actor_id


def replace(session: Session, project: Project, specs: list[RepoSpec], actor_id: Optional[UUID]) -> None:
    """Make `specs` the project's whole set: rows named by id are updated, rows without one inserted, every other
    row deleted; then the projection. A `POLICY` key a spec omits stores its repository's prior (`prior_policies`).
    `specs` is validated (ids belong to this project, one primary when non-empty, no duplicate URL).

    Updates run in two flushes so a primary moving between rows, or two rows trading URLs, never passes through
    a state the unique indexes refuse."""
    stored = repos_of(session, project.id)
    existing = {row.id: row for row in stored}
    # Before any delete: each repository keeps the policy of the stored row naming it (`prior_policies`).
    priors = prior_policies(stored, [(spec.id, spec.repo_url) for spec in specs], inherited_policy(project))
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
    for position, (spec, prior) in enumerate(zip(specs, priors, strict=True)):
        values: dict[str, Any] = dict(
            prior,
            name=display_name(spec.repo_url, spec.forge, spec.forge_base_url),
            repo_url=spec.repo_url, forge=spec.forge, forge_base_url=spec.forge_base_url,
            integration_branch=spec.integration_branch, is_primary=spec.is_primary, position=position,
        )
        values.update({key: getattr(spec, key) for key in POLICY if getattr(spec, key) is not None})
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
            integration_branch=project.integration_branch, **inherited_policy(project),
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
