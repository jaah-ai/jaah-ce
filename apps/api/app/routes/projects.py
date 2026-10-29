"""Project CRUD endpoints (cockpit-db-primary-project-management-with-cockpit-crud spec, R2/T4).

Makes `projects` authoritative and editable from the console, beside the read-only `GET /api/projects`
(`app/routes/portfolio.py`). Update/delete/erase audit via `record_write` in the mutation's transaction;
the create is audited by `UserActivityMiddleware` (best-effort). `repo_url` is not unique:
several projects may share one repo (the project id is the identity).

Removal: `POST /api/projects/{id}/erase` fans `delete_one_slot` over every slot then deletes the row;
`DELETE /api/projects/{id}` is metadata-only and refuses while any slot row survives. Neither is refused
on occupancy or liveness, so a directory may outlive its project — erase names each orphan.

`apply_project_update` is the one update writer, shared by `PUT /api/projects/{id}` and the agent
`PUT /api/agent/tasks/{task_id}/project` (`app/routes/agent_project_settings.py`). It dispatches
`sync_access_guard` when `branch_prefix`/`push_enabled` change,
and `rename_tmux` per slot on a rename.

The seven mandatory branch/gate/stack fields (`_GOVERNANCE_FIELDS`) cannot be nulled (a gate
or stack command may be `''`, meaning none; a new project's four are seeded from its org's Projects > Defaults
settings), cannot be authored by a node token, and need superadmin or an org-admin with `nodes:edit` plus a write grant on
a node in the project's org (`_has_authority_to_author_governance`). The two `governance-authority` GET
routes and the agent module return that same verdict, via the unprefixed aliases.
"""
from __future__ import annotations


import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Iterable, Literal, Mapping, Optional
# The type is imported for the "Humans" annotations and the member route's response model.
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import String, and_, cast, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import (
    agent_providers, agent_resolution, ce_caps, credential_consumers, edition_features, forges, git_access, git_reach, github_api, merge_lane_state, model_catalog,
    org_limits, project_repos, provider_auth_policy, slot_allocation, slot_mutate, slot_title, static_slots_policy,
)
from app.operator_settings import defaults as settings_defaults
from app.persona_registry import PROJECT_DEFAULT_PERSONAS
from app.auth import require_superadmin, require_user
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import feature, superadmin_only
from app.authz.object_access import can_access, named_user_may_start, named_user_reaches_project
from app.authz.principal import org_admin_writer, Principal, current_principal
from app.authz.visibility import ObjectAccess, node_reach_clause
from app.db import SessionLocal, get_session
from app.entity_shape import StrictModel, constraint_name, from_row, get_or_404
from app.project_identity import project_or_404
from app.project_onboarding import file_onboarding_task
from app.access_admin import PROJECT_ROLE_FIELDS
from app.models.access import RoleBinding, UserGroupMembership
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.node_groups import NodeProject
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import Project, ProjectRepo
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.node_clone import resolve_clone
from app.project_validation import has_duplicate_tech_stack_entry
from app.command_read import withhold_host_paths
from app.routes.commands import _resolve_command_args, default_dispatch_node, dispatch_and_audit_sync
from app.routes.pool_slots import SlotDeleteOut, _clone_git_access_warning, delete_one_slot, require_slot_writer
from app.node_identity import node_id_or_404, node_matches
from app.tenancy import seam
from app.tenancy.copy import governance_authority
from app.tenancy.create_surface import named_org
from app.tenancy.ids import is_tenant_org
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
from app.user_activity import record_write

router = APIRouter(prefix="/api/projects", tags=["projects"])
log = logging.getLogger("cockpit-projects")

# Characters `git check-ref-format` refuses, minus those the whitespace/control test covers (`[` because
# a ref is also a rev-parse expression, `\` because it is a path).
_ILLEGAL_REF_CHARS = "~^:?*[\\"


def _reject_illegal_git_ref(value: str, *, field: str, kind: str) -> None:
    """`git check-ref-format`'s rules for a branch name, applied to `integration_branch` and to
    `branch_prefix` (trailing slash stripped by the caller); `kind` shapes the message.

    Adds the leading-dash rule git does not enforce: the value reaches `git fetch` as bare argv without
    `--`, so `--upload-pack=…` would be parsed as an option. Checked per segment. Not a shell-out: the API
    container has no guaranteed git binary."""

    def reject(reason: str) -> None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} is not a legal git {kind}: {reason} (got '{value}') — every branch this "
                "project's runners create or push is built from it, so an illegal value fails later, on "
                "another machine, as a broken build"
            ),
        )

    if value == "@":
        reject("'@' alone is reserved")
    if value == "HEAD":
        reject("'HEAD' is a symbolic ref whose meaning changes per checkout, not a branch name")
    if "@{" in value:
        reject("'@{' is not allowed (it is reflog syntax, not part of a ref name)")
    for char in value:
        if char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F:
            reject("whitespace and control characters are not allowed")
        if char in _ILLEGAL_REF_CHARS:
            reject(f"'{char}' is not allowed")
    if ".." in value:
        reject("'..' is not allowed")
    segments = value.split("/")
    if any(not segment for segment in segments):
        # A leading, trailing or doubled `/` is an empty segment; git rejects all three.
        reject("a path segment cannot be empty (no leading or trailing '/', no '//')")
    if segments[:2] == ["refs", "heads"]:
        # Consumers qualify the value, so a pre-qualified one yields `refs/heads/refs/heads/x`.
        reject("give the branch name alone, not its fully-qualified 'refs/heads/…' form")
    for segment in segments:
        if segment.startswith("."):
            reject(f"a path segment cannot start with '.' ('{segment}')")
        if segment.startswith("-"):
            # The argv rule: no `--` separator, so a dash-leading word is consumed as an option.
            reject(f"a path segment cannot start with '-' ('{segment}') — it would be read as a git option")
        if segment.endswith("."):
            reject(f"a path segment cannot end with '.' ('{segment}')")
        if segment.endswith(".lock"):
            reject(f"a path segment cannot end with '.lock' ('{segment}')")


def _required_value(raw: Any, *, field: str) -> Any:
    """A mandatory field's value, or a 422 for None or a blank string. Booleans pass (`is None`, not
    truthiness)."""
    if raw is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} cannot be null — it is a mandatory field with no fallback anywhere "
                "(app/models/projects.py's Project); omit it entirely to keep the current value"
            ),
        )
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{field} cannot be blank — it is a mandatory field with no fallback anywhere "
                    "(app/models/projects.py's Project)"
                ),
            )
        return value
    return raw


def _normalized_branch_prefix(raw: Any) -> str:
    """`branch_prefix` stored canonically: `''` for "no prefix", otherwise ending in exactly one `/`.

    The one mandatory field where `''` is legal; null is refused. Normalized so consumers concatenate
    `{branch_prefix}{rest}`."""
    if raw is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "branch_prefix cannot be null — it is a mandatory field (app/models/projects.py's Project); "
                "send '' for no prefix"
            ),
        )
    value = str(raw).strip()
    if not value:
        return ""
    body = value.rstrip("/")
    if not body:
        # `'/'` normalizes to neither `''` nor a legal prefix.
        raise HTTPException(
            status_code=422,
            detail="branch_prefix cannot be only '/' — send '' for no prefix, or a real prefix like 'sn/'",
        )
    _reject_illegal_git_ref(body, field="branch_prefix", kind="branch prefix")
    return body + "/"


def _validated_shell_command(raw: Any, *, field: str) -> str:
    """A shell command the fleet hands to a shell on a node: mandatory, single-line, no NUL.

    One validator for the gate pair (`run_gate.py`, `worktree_ops.py::check`) and the stack pair
    (`apps/jaahd/stack_ops.py`) — same sink. Only shape is checked (the CP has no checkout); a newline
    would make one command two, the second ungated."""
    value = _required_value(raw, field=field)
    if not isinstance(value, str):
        # `_required_value` passes non-strings through (for `push_enabled`), so without this the `in`
        # tests would 500 instead of 422.
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be a string command, not {type(raw).__name__}",
        )
    if "\n" in value or "\r" in value or "\0" in value:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} must be a single command on one line — a newline would make the runner execute "
                "a second, ungated command"
            ),
        )
    return value


# The four shell-command columns a node runs: blank-able, seeded at create from Projects > Defaults.
_COMMAND_FIELDS = ("gate_quick_cmd", "gate_full_cmd", "stack_up_cmd", "stack_down_cmd")


def _validated_optional_command(raw: Any, *, field: str) -> str:
    """A gate or stack command (`gate_quick_cmd`/`gate_full_cmd`/`stack_up_cmd`/`stack_down_cmd`): `''`
    (after strip) is legal and means "this project has no such command"; null is still refused, and a
    non-blank value is a `_validated_shell_command`."""
    if raw is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} cannot be null — it is a mandatory field (app/models/projects.py's Project); "
                "send '' for none"
            ),
        )
    if isinstance(raw, str) and not raw.strip():
        return ""
    return _validated_shell_command(raw, field=field)



# Schemes that execute in the operator's browser when the console renders the URL as an `href`. A
# denylist, since `repo_url` legitimately holds `git@github.com:Owner/Repo.git` and bare `Owner/Repo`.
_DANGEROUS_URL_SCHEME = re.compile(r"^[\s\x00-\x1f]*(javascript|data|vbscript|blob|file)\s*:",
                                   re.IGNORECASE)

# Refused before the denylist: browsers strip tab/CR/LF inside a scheme (`java\tscript:`). Same class as
# `url_validation._FORBIDDEN_CHARS` and the console's `safeHref`.
_URL_CONTROL_BYTES = re.compile(r"[\x00-\x20\x7f]")

# The host swap: special schemes treat any run of `/` and `\` as the authority delimiter, so an `@` before
# the first real path separator means label and destination disagree. Scoped to http/https —
# `ssh://git@github.com/o/r.git` is legitimate.
_URL_USERINFO = re.compile(r"^https?:[/\\]*[^/?#\\]*@", re.IGNORECASE)
# The one userinfo form that is not a host swap: Azure DevOps' own Clone button gives
# `https://<org>@dev.azure.com/<org>/…`, whose label and destination agree.
_AZURE_CLONE_USERINFO = re.compile(r"^https://([A-Za-z0-9-]+)@dev\.azure\.com/\1/", re.IGNORECASE)


def _validated_default_model(raw: Any) -> Optional[str]:
    """The project's `default_model`: a model TIER (`app/model_catalog.py`; a legacy Claude alias is folded
    onto its tier), or None/empty for "no project default".

    A shell sink downstream (the node turns it into `--model <alias>`), so the closed tier set is also the
    safety allowlist."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=422,
            detail=f"default_model must be a model tier string, not {type(raw).__name__}",
        )
    try:
        return model_catalog.validate_model(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"default_model: {exc}") from None


def _validated_default_persona(raw: Any) -> Optional[str]:
    """The project's `default_persona`: one of `PROJECT_DEFAULT_PERSONAS`, or None/empty for "the org default"."""
    if raw is None or raw == "":
        return None
    if raw not in PROJECT_DEFAULT_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=f"default_persona must be one of {', '.join(PROJECT_DEFAULT_PERSONAS)}",
        )
    return str(raw)


def _validated_repo_url(raw: Any) -> Optional[str]:
    """`repo_url`, refused only for the schemes that execute when the console renders it as a link.

    Not an http(s) allowlist — see `_DANGEROUS_URL_SCHEME`. React does not block `javascript:` hrefs
    (`apps/web/src/pages/Projects.tsx`); `safeHref` covers render sites, this is the boundary half."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=422,
            detail=f"repo_url must be a string, not {type(raw).__name__}",
        )
    if _URL_CONTROL_BYTES.search(raw.strip()):
        raise HTTPException(
            status_code=422,
            detail=(
                "repo_url may not contain a space or control byte — a browser strips them and would "
                "follow `java\\tscript:` as `javascript:`, past the scheme check below"
            ),
        )
    if _URL_USERINFO.match(raw.strip()) and not _AZURE_CLONE_USERINFO.match(raw.strip()):
        raise HTTPException(
            status_code=422,
            detail=(
                "repo_url may not carry userinfo before its host — `https://github.com@evil.example/` "
                "reads as github.com to an operator and resolves somewhere else"
            ),
        )
    if _DANGEROUS_URL_SCHEME.match(raw):
        raise HTTPException(
            status_code=422,
            detail=(
                "repo_url may not use a script-executing scheme (javascript:, data:, vbscript:, blob:, "
                "file:) — the console renders this value as a clickable link in an operator's session"
            ),
        )
    return raw


def _derived_forge(fields: dict[str, Any], current: Optional[Project]) -> dict[str, Optional[str]]:
    """`forge`/`forge_base_url` as stored: DERIVED from the repo URL (AC1.1), refused with the forge
    model's own sentence when it names no supported forge (AC1.3) — never saved to fail at dispatch.

    An unsent field falls back to `current`'s, so an update re-checks the whole triple — but the stored
    forge is carried only when it is a DECLARATION (a self-managed GitLab), never a derived value: a
    repo-URL-only PUT from github.com to gitlab.com must not be refused as "not GitHub". No repo URL
    clears both."""
    current_ref = forges.repo_ref_or_none(current)
    declaration = current_ref is not None and current_ref.host not in forges.KNOWN_HOSTS

    def value(key: str) -> Any:
        if key in fields:
            return fields[key]
        if key != "repo_url" and not declaration:
            return None
        return getattr(current, key, None) if current is not None else None

    repo_url, declared, base_url = value("repo_url"), value("forge"), value("forge_base_url")
    for key, raw in (("forge", declared), ("forge_base_url", base_url)):
        if raw is not None and not isinstance(raw, str):
            raise HTTPException(status_code=422, detail=f"{key} must be a string, not {type(raw).__name__}")
    base_url = (base_url or "").strip() or None
    if not (repo_url or "").strip():
        return {"forge": None, "forge_base_url": None}
    declared = (declared or "").strip() or None
    # A declaration carried over from the stored row was made for the OLD host; on a known host it lapses.
    carried = "forge" not in fields and "forge_base_url" not in fields
    try:
        if carried and declared is not None:
            try:
                ref = forges.parse_repo_url(repo_url, None)
                return {"forge": ref.forge, "forge_base_url": None}
            except forges.ForgeUrlError:
                pass
        ref = forges.parse_repo_url(repo_url, declared, base_url)
    except forges.ForgeUrlError as exc:
        raise HTTPException(status_code=422, detail=f"repo_url: {exc}") from None
    return {"forge": ref.forge, "forge_base_url": base_url if ref.root != ref.host else None}


# ── A whole-set `repos` write ─────────────────────────────────────────────────────────────────

#: The legacy single-repo keys `repos` replaces. `repo_url` beside `repos` is always refused; the other three
#: only when they would say something the rows do not (`_split_repos`).
_LEGACY_REPO_FIELDS = project_repos.PROJECTED

#: The one sentence for removing the primary while others remain (AS4), on both write shapes.
_PICK_ANOTHER_PRIMARY = "Pick another primary first"


def _repo_error(loc: list[Any], msg: str, code: str) -> dict[str, Any]:
    """One entry of a 422 `detail` list, in FastAPI's own shape so the console reads it like a body error."""
    return {"loc": ["body", *loc], "msg": msg, "type": code}


def _detail_text(exc: HTTPException) -> str:
    return exc.detail if isinstance(exc.detail, str) else str(exc.detail)


def _repo_keys(url: str, forge: Optional[str], forge_base_url: Optional[str]) -> set[str]:
    """What makes two URLs the same repository (AS3): the stored text, case- and space-folded, and the forge root
    plus path when it parses (`.git`, scheme and host case aside)."""
    keys = {project_repos.url_key(url)}
    try:
        ref = forges.parse_repo_url(url, forge, forge_base_url)
        keys.add(f"{ref.root}/{ref.path}".lower())
    except forges.ForgeUrlError:
        pass
    return keys


def _validated_repos(
    rows: list[dict[str, Any]], current: list[ProjectRepo]
) -> list[project_repos.RepoSpec]:
    """`repos` as the set to store, or ONE 422 naming every bad row by `["body","repos",<i>,<field>]`, so nothing is
    saved and the form marks each row at once.

    Codes: `invalid_repo_url` (blank, or refused by `_validated_repo_url`/`_derived_forge` with their own
    message), `duplicate_repo` (AS3: the same URL, or the same repository spelled another way, twice),
    `integration_branch_required` (AS10), `invalid_integration_branch`, `unknown_repo` (an `id` that is not one
    of this project's rows), `primary_removed` (AS4: the current primary dropped and no new one named), and
    `primary_required` (not exactly one primary)."""
    by_id = {row.id: row for row in current}
    errors: list[dict[str, Any]] = []
    specs: list[project_repos.RepoSpec] = []
    seen_urls: dict[str, int] = {}
    seen_ids: set[int] = set()
    for index, row in enumerate(rows):
        loc = ["repos", index]
        row_id = row.get("id")
        stored = by_id.get(row_id) if row_id is not None else None
        if row_id is not None and (stored is None or row_id in seen_ids):
            errors.append(_repo_error([*loc, "id"], "This repository is not connected to this project", "unknown_repo"))
            continue
        if row_id is not None:
            seen_ids.add(row_id)
        url = (row.get("repo_url") or "").strip()
        derived: Optional[dict[str, Optional[str]]] = None
        if not url:
            errors.append(_repo_error([*loc, "repo_url"], "repo_url is required", "invalid_repo_url"))
        else:
            sent = {"repo_url": url, **{key: row[key] for key in ("forge", "forge_base_url") if row.get(key) is not None}}
            try:
                _validated_repo_url(url)
                derived = _derived_forge(sent, stored)
            except HTTPException as exc:
                errors.append(_repo_error([*loc, "repo_url"], _detail_text(exc), "invalid_repo_url"))
        if derived is not None:
            # The same repository twice, however it is spelled (`.git`, scheme, case of the host).
            keys = _repo_keys(url, derived["forge"], derived["forge_base_url"])
            if any(key in seen_urls for key in keys):
                errors.append(_repo_error(
                    [*loc, "repo_url"], "This repository is already connected to this project", "duplicate_repo",
                ))
                derived = None
            else:
                seen_urls.update({key: index for key in keys})
        branch = (row.get("integration_branch") or "").strip()
        if not branch:
            errors.append(_repo_error(
                [*loc, "integration_branch"], "integration_branch is required for every repository",
                "integration_branch_required",
            ))
        else:
            try:
                _reject_illegal_git_ref(branch, field="integration_branch", kind="branch name")
            except HTTPException as exc:
                errors.append(_repo_error([*loc, "integration_branch"], _detail_text(exc), "invalid_integration_branch"))
                branch = ""
        if derived is not None and branch:
            specs.append(project_repos.RepoSpec(
                id=row_id, repo_url=url, forge=derived["forge"], forge_base_url=derived["forge_base_url"],
                integration_branch=branch, is_primary=bool(row.get("is_primary")),
            ))
    primaries = [index for index, row in enumerate(rows) if row.get("is_primary")]
    if rows and not primaries:
        current_primary = next((row.id for row in current if row.is_primary), None)
        if current_primary is not None and current_primary not in {row.get("id") for row in rows}:
            errors.append(_repo_error(["repos", 0, "is_primary"], _PICK_ANOTHER_PRIMARY, "primary_removed"))
        else:
            errors.append(_repo_error(["repos", 0, "is_primary"], "Mark one repository as primary", "primary_required"))
    for index in primaries[1:]:
        errors.append(_repo_error(["repos", index, "is_primary"], "Only one repository can be primary", "primary_required"))
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    return specs


def _split_repos(
    sent: dict[str, Any], current: Optional[Project]
) -> tuple[dict[str, Any], Optional[list[dict[str, Any]]]]:
    """`sent` without `repos`, and the `repos` list (None when absent or null — then the legacy keys, if any, write
    the primary row).

    Beside `repos`, `repo_url` is refused (one write, one shape); `forge`/`forge_base_url`/`integration_branch` are
    refused unless they resend the stored value (the Edit form resends every field), or on a create the primary
    row's branch — they are the rows' to say."""
    sent = dict(sent)
    rows = sent.pop("repos", None)
    if rows is None:
        return sent, None
    errors: list[dict[str, Any]] = []
    for field_name in _LEGACY_REPO_FIELDS:
        if field_name not in sent:
            continue
        if current is not None:
            resend = field_name != "repo_url" and sent[field_name] == getattr(current, field_name)
        else:
            # A create has nothing stored: the branch may restate the primary row's (the wizard sends it).
            primary = next((row for row in rows if (row.get("is_primary") if isinstance(row, dict) else False)), None)
            resend = field_name == "integration_branch" and primary is not None and (
                sent[field_name] == primary.get("integration_branch"))
        if not resend:
            errors.append(_repo_error(
                [field_name], f"Send repos or {field_name}, not both — set it on the repository row",
                "repos_conflict",
            ))
        sent.pop(field_name)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    return sent, [row if isinstance(row, dict) else dict(row) for row in rows]


def _repos_author_the_branch(
    rows: list[dict[str, Any]], project: Optional[Project]
) -> bool:
    """Would this `repos` write change `projects.integration_branch` (the primary's branch, a governance field)?
    Only then does it take the governance door — branches of the other repositories are any `projects:edit`
    holder's to set, as their URLs are (AS9). On create (`project` None) the comparison is with the column's
    default, which a legacy create leaving the branch out also gets."""
    primary = next((row for row in rows if row.get("is_primary")), None)
    if primary is None:
        return False
    current = project.integration_branch if project is not None else Project.__table__.c.integration_branch.default.arg
    return (primary.get("integration_branch") or "").strip() != current


class TechStackEntry(StrictModel):
    """One Tech Stack row, and both keys are required.

    A loose `list[dict[str, str]]` accepted `[{"layer": "frontend"}]`, which the Edit form then crashed
    on. `ProjectOut` keeps the loose type so legacy rows stay readable — strict in, lenient out, until a
    migration cleans the column.
    """

    # `extra="forbid"`, so an unknown key is refused rather than silently dropped.
    layer: str
    technology: str


class ProjectPlacement(BaseModel):
    """Where a new project runs (`POST /api/projects`' `placement`): the nodes it is assigned to.

    Present, the project is assigned to each node with no static slot, so each task makes its own worktree
    there (`slot_allocation.add_dynamic`), all in the create's one transaction. Nothing to enable: every
    project is pool-capable."""

    # Stripped, as `routes/pool_slots.py::_resolved_node_id` strips the same value.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Each node's uuid (`app/node_identity.py`); a name is not an identity.
    node_ids: list[Annotated[str, StringConstraints(min_length=1)]] = Field(min_length=1, max_length=50)


#: Re-exported for this module's body models and `routes/assignments.py`.
MAX_STATIC_SLOTS = static_slots_policy.MAX_STATIC_SLOTS


class ProjectRepoIn(StrictModel):
    """One row of a whole-set `repos` write: with `id` that row is updated, without one a row is
    inserted; every stored row the list does not name is deleted. Exactly one entry `is_primary` in a non-empty
    list. `repo_url` and `integration_branch` are optional here only so their absence is answered by
    `_validated_repos` with the row's own `loc`; both are required."""

    id: Optional[int] = None
    repo_url: Optional[str] = None
    # Declared only for a self-managed GitLab, as on the project (`ProjectCreate.forge`).
    forge: Optional[str] = None
    forge_base_url: Optional[str] = None
    integration_branch: Optional[str] = None
    is_primary: bool = False


class ProjectRepoOut(BaseModel):
    """One connected repository (`models.ProjectRepo`); `name` is its `owner/name`, `web_url` its forge page."""

    id: int
    repo_url: str
    forge: Optional[str]
    forge_base_url: Optional[str]
    integration_branch: str
    is_primary: bool
    name: str
    web_url: Optional[str]


def project_repo_out(row: ProjectRepo) -> ProjectRepoOut:
    ref = forges.repo_ref_or_none(row)
    return from_row(ProjectRepoOut, row, web_url=ref.web_url() if ref is not None else None)


class _ProjectFieldsIn(BaseModel):
    """The columns the create and the edit both take, each optional on the wire: `ProjectCreate` adds what only a
    birth carries (`name`, placement, org), `ProjectUpdate` what only an edit may touch.

    No `extra="forbid"`: the edit form resends every read field (`has_repo`, `web_url`, ...), so an undeclared
    key — a retired column such as the pool fields included — is ignored, never stored and never echoed."""

    # With no repository (`repos` empty or absent, no `repo_url`), the project's tasks run in an empty folder slot
    # (`models.Project.has_repo`).
    repo_url: Optional[str] = None
    # Declared only for a self-managed GitLab (`'gitlab'`); a known host derives its forge and a mismatch
    # is refused (`app/forges.py::parse_repo_url`). What is stored is the derived value, never this one. On an
    # edit, re-derived whenever any of the three is sent.
    forge: Optional[str] = None
    forge_base_url: Optional[str] = None
    # Optional on the wire, mandatory in the column: omitted takes the default (on an edit: leaves it alone); an
    # explicit null is refused by `_validated_fields` so the 422 names the field — the Edit form cannot null
    # these seven (`''` is a value for `branch_prefix` and the gate pair). The gate and stack commands default
    # to the org's Projects > Defaults settings (`''` = none).
    integration_branch: Optional[str] = None
    branch_prefix: Optional[str] = None
    gate_quick_cmd: Optional[str] = None
    gate_full_cmd: Optional[str] = None
    stack_up_cmd: Optional[str] = None
    stack_down_cmd: Optional[str] = None
    push_enabled: Optional[bool] = None
    default_model: Optional[str] = None
    port_vars: Optional[list[str]] = None
    port_block: Optional[int] = None
    tech_stack: Optional[list[TechStackEntry]] = None
    notes: Optional[str] = None
    # See `models.Project.agent_instructions`. Optional like `notes` (omitted → `''`; on an edit an explicit null
    # clears it); a node token may not author it (`_reject_governance_from_a_node_token`).
    agent_instructions: Optional[str] = None
    # A `credentials.id` uuid string; its check needs no project, so it is present on create. On an edit an
    # explicit null clears it — how an operator turns this project's Jira features off.
    jira_credential_id: Optional[UUID] = None
    # The project's default agent — the one picker's two halves (DD12), exclusive (422 if both; setting one
    # clears the other). An unpicked task runs it (`agent_resolution.resolve_agent`). Null clears. Gated by the
    # route's own `projects:edit` (DD13). Set at birth so the onboarding task the create files runs on the
    # creator's pick, not "Any" (`project_onboarding.py`). Applied by `_create_project_in`.
    default_agent_id: Optional[UUID] = None
    default_provider: Optional[str] = None
    # The project's repositories as a whole set (`ProjectRepoIn`), replacing the stored one; exclusive with
    # `repo_url` (`_split_repos`).
    repos: Optional[list[ProjectRepoIn]] = None


class ProjectCreate(_ProjectFieldsIn):
    name: str
    # The "Humans" pair is edit-only: its validation proves the named user can reach this project, which
    # is impossible before the project exists.

    # When set, the row is stamped with the source's data timestamp instead of now(); no in-tree caller.
    updated_at: Optional[datetime] = None
    # Create-only; absent leaves every default as it was (`ProjectPlacement`).
    placement: Optional[ProjectPlacement] = None
    # Create-only: the org the project lands in. Absent or the caller's own = their own org. Another org is
    # the operator's alone (seeding a tenant from the console) and a 422 for anyone else — never ignored
    # (`create_project`).
    org_id: Optional[UUID] = None


class ProjectUpdate(_ProjectFieldsIn):
    """`_ProjectFieldsIn` with `name` optional too: only fields present in the request body
    (`model_dump(exclude_unset=True)`) are applied, so a `PUT` here is a partial update. Undeclared keys are
    ignored, as on the create."""

    name: Optional[str] = None
    # The persona a new task starts as (`models.Project.default_persona`); null or `''` clears to the org default.
    default_persona: Optional[str] = None
    # The "Humans" pair — edit-only (see `ProjectCreate`); an explicit null clears.
    business_analyst_id: Optional[UUID] = None
    architect_id: Optional[UUID] = None
    # Never writable (`models.Project.system_kind`): accepted only so a resend of the stored value passes and a
    # change is refused with a sentence (`_refuse_system_project_edit`) rather than silently dropped.
    system_kind: Optional[str] = None
    # MicroVM (`app/microvm_egress.py`, `app/microvm_sizing.py`): this project's edits to the default egress
    # allowlist (each list replaces the stored one; null clears it) and its VM size override (null clears one).
    egress_allow: Optional[list[str]] = None
    egress_deny: Optional[list[str]] = None
    microvm_vcpus: Optional[int] = None
    microvm_ram_mib: Optional[int] = None
    microvm_disk_gib: Optional[int] = None
    # PRs per merge-lane batch (`merge_lane.batch_max`); null clears to the default.
    merge_lane_batch_max: Optional[int] = None


class ProjectOut(BaseModel):
    id: int
    name: str
    # No default: `_project_out` is the one constructor, so forgetting it is a TypeError. Null is a real
    # state here — `org_id` has no FK, so an unmatched id resolves to null (`app/tenancy/labels.py`).
    org: Optional[OrgRef]
    # Whether a repository is connected (`models.Project.has_repo`). No default: the Edit form hides every
    # repository field off this.
    has_repo: bool
    # `'nodes'` for the org's system Nodes project (`app/node_targets.py`), null for every other; the console
    # renders a System chip off it.
    system_kind: Optional[str] = None
    repo_url: Optional[str] = None
    # Retired: always true, for a console tab still on the previous bundle, which gates task
    # creation on it. Goes with the `projects.fleet_enabled` column.
    fleet_enabled: bool = True
    # `projects.forge`; null = no repo URL, or a legacy one naming no supported forge (the form asks).
    forge: Optional[str] = None
    forge_base_url: Optional[str] = None
    # The repo's own page on its forge (`RepoRef.web_url`), what the project list links; null with `forge`.
    web_url: Optional[str] = None
    # What a node clones (`RepoRef.clone_url`, the forge-normalized HTTPS URL); null with `forge`.
    clone_url: Optional[str] = None
    # Non-optional on the way out: the column is NOT NULL.
    integration_branch: str
    branch_prefix: str
    gate_quick_cmd: str
    gate_full_cmd: str
    stack_up_cmd: str
    stack_down_cmd: str
    push_enabled: bool
    default_model: Optional[str] = None
    # The default agent (`ProjectUpdate`): at most one of the two is set.
    default_agent_id: Optional[UUID] = None
    default_provider: Optional[str] = None
    # Null: new tasks start as the org's "Persona" default.
    default_persona: Optional[str] = None
    # The merge lane (`app/merge_lane_state.py::lane_state`). No defaults: `_project_out` is the one constructor.
    # `merge_lane` is whether this project is lane-active; `merge_lane_reason` is one line on why not, when
    # the org has the feature; and `merge_lane_offered` is whether the org has it at all (the settings field shows on it).
    merge_lane: bool
    merge_lane_reason: Optional[str]
    merge_lane_offered: bool
    # The project's own batch cap; null = the lane's default (`MergeLaneOut.batch_max` is the effective one).
    merge_lane_batch_max: Optional[int] = None
    # Whether the project's org holds Live Cast (`app/live_cast/entitlement.py`): the console asks
    # `GET …/live-cast` only then, since every Live Cast route 403s without it. No default, as the lane's.
    live_cast_offered: bool
    port_vars: Optional[list[str]] = None
    port_block: Optional[int] = None
    tech_stack: Optional[list[dict[str, str]]] = None
    notes: Optional[str] = None
    # No default: a serialized null would seed a blank textarea, and the next save would PUT null and wipe
    # human-written prose nothing else holds.
    agent_instructions: Optional[str]
    # No default: the form resends this key on every save, so a serialized null would clear the column.
    jira_credential_id: Optional[UUID]
    business_analyst_id: Optional[UUID] = None
    architect_id: Optional[UUID] = None
    ingested_at: datetime
    updated_at: datetime
    # ── MicroVM (architecture §4.9) ── The project's egress edits; `egress_default` is what it edits (the built-in
    # list plus the configured CP and forge hosts), `egress_effective` the list its VMs get. Read-only, both.
    egress_allow: list[str] = []
    egress_deny: list[str] = []
    egress_default: list[str] = []
    egress_effective: list[str] = []
    # `{vcpus|ram_mib|disk_gib: {value, source}, override: {...}}` before any node override: project > org >
    # system (`microvm_sizing.sizing_out`), and the project's own values.
    microvm_sizing: dict[str, Any] = {}
    # Assigned to an enabled MicroVM node: a console session can open on it, repository or not
    # (`routes/console.py::console_new_session`).
    on_microvm: bool = False
    # Why a task started now could not run, or null when it can be placed (`microvm_page`): the project has no node,
    # or its org no enabled credential account and is not on the Jaah account. The task editor disables Go on it;
    # `dispatch_block_kind` (`no_node` | `no_account`) lets it spare a task that picked an SDK agent the account.
    dispatch_block: Optional[str] = None
    dispatch_block_kind: Optional[str] = None
    # Every connected repository in display order; `repo_url`/`forge`/`forge_base_url`/
    # `integration_branch` above are the primary's.
    repos: list[ProjectRepoOut] = []


class ProjectCreateOut(ProjectOut):
    """`POST /api/projects`' 201: the row, plus the placement's git-access advisory (the first placed node's
    `routes/pool_slots.py::_clone_git_access_warning`) — null without a placement, or when every clone resolves."""

    placement_warning: Optional[str] = None
    # The onboarding task the create filed (`app/project_onboarding.py`), or null when none was.
    onboarding_task_id: Optional[int] = None


@dataclass(frozen=True)
class MicrovmPage:
    """What `_project_out` reads beyond the row, resolved once per page (`microvm_page`): each project's org row
    (its sizing tier), the configured CP host (an allowlist entry), which of the page's projects an enabled
    MicroVM node runs, and each unplaceable project's `dispatch_block_kind`."""

    orgs: Mapping[UUID, Org]
    cp_host: Optional[str]
    on_microvm: frozenset[int] = frozenset()
    dispatch_blocks: Mapping[int, str] = field(default_factory=dict)


#: `ProjectOut.dispatch_block`'s sentences, keyed by `dispatch_block_kind`.
NO_NODE_BLOCK = "No node runs this project. Add one in the project's Slots tab."
NO_ACCOUNT_BLOCK = "No enabled coding agent account in this organization. Add one in Settings › Credentials."
DISPATCH_BLOCKS: dict[str, str] = {"no_node": NO_NODE_BLOCK, "no_account": NO_ACCOUNT_BLOCK}


def _dispatch_blocks(session: Session, projects: list[Project]) -> dict[int, str]:
    """`{project id: dispatch_block_kind}` for each project a started task could not be placed for: no
    node hosts it (`slot_allocation.hosting_node_ids_by_project`), else its org has no enabled `accounts` row while
    not on the Jaah account and no default SDK agent spares it one. Four reads per page, none per project."""
    if not projects:
        return {}
    hosting = slot_allocation.hosting_node_ids_by_project(session, [project.id for project in projects])
    org_ids = {project.org_id for project in projects if project.org_id is not None}
    with_coder = org_limits.orgs_with_a_coder(session, org_ids)
    out: dict[int, str] = {}
    for project in projects:
        if not hosting.get(project.id):
            out[project.id] = "no_node"
        elif (project.default_agent_id is None and project.org_id is not None
              and project.org_id not in with_coder):
            out[project.id] = "no_account"
    return out


def microvm_page(session: Session, projects: Iterable[Project]) -> MicrovmPage:
    """One `orgs` select, one Operator Settings read and one assignment read (when a MicroVM node exists) for a
    page of projects."""
    projects = list(projects)
    ids = {project.org_id for project in projects if project.org_id is not None}
    orgs = {row.id: row for row in session.scalars(select(Org).where(Org.id.in_(ids)))} if ids else {}
    page_ids = {project.id for project in projects}
    on_microvm: set[int] = set()
    if page_ids:
        # A node of the project's own org only: New session opens a VM on the caller's org's node alone.
        vm_nodes = dict(session.execute(
            select(Node.id, Node.org_id).where(Node.microvm.is_(True), Node.enabled.is_(True))).all())
        if vm_nodes:
            org_of = {project.id: project.org_id for project in projects}
            on_microvm.update(a.project_id for a in slot_allocation.effective_assignments(session)
                              if a.project_id in page_ids and a.node_id in vm_nodes
                              and vm_nodes[a.node_id] == org_of[a.project_id])
    cp_host = None
    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs.
        from app import microvm_egress

        cp_host = microvm_egress.cp_host()
    return MicrovmPage(orgs=orgs, cp_host=cp_host, on_microvm=frozenset(on_microvm),
                       dispatch_blocks=_dispatch_blocks(session, projects))


def live_cast_holders(session: Session, org_ids: Iterable[Optional[UUID]]) -> frozenset[UUID]:
    """Which of these orgs hold Live Cast now (`app/live_cast/entitlement.py::holders`): none in an edition without
    it, so no project there is offered it."""
    if not edition_features.enabled("live_cast"):
        return frozenset()
    from app.live_cast.entitlement import holders

    return holders(session, org_ids)


def _project_out(
    project: Project, *, org: Optional[OrgRef], lane: merge_lane_state.LaneState, vm: MicrovmPage,
    repos: list[ProjectRepo], live_cast: frozenset[UUID],
) -> ProjectOut:
    """The one constructor for this response — `routes/portfolio.py::list_projects` returns it too.

    `org`, `lane`, `vm`, `repos` and `live_cast` are resolved by the caller (one `org_refs` call, then `org_ref_of`,
    `app/tenancy/labels.py`; one `merge_lane.lane_states` call; one `microvm_page` call; one
    `project_repos.repos_by_project` call; one `live_cast_holders` call), never queried here,
    to avoid an N+1; keyword-only with no default so every call site decides."""
    ref = forges.repo_ref_or_none(project)
    microvm: dict[str, Any] = {}
    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs: the model's defaults.
        from app import microvm_egress, microvm_sizing

        configured = (vm.cp_host, microvm_egress.forge_host(project))
        microvm = dict(
            egress_default=microvm_egress.effective(extra=configured),
            egress_effective=microvm_egress.effective(
                project.egress_allow or (), project.egress_deny or (), extra=configured
            ),
            microvm_sizing=microvm_sizing.sizing_out(None, project, vm.orgs.get(project.org_id), own=project),
        )
    return ProjectOut(
        id=project.id, name=project.name, org=org,
        has_repo=project.has_repo,
        system_kind=project.system_kind,
        repo_url=project.repo_url,
        forge=project.forge, forge_base_url=project.forge_base_url,
        web_url=ref.web_url() if ref is not None else None,
        clone_url=ref.clone_url() if ref is not None else None,
        integration_branch=project.integration_branch, branch_prefix=project.branch_prefix,
        gate_quick_cmd=project.gate_quick_cmd, gate_full_cmd=project.gate_full_cmd,
        stack_up_cmd=project.stack_up_cmd, stack_down_cmd=project.stack_down_cmd,
        push_enabled=project.push_enabled,
        default_model=project.default_model,
        default_agent_id=project.default_agent_id, default_provider=project.default_provider,
        default_persona=project.default_persona,
        merge_lane=lane.active, merge_lane_reason=lane.reason,
        merge_lane_offered=lane.offered,
        merge_lane_batch_max=project.merge_lane_batch_max,
        live_cast_offered=project.org_id in live_cast,
        port_vars=project.port_vars, port_block=project.port_block,
        tech_stack=project.tech_stack, notes=project.notes,
        agent_instructions=project.agent_instructions,
        jira_credential_id=project.jira_credential_id,
        # Here only: `list_projects` returns this function's output, and that is what the edit page reads.
        business_analyst_id=project.business_analyst_id, architect_id=project.architect_id,
        ingested_at=project.ingested_at,
        updated_at=project.updated_at,
        egress_allow=list(project.egress_allow or []), egress_deny=list(project.egress_deny or []),
        **microvm,
        on_microvm=project.id in vm.on_microvm,
        dispatch_block=DISPATCH_BLOCKS.get(vm.dispatch_blocks.get(project.id, "")),
        dispatch_block_kind=vm.dispatch_blocks.get(project.id),
        repos=[project_repo_out(row) for row in repos],
    )


def _find_conflict(
    session: Session, *, name: Optional[str], org_id: Optional[UUID], exclude_id: Optional[int] = None,
) -> tuple[Optional[str], Optional[Project]]:
    """Return ("name", project) for the row in `org_id` already carrying `name`, excluding `exclude_id`.

    Per org (`uq_projects_org_name`): another org's project of the same name is no conflict, so nothing
    here can reveal it.

    `repo_url` is deliberately NOT unique: it is only where a project is cloned from, and several projects
    may share one repo — the project id is the identity everywhere (paths, compose stacks, and the guards'
    lookup, `tools/project_config.py::config_for`)."""
    if name:
        stmt = select(Project).where(Project.org_id == org_id, Project.name == name)
        if exclude_id is not None:
            stmt = stmt.where(Project.id != exclude_id)
        existing = session.scalar(stmt)
        if existing is not None:
            return "name", existing
    return None, None


def _race_conflict_detail(exc: IntegrityError, name: Optional[str]) -> str:
    """The 409 for a `projects` unique constraint the flush lost after `_find_conflict`'s pre-check saw nothing.

    Only a constraint Postgres names is answered, each with its own sentence (`uq_projects_org_name`,
    `uq_projects_org_id_port_block`); others re-raise. Both keys are per org, so the colliding row is
    always the caller's own org's and the plain sentence reveals nothing across orgs. No id: the row
    committed after this transaction's snapshot, so it is not re-read here."""
    constraint = constraint_name(exc)
    if constraint == "uq_projects_org_id_port_block":
        return "that port block is already used by another project — pick a free one or leave it blank"
    if constraint != "uq_projects_org_name":
        raise exc
    # Only a name write can reach this constraint, so `None` here is that invariant failing loudly.
    if name is None:
        raise exc
    return f"project with name '{name}' already exists"


#: A project's MicroVM fields (`ProjectUpdate`'s): its egress edits and its VM size override.
_MICROVM_FIELDS = frozenset(("egress_allow", "egress_deny", "microvm_vcpus", "microvm_ram_mib", "microvm_disk_gib"))


def _validated_fields(
    fields: dict[str, Any], *, creating: bool, current: Optional[Project] = None
) -> dict[str, Any]:
    """The single home for Project field validation/normalization, shared by create and update.

    `creating=True`: None-valued keys are stripped here, after the mandatory seven refuse nulls —
    naming a column with None in the INSERT would write NULL over its defaults. `creating=False`: an
    explicit `None` clears the column, except for the mandatory seven. `current` is the row an update
    edits, so a forge sent without its `repo_url` is checked against the stored one."""
    fields = dict(fields)
    if creating or "name" in fields:
        name = (fields.get("name") or "").strip()
        if not name:
            raise HTTPException(
                status_code=422,
                detail="name is required" if creating else "name cannot be blank",
            )
        fields["name"] = name
    if fields.get("tech_stack") and has_duplicate_tech_stack_entry(fields["tech_stack"]):
        raise HTTPException(
            status_code=422,
            detail="tech_stack contains a duplicate (layer, technology) entry",
        )
    # The mandatory seven. `in fields`, not truthiness: `False`, `0` and `''` are legitimate.
    if "integration_branch" in fields:
        branch = _required_value(fields["integration_branch"], field="integration_branch")
        _reject_illegal_git_ref(branch, field="integration_branch", kind="branch name")
        fields["integration_branch"] = branch
    if "branch_prefix" in fields:
        fields["branch_prefix"] = _normalized_branch_prefix(fields["branch_prefix"])
    # All four are shell strings a node executes, so one shape validator; a blank one means the project
    # has no such command.
    for field_name in _COMMAND_FIELDS:
        if field_name in fields:
            fields[field_name] = _validated_optional_command(fields[field_name], field=field_name)
    # Nullable, and a blank is a legitimate clear; the validator decides.
    if "default_model" in fields:
        fields["default_model"] = _validated_default_model(fields["default_model"])
    if "default_persona" in fields:
        fields["default_persona"] = _validated_default_persona(fields["default_persona"])
    if "repo_url" in fields:
        fields["repo_url"] = _validated_repo_url(fields["repo_url"])
    if {"repo_url", "forge", "forge_base_url"} & fields.keys():
        fields.update(_derived_forge(fields, current))
    if "push_enabled" in fields:
        # NOT NULL boolean: only an explicit null is left to catch.
        fields["push_enabled"] = _required_value(fields["push_enabled"], field="push_enabled")
    if not edition_features.enabled("microvm"):
        # The community edition has no MicroVMs: a resent value is no change, a changed one is not in it.
        for field_name in _MICROVM_FIELDS & fields.keys():
            if (fields.pop(field_name) or None) != (getattr(current, field_name, None) or None):
                raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    if edition_features.enabled("microvm"):
        from app import microvm_egress, microvm_sizing

        for field_name in ("egress_allow", "egress_deny"):
            if field_name in fields:
                # NOT NULL with `{}`: an explicit null clears the list.
                try:
                    fields[field_name] = microvm_egress.validated_names(fields[field_name])
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from None
        try:
            fields.update(microvm_sizing.validated(fields))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    if fields.get("merge_lane_batch_max") is not None:
        low, high = merge_lane_state.LANE_BATCH_MAX_BOUNDS
        value = fields["merge_lane_batch_max"]
        if isinstance(value, bool) or not low <= value <= high:
            raise HTTPException(status_code=422, detail=f"Set the batch size between {low} and {high}, or clear it.")

    if creating:
        # After the mandatory seven refuse nulls — see the docstring.
        fields = {key: value for key, value in fields.items() if value is not None}
    return fields


# The two "Humans" columns, validated against project access; imported because
# `access_admin.revoke_orphaned_project_roles` iterates the same pair.
_ROLE_FIELDS = PROJECT_ROLE_FIELDS


def _validated_project_role(
    session: Session, value: UUID, *, field: str, project_id: int, actor: AdminUser,
) -> UUID:
    """One "Humans" field's user id, refused unless it names a real, non-system user who can already
    reach this project. Called once per field.

    Naming someone routes this project's human-gate questions to them (`notify_targets._role_recipient`),
    and any `projects:edit` holder may edit this, so the named user must already reach the project. The
    role grants no access of its own; answering still needs `tasks:edit`. The system actor is refused
    before the probe. 422 naming the field, never the account (no uuid→email oracle)."""
    user = session.get(AdminUser, value)
    if user is None:
        raise HTTPException(
            status_code=422,
            detail=f"{field} does not name a known user (id={value})",
        )
    if user.is_system:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} may not be the system actor: it is non-loginable, so it can never answer the "
                "questions this role exists to receive"
            ),
        )
    # The probe is `authz.named_user_reaches_project` (shared with the assignee door); only the refusal
    # sentence is this route's.
    if not named_user_reaches_project(session, user, project_id, actor=actor):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} names a user with no access to this project — naming them would grant "
                "them write access to its tasks, so the role may only be given to someone who can "
                "already reach the project. Grant them access to it first, then set the role"
            ),
        )
    return value


def _validated_jira_credential(
    session: Session, value: UUID, *, principal: Principal | None, org_id: Optional[UUID]
) -> UUID:
    """The `jira_credential_id` a project is being pointed at, refused unless it names a real `jira`
    credential this caller can read AND that the ORG vouches for — an org credential (`owner_id` NULL). A
    personal row is never vouched, pooled or not: its owner can re-point its host.

    The type check keeps a `slack` id out of a jira column. Vouching matters because the control plane
    uses this credential for `routes/jira.py` lookups and unattended `app/task_external_comment.py` posts:
    any member can mint a PERSONAL credential (`authz.stamp_owner_on_create`), so readability alone would
    let a project member aim the CP at any host. Only an Org Owner/Admin makes an org row. Cost: a personal credential cannot back a project's Jira link. `principal is None` is
    refused. 422, never echoing the credential's name or host. The credential must also be the project's
    org's (`org_id`, the row's org): the operator plane's seam narrows nothing, so without this an operator
    could pair one org's project with another's credential and meet the same-org composite key as a 500.
    `None` (an unstamped create) leaves the seam to decide."""
    credential = session.get(Credential, value)
    vouched = credential is not None and credential.owner_id is None
    if (
        not vouched
        or (org_id is not None and credential.org_id != org_id)
        or principal is None
        or not can_access(session, principal, "credential", value, mode=ObjectAccess.READ)
    ):
        # One message for "no such row", "not vouched" and "not yours" — no existence oracle.
        raise HTTPException(
            status_code=422,
            detail=(
                "jira_credential_id does not name a credential you can use. Pick one from the "
                "Credentials list, or ask for access to it first"
            ),
        )
    if credential.type != "jira":
        raise HTTPException(
            status_code=422,
            detail=f"jira_credential_id must reference a 'jira' credential, not '{credential.type}'",
        )
    # The control plane opens it for Jira lookups and posts (D1, AC-5.3).
    credential_consumers.refuse_if_nodevault(credential, credential_consumers.PROJECT_JIRA)
    return value


#: How many auto-chosen port blocks a create tries before its unique-key loss is answered 409.
_AUTO_PORT_BLOCK_ATTEMPTS = 5

#: The lowest block a create auto-allocates. Blocks below it stay free for a caller to name explicitly
#: (a `port_block` under 10 is still valid when sent), so auto-chosen projects never crowd them out.
AUTO_PORT_BLOCK_FLOOR = 10


def _next_free_port_block(session: Session, org: Optional[UUID]) -> int:
    """The lowest integer at or above `AUTO_PORT_BLOCK_FLOOR` not already held by some project in this
    org, so a freed block is reused before the range grows (`[10, 11, 13]` -> `12`, not `14`). Blocks
    below the floor are never auto-chosen, held or not.

    Per org, so allocations disclose nothing about other tenants and two orgs may share a block (bands
    collide only on one host, and a host belongs to one org; spec §4.4). `UNIQUE(org_id, port_block)`
    refuses a concurrent duplicate. Other insert paths get NULL (no server default can say "next free")."""
    taken = set(
        session.scalars(
            select(Project.port_block).where(
                Project.org_id == org, Project.port_block.is_not(None)
            )
        )
    )
    candidate = AUTO_PORT_BLOCK_FLOOR
    while candidate in taken:
        candidate += 1
    return candidate


# What a project with no repository does not use: the Edit form hides them, and `_without_repo_fields`
# drops them from a write that leaves the project with none, so the stored values come back unchanged when a
# repository is connected again.
_REPO_ONLY_FIELDS = (
    "repo_url", "forge", "forge_base_url", "integration_branch", "branch_prefix", "push_enabled",
    "gate_quick_cmd", "gate_full_cmd", "stack_up_cmd", "stack_down_cmd",
)


#: What a system project (`models.Project.system_kind`) never takes: its kind and its repository are what
#: make it the platform's (`app/node_targets.py`). Name, notes and the like stay editable.
_SYSTEM_PROJECT_FIXED_FIELDS = ("system_kind", *_REPO_ONLY_FIELDS)


def _refuse_system_project_edit(project: Project, sent: dict[str, Any]) -> None:
    """422 when `sent` changes a field a system project fixes, or gives an ordinary project a `system_kind`. A
    resend of the stored value passes: the edit form sends every field."""
    if "system_kind" in sent and sent["system_kind"] != project.system_kind:
        raise HTTPException(status_code=422, detail="a project's system kind is set by the platform, never edited")
    if project.system_kind is None:
        return
    changed = sorted(f for f in _SYSTEM_PROJECT_FIXED_FIELDS if f in sent and sent[f] != getattr(project, f))
    if sent.get("repos"):
        # A system project never has a repository row, so any non-empty set is a change.
        changed.append("repos")
    if changed:
        raise HTTPException(
            status_code=422,
            detail=f"{project.name} is a system project, so its {', '.join(changed)} cannot be changed",
        )


def _refuse_system_project_delete(project: Project) -> None:
    """422 for deleting a system project: the platform recreates and relies on it (`app/node_targets.py`)."""
    if project.system_kind is not None:
        raise HTTPException(
            status_code=422,
            detail=f"{project.name} is a system project, so it cannot be deleted",
        )


def _delete_project_tasks(session: Session, project_id: int, actor: AdminUser) -> int:
    """Delete every Task filed in this project, as the task list's Delete does (`delete_task_core`, live ones
    included — their runners are stopped): `tasks.project_id` carries no delete action, and a project is
    deletable at any time. Hidden rows and other members' drafts too — the project goes, so
    everything in it does. Each delete commits; a child already removed with its parent is skipped. Re-read
    until none is left, so a task filed meanwhile (a cron, an automation) cannot fail the row's delete.
    Returns how many were deleted. Blocking work (commits, intents, object store): callers on the event loop
    run it in the threadpool."""
    from app.routes.tasks.delete import delete_task_core

    deleted = 0
    while task_ids := session.scalars(
        select(Task.id).where(Task.project_id == project_id).order_by(Task.id)
    ).all():
        for task_id in task_ids:
            task = session.scalar(select(Task).where(Task.id == task_id))
            if task is not None:
                delete_task_core(task, session, allow_live=True, actor=actor)
                deleted += 1
    return deleted


def _will_have_repo(sent: dict[str, Any], current: Optional[Project]) -> bool:
    """Whether the project has a repository once `sent` is applied: a `repos` set says it, else a `repo_url` (the
    legacy single-repo write), else it stays as stored (a create: none)."""
    if sent.get("repos") is not None:
        return bool(sent["repos"])
    if "repo_url" in sent:
        return bool((sent["repo_url"] or "").strip())
    return current is not None and current.has_repo


def _without_repo_fields(sent: dict[str, Any], current: Optional[Project]) -> dict[str, Any]:
    """`sent` minus `_REPO_ONLY_FIELDS` when the project will have no repository — never validated, written or
    cleared, so the stored values come back when one is connected. `repo_url` stays: it is how a legacy write
    removes the repository."""
    if _will_have_repo(sent, current):
        return sent
    return {key: value for key, value in sent.items() if key == "repo_url" or key not in _REPO_ONLY_FIELDS}


# The seven branch/gate/stack fields whose authorship is restricted — read by both authority checks and
# spelled once. `tools/project_config.py::GOVERNANCE_FIELDS` stays at five deliberately.
_GOVERNANCE_FIELDS = (
    "integration_branch", "branch_prefix", "gate_quick_cmd", "gate_full_cmd",
    "stack_up_cmd", "stack_down_cmd", "push_enabled",
)
# An importable alias for `app/routes/agent_project_settings.py`, not a second tuple. The underscored name
# stays the definition: the authorization-policy test's census keys on
# `("projects.py", "_has_authority_to_author_governance", "nodes:edit")`.
GOVERNANCE_FIELDS = _GOVERNANCE_FIELDS


def _governance_named(fields: dict[str, Any], project: Optional[Project]) -> list[str]:
    """The governance fields a payload authors: those it names, plus `integration_branch` when its `repos` set
    would change the primary's branch (`_repos_author_the_branch`). `project` None is a create, compared with the
    column default."""
    named = [field_name for field_name in _GOVERNANCE_FIELDS if field_name in fields]
    rows = fields.get("repos")
    if rows and "integration_branch" not in named and _repos_author_the_branch(rows, project):
        named.insert(0, "integration_branch")
    return named


def _reject_governance_from_a_node_token(
    fields: dict[str, Any], principal: Principal | None
) -> None:
    """A cookie-less `X-Agent-Token` caller (`principal is None`) may not author any of the seven
    governance fields: 422 naming the one it sent.

    Defense in depth: no route here admits a principal-less caller today (`app.auth.NODE_TOKEN_API_PATHS`
    grants `GET` only), but `None` is allow-all elsewhere in `app/authz/`, and four of these fields are
    handed to a shell. Refused, not dropped."""
    if principal is not None:
        return
    for field_name in _governance_named(fields, None):
        if field_name in _GOVERNANCE_FIELDS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{field_name} may not be set by a node-token caller: an X-Agent-Token create has no "
                    "principal, so it may not author this project's governance — the gate and stack "
                    "commands are executed by a shell on a runner, and the branch/push fields decide "
                    "what that runner may push. Omit it to take the column default, then set it from the "
                    "console as a signed-in user."
                ),
            )
    # Two non-governance fields refused for the same reason (allow-all is wrong with no principal), each
    # with its own refusal. `jira_credential_id`: the fault here is authorship — a node secret may not
    # author Jira wiring — distinct from `_validated_jira_credential`'s own refusal.
    if "jira_credential_id" in fields:
        raise HTTPException(
            status_code=422,
            detail=(
                "jira_credential_id may not be set by a node-token caller: an X-Agent-Token create "
                "has no principal, so nothing bounds which credential it could name, and pointing a "
                "project at one is what makes the control plane use that account's Jira token. Omit "
                "it, then set it from the console as a signed-in user."
            ),
        )
    # `agent_instructions` is injected into every dispatched persona's system prompt as trusted
    # directives, so it must always have an attributable author; a node token has none. Not the seven's
    # authority argument — any `projects:edit` holder may set it.
    if "agent_instructions" in fields:
        raise HTTPException(
            status_code=422,
            detail=(
                "agent_instructions may not be set by a node-token caller: this text is injected as "
                "trusted directives into the system prompt of every runner dispatched on this project, "
                "and an X-Agent-Token create has no principal — so nobody would be on record as having "
                "authored it. Omit it, then set it from the console as a signed-in user."
            ),
        )


def _administers_a_node_in(session: Session, principal: Principal, org_id: UUID) -> bool:
    """Does `principal` reach, for WRITE, at least one node in `org_id`?

    The node half of the governance door (`_has_authority_to_author_governance` says why). The same
    `can_access(..., ObjectAccess.WRITE)` probe `PUT /api/nodes/{node_id}` makes, asked of any of the
    org's nodes. Two steps: `node_reach_clause` narrows candidates, then `can_access` answers WRITE per
    row — no `.limit(1)`, which would break once `NODE_SPEC` gains a narrower `write_clause`. `Node.org_id == org_id` restates the seam so
    the answer is plane-independent. Not reached for a superadmin."""
    candidates = session.scalars(
        select(Node.id)
        .where(Node.org_id == org_id)
        .where(node_reach_clause(principal, Node.id))
    ).all()
    return any(
        can_access(session, principal, "node", node_id, mode=ObjectAccess.WRITE)
        for node_id in candidates
    )


def _has_authority_to_author_governance(
    session: Session,
    actor: AdminUser,
    principal: Principal | None,
    project: Project | None,
) -> bool:
    """The door itself: may this caller author `_GOVERNANCE_FIELDS` on `project` (or, for `None`, on a
    project they are about to create)? Yes if they are a superadmin, or hold `nodes:edit` org-wide and
    WRITE reach on a node in their own org and the project is in that org.

    One home for the two mutating paths (`_require_authority_to_author_governance`), the two
    `governance-authority` routes and the agent read (`app/routes/agent_project_settings.py`, asked of
    the launcher) — the browser cannot see `role_bindings`, so a mirror would drift.

    `projects:edit` is not enough: the gate and stack commands run under a shell on every runner. The
    org-admin door is sound because `nodes.org_id` is immutable (`app/tenancy/node_org.py`) and placement
    is org-floored (`app/placement_grants.py`), so the command runs only on the caller's own org's
    machines. The key alone administers zero machines, so WRITE reach on a node is required
    (`_administers_a_node_in`). The org comparison reads
    `Principal.org_id`, not `seam.current_scope()` (which may be the `OPERATOR` plane). Defence in depth:
    the seam already hides cross-org rows. On create the org half holds by construction (the insert is
    stamped with the caller's org, or for the operator alone a named tenant, `create_project`); the node half is not exempted. A NULL `project.org_id` is refused
    (`None == None` must not read as "my org"). Evaluated key → org → node, cheapest first.
    """
    if actor.is_superadmin:
        return True
    # The org-admin arm. `principal is None` (node token, already 422'd) fails it.
    if principal is not None and principal.has_org(permission_key("nodes", "edit")):
        own_org = principal.org_id
        # `own_org is not None` first: covers an unstamped create and `None == None` on update. The node
        # conjunct is last only because it is the one DB round trip.
        if (
            own_org is not None
            and (project is None or project.org_id == own_org)
            and _administers_a_node_in(session, principal, own_org)
        ):
            return True
    return False


# The public alias for the agent read — one implementation. The underscored name stays the definition for
# `test_authz_policy.py`'s `(module, function, key)` census.
has_authority_to_author_governance = _has_authority_to_author_governance


def _require_authority_to_author_governance(
    session: Session,
    fields: dict[str, Any],
    actor: AdminUser,
    principal: Principal | None,
    project: Project | None,
) -> None:
    """The raising half: 403 unless `_has_authority_to_author_governance` admits a payload that names one
    of the seven. A payload touching none (`notes`, `tech_stack`) is unaffected.

    The payload test comes first (free), so an ordinary edit never pays the grant probe. Called on both
    mutating paths — creating with these values is the same capability. A handler check because the
    condition is a property of the payload; tests authorship (`in fields`), not change. 403, not 422. The
    refusal sentence forks by seat: a tenant reads `app/tenancy/copy.py::governance_authority`."""
    named = _governance_named(fields, project)
    if not named:
        return
    if _has_authority_to_author_governance(session, actor, principal, project):
        return
    if is_tenant_org(actor.org_id):
        raise HTTPException(status_code=403, detail=governance_authority(named))
    raise HTTPException(
        status_code=403,
        detail=(
            "superadmin, or an org-admin holding nodes:edit org-wide AND write reach on a node in this "
            f"project's org, is required to change {', '.join(named)}: the gate and stack commands are "
            "executed by a shell as the runner user on every node (at the end of every runner turn, on "
            "every commit, and on every stack start or stop), and the branch/push fields decide what "
            "that runner may push — so this project's governance is not editable with the project "
            "role's projects:edit permission. The org-admin door is authority over the machines the "
            "command runs on, which are this org's own; it does not open on another org's project. Ask "
            "a superadmin, or PUT only the non-governance fields."
        ),
    )


def _create_project_row(
    session: Session, body: ProjectCreate, principal: Principal | None, actor: AdminUser | None,
    org_id: Optional[UUID],
) -> Project:
    """Insert/409 logic for `POST /api/projects` (Connect Existing).

    The one insertion point. No creator grant: the creator holds `projects:create`, which only org-wide
    roles carry, so they already reach the new row.
    `principal` and `actor` have no defaults, so a new create path must state its actor. `org_id` is the
    org the row will be stamped with (the bound one), for the port block, gate defaults and cap."""
    # `exclude_unset`, not `exclude_none`: `_validated_fields` strips None itself, after the mandatory
    # seven refuse explicit nulls.
    # `placement` and `org_id` are not settable columns, and the default agent needs the row's org; `create_project`
    # applies them.
    sent = _without_repo_fields(
        body.model_dump(exclude_unset=True, exclude={"placement", "org_id", *_DEFAULT_AGENT_FIELDS}), None,
    )
    # Authority before shape.
    _reject_governance_from_a_node_token(sent, principal)
    # Same order as `update_project`: no-principal 422 first, then the authority 403. Skipped with no
    # actor — no caller reaches that today (the route takes `require_user`); kept so re-admitting a
    # token caller fails closed. `project=None` is the create path (see the door's docstring).
    if actor is not None:
        _require_authority_to_author_governance(session, sent, actor, principal, None)
    sent, repo_rows = _split_repos(sent, None)
    repo_specs = _validated_repos(repo_rows, []) if repo_rows is not None else None
    fields = _validated_fields(sent, creating=True)
    # The repository columns are the primary row's projection, written only by `app/project_repos.py`.
    legacy_repo = {key: fields.pop(key) for key in _LEGACY_REPO_FIELDS if key in fields}
    # The gate and stack commands from the org's Projects > Defaults settings — server-side seeding, so
    # after the governance authorship checks above (they judge only what the body sent). No actor (no
    # caller today) has no org to read an org-tier knob for, so it gets the platform default: none.
    command_defaults = (
        settings_defaults.project_create_defaults(org_id) if actor is not None
        else dict.fromkeys(_COMMAND_FIELDS, "")
    )
    # Validated like a sent value (defense in depth): the settings tier has its own writer checks, but the
    # row is what a node hands to a shell, so it is not trusted to have kept them.
    for field_name in _COMMAND_FIELDS:
        if field_name not in fields:
            fields[field_name] = _validated_optional_command(command_defaults[field_name], field=field_name)
    # Not a settings column — applied after construction (see below).
    updated_at = fields.pop("updated_at", None)
    name = fields["name"]

    field, existing = _find_conflict(session, name=name, org_id=org_id)
    if existing is not None:
        conflicting_value = name
        raise HTTPException(
            status_code=409,
            detail=(
                f"project with {field} '{conflicting_value}' already exists "
                f"(id={existing.id}, name={existing.name})"
            ),
        )

    auto_block = "port_block" not in fields
    if auto_block:
        # The org the row will be stamped with, so the block must be free there. `None` allocates in the
        # unstamped space.
        fields["port_block"] = _next_free_port_block(session, org_id)

    # Checked on create too (unlike the edit-only Humans pair). `in fields`: `_validated_fields` has
    # already stripped None-valued keys.
    if "jira_credential_id" in fields:
        fields["jira_credential_id"] = _validated_jira_credential(
            session, fields["jira_credential_id"], principal=principal, org_id=org_id
        )

    if principal is not None:
        # Provenance only (authorization reads `role_bindings`); conditional, since "no recorded
        # creator" is a real state.
        fields["created_by_id"] = principal.user_id

    # The Projects cap (`app/org_limits.py`), in the org the row will be stamped with, before the write.
    if actor is not None:
        org_limits.require_headroom(session, org_id, "projects")
    project = Project(**fields)
    if updated_at is not None:
        # Explicit so the INSERT carries the source's timestamp (`onupdate` fires only on UPDATE).
        project.updated_at = updated_at
    # A block THIS route chose lost a race to a concurrent create (both read the same lowest free block):
    # choose again rather than 409 a caller who named none. A block the caller named still 409s. Added
    # INSIDE the savepoint: `begin_nested()` flushes what is pending before it opens one.
    for attempt in range(_AUTO_PORT_BLOCK_ATTEMPTS):
        try:
            with session.begin_nested():
                session.add(project)
                session.flush()
            break
        except IntegrityError as exc:
            if (auto_block and attempt + 1 < _AUTO_PORT_BLOCK_ATTEMPTS
                    and constraint_name(exc) == "uq_projects_org_id_port_block"):
                # The savepoint rollback left the row transient; the next pass adds it back with this block.
                with session.no_autoflush:
                    project.port_block = _next_free_port_block(session, org_id)
                continue
            session.rollback()
            raise HTTPException(status_code=409, detail=_race_conflict_detail(exc, name))
    actor_id = actor.id if actor is not None else None
    if repo_specs is not None:
        project_repos.replace(session, project, repo_specs, actor_id)
    elif legacy_repo:
        project_repos.upsert_primary(session, project, legacy_repo, actor_id)
    # While the org is still onboarding (D15), a new project is placed on every node of its org.
    from app.routes.assignments import auto_assign_as  # deferred: that module imports this one

    auto_assign_as(session, actor, project_id=project.id)
    return project


def _placement_nodes(session: Session, placement: ProjectPlacement) -> list[tuple[str, Node]]:
    """The nodes a placement names, each with the caller's spelling of its id, refused before any write: 404
    unknown (seam-scoped), 409 disabled, and the Multi-Node Host refusal (`app/vnodes.py`). A node named twice
    is placed once."""
    nodes: dict[UUID, tuple[str, Node]] = {}
    for node_id in placement.node_ids:
        node_uuid = node_id_or_404(session, node_id)
        node = get_or_404(session, Node, node_uuid, detail=f"unknown node '{node_id}'")
        if not node.enabled:
            raise HTTPException(
                status_code=409,
                detail=f"node '{node_id}' is disabled, so no slot on it would lease — enable it first",
            )
        if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
            from app import vnodes

            vnodes.refuse_work_on_host(session, node.id)
        nodes[node.id] = (node_id, node)
    return list(nodes.values())


def _assign_without_slots(session: Session, node: Node, project: Project, actor: AdminUser) -> None:
    """Place a project on `node`: an explicit assignment holding no static slot. One the onboarding latch
    already made (`auto_assign_as`) is kept. Flushes, never commits."""
    row = session.scalar(select(NodeProject).where(
        NodeProject.node_id == node.id, NodeProject.project_id == project.id
    ))
    if row is None:
        session.add(NodeProject(
            # No override (`static_slots` NULL): no static slot; each task makes its own worktree.
            node_id=node.id, project_id=project.id, org_id=node.org_id,
            created_by_id=actor.id, updated_by_id=actor.id,
        ))
    session.flush()


@router.post("", response_model=ProjectCreateOut, status_code=201)
@feature("projects", "create")
def create_project(
    body: ProjectCreate,
    # `require_user`: `app.auth.NODE_TOKEN_API_PATHS` grants the token `GET /api/projects` only, and this
    # row carries what runners clone and act on.
    actor: AdminUser = Depends(require_user),
    # The same principal `actor` names, in the shape every authz helper takes.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ProjectCreateOut:
    """Connect Existing: create a DB row for an already-existing repo. No scaffolding is triggered; the
    project's onboarding task is filed and started (`app/project_onboarding.py`).

    `org_id` naming another org is refused (422) unless the caller is the operator, whose create then runs
    in that org (`_create_project_in`)."""
    target = body.org_id
    if target is None or target == actor.org_id:
        return _create_project_in(session, body, actor, principal, actor.org_id)
    if principal is None or not principal.is_operator:
        raise HTTPException(status_code=422, detail="org_id must be your own organization")
    if not is_tenant_org(target):
        raise HTTPException(status_code=422, detail="org_id must be a tenant organization")
    # The operator seeding a tenant: the guard bound this write to the operator's own org (R5.7), so leave
    # it and enter the NAMED org on a session of its own — every read (name conflict, port block, node,
    # credential, cap) and the stamp are then that org's, as for its own member. Admitted by
    # `principal.is_operator` just above; `named_org` refuses a missing or disabled org with a 422.
    if body.placement is not None:
        # A pool clone authenticates as its declarer, and the operator is no member of the tenant org.
        raise HTTPException(status_code=422, detail="placement is the org's own to declare — create without it")
    with seam.detached(), seam.bound_to(target), SessionLocal() as bound:
        return _create_project_in(bound, body, actor, principal, named_org(bound, target), audit_in=target)


def _create_project_in(
    session: Session, body: ProjectCreate, actor: AdminUser, principal: Principal | None, org_id: UUID,
    *, audit_in: Optional[UUID] = None,
) -> ProjectCreateOut:
    """`create_project` on a session bound to `org_id`.

    With `placement`, the slot-writer bar and the nodes are checked before any write, and the assignments are
    written in the same transaction as the row, so any refusal leaves no project behind."""
    placement = body.placement
    nodes: list[tuple[str, Node]] = []
    if placement is not None:
        require_slot_writer(actor, principal)
        nodes = _placement_nodes(session, placement)
    project = _create_project_row(session, body, principal, actor, org_id)
    # After the flush stamped the row's org; nothing is committed yet, so a refusal leaves no project behind.
    default_agent = body.model_dump(exclude_unset=True, include=set(_DEFAULT_AGENT_FIELDS))
    if default_agent:
        _apply_default_agent(session, default_agent, project)
        project.default_agent_id = default_agent.get("default_agent_id")
        project.default_provider = default_agent.get("default_provider")
    placement_warning: Optional[str] = None
    for node_id, node in nodes:
        # The row's org is known only after the flush stamped it. A node of another org is refused in
        # `node_id_or_404`'s words, so a caller cannot tell it from a node that does not exist.
        if node.org_id != project.org_id:
            raise HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
        # No static slot: each task makes its own worktree there (`slot_allocation.add_dynamic`).
        _assign_without_slots(session, node, project, actor)
    if project.has_repo and nodes:
        # Every placed node's slots are declared as the creator, so their own connection is the one the clones
        # use: asked once, named on the first node. Not the slot rows, which do not exist yet.
        _, node = nodes[0]
        placement_warning = _clone_git_access_warning(
            session, node.name, node.id, project.id, project.name, declarer_id=actor.id
        )
    if audit_in is not None:
        # The operator seeding a tenant: filed in the tenant's own trail too (the middleware row is the actor's).
        record_write(
            session, actor, "project_created", f"Created project {project.name}",
            data={"project_id": project.id, "name": project.name}, in_org=audit_in,
        )
    session.commit()
    # After the commit, best effort: a refused onboarding task never undoes the project. Not when the operator
    # seeds a tenant (`audit_in`): the task would run in that org as someone who is no member of it, the reason
    # `create_project` refuses a placement there too.
    onboarding_task_id = (
        file_onboarding_task(session, project, actor, principal) if audit_in is None else None
    )
    session.refresh(project)
    # After the refresh: `write_seam.stamp_org` sets `org_id` during the flush, so this resolves the value
    # the row landed with (`app/tenancy/labels.py`).
    out = _project_out(
        project, org=org_ref_of(org_refs(session, [project.org_id]), project.org_id),
        lane=merge_lane_state.lane_state(session, project), vm=microvm_page(session, [project]),
        repos=project_repos.repos_of(session, project.id), live_cast=live_cast_holders(session, [project.org_id]),
    )
    return ProjectCreateOut(
        **out.model_dump(), placement_warning=placement_warning, onboarding_task_id=onboarding_task_id,
    )


class GovernanceAuthorityOut(BaseModel):
    """The door's own answer, on the wire — what `_has_authority_to_author_governance` said about this
    caller and this project (or a create), so a surface offers exactly what the mutation will accept.

    One boolean for all seven, since they share one door. Named for the authority, not the viewer: the
    rule cannot be rebuilt in a browser (the node half reads `role_bindings`)."""

    may_author_governance: bool


# The two verdict routes, each declared with the same access as the mutation it predicts. Not an optional
# query param (`authz._enforce_object_params` reads path params only); not a `ProjectOut` field (the
# create form has no row, and a per-row grant probe on `list_projects` would be waste). Pinned to the
# guard by `tests/test_projects_governance_org_admin.py`. A future `GET /api/projects/{project_id}` must be
# declared below the id-less route, or `project_id: int` would 422 on `governance-authority`.


@router.get("/{project_id}/governance-authority", response_model=GovernanceAuthorityOut)
@feature("projects", "edit", object_type="project", object_params=("project_id",))
def project_governance_authority(
    project_id: int,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> GovernanceAuthorityOut:
    """May this caller author this project's seven governance fields? The Edit form's Governance tab
    reads it; `_require_authority_to_author_governance` enforces the same answer on the PUT.

    The same read `update_project` makes, so a cross-org row is a 404 here as there."""
    project = project_or_404(session, project_id)
    return GovernanceAuthorityOut(
        may_author_governance=_has_authority_to_author_governance(
            session, actor, principal, project
        )
    )


class ProjectGitReachOut(BaseModel):
    """Whether the CALLER's git credentials can write to this project's primary repository — what a task they
    start is served (`git_reach.writer_for_user`). `blocked`: none can, `problem` says what is missing and
    `fix_url` where to fix it; `fallback`: their selected connection cannot, so `credential` (another of theirs)
    is used; `ok`: it can, or GitHub cannot tell; `unknown`: nothing to check (no repository, another forge, no
    connection — those have their own prompts)."""

    state: Literal["ok", "fallback", "blocked", "unknown"]
    credential: Optional[str] = None
    problem: Optional[str] = None
    fix_url: Optional[str] = None


@router.get("/{project_id}/git-reach", response_model=ProjectGitReachOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def project_git_reach(
    project_id: int,
    actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> ProjectGitReachOut:
    """The project page's and Start's preflight: can the caller's own credentials push and open PRs here? Asked
    of the caller only — a task they start is served their credentials — and never cached past `git_reach`'s own
    answer cache."""
    project = project_or_404(session, project_id)
    try:
        repo = git_access.project_repo(project, retry="then reload this page")
    except git_access.GitAccessError:
        return ProjectGitReachOut(state="unknown")
    if not project.has_repo or repo.forge != "github":
        return ProjectGitReachOut(state="unknown")
    try:
        pick = git_reach.writer_for_user(session, actor.id, repo)
    except git_access.GitAccessError:
        return ProjectGitReachOut(state="unknown")
    if pick.problem:
        return ProjectGitReachOut(state="blocked", credential=pick.credential.name, problem=pick.problem,
                                  fix_url=pick.fix_url)
    return ProjectGitReachOut(state="fallback" if pick.fallback else "ok", credential=pick.credential.name)


@router.get("/governance-authority", response_model=GovernanceAuthorityOut)
@feature("projects", "create")
def create_governance_authority(
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> GovernanceAuthorityOut:
    """The same question with no row — what the Create form asks, and what `_create_project_row` will
    answer with a 403 if it is `false`.

    `project=None` satisfies the org half by construction but not the grant half, so an org-admin with no
    node of their own gets `false`. `require_user`, like the POST it predicts."""
    return GovernanceAuthorityOut(
        may_author_governance=_has_authority_to_author_governance(session, actor, principal, None)
    )


# Per-side cap on the `agent_instructions` before/after in the audit row: the column is unbounded and a
# row is written on every edit. Not `dispatch_compose.placeholders._PROJECT_INSTRUCTIONS_MAX_CHARS`, which bounds what
# a runner is shown.
_AUDIT_INSTRUCTIONS_MAX_CHARS = 2000


def apply_project_update(
    session: Session,
    project: Project,
    sent: dict[str, Any],
    actor: AdminUser,
    principal: Principal | None,
) -> Project:
    """The whole project-row write: both authority checks, every field validation, the conflict
    refusals, the audit row, the commit, and the two post-commit node dispatches. `sent` is
    `model_dump(exclude_unset=True)` (an explicit null is a clear); `project` is already resolved.
    Returns it, refreshed.

    Shared by `PUT /api/projects/{project_id}` and the agent `PUT /api/agent/tasks/{task_id}/project`
    (`app/routes/agent_project_settings.py`, applied to the task's launcher). A second implementation
    would become the way around the stricter door. The order below
    is load-bearing: no-principal 422 before the authority 403, and every before-read ahead of the
    setattr loop.
    """
    # Before the repository fields are dropped below: a system project refuses a repo change, never ignores it.
    _refuse_system_project_edit(project, sent)
    sent = {field: value for field, value in sent.items() if field != "system_kind"}
    # A project with no repository ignores its repository fields (`_without_repo_fields`), before any check.
    has_repo_after = _will_have_repo(sent, project)
    sent = _without_repo_fields(sent, project)
    # Authority before shape, both on the pre-validation `sent` (an explicit null names the field too).
    # The route's own `projects:edit` declaration has already run.
    _reject_governance_from_a_node_token(sent, principal)
    # Then the 403. `project` is the caller's already-read (seam-filtered) row, for the org half.
    _require_authority_to_author_governance(session, sent, actor, principal, project)
    sent, repo_rows = _split_repos(sent, project)
    repo_specs = (
        _validated_repos(repo_rows, project_repos.repos_of(session, project.id)) if repo_rows is not None else None
    )
    # Connecting the first repository or removing the last re-sizes every assignment's statics (declares them, or
    # retires them), so it is a placement change and takes the assignment gate (D13).
    if has_repo_after != project.has_repo:
        require_assignment_writer(actor, principal)
    has_repo_before = project.has_repo

    # Read before the setattr loop; the guard-sync dispatch turns on whether these two actually changed.
    guard_fields_before = (project.branch_prefix, project.push_enabled)
    # Same reason: the tmux fan-out at the bottom must dispatch nothing for a re-PUT of the same name.
    name_before = project.name
    # Same reason, for the audit: the write overwrites the prior prose, so this local is its only copy.
    agent_instructions_before = project.agent_instructions

    # `exclude_unset`: an explicit null is the edit (the form clears by PUTting one), so defaults do not
    # re-apply.
    updates = _validated_fields(sent, creating=False, current=project)
    # The repository columns are the primary row's projection, written only by `app/project_repos.py`.
    legacy_repo = {key: updates.pop(key) for key in _LEGACY_REPO_FIELDS if key in updates}
    if legacy_repo.get("repo_url") and any(
        _repo_keys(legacy_repo["repo_url"], legacy_repo.get("forge"), legacy_repo.get("forge_base_url"))
        & _repo_keys(row.repo_url, row.forge, row.forge_base_url)
        for row in project_repos.repos_of(session, project.id) if not row.is_primary
    ):
        raise HTTPException(status_code=422, detail=[_repo_error(
            ["repo_url"], "This repository is already connected to this project", "duplicate_repo",
        )])
    _apply_default_agent(session, updates, project)
    # The "Humans" pair, validated here (needs the project, unlike `_validated_fields`). An explicit null
    # is a clear and needs no check.

    # Only a changed value: `buildBody` resends both role keys on every save, and 422ing an unchanged
    # (maybe since-revoked) name would wedge the form. Orphans are
    # `access_admin.revoke_orphaned_project_roles`' job.
    roles_before = {field_name: getattr(project, field_name) for field_name in _ROLE_FIELDS}
    jira_credential_before = project.jira_credential_id
    for field_name in _ROLE_FIELDS:
        value = updates.get(field_name)
        if value is not None and value != roles_before[field_name]:
            updates[field_name] = _validated_project_role(
                session, value, field=field_name, project_id=project.id,
                # The whole actor: `authz.named_user_reaches_project` reads its org, as
                # `list_project_members` does.
                actor=actor,
            )

    # The same only-if-changed rule (`buildBody` resends it). A null clear needs no check.
    if updates.get("jira_credential_id") is not None and (
        updates["jira_credential_id"] != project.jira_credential_id
    ):
        updates["jira_credential_id"] = _validated_jira_credential(
            session, updates["jira_credential_id"], principal=principal, org_id=project.org_id
        )

    # `repo_url` is only where the project is cloned FROM: paths and compose stacks are keyed on the
    # project id (`app/node_clone.py`) and a node re-points each clone's `origin` at a changed URL
    # (`apps/jaahd/pool_origins.py`), so it is editable at any time — held slots included.
    field, existing = _find_conflict(
        session, name=updates.get("name"), org_id=project.org_id, exclude_id=project.id,
    )
    if existing is not None:
        conflicting_value = updates.get(field)
        raise HTTPException(
            status_code=409,
            detail=(
                f"project with {field} '{conflicting_value}' already exists "
                f"(id={existing.id}, name={existing.name})"
            ),
        )

    for field_name, value in updates.items():
        setattr(project, field_name, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail=_race_conflict_detail(exc, updates.get("name")),
        )
    # After the row's own flush, so a lost name race is still answered 409 above.
    if repo_specs is not None:
        project_repos.replace(session, project, repo_specs, actor.id)
    elif legacy_repo:
        try:
            project_repos.upsert_primary(session, project, legacy_repo, actor.id)
        except project_repos.PrimaryStillNeeded:
            session.rollback()
            raise HTTPException(status_code=422, detail=[_repo_error(
                ["repo_url"], _PICK_ANOTHER_PRIMARY, "primary_removed",
            )]) from None

    # The role columns carry before/after (naming someone confers task-write via
    # `authz.editable_task_clause`), in `routes/tasks/update.py::update_task`'s shape, only for fields that moved.
    written = set(updates) | set(legacy_repo) | ({"repos"} if repo_specs is not None else set())
    data: dict[str, Any] = {"project_id": project.id, "fields": sorted(written)}
    # `agent_instructions` too: trusted directives every runner follows, overwritten by this write
    # (a create's payload is already logged by `UserActivityMiddleware`). Capped per side; change-tested,
    # so a resend records nothing. First of the three: `test_activity_kinds` pins `data["role_changes"]`
    # to a 12-line window above the `record_write` below.
    if project.agent_instructions != agent_instructions_before:
        data["agent_instructions_change"] = {
            "before": (
                None if agent_instructions_before is None
                else agent_instructions_before[:_AUDIT_INSTRUCTIONS_MAX_CHARS]
            ),
            "after": (
                None if project.agent_instructions is None
                else project.agent_instructions[:_AUDIT_INSTRUCTIONS_MAX_CHARS]
            ),
        }
    # `str()` here: `user_activity._uuids_to_str` walks one level only, and nested uuids would raise at
    # the flush.
    role_changes: dict[str, dict[str, str | None]] = {}
    for field_name in _ROLE_FIELDS:
        before, after = roles_before[field_name], getattr(project, field_name)
        if before != after:
            role_changes[field_name] = {
                "before": None if before is None else str(before),
                "after": None if after is None else str(after),
            }
    if role_changes:
        data["role_changes"] = role_changes
    # Before/after for the Jira credential: repointing it changes which token the control plane uses.
    if project.jira_credential_id != jira_credential_before:
        data["jira_credential_change"] = {
            "before": jira_credential_before, "after": project.jira_credential_id,
        }
    record_write(
        session, actor, "project_updated", f"Updated project {project.name}", data=data,
    )
    session.commit()
    if project.has_repo != has_repo_before:
        # A repository flip re-sizes every assignment now, not at the next sweep: with no repo left, the free
        # statics are retired and a held one demoted; with one connected, the statics are declared again.
        slot_allocation.converge(session, project_id=project.id, actor=actor)
        session.commit()
    session.refresh(project)

    if (project.branch_prefix, project.push_enabled) != guard_fields_before:
        # These are markers in the clone's git config, not read back from this DB,
        # so a value change is inert until the guard is re-installed.

        # Fire-and-forget, post-commit: idempotent, and `check-access-guard.py` catches a lost dispatch.

        # `resolve_clone` only checks the path is computable; `sync_access_guard` may still fail if the
        # clone does not exist yet (accepted, covered by the conformance checker).

        # `repo_root` is injected by `_resolve_command_args` from its own allowlist.

        # Per plane via `default_dispatch_node`: a tenant's clone is on the tenant's node, never the CP's.
        # Its 409 (no eligible node) is caught and logged — the edit is committed and the re-install is
        # at-most-once by design.
        try:
            node_id: Optional[str] = default_dispatch_node(session, principal)
        except HTTPException as exc:
            # Only the stated 409 is skippable; any other status is a refusal and must reach the caller.
            if exc.status_code != 409:
                raise
            log.warning(
                "sync_access_guard for project %s not dispatched — no eligible node in the caller's org: %s",
                project.id, exc.detail,
            )
            node_id = None
        node = (
            session.scalar(select(Node).where(node_matches(node_id))) if node_id is not None else None
        )
        if node_id is not None and resolve_clone(node.jaah_home if node else None, project.id):
            args = _resolve_command_args(
                # The command_type is unchanged: renaming a bus command is a coordinated node-side change.
                "sync_access_guard",
                {
                    "project_id": project.id,
                    "branch_prefix": project.branch_prefix,
                    "push_enabled": project.push_enabled,
                },
                session, node_id,
            )
            dispatch_and_audit_sync(session, node_id, "sync_access_guard", args, actor.email)
        elif node_id is not None:
            # Never silent: an unsynced clone pushes under its old markers until the node next clones.
            log.warning(
                "sync_access_guard for project %s not dispatched — node %r resolves no clone (%s)",
                project.id, node_id, "no such node" if node is None else "the node has no jaah_home",
            )

    if project.name != name_before:
        _rerender_slot_window_names(session, project, name_before, actor)

    return project


@router.put("/{project_id}", response_model=ProjectOut)
@feature("projects", "edit", object_type="project", object_params=("project_id",))
def update_project(
    project_id: int,
    body: ProjectUpdate,
    actor: AdminUser = Depends(require_user),
    # For both authority checks: `principal is None` is the node-token caller, and the org-admin door
    # needs the key, org and node grants off one resolution.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ProjectOut:
    project = project_or_404(session, project_id)

    # The HTTP shell: every guard, the audit row and the dispatches are `apply_project_update`'s.
    project = apply_project_update(
        session, project, body.model_dump(exclude_unset=True), actor, principal
    )
    return _project_out(
        project, org=org_ref_of(org_refs(session, [project.org_id]), project.org_id),
        lane=merge_lane_state.lane_state(session, project), vm=microvm_page(session, [project]),
        repos=project_repos.repos_of(session, project.id), live_cast=live_cast_holders(session, [project.org_id]),
    )


#: The default-agent pair: one picker, two halves (`_apply_default_agent`).
_DEFAULT_AGENT_FIELDS = ("default_agent_id", "default_provider")

#: The one picker writes one half (DD12, `ck_projects_agent_or_provider`).
_DEFAULT_AGENT_OR_PROVIDER = "Pick a default built-in runner or a default SDK agent for this project, not both"


def _apply_default_agent(session: Session, updates: dict[str, Any], project: Project) -> None:
    """Validate the default-agent pair in `updates` in place: a built-in must be a known provider, an agent one
    the project's org may run (one 422 for missing and another org's, N7); both is a 422, and setting one half
    clears the other. A disabled or unfetched default is accepted: its tasks fail naming why (DD19). A changed
    default the org's plan does not carry (an SDK agent without Agents, a provider with no cell on its grid) is a 422."""
    agent_id, provider = updates.get("default_agent_id"), updates.get("default_provider")
    if agent_id is not None and provider:
        raise HTTPException(status_code=422, detail=_DEFAULT_AGENT_OR_PROVIDER)
    if "default_provider" in updates:
        if provider and provider not in agent_providers.PROVIDER_VALUES:
            raise HTTPException(
                status_code=422,
                detail=f"default_provider must be one of {', '.join(agent_providers.PROVIDER_VALUES)} "
                       f"(got {provider!r})",
            )
        updates["default_provider"] = provider or None
        if provider and provider != project.default_provider and not provider_auth_policy.provider_on_plan(
            session, project.org_id, provider
        ):
            raise HTTPException(status_code=422, detail=provider_auth_policy.refusal(session, project.org_id, provider))
        if provider:
            updates["default_agent_id"] = None
    if agent_id is not None and agent_id != project.default_agent_id:
        # Only a changed value: the form resends every key, and an unchanged (maybe since-disabled) default must
        # not wedge every save.
        agent = agent_resolution.load_agent(session, agent_id)
        if agent is None or not agent_resolution.visible_to(session, agent, project.org_id):
            raise HTTPException(status_code=422, detail="default_agent_id names no agent this organization can use")
        off = agent_resolution.cell_refusal(session, agent, project.org_id)
        if off is not None:
            raise HTTPException(status_code=422, detail=off)
    if agent_id is not None:
        updates["default_provider"] = None


def _rerender_slot_window_names(
    session: Session, project: Project, name_before: str, actor: AdminUser
) -> None:
    """After a rename: re-derive every declared slot's `name` and relabel its live tmux window.

    The window name is composed at launch (`apps/jaahd/fleet_launch.py::slot_window_name`) and never
    re-read. `name_before` identifies a derived `<old project>/<number>` label (`slot_title.stored_opinion`),
    which would otherwise pass for an operator's choice. Two passes: pass 1 re-derives `name` durably on
    every slot and commits (releasing row locks before any bus round trip); pass 2 relabels best-effort
    per slot on each slot's own node."""
    slots = session.execute(
        select(
            # The node uuid as text (the bus routes on it; a name is not an identity). INNER: the FK is NOT
            # NULL ON DELETE CASCADE.
            cast(Worktree.node_id, String).label("node_id"),
            # Labelled, apart from the slot's `name`; `default_title` rides along for `stored_opinion`.
            Worktree.name.label("slot_name"), Worktree.default_title,
            Worktree.number, Worktree.path, Worktree.task_id,
            Task.state.label("task_state"), Task.title.label("task_title"),
        )
        .select_from(Worktree)
        .join(Node, Node.id == Worktree.node_id)
        # LEFT: `worktrees.task_id` has no FK, so a deleted task yields NULL rather than dropping the slot.
        .outerjoin(Task, Task.id == Worktree.task_id)
        .where(Worktree.project_id == project.id)
        .order_by(Node.name, Worktree.number)
    ).all()
    # ── PASS 1: the derived label follows the rename, durably ─────────────────────────────────────────
    # Every slot whose `name` is the pre-rename derived tail, through the one MUTATED writer, attributed
    # to the renaming operator.
    names_now: dict[tuple[str, int], str] = {}
    rederived = False
    for slot in slots:
        name_now = slot.slot_name
        if (name_now or "").strip() == slot_title.default_name(name_before, slot.number):
            name_now = slot_title.default_name(project.name, slot.number)
            slot_mutate.apply_mutation(
                session, slot.node_id, project.id, slot.number,
                actor_id=actor.id, name=name_now,
            )
            rederived = True
        names_now[(slot.node_id, slot.number)] = name_now
    # Explicit, not left to pass 2's incidental commit; skipped when nothing was re-derived.
    if rederived:
        session.commit()

    # ── PASS 2: relabel the live window, best effort ───────────────────────────────────────────────────
    for slot in slots:
        if not slot.node_id or not slot.path:
            # No node, or no path for `worktree_ops.rename_tmux` to find the window; pass 1 already fixed
            # `name`.
            continue
        title = slot_title.resolved_title(
            # Not the raw `name` (a derived label would replace the task Title); see `stored_opinion`.
            # The node refuses an empty title, which `resolved_title` cannot return.
            slot_title.stored_opinion(
                names_now[(slot.node_id, slot.number)],
                default_title=slot.default_title,
                default_name=slot_title.default_name(project.name, slot.number),
            ),
            task_active=slot_title.task_active_from_state(slot.task_state),
            task_id=slot.task_id,
            task_title=slot.task_title,
        )
        dispatch_and_audit_sync(
            session,
            slot.node_id,
            "rename_tmux",
            {"project_id": project.id, "project": project.name, "number": slot.number, "path": slot.path,
             "title": title},
            actor.email,
        )


class ProjectMemberOut(BaseModel):
    """One candidate for the project's "Humans" dropdowns — identity and label, nothing else.

    Not `routes/users.py::UserOut`, which carries the superadmin admin record (`is_superadmin`,
    provenance, preferences, memberships)."""

    id: UUID
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
@feature("projects", "view", object_type="project", object_params=("project_id",))
def list_project_members(
    project_id: int,
    may_start: bool = Query(False),
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[ProjectMemberOut]:
    """Every user who can reach this project — the candidate set for its Business Analyst and Architect.

    The same question `_validated_project_role` refuses on, so the form offers only what the PUT accepts.
    Disclosing the member list to project viewers is decided (`GET /api/users` needs `users:view` and is
    wider). One statement, `or_` arms, so SQL de-duplicates and orders. A live role binding, direct or
    through a group, on the project or its org reaches it (`app/access.py`'s
    inheritance; every role carries `projects:view`, an account binding none on a project).

    Superadmins appear without a binding (matching `visible_project_clause`) on the operator plane
    only: a tenant project viewer need not hold `users:view`, so that arm would re-derive `is_superadmin`
    for a reader the Directory would not show it to. `is_system` is excluded from every arm.

    `may_start`: only the members an acting operator's Start may push as (`named_user_may_start`, the
    `push_as_id` vetting), so the Start dialog offers only whom the server accepts. Its one reader is that
    operator, and to anyone else the filter would disclose who holds `tasks:edit` or is disabled: refused."""
    if may_start and (principal is None or not principal.acting):
        raise HTTPException(status_code=403, detail="may_start is only for an operator acting as an org")
    # The guard normally 404s an unknown id first; kept because an in-process caller would otherwise get
    # the full superadmin roster for an id naming nothing.
    project_row = project_or_404(session, project_id)

    scope_arms = [RoleBinding.project_id == project_id]
    if project_row.org_id is not None:
        scope_arms.append(RoleBinding.scope_org_id == project_row.org_id)
    live_here = and_(
        or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > func.now()), or_(*scope_arms)
    )
    direct = (
        select(literal(1))
        .select_from(RoleBinding)
        .where(RoleBinding.user_id == AdminUser.id, live_here)
        .correlate(AdminUser)
        .exists()
    )
    via_group = (
        select(literal(1))
        .select_from(RoleBinding)
        .join(UserGroupMembership, UserGroupMembership.group_id == RoleBinding.group_id)
        .where(UserGroupMembership.user_id == AdminUser.id, live_here)
        .correlate(AdminUser)
        .exists()
    )
    # The third arm is the operator plane's: tested by `is_tenant_org` on the actor's org, not
    # `is_superadmin` (a tenant's own superadmin must not reopen it).
    reach = [direct, via_group]
    if not is_tenant_org(actor.org_id):
        reach.append(AdminUser.is_superadmin.is_(True))
    else:
        # A tenant gets two non-disclosing arms back — without them a tenant superadmin (allow-all, no
        # binding) saw empty dropdowns on operator-provisioned projects. First, the caller itself.
        reach.append(AdminUser.id == actor.id)
        # Second, the two ids already on this project's row, so `MemberSelectField`'s "not in this list"
        # arm does not fire (and leak) for a role-holder.
        held_roles = [
            getattr(project_row, field_name)
            for field_name in _ROLE_FIELDS
            if getattr(project_row, field_name) is not None
        ]
        if held_roles:
            reach.append(AdminUser.id.in_(held_roles))
    rows = session.scalars(
        select(AdminUser)
        .where(AdminUser.is_system.is_(False), or_(*reach))
        .order_by(AdminUser.email)
    ).all()
    if may_start:
        rows = [
            row for row in rows
            if named_user_may_start(session, row, project_id, project_row.org_id, actor=actor)
        ]
    return [
        ProjectMemberOut(
            id=row.id, email=row.email, first_name=row.first_name, last_name=row.last_name
        )
        for row in rows
    ]


def _declared_slot_count(session: Session, project_id: int) -> int:
    """How many pool slots are declared for this project, fleet-wide (every node).

    Keyed on `projects.id`, never the name: this feeds three refusals and must not answer 0 for a renamed
    project."""
    return int(
        session.scalar(
            select(func.count()).select_from(Worktree).where(Worktree.project_id == project_id)
        )
        or 0
    )


class ProjectDocsOut(BaseModel):
    """The docs a project offers for browsing. `enabled` false is a plain fact (no root `mkdocs.yml`, or
    the operator's token cannot see the repo), not an error."""

    enabled: bool
    # Which `enabled=False` this is — a discriminator a UI may branch on, so rewording `reason` cannot
    # change what the page claims. Absent when enabled.
    reason_code: Literal["no_mkdocs", "not_visible"] | None = None
    project_name: str
    site_name: str | None = None
    paths: list[str] = []
    ref: str | None = None
    # Built here from the server's single-home owner/repo parse, not re-derived in the browser.
    blob_base: str | None = None
    reason: str | None = None


def _docs_repo(project_id: int, session: Session) -> tuple[str, str, str]:
    """`(owner/repo, ref, project_name)` for a project, or 404/422. The ref is the project's own
    `integration_branch`."""
    project = project_or_404(session, project_id, "project not found")
    # GitHub-only surface: a repo on another forge has no docs browser (PRD §8).
    ref = forges.repo_ref_or_none(project)
    owner_repo = ref.path if ref is not None and ref.forge == "github" else None
    if not owner_repo:
        raise HTTPException(status_code=422, detail="project has no GitHub repo_url")
    return owner_repo, project.integration_branch, project.name


def _docs_token(session: Session, actor: AdminUser, owner_repo: str) -> str:
    """The GitHub token these two routes read a repo with: the requesting operator's own.

    Reading as the person means two operators may get different answers, and one a 409 naming what to
    connect. Resolved per call via `git_access.resolve_for_user` (live grants), never cached. Refusal
    codes mirror `routes/agent/task_secrets.py::_git_token_or_refusal`; `str(exc)` never contains a token.
    """
    try:
        # The docs browser is GitHub-only (`_docs_repo`), so the person's github.com connection.
        credential = git_access.resolve_for_user(
            session, actor.id, forges.parse_repo_url(f"https://github.com/{owner_repo}", "github"),
            # Not "start the task again" — meaningless on a docs page (`git_access._RETRY_TASK`).
            retry="then reload this page",
        )
        return git_access.token_for(session, credential)
    except git_access.GitAccessNeedsReconnect as exc:
        # 410: the credential is configured but its GitHub authorization is gone; only its owner can
        # reconnect.
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except git_access.GitAccessError as exc:
        # 409, not 403: the caller may ask; the state of the world conflicts.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{project_id}/docs", response_model=ProjectDocsOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def list_project_docs(
    project_id: int,
    actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> ProjectDocsOut:
    """Every markdown document this project offers, read live from GitHub as the caller.

    Nothing is installed and MkDocs never runs: a committed `mkdocs.yml` is the opt-in, read only for
    `site_name` and `docs_dir`. The list is what the caller may see (`_docs_token`).
    """
    owner_repo, ref, project_name = _docs_repo(project_id, session)
    try:
        token = _docs_token(session, actor, owner_repo)
        site_name, docs_dir = github_api.docs_config(owner_repo, ref, token=token)
    except github_api.RepoNotVisible as exc:
        # Not "add an mkdocs.yml": the repo did not answer to this operator's token
        # (`github_api.RepoNotVisible`). `enabled=False`, so the page renders its own empty state.
        return ProjectDocsOut(
            enabled=False,
            reason_code="not_visible",
            project_name=project_name,
            ref=ref,
            reason=(
                f"{exc}. Docs are read with YOUR connected GitHub account — ask for access to "
                f"{owner_repo}, or connect a different account under Profile > Git."
            ),
        )
    except github_api.DocsNotEnabled:
        return ProjectDocsOut(
            enabled=False,
            reason_code="no_mkdocs",
            project_name=project_name,
            ref=ref,
            reason=f"{owner_repo} has no mkdocs.yml on {ref} — add one to make its docs browsable.",
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}") from exc
    try:
        paths = github_api.docs_tree(owner_repo, ref, docs_dir, token=token)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}") from exc
    return ProjectDocsOut(
        enabled=True,
        project_name=project_name,
        site_name=site_name,
        paths=paths,
        ref=ref,
        blob_base=f"https://github.com/{owner_repo}/blob/{ref}",
    )


@router.get("/{project_id}/docs/file")
@feature("projects", "view", object_type="project", object_params=("project_id",))
def read_project_doc(
    project_id: int,
    path: str,
    actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """One document's raw markdown, read as the caller (`_docs_token`).

    `path` must be a member of the list `list_project_docs` returns — an allowlist, so traversal and
    encoding tricks need no special handling.
    """
    owner_repo, ref, _name = _docs_repo(project_id, session)
    try:
        token = _docs_token(session, actor, owner_repo)
        _, docs_dir = github_api.docs_config(owner_repo, ref, token=token)
        if path not in github_api.docs_tree(owner_repo, ref, docs_dir, token=token):
            raise HTTPException(status_code=404, detail="no such document")
        content = github_api.docs_file(owner_repo, ref, path, token=token)
    except github_api.RepoNotVisible as exc:
        # 404 like its sibling, with the reason true for this caller.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except github_api.DocsNotEnabled as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}") from exc
    if content is None:
        raise HTTPException(status_code=404, detail="no such document")
    return {"path": path, "content": content, "ref": ref}


#: The administrator bar on `DELETE /api/projects/{id}` — `authz.org_admin_writer`.
require_project_deleter = org_admin_writer("Project records")

#: The two keys an assignment write needs beside the deleter bar (D13): placing a project on a node creates
#: and destroys worktrees, so it is both halves of the project's own lifecycle.
_ASSIGNMENT_KEYS = (permission_key("projects", "create"), permission_key("projects", "delete"))


def require_assignment_writer(actor: AdminUser, principal: Principal | None) -> None:
    """403 unless the caller may write an assignment or its static override (D13):
    `projects:create` AND `projects:delete`, plus the project-deleter bar (`require_project_deleter`). The one
    home for that gate — `routes/assignments.py` and `apply_project_update` ask it."""
    require_project_deleter(actor, principal)
    missing = [key for key in _ASSIGNMENT_KEYS if principal is None or not principal.has(key)]
    if missing:
        raise HTTPException(
            status_code=403,
            detail=f"changing where a project runs needs {' and '.join(missing)}",
        )


@router.delete("/{project_id}", status_code=204)
@feature("projects", "delete", object_type="project", object_params=("project_id",))
async def delete_project(
    project_id: int,
    # Superadmin: the single-slot verbs are, and this is strictly more destructive
    # (`require_slot_writer`'s shape).
    actor: AdminUser = Depends(require_project_deleter),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Delete the project, never refused for what it holds: every Task in it goes too
    (`_delete_project_tasks`), and the cascade takes the ingest-cache rows (`slot_intents`, `fetch_stamps`).
    Never touches the git repo or the markdown card file on disk (R2 edge case).

    A project with declared worktree slots is erased as `erase_project` erases it — every slot's directory
    on its node, then the row — behind the slot writer's own bar, which deleting each slot by hand needs
    anyway. Only a slot no node can address still refuses (409), as there."""
    project = project_or_404(session, project_id)
    _refuse_system_project_delete(project)
    if _declared_slot_count(session, project.id):
        require_slot_writer(actor, principal)
        return await _erase_slots_then_delete(session, project, actor)
    name = project.name
    tasks_deleted = await run_in_threadpool(_delete_project_tasks, session, project_id, actor)
    session.delete(project)
    record_write(
        session, actor, "project_deleted", f"Deleted project {name}",
        data={"project_id": project_id, "name": name, "tasks_deleted": tasks_deleted},
    )
    session.commit()
    return Response(status_code=204)


async def _erase_slot_on_its_own_session(
    node_id: str, project_id: int, number: int, *, actor_email: str, actor_org: Optional[UUID]
) -> SlotDeleteOut:
    """One cascade leg, on a fresh `Session` (sessions cannot be shared across concurrent coroutines,
    and each leg holds a row lock across a node round trip).

    Returns the leg's outcome: `delete_one_slot` does not raise on an unreachable node, and this route
    must name orphaned directories. No `force` argument — Delete always forces."""
    with SessionLocal() as session:
        # `actor_org` is passed, never defaulted: `delete_one_slot` reads a missing org as a tenant.
        return await delete_one_slot(
            session, node_id, project_id, number, actor_email=actor_email, actor_org=actor_org,
        )


@router.post(
    "/{project_id}/erase",
    status_code=204,
    # Pins the destructive reading of "Erase Project" (the project itself, not its contents) for the
    # decorator's reader; no schema is served today (`openapi_url=None`).
    summary="Delete the project itself, and every worktree slot it declares",
)
@superadmin_only(Tenancy.OPERATOR)
async def erase_project(
    project_id: int,
    actor: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> Response:
    """This deletes the project itself, not merely the slots it declares. Removes every worktree row,
    asks every node to delete its directory (slot 0 included), then deletes the `projects` row; getting
    it back means re-creating it under a new id. 204 on success. To keep the project, delete each slot
    (`POST /api/worktrees/{project}/{number}/erase`) instead.

    Cannot be refused on occupancy or liveness: a hold buys a teardown, an unreachable node a report
    (`app/slot_teardown.py`, `pool_slots.py::delete_one_slot`). The final delete cascades away the legs'
    `release_slot` intents — harmless only because the kill rides the awaited erase
    (`slot_teardown.end_holder`).

    Accepted outcome: directories may survive on unreachable nodes. Each is named in a WARNING, in the
    `project_erased` audit payload (`orphaned_directories`), and in a refusal's 409 — the log line is
    emitted before any refusal can return, since those rows are already committed gone.

    Two phases: numbered slots, then slot 0 (the clone every sibling's `.git` points into) only once
    phase 1 succeeded. A leg raises only for an unaddressable slot; that refuses with a 409, keeps the
    row, and a retry resumes (the node handler is idempotent). Every Task in the project goes too
    (`_delete_project_tasks`)."""
    project = project_or_404(session, project_id)
    _refuse_system_project_delete(project)
    return await _erase_slots_then_delete(session, project, actor)


def _unexpected_leg_error(error: BaseException, actor: AdminUser) -> str:
    """A leg's non-HTTP failure as the 409 names it: verbatim for the operator, with host paths withheld for a
    tenant (`app/command_read.py`), whose `DELETE` reaches this cascade too."""
    return withhold_host_paths(str(error)) if is_tenant_org(actor.org_id) else str(error)


async def _erase_slots_then_delete(session: Session, project: Project, actor: AdminUser) -> Response:
    """`erase_project`'s work, shared with `delete_project` for a project that declares slots: erase every
    slot, then delete its tasks and the row. 409 as `erase_project` documents."""
    project_id = project.id
    name = project.name

    rows = session.execute(
        # The node uuid as text, which every leg reads (a name is not an identity); the name only for the
        # sentences and the audit.
        select(cast(Worktree.node_id, String), Worktree.number, Node.name)
        .select_from(Worktree)
        .join(Node, Node.id == Worktree.node_id)
        .where(Worktree.project_id == project.id)
        .order_by(Node.name, Worktree.number)
    ).all()
    slots = [(node_id, number) for node_id, number, _name in rows]
    node_names = {node_id: node_name for node_id, _number, node_name in rows}
    numbered = [(node_id, number) for node_id, number in slots if number != 0]
    clones = [(node_id, number) for node_id, number in slots if number == 0]

    async def _erase_phase(
        rows: list[tuple[str, int]],
    ) -> tuple[list[tuple[str, int, object]], list[tuple[str, int, str]]]:
        """One phase's legs, all at once. Returns (legs that raised, directories left orphaned).

        Kept apart: a raise means the row may survive and the project must not be deleted; an orphan is a
        completed deletion with a fact attached. `isinstance`, not truthiness — a `SlotDeleteOut` is
        always truthy."""
        outcomes = await asyncio.gather(
            *(
                _erase_slot_on_its_own_session(
                    node_id, project_id, number, actor_email=actor.email, actor_org=actor.org_id,
                )
                for node_id, number in rows
            ),
            return_exceptions=True,
        )
        raised = [
            (node_id, number, outcome)
            for (node_id, number), outcome in zip(rows, outcomes)
            if isinstance(outcome, BaseException)
        ]
        orphaned = [
            # The node's own words when given; the fallback only keeps a report from being blank.
            (node_id, number, outcome.node_detail or "the node did not say why")
            for (node_id, number), outcome in zip(rows, outcomes)
            if isinstance(outcome, SlotDeleteOut) and not outcome.node_erased
        ]
        return raised, orphaned

    failures, orphaned = await _erase_phase(numbered)
    if not failures:
        # Phase 2 still waits on phase 1 succeeding. An orphaned numbered directory does not hold it back:
        # its row is gone, and the node's slot-0 handler re-checks `git worktree list` itself.
        failures, clone_orphans = await _erase_phase(clones)
        orphaned += clone_orphans
    # The orphan report is emitted here, before any refusal can return: those legs already committed
    # their row deletions, so a later refusal would otherwise lose the only record, and a retry only
    # re-enumerates surviving rows.
    if orphaned:
        # WARNING: somebody has to go to that host. `%d of %d` for scale.
        log.warning(
            "erase project '%s': %d of %d worktree row(s) are gone but their directory/ies survive on "
            "their node(s) — %s",
            name, len(orphaned), len(slots),
            "; ".join(f"{node_names.get(node_id, node_id)}#{number}: {detail}" for node_id, number, detail in orphaned),
        )

    # One sentence for both refusal arms, so the 409 itself tells the operator which hosts to visit.
    orphan_sentence = (
        f". {len(orphaned)} directory/ies whose rows ARE gone survive on their node(s): "
        + "; ".join(f"{node_names.get(node_id, node_id)}#{number}: {detail}" for node_id, number, detail in orphaned)
    ) if orphaned else ""

    if failures:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name} was NOT deleted — {len(failures)} of {len(slots)} slot(s) could not be removed "
                f"at all (the rest are gone): "
                + "; ".join(
                    f"{node_names.get(node_id, node_id)}#{number}: "
                    f"{error.detail if isinstance(error, HTTPException) else _unexpected_leg_error(error, actor)}"
                    for node_id, number, error in failures
                )
                # Named in the response too: a refusal writes no audit row.
                + orphan_sentence
            ),
        )

    # Re-counted after the cascade: a slot committed while the legs ran would otherwise be orphaned.
    remaining = _declared_slot_count(session, project_id)
    if remaining:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name} gained {remaining} declared slot(s) while its erase was in flight — the project "
                f"row was NOT deleted; re-run this call to erase them too" + orphan_sentence
            ),
        )

    tasks_deleted = await run_in_threadpool(_delete_project_tasks, session, project_id, actor)
    session.delete(project)
    record_write(
        session, actor, "project_erased", f"Erased project {name} and {len(slots)} worktree slot(s)",
        data={"project_id": project_id, "name": name, "tasks_deleted": tasks_deleted,
              "slots": [{"node_id": node_id, "node_name": node_names.get(node_id), "number": number}
                        for node_id, number in slots],
              # Always present, empty included (§absence-is-not-a-state).
              "orphaned_directories": [
                  {"node_id": node_id, "node_name": node_names.get(node_id), "number": number, "detail": detail}
                  for node_id, number, detail in orphaned
              ]},
    )
    session.commit()
    return Response(status_code=204)
