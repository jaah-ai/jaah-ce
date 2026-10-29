"""Jira READS for the console's task form — the issue typeahead and one issue's text.

PROJECT-SCOPED because the form knows its project id and, on the create path, has no task id yet. A
project's `jira_credential_id` is the whole of its Jira config: the site and the optional project key
both live on the credential row it names.

NOT A JIRA PROXY, which is this file's SSRF argument. The caller supplies `q`, `limit` and an issue
key — there is no caller-supplied URL, host or JQL anywhere. The site comes from the credential the
project itself names, and every outbound bound (no redirects, a 64 KiB streamed cap, the origin pin)
plus all JQL validation lives in `app/jira_api.py`. Nothing here builds a request.

THAT ARGUMENT RESTS ON WHO MAY NAME THE CREDENTIAL, so it is only as strong as
`routes/projects.py::_validated_jira_credential`. A `jira` row's `config.base_url` is whatever its
creator typed and the registry spec puts no `require_https_base_url` on it, so a caller who could
attach a row THEY minted would be supplying the host after all, one hop earlier. That is precisely
what the attach-time key-AND-grant check refuses; read its note before loosening it.

The credential row is encrypted at rest and PLAINTEXT IN THIS PROCESS (`app/models/credentials.py::Credential`),
which is the half this rule is about: no token, no `Authorization` header and no whole row reaches a log
line, an exception message or a response body. `jira_api`'s messages
name a field LABEL or `site.host` and are safe to forward; the one arm that instead writes its own
prose is 401/403 (`_jira_failure`)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import jira_api
from app.auth import require_user
from app.authz.declarations import feature
from app.db import get_session
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.projects import Project
from app.entity_shape import get_or_404

router = APIRouter(prefix="/api/projects", tags=["jira"])

# ONE message for "no id set" and "the id names nothing": both are the same unfinished configuration
# from the operator's seat, and the fix is the same field on the same tab.
_NO_CREDENTIAL = (
    "This project has no Jira credential configured — set one on the project's General tab."
)


class JiraIssueOut(BaseModel):
    """One typeahead row. `status` is None when Jira's decoration call did not answer — best-effort
    in `jira_api.search_issues`, because a lookup that dies for want of a status chip is worse."""

    key: str
    summary: str
    status: Optional[str] = None


class JiraIssueDetailOut(BaseModel):
    """One issue as the form consumes it: `description` is markdown rendered from Jira's ADF, and is
    `""` — never a placeholder — for an issue with no description."""

    key: str
    summary: str
    description: str
    url: str


def _site(session: Session, project_id: int) -> jira_api.JiraSite:
    """This project's Jira site, or the 409 an operator can act on.

    A MISSING OR DANGLING `jira_credential_id` IS NEITHER A 500 NOR AN EMPTY LIST: an empty list
    would read as "Jira has no matching issues", a lie the operator would act on.
    `JiraCredentialInvalid` is the second 409 — its message names the unusable FIELD LABEL and never
    a stored value."""
    # THE GUARD NORMALLY ANSWERS THIS FIRST — `can_access` on a row that does not exist is False, so
    # an unknown id 404s in `_enforce_object_params` before the handler runs, superadmins included.
    # Kept because that is a property of the DECLARATION, not of this function.
    project = get_or_404(session, Project, project_id, detail="unknown project")
    credential = (
        session.get(Credential, project.jira_credential_id)
        if project.jira_credential_id is not None
        else None
    )
    if credential is None:
        raise HTTPException(status_code=409, detail=_NO_CREDENTIAL)
    try:
        return jira_api.site_from_credential(credential)
    except jira_api.JiraCredentialInvalid as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _jira_failure(exc: jira_api.JiraApiError) -> HTTPException:
    """A `JiraApiError` as the status the CALLER should see, mapped off `.status` alone.

    404 is forwarded with Jira's own wording, which does NOT claim the issue is absent — Jira
    conflates "no such issue" with "no permission" deliberately. 400 is the caller's own bad issue
    key. 401/403 becomes a 502 with OUR prose: it is our stored credential Jira refused, and a 403
    would read to the caller as their own permission problem. 429 becomes a 503 carrying
    `Retry-After`, and everything else — a transport failure's `None` included — a 502."""
    if exc.status == 404:
        return HTTPException(status_code=404, detail=str(exc))
    if exc.status == 400:
        return HTTPException(status_code=400, detail=str(exc))
    if exc.status in (401, 403):
        return HTTPException(
            status_code=502,
            detail=(
                f"Jira rejected our credentials (HTTP {exc.status}) — check the account email and "
                "API token on the Jira credential this project names."
            ),
        )
    if exc.status == 429:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        return HTTPException(status_code=503, detail=str(exc), headers=headers)
    return HTTPException(status_code=502, detail=str(exc))


@router.get("/{project_id}/jira/issues", response_model=list[JiraIssueOut])
@feature("projects", "view", object_type="project", object_params=("project_id",))
def search_project_jira_issues(
    project_id: int,
    q: str = Query(default=""),
    limit: int = Query(default=15, ge=1, le=50),
    _actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[JiraIssueOut]:
    """Issues matching `q` on this project's Jira site — the task form's issue picker.

    AUTHORIZATION IS `projects:view` ON THIS PROJECT. Reading this project's Jira through the
    credential the project itself names is bounded by being able to read the project, and the guard
    404s a project this caller cannot reach before the handler runs.

    IT DELIBERATELY DOES NOT CONSULT THE `credential` GRANT, and what carries that is ENTIRELY the
    attach-time check: `routes/projects.py::_validated_jira_credential` required, AT THE MOMENT THE
    ROW WAS ATTACHED, that it be an ORG credential (`owner_id` NULL) the setter could read — never a
    personal one, which any member can mint and whose owner could later re-point its host. Readers of
    THIS route are bounded by the project alone because the org vouched for that credential; that
    premise lives in `_validated_jira_credential`, and weakening the check there silently widens this
    route.

    `ge`/`le` on `limit` are pydantic constraints, safe for the reason `credentials.py` gives: a
    query parameter never carries a secret. A blank `q` is `search_issues`' own
    empty-list-and-no-call case and is left to it rather than re-checked here."""
    site = _site(session, project_id)
    try:
        refs = jira_api.search_issues(site, q, limit=limit)
    except jira_api.JiraApiError as exc:
        raise _jira_failure(exc) from exc
    return [JiraIssueOut(key=ref.key, summary=ref.summary, status=ref.status) for ref in refs]


@router.get("/{project_id}/jira/issues/{issue_key}", response_model=JiraIssueDetailOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def get_project_jira_issue(
    project_id: int,
    issue_key: str,
    _actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> JiraIssueDetailOut:
    """One issue's summary, markdown description and browse URL — what the form drops into the
    Description field before anything is saved. Authorization is the picker's, argued there.

    `url` embeds `site.base`, which may legitimately carry `user:pass@` userinfo. `base_url` is a
    non-secret `url` field by the registry's own classification (`app/credentials/registry.py`), so
    this discloses the site — never the API token — to a `projects:view` holder.

    The issue key is validated by `jira_api` (a 400 for anything that is not a key) and echoed back
    from the request rather than from the response: `get_issue` asks for `summary,description` only."""
    site = _site(session, project_id)
    try:
        summary, description = jira_api.get_issue(site, issue_key)
        url = jira_api.issue_browse_url(site, issue_key)
    except jira_api.JiraApiError as exc:
        raise _jira_failure(exc) from exc
    return JiraIssueDetailOut(
        key=issue_key.strip(), summary=summary, description=description, url=url
    )
