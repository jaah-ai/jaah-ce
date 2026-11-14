"""What a new project is, answered when it is created (`POST /api/projects` `intake`), and what Jaah makes for it.

Five answers (`Kind`): a new web app gets an Easy Mode hosted app (`hosted_apps/service.py::create`) for every org —
on the org's Easy Mode shared node with the plan switch on, else a plain project on its own machines; a new app that
is not a web app, or a new business project, gets a plain private repo in the Easy Mode GitHub org
(`make_plain_repo`); an existing app, or an existing business project, connects the person's own repository. Either
way the answer is written into the project's Agent instructions (`setup_block`) and the first task already knows it
(`project_onboarding.py`). A business project's first task is the Business Wizard
(`project_onboarding.py::file_business_wizard_task`). The route that orders these steps is
`routes/projects.py::_create_project_in`; absent `intake`, it creates exactly as before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import edition_features, jaah_repos

log = logging.getLogger(__name__)

Kind = Literal["web_app", "other_app", "business", "existing_app", "existing_business"]
#: The answers whose repository Jaah makes.
JAAH_MADE: tuple[str, ...] = ("web_app", "other_app", "business")
#: The answers whose first task is the Business Wizard (`project_onboarding.py::file_business_wizard_task`).
BUSINESS_KINDS: tuple[str, ...] = ("business", "existing_business")

# The sentences a person reads (the UI copy standards).
JAAH_GIVES_THE_REPO = "A new project gets its repository from Jaah."
PICK_YOUR_REPO = "Pick your repository."
DESCRIBE_YOUR_APP = "Describe your app."
DESCRIBE_YOUR_INITIATIVE = "Describe the initiative."
#: The longest description a create takes (`ProjectIntake.description`).
MAX_DESCRIPTION_CHARS = 4000
DESCRIPTION_TOO_LONG = f"Keep the description under {MAX_DESCRIPTION_CHARS:,} characters."
#: The one starter a new web app is made from (`hosted_apps/catalog.py::TEMPLATES`): the person never picks one.
WEB_APP_TEMPLATE = "web"
#: A Jaah-made repo's refusal while the GitHub half of the Easy Mode setup (its admin login, or the GitHub App's key)
#: is missing, or in an edition without it.
REPO_NOT_SET_UP = "Repository hosting isn't set up."
#: The operator's Settings sections where each half of that setup is fixed (`refused_not_set_up`).
FIX_RUNNER_APP = "/settings?area=integrations&section=github"
FIX_GITHUB_SETUP = "/settings?area=billing&section=easy-mode"
NO_HOSTING_IN_EDITION = "This edition does not include it."
GITHUB_UNREACHABLE = "GitHub could not be reached. Try again."
GITHUB_REFUSED = "GitHub refused to make the repository. Try again."
NO_FREE_NAME = "No repository name is free for that name. Try another name."
NOT_OWN_ORG = "A new project's setup runs in your own organization."

#: A plain repo's branch layout: README on `main`, work integrated on `develop`, runners' branches under `fleet/`.
PLAIN_INTEGRATION_BRANCH = "develop"
PLAIN_BRANCH_PREFIX = "fleet/"
_MAX_SUFFIX = 99


class ProjectIntake(BaseModel):
    """What the project is: `kind`, and for a web app or a business project the person's own words for it
    (`description`, required there, ignored elsewhere), which seed the App Wizard or the Business Wizard
    (`project_onboarding.py`). An old client's `template` is ignored:
    every new web app starts from `WEB_APP_TEMPLATE`."""

    kind: Kind
    #: Not a pydantic bound: a longer one is refused in `refuse_invalid`'s own words.
    description: Optional[str] = None


def refuse_invalid(intake: ProjectIntake, *, repo_url: Optional[str], repo_count: int) -> None:
    """422 for an answer the create cannot honour, before any write."""
    if intake.kind in JAAH_MADE:
        if repo_count or (repo_url or "").strip():
            raise HTTPException(status_code=422, detail=JAAH_GIVES_THE_REPO)
    elif not repo_count and not (repo_url or "").strip():
        raise HTTPException(status_code=422, detail=PICK_YOUR_REPO)
    if intake.kind == "web_app" or intake.kind in BUSINESS_KINDS:
        if not (intake.description or "").strip():
            raise HTTPException(status_code=422,
                                detail=DESCRIBE_YOUR_APP if intake.kind == "web_app" else DESCRIBE_YOUR_INITIATIVE)
        if len(intake.description or "") > MAX_DESCRIPTION_CHARS:
            raise HTTPException(status_code=422, detail=DESCRIPTION_TOO_LONG)


def refused_not_set_up(reason: str, fix_url: Optional[str], *, operator: bool) -> HTTPException:
    """The 503 for a Jaah-made repo while its setup is incomplete. The operator — the one who can fix it — reads
    `REPO_NOT_SET_UP`, then `reason` (what is missing), with `fix_url`, the Settings section to fix it in; anyone
    else reads `REPO_NOT_SET_UP` alone, since `reason` names the operator's own settings and credentials."""
    log.warning("project intake: repository hosting is not set up: %s", reason)
    if not operator:
        return HTTPException(status_code=503, detail=REPO_NOT_SET_UP)
    return HTTPException(status_code=503, detail={"message": f"{REPO_NOT_SET_UP} {reason}", "fix_url": fix_url})


def refuse_without_runner_app(*, operator: bool) -> None:
    """503 before any GitHub call or write when the console's GitHub App cannot mint runner tokens: every runner
    token for a Jaah-made repo comes from it (`easy_mode_runner_tokens.py`), so without it the starter task could
    not clone."""
    if not edition_features.enabled("easy_mode_hosting"):
        raise refused_not_set_up(NO_HOSTING_IN_EDITION, None, operator=operator)
    from app import easy_mode_runner_tokens

    problem = easy_mode_runner_tokens.setup_problem()
    if problem is not None:
        raise refused_not_set_up(problem, FIX_RUNNER_APP, operator=operator)


def what_it_is(kind: str, *, address: Optional[str] = None) -> str:
    """The answer in words (never the raw `kind`)."""
    if kind == "web_app":
        return f"A new web app, hosted by Jaah at https://{address}" if address else "A new web app, hosted by Jaah"
    return {
        "other_app": "A new app (not a web app)",
        "business": "A business project",
        "existing_app": "An existing app",
        "existing_business": "An existing business project",
    }[kind]


def setup_block(what: str, repository: str) -> str:
    """The lines prepended to the project's Agent instructions."""
    return f"About this project (from setup):\n- What it is: {what}\n- Repository: {repository}\n"


def made_by_jaah(full_name: str) -> str:
    return f"made by Jaah: {full_name}"


def with_block(block: str, instructions: Optional[str]) -> str:
    """`block` before whatever the person wrote, a blank line between."""
    return f"{block}\n{instructions}" if instructions and instructions.strip() else block


def marker(org_id: UUID) -> str:
    """What a plain repo's description ends with: the org that made it, so only that org's retry adopts it."""
    return f"(org {org_id})"


def make_plain_repo(session: Session, org_id: UUID, name: str, *, operator: bool) -> tuple[str, int]:
    """`(owner/repo, id)` of a new private repo for project `name` in the Easy Mode GitHub org: the first of
    `<org slug>-<slugify(name)>`, `-2`, … that is free (at most 32 + 1 + 40 characters, inside GitHub's 100) — or that is this org's own repo from a create that failed after GitHub
    made it (`github.create_plain_repo`'s adoption) and no project holds. Refusals write nothing: 503 when the GitHub
    half of the Easy Mode setup is missing (`refused_not_set_up`) or GitHub cannot be reached, 502 when GitHub
    refuses."""
    if not edition_features.enabled("easy_mode_hosting"):
        raise refused_not_set_up(NO_HOSTING_IN_EDITION, None, operator=operator)
    from app.hosted_apps import config as hosting_setup, github
    from app.hosted_apps.names import slugify, with_suffix

    try:
        found = hosting_setup.github_setup()
        token = hosting_setup.github_token(found)
    except hosting_setup.NotSetUp as exc:
        raise refused_not_set_up(str(exc), FIX_GITHUB_SETUP, operator=operator) from None
    prefix = jaah_repos.repo_prefix(jaah_repos.org_slug_of(session, org_id))
    base = slugify(name)
    tag = marker(org_id)
    description = f"{name} — made by Jaah {tag}"
    now = datetime.now(timezone.utc)
    for number in range(1, _MAX_SUFFIX + 1):
        repo_name = prefix + (base if number == 1 else with_suffix(base, number))
        # Two creates of one org trying the same name take it one after the other, so neither adopts the repo the
        # other just made (held to the commit; any other create waits on nothing).
        session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"jaah-repo:{org_id}:{repo_name}"))))
        if jaah_repos.name_taken(session, f"{found.github_org}/{repo_name}"):
            continue
        try:
            made = github.create_plain_repo(token, found.github_org, repo_name, description, marker=tag, now=now)
        except github.GitHubUnreachable as exc:
            log.warning("project intake: %s", exc)
            raise HTTPException(status_code=503, detail=GITHUB_UNREACHABLE) from None
        except github.GitHubError as exc:
            log.warning("project intake: %s", exc)
            raise HTTPException(status_code=502, detail=GITHUB_REFUSED) from None
        if made is not None:
            return made
    raise HTTPException(status_code=409, detail=NO_FREE_NAME)
