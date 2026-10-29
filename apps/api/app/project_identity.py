"""The one place a project identity is read, and rendered back to a name.

Three roles, one boundary that splits them:

  * IDENTITY — joins, keys, FKs, dict keys, bus payloads: ``projects.id`` (int). Never changes.
  * PLACEMENT — filesystem paths, compose stack names, log filenames: ``<org>/<repo>`` from ``repo_url``.
  * DISPLAY — console lists, tmux windows, log lines, notifications: ``projects.name``. Freely editable.

Inbound, a project arrives as its id; a name never resolves to a row (names are unique per org, not
fleet-wide), except the agent-plane rollout fallback :func:`legacy_project_id`. Outbound, an id is read
back to a name through a join, never a stored copy. Internal row/hold/intent dicts carry both
``{"project_id": 3, "project": "acme"}``: filter on ``project_id``, render ``project``, never the reverse.
"""
from __future__ import annotations

from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.projects import Project
from app.entity_shape import get_or_404


def project_or_404(session: Session, project_id: int, detail: Optional[str] = None) -> Project:
    """The project, or 404 (`unknown project {id}` unless `detail` says otherwise): nonexistent or in
    another org, indistinguishably.

    `get_or_404`, never `session.get`: the tenancy seam hooks the SELECT, and a primary-key `get` reads
    across the org boundary (the tenancy bypass census lists each such site)."""
    return get_or_404(session, Project, project_id, detail=detail or f"unknown project {project_id}")


def legacy_project_id(session: Session, name: str, org_id: Optional[UUID]) -> Optional[int]:
    """``projects.id`` for ``name`` inside ``org_id``, else None — the agent-plane rollout fallback only.

    A daemon older than ``project_id`` on the wire still reports a project by name; names are unique per
    org (``uq_projects_org_name``), not fleet-wide, so the calling node's org is what makes the name mean
    one row. No org (an unstamped node) resolves nothing: fail closed. Exact and case-sensitive: a fuzzy
    match silently mutates the wrong project's slot. Nothing else resolves a name to a row."""
    if not name or org_id is None:
        return None
    return session.scalar(select(Project.id).where(Project.org_id == org_id, Project.name == name))


def name_expr():
    """A correlated scalar subquery selecting ``projects.name`` for the outer row's ``project_id``.

    Prefer a plain ``join(Project, ...)`` where possible; this is for readers whose FROM clause is
    load-bearing (a partial-index scan, an ``ON CONFLICT`` target, ``app/slot_state.py``)."""
    return select(Project.name)


def names_by_id(session: Session, project_ids: Iterable[int]) -> dict[int, str]:
    """``{id: name}`` for the ids given, in one query.

    An id with no row is absent from the result; no placeholder is invented — the caller decides how to
    render a deleted project."""
    ids = {int(pid) for pid in project_ids if pid is not None}
    if not ids:
        return {}
    return {
        int(row.id): str(row.name)
        for row in session.execute(select(Project.id, Project.name).where(Project.id.in_(ids))).all()
    }


def label(session: Session, project_id: int) -> str:
    """Display name for one ``project_id``, for a log line or an error string.

    One row per call, never on the per-tick hot path (bulk readers use :func:`names_by_id`). The
    ``project#<id>`` fallback is NOT a name: it says the id resolved to nothing."""
    return names_by_id(session, [project_id]).get(project_id, f"project#{project_id}")
