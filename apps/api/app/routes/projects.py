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
`sync_access_guard` when any repository's Branch policy changes (`_access_guard_state`), and
re-derives each slot's derived name on a rename.

The seven mandatory branch/gate/stack fields (`_GOVERNANCE_FIELDS`) cannot be nulled (a gate
or stack command may be `''`, meaning none; a new project's four are seeded from its org's Projects > Defaults
settings), cannot be authored by a node token, and need superadmin or an org-admin with `nodes:edit` plus a write grant on
a node in the project's org (`has_authority_to_author_governance`). The two `governance-authority` GET
routes and the agent module return that same verdict, via the unprefixed aliases.
"""
from __future__ import annotations


import asyncio
import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, Iterable, Literal, Mapping, Optional, Sequence, TypeVar, Union
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StringConstraints
from sqlalchemy import String, and_, cast, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import (
    act_as, agent_providers, agent_resolution, blind_review, ce_caps, edition_features, forges, git_access, git_reach, github_api, jaah_repo_replace, jaah_repos, merge_lane_state, model_catalog,
    org_limits, plan_terms, project_repos, provider_auth_policy, slot_allocation, slot_mutate, slot_title,
)
from app.operator_settings import defaults as settings_defaults
from app import project_intake
from app.pipeline_gates import full_gate
from app.project_intake import ProjectIntake
# The community edition has no Easy Mode hosting: its creates make no hosted app, so `hosted_app` stays null there.
if edition_features.enabled("easy_mode_hosting"):
    from app.routes.hosted_apps import HostedAppOut, _out as _hosted_app_out  # pyright: ignore[reportPrivateUsage]
else:
    HostedAppOut = None  # type: ignore[assignment,misc]  # pyright: ignore
from app.persona_registry import PROJECT_DEFAULT_PERSONAS
from app.auth import require_superadmin, require_user
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import feature, superadmin_only
from app.authz.object_access import can_access, named_user_may_start, require_org_admin
from app.authz.principal import (
    AsyncCaller,
    Principal,
    current_caller,
    current_principal_async,
    org_admin_bar,
    org_admin_writer,
    owner_seat,
)
from app.authz.visibility import ObjectAccess, node_reach_clause
from app import db_async
from app.db_async import DbDep, on_session
from app.entity_shape import StrictModel, constraint_name, from_row, get_or_404, or_404, refused_as, reject_cleared
from app.actors import actor_names
from app.identities import display_name
from app.project_identity import project_or_404
from app.project_env import PortSpec, port_specs_by_project
from app.trackers import project_links as tracker_links
from app.project_onboarding import file_app_wizard_epic, file_business_wizard_task, file_onboarding_task
from app.releases import basics, pipeline as release_pipeline, pipeline_doc, stages as release_stages
from app.production_qa import gate as production_qa_gate
from app.production_qa import probes as production_qa_probes
from app.production_qa.vocabulary import REVERT_POLICIES
from app.models.access import RoleBinding, UserGroupMembership
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import ROUTING_MODES, Project, ProjectRepo
from app.agent_router import modes
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.project_validation import has_duplicate_tech_stack_entry
from app.command_read import withhold_host_paths
from app.node_clone import has_home
from app.outcome_ledger import security as outcome_security
from app.outcome_ledger import settings as outcome_settings
from app.routes.commands import _resolve_command_args, dispatch_and_audit_sync
from app.routes.pool_slots import SlotDeleteOut, _clone_git_access_warning, delete_one_slot
from app.node_identity import names_by_id, node_id_or_404
from app.tenancy import seam
from app.tenancy.copy import governance_authority
from app.tenancy.create_surface import named_org
from app.tenancy.ids import is_tenant_org
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
from app.user_activity import record_write

if TYPE_CHECKING:  # benchmark mode is cloud-only (`packages/editions/features.toml`)
    from app.outcome_ledger.benchmark import Budget

router = APIRouter(prefix="/api/projects", tags=["projects"])
log = logging.getLogger("cockpit-projects")
T = TypeVar("T")


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
        raise HTTPException(status_code=422, detail=f"{field} must be a string command, not {type(raw).__name__}")
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
    with refused_as(422, ValueError, prefix="default_model: "):
        return model_catalog.validate_model(raw)


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
        raise HTTPException(status_code=422, detail=f"repo_url must be a string, not {type(raw).__name__}")
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
    with refused_as(422, forges.ForgeUrlError, prefix="repo_url: "):
        if carried and declared is not None:
            try:
                ref = forges.parse_repo_url(repo_url, None)
                return {"forge": ref.forge, "forge_base_url": None}
            except forges.ForgeUrlError:
                pass
        ref = forges.parse_repo_url(repo_url, declared, base_url)
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


def _validated_repos(
    rows: list[dict[str, Any]], current: list[ProjectRepo]
) -> list[project_repos.RepoSpec]:
    """`repos` as the set to store, or ONE 422 naming every bad row by `["body","repos",<i>,<field>]`, so nothing is
    saved and the form marks each row at once.

    Codes: `invalid_repo_url` (blank, or refused by `_validated_repo_url`/`_derived_forge` with their own
    message), `duplicate_repo` (AS3: the same URL, or the same repository spelled another way, twice),
    `integration_branch_required` (AS10), `invalid_integration_branch`, `invalid_branch_prefix`, `unknown_repo`
    (an `id` that is not one of this project's rows), `primary_removed` (AS4: the current primary dropped and no new one named), and
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
            keys = project_repos.repo_keys(url, derived["forge"], derived["forge_base_url"])
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
        prefix: Optional[str] = None
        if row.get("branch_prefix") is not None:
            try:
                prefix = _normalized_branch_prefix(row["branch_prefix"])
            except HTTPException as exc:
                errors.append(_repo_error([*loc, "branch_prefix"], _detail_text(exc), "invalid_branch_prefix"))
                branch = ""
        if derived is not None and branch:
            specs.append(project_repos.RepoSpec(
                id=row_id, repo_url=url, forge=derived["forge"], forge_base_url=derived["forge_base_url"],
                integration_branch=branch, is_primary=bool(row.get("is_primary")), branch_prefix=prefix,
                push_enabled=row.get("push_enabled"), allow_force_push=row.get("allow_force_push"),
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


#: The most nodes one create may place on (`ProjectPlacement.node_ids`).
MAX_PLACEMENT_NODES = 500


class ProjectPlacement(BaseModel):
    """The nodes a new project's creator expects it on (`POST /api/projects`' `placement`).

    Checked and answered with the clone advisory only: nothing is written, since a new project runs on every node
    of its org (the All group, `app/placement_groups.py`) and each task makes its own worktree there
    (`slot_allocation.add_dynamic`)."""

    # Stripped, as `routes/pool_slots.py::_resolved_node_id` strips the same value.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Each node's uuid (`app/node_identity.py`); a name is not an identity. The console's New project places on
    # every enabled node the org has, so the bound is a body-size floor, not a placement policy.
    node_ids: list[Annotated[str, StringConstraints(min_length=1)]] = Field(min_length=1, max_length=MAX_PLACEMENT_NODES)


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
    # The row's Branch policy (`project_repos.POLICY`), governance fields like the project's pair
    # (`_governance_named`). Omitted (or null) keeps the stored policy of the same repository, whichever row held
    # it; a repository no row held inherits the project's prefix and push flag, force push off.
    # The prefix is normalised as the project's (`_normalized_branch_prefix`).
    branch_prefix: Optional[str] = None
    push_enabled: Optional[bool] = None
    allow_force_push: Optional[bool] = None


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
    branch_prefix: str
    push_enabled: bool
    allow_force_push: bool


def project_repo_out(row: ProjectRepo) -> ProjectRepoOut:
    ref = forges.repo_ref_or_none(row)
    return from_row(ProjectRepoOut, row, web_url=ref.web_url() if ref is not None else None)


class ProjectSettingsFieldsIn(BaseModel):
    """The project-settings columns every writer takes, each optional on the wire — the console's create and
    edit (`_ProjectFieldsIn`) and a runner's own-project patch (`agent_project_settings.py::AgentProjectSettingsIn`).

    Optional on the wire, mandatory in the column: omitted takes the default (on an edit: leaves it alone); an
    explicit null is refused by `_validated_fields` so the 422 names the field — the Edit form cannot null
    these seven (`''` is a value for `branch_prefix` and the gate pair). The gate and stack commands default
    to the org's Projects > Defaults settings (`''` = none)."""

    integration_branch: Optional[str] = None
    branch_prefix: Optional[str] = None
    gate_quick_cmd: Optional[str] = None
    gate_full_cmd: Optional[str] = None
    stack_up_cmd: Optional[str] = None
    stack_down_cmd: Optional[str] = None
    push_enabled: Optional[bool] = None
    default_model: Optional[str] = None
    tech_stack: Optional[list[TechStackEntry]] = None
    notes: Optional[str] = None


class _ProjectFieldsIn(ProjectSettingsFieldsIn):
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
    # See `models.Project.agent_instructions`. Optional like `notes` (omitted → `''`; on an edit an explicit null
    # clears it); a node token may not author it (`_reject_governance_from_a_node_token`).
    agent_instructions: Optional[str] = None
    # No `jira_credential_id`: the per-project Jira secret is retired (Jira is a tracker connection). Old clients
    # still send it; undeclared, it is ignored (see the class note) and never written.
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

    # When set, the row is stamped with the source's data timestamp instead of now(); no in-tree caller.
    updated_at: Optional[datetime] = None
    # Create-only; absent leaves every default as it was (`ProjectPlacement`).
    placement: Optional[ProjectPlacement] = None
    # Create-only: the org the project lands in. Absent or the caller's own = their own org. Another org is
    # the operator's alone (seeding a tenant from the console) and a 422 for anyone else — never ignored
    # (`create_project`).
    org_id: Optional[UUID] = None
    # Create-only: what the project is (`app/project_intake.py`). Absent creates exactly as before; present, a new
    # app or business project gets its repository from Jaah and an existing app must name its own.
    intake: Optional[ProjectIntake] = None
    # Create-only: the preset the pipeline starts from (`app/releases/pipeline_doc.py::PRESETS`). Absent or
    # `standard` creates exactly as before; a project without a repository takes `ideas_only` only.
    pipeline_preset: Optional[str] = Field(default=None, max_length=64)


class ProjectUpdate(_ProjectFieldsIn):
    """`_ProjectFieldsIn` with `name` optional too: only fields present in the request body
    (`model_dump(exclude_unset=True)`) are applied, so a `PUT` here is a partial update. Undeclared keys are
    ignored, as on the create."""

    name: Optional[str] = None
    # The persona a new task starts as (`models.Project.default_persona`); null or `''` clears to the org default.
    default_persona: Optional[str] = None
    # "Run jobs as" (`models.Project.run_jobs_as_id`): the org default's override; an explicit null clears. Org Admin
    # and above only (`_validated_run_jobs_as`).
    run_jobs_as_id: Optional[UUID] = None
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
    # The project's own merge lane switch (`_apply_merge_lane_opt_in`): turning it on is refused 409 while the org's
    # plan has no lane; stamps `merge_lane_enabled_by`.
    merge_lane_enabled: Optional[bool] = None
    # Project memory On/Off for the project's tasks (`models.Project.project_memory`); null = the org's default.
    project_memory: Optional[bool] = None
    # The outcome scan interval in minutes (`outcome_ledger.settings.scan_interval`); null inherits the org's.
    outcome_scan_interval_minutes: Optional[int] = None
    # The project's own CI security-check name patterns, added to the defaults (`outcome_ledger.security`); the list
    # replaces the stored one, null clears it.
    security_check_patterns: Optional[list[str]] = None
    # Intelligent routing (`app/agent_router/modes.py`, `_validated_routing`): the mode (`inherit` = off, capped at
    # the org's maximum while its plan carries the feature), the latency cap in hours (null = none) and blind review.
    routing_mode: Optional[str] = None
    routing_latency_cap_hours: Optional[float] = None
    blind_review: Optional[bool] = None
    blind_review_other_vendor: Optional[bool] = None
    # The share of 30-day spend exploration and benchmark runs may take, 0..1 (`Project.experiment_budget_share`).
    # Strict: a boolean is not a share.
    experiment_budget_share: Optional[Union[StrictInt, StrictFloat]] = None
    # Production QA (`app/production_qa/`, `_validated_production_qa`): the opt-in (refused 409 while the org's switch
    # is off; stamps `production_qa_enabled_by`), the deploy workflow paths (the list replaces the stored one), the
    # revert policy, and the regression window and thresholds. The production environment is the Production
    # stage's (`PUT /api/projects/{id}/pipeline`).
    production_qa_enabled: Optional[bool] = None
    deploy_workflows: Optional[list[str]] = None
    # Benchmark mode (`app/outcome_ledger/benchmark.py`, `_apply_benchmark`): the opt-in (turning it on is refused 409
    # while the org's switch is off) and the share of coder tasks sampled into duels, 0..1. Strict, as the budget.
    benchmark_opt_in: Optional[bool] = None
    benchmark_sample_rate: Optional[Union[StrictInt, StrictFloat]] = None
    revert_policy: Optional[str] = None
    regression_window_min: Optional[int] = None
    regression_error_ratio: Optional[float] = None
    regression_min_events: Optional[int] = None
    regression_p95_ratio: Optional[float] = None
    # Native probes (`app/production_qa/probes.py::validated`): the health URLs and smoke commands; each list replaces
    # the stored one, null clears it. Authored by `projects:edit` only — the PUT's own permission.
    health_probes: Optional[list[dict[str, Any]]] = None
    smoke_commands: Optional[list[dict[str, Any]]] = None
    # What happens to the project's Jaah-made repository when this save replaces or removes it
    # (`app/jaah_repo_replace.py`): `move` its code here (only while the 409 offers it) or `retire` it. Absent when
    # one is needed: 409 `jaah_repo_replaced`, nothing written.
    jaah_repo: Optional[Literal["move", "retire"]] = None
    # The hosted app's address, typed to confirm its delete, when either answer above would delete a running app.
    jaah_repo_confirm: Optional[str] = None


class ProjectSettingsFieldsOut(BaseModel):
    """`ProjectSettingsFieldsIn` read back — by `ProjectOut` and the runner's
    `agent_project_settings.py::AgentProjectSettingsOut`. Non-optional on the way out: the column is NOT NULL."""

    integration_branch: str
    branch_prefix: str
    gate_quick_cmd: str
    gate_full_cmd: str
    stack_up_cmd: str
    stack_down_cmd: str
    push_enabled: bool
    default_model: Optional[str] = None
    tech_stack: Optional[list[dict[str, str]]] = None
    notes: Optional[str] = None


class LiveUrlOut(BaseModel):
    """One pipeline stage's live address: `{stage, url}`."""

    stage: str
    url: str


class ProjectOut(ProjectSettingsFieldsOut):
    id: int
    name: str
    # Read-only: the names of the project's Port variables (`app/project_env.py::port_specs`), which the
    # Environment tab edits. Kept for the readers that only need the names (the pool audits).
    port_vars: list[str]
    # No default: `_project_out` is the one constructor, so forgetting it is a TypeError. Null is a real
    # state here — `org_id` has no FK, so an unmatched id resolves to null (`app/tenancy/labels.py`).
    org: Optional[OrgRef]
    # Whether a repository is connected (`models.Project.has_repo`). No default: the Edit form hides every
    # repository field off this.
    has_repo: bool
    # `'nodes'` for the org's system Nodes project (`app/node_targets.py`), null for every other; the console
    # renders a System chip off it.
    system_kind: Optional[str] = None
    # The primary repository's `allow_force_push` (`project_repos.POLICY`); read-only here, set on the row.
    allow_force_push: bool = False
    repo_url: Optional[str] = None
    # `projects.forge`; null = no repo URL, or a legacy one naming no supported forge (the form asks).
    forge: Optional[str] = None
    forge_base_url: Optional[str] = None
    # The repo's own page on its forge (`RepoRef.web_url`), what the project list links; null with `forge`.
    web_url: Optional[str] = None
    # What a node clones (`RepoRef.clone_url`, the forge-normalized HTTPS URL); null with `forge`.
    clone_url: Optional[str] = None
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
    # The project's own switch (`projects.merge_lane_enabled`), off by default: `merge_lane` is true only while it is on.
    merge_lane_enabled: bool = False
    # The pipeline's kind as stored (`app/releases/basics.py`): the Pipeline tab shows while it is `custom`;
    # `GET …/pipeline/basic` serves the derived one and the section's settings.
    pipeline_mode: str
    # The project's own batch cap; null = the lane's default (`MergeLaneOut.batch_max` is the effective one).
    merge_lane_batch_max: Optional[int] = None
    # Project memory On/Off; null = the org's default (`project_memory.switch.effective_memory`).
    project_memory: Optional[bool] = None
    # The project's own outcome scan interval; null = the org's (`GET …/outcome-scan` shows the one in force).
    outcome_scan_interval_minutes: Optional[int] = None
    # The `repo_move` task moving the project off its Jaah-made repository while that move is open
    # (`jaah_repo_replace.open_move_task_id`); set on the `PUT` answer only, null elsewhere.
    move_task_id: Optional[int] = None
    # The project's own CI security-check patterns (beside the defaults, `outcome_ledger.security.DEFAULT_PATTERNS`).
    security_check_patterns: list[str] = []
    # Intelligent routing (`ProjectUpdate`'s four), and what the form offers: `routing_cap` is the highest mode the
    # project may choose now (`modes.cap`: the org's maximum, `off` without the plan feature), `routing_cap_reason`
    # the sentence a disabled mode shows (`modes.refusal`), null when nothing is disabled.
    routing_mode: str = "inherit"
    routing_latency_cap_hours: Optional[float] = None
    blind_review: bool = False
    blind_review_other_vendor: bool = True
    experiment_budget_share: float = 0.1
    routing_cap: str = "off"
    routing_cap_reason: Optional[str] = None
    # Why Blind review cannot be turned on now (`blind_review.toggle_refusal`), null when it can: the form shows it
    # beside the switch, and turning it on is refused with the same sentence.
    blind_review_reason: Optional[str] = None
    # Whether the project's org holds Live Cast (`app/live_cast/entitlement.py`): the console asks
    # `GET …/live-cast` only then, since every Live Cast route 403s without it. No default, as the lane's.
    live_cast_offered: bool
    # Production QA (`ProjectUpdate`'s fields read back). `production_qa_offered` is the org's switch
    # (`production_qa/gate.py::org_on`): the opt-in is refused while it is off. No default, as the lane's.
    production_qa_offered: bool
    production_qa_enabled: bool = False
    production_qa_enabled_by: Optional[UUID] = None
    deploy_workflows: list[str] = []
    revert_policy: str = "off"
    regression_window_min: int = 60
    regression_error_ratio: float = 1.5
    regression_min_events: int = 20
    regression_p95_ratio: float = 1.3
    health_probes: list[dict[str, Any]] = []
    smoke_commands: list[dict[str, Any]] = []
    # Benchmark mode (`ProjectUpdate`'s two read back). `benchmark_offered` is the org's switch: the console draws the
    # benchmark settings only while it is on. The month's shadow spend and its cap (`benchmark.budget`) ride only then
    # (null otherwise); `benchmark_budget_reached` is the sampler's own stop.
    benchmark_offered: bool
    benchmark_opt_in: bool = False
    benchmark_sample_rate: float = 0.05
    benchmark_spent_usd: Optional[float] = None
    benchmark_cap_usd: Optional[float] = None
    benchmark_budget_reached: bool = False
    # No default: a serialized null would seed a blank textarea, and the next save would PUT null and wipe
    # human-written prose nothing else holds.
    agent_instructions: Optional[str]
    run_jobs_as_id: Optional[UUID] = None
    # The "Run jobs as" person's name (`app/actors.py::actor_names`), resolved by `_project_out`'s caller.
    run_jobs_as_name: Optional[str] = None
    # Who the project's background jobs actually run as (`git_access.project_forge_person`) and, when they hold no
    # login on its forge root or the pick was skipped for want of one, why (`jobs_run_as_out`). The one-project
    # reads and the `PUT` answer only; null elsewhere.
    jobs_run_as_name: Optional[str] = None
    jobs_run_as_warning: Optional[str] = None
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
    # `<github org>/<org slug>-` (`jaah_repos.jaah_repo_prefix`), "" when no Jaah org is set: the node's credential helper
    # and pre-push guard refuse a destination in the Jaah org whose path does not start with it.
    jaah_repo_prefix: str = ""
    # On a Jaah repo it is Jaah's to run (`jaah_repos.managed_ids`): the console shows an org member only Name, Default
    # agent and persona, Agent instructions, Access and Budget, and hides its repositories; the writes refuse the rest.
    jaah_managed: bool = False
    # Each pipeline stage with an address, in pipeline order (`release_stages.live_urls`): General's live line.
    live_urls: list[LiveUrlOut] = []


class ProjectCreateOut(ProjectOut):
    """`POST /api/projects`' 201: the row, plus the placement's git-access advisory (the first placed node's
    `routes/pool_slots.py::_clone_git_access_warning`) — null without a placement, or when every clone resolves."""

    placement_warning: Optional[str] = None
    # The task the create filed and the person lands on (`app/project_onboarding.py`) — a new web app's App Wizard
    # epic, a business project's Business Wizard task, else the onboarding task — or null when none was.
    onboarding_task_id: Optional[int] = None
    # The Easy Mode app a `web_app` intake made (`app/project_intake.py`), or null.
    hosted_app: Optional[HostedAppOut] = None


@dataclass(frozen=True)
class MicrovmPage:
    """What `_project_out` reads beyond the row, resolved once per page (`microvm_page`): each project's org row
    (its sizing tier and routing maximum), the configured CP host (an allowlist entry), which of the page's projects
    an enabled MicroVM node runs, each unplaceable project's `dispatch_block_kind`, and each org's routing cap and
    the sentence above it (`modes.cap`, `modes.refusal`)."""

    orgs: Mapping[UUID, Org]
    cp_host: Optional[str]
    on_microvm: frozenset[int] = frozenset()
    dispatch_blocks: Mapping[int, str] = field(default_factory=dict)
    routing_caps: Mapping[UUID, tuple[str, Optional[str]]] = field(default_factory=dict)
    #: Which orgs' plans include intelligent routing, and which of the page's projects a node that can run a blind
    #: review hosts (`blind_review.capable_node_ids`): together, `blind_review.toggle_refusal`'s inputs.
    routing_plans: Mapping[UUID, bool] = field(default_factory=dict)
    blind_capable: frozenset[int] = frozenset()


#: `ProjectOut.dispatch_block`'s sentences, keyed by `dispatch_block_kind`.
NO_NODE_BLOCK = "No node runs this project. Add one on the Nodes page, or change its Node groups."
NO_ACCOUNT_BLOCK = "No enabled coding agent account in this organization. Add one in Settings › Credentials."
DISPATCH_BLOCKS: dict[str, str] = {"no_node": NO_NODE_BLOCK, "no_account": NO_ACCOUNT_BLOCK}


def _dispatch_blocks(session: Session, projects: list[Project]) -> dict[int, str]:
    """`{project id: dispatch_block_kind}` for each project a started task could not be placed for: no
    node is in its node groups (`slot_allocation.placement_node_ids_by_project`), else its org has no enabled `accounts` row while
    not on the Jaah account and no default SDK agent spares it one. Four reads per page, none per project."""
    if not projects:
        return {}
    placed = slot_allocation.placement_node_ids_by_project(session, [project.id for project in projects])
    org_ids = {project.org_id for project in projects if project.org_id is not None}
    with_coder = org_limits.orgs_with_a_coder(session, org_ids)
    out: dict[int, str] = {}
    for project in projects:
        if not placed.get(project.id):
            out[project.id] = "no_node"
        elif (project.default_agent_id is None and project.org_id is not None
              and project.org_id not in with_coder):
            out[project.id] = "no_account"
    return out


def microvm_page(session: Session, projects: Iterable[Project]) -> MicrovmPage:
    """One `orgs` select, one Operator Settings read and one placement read (when a MicroVM node exists) for a
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
            placed = slot_allocation.placement_node_ids_by_project(session, page_ids)
            on_microvm.update(pid for pid, nodes in placed.items()
                              if any(vm_nodes.get(n) == org_of[pid] for n in nodes))
    cp_host = None
    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs.
        from app import microvm_egress

        cp_host = microvm_egress.cp_host()
    return MicrovmPage(orgs=orgs, cp_host=cp_host, on_microvm=frozenset(on_microvm),
                       dispatch_blocks=_dispatch_blocks(session, projects),
                       routing_caps={org_id: _routing_cap(session, org) for org_id, org in orgs.items()},
                       routing_plans={org_id: plan_terms.intelligent_routing(session, org)
                                      for org_id, org in orgs.items()},
                       blind_capable=_blind_capable(session, page_ids))


def _blind_capable(session: Session, project_ids: set[int]) -> frozenset[int]:
    """Which of `project_ids` may start a task on a node that can run a blind review. Two reads per page."""
    if not project_ids:
        return frozenset()
    hosting = slot_allocation.placement_node_ids_by_project(session, project_ids)
    capable = blind_review.capable_node_ids(session, frozenset().union(*hosting.values()))
    org_of = dict(session.execute(select(Node.id, Node.org_id).where(Node.id.in_(capable))).all()) if capable else {}
    orgs = dict(session.execute(select(Project.id, Project.org_id).where(Project.id.in_(project_ids))).all())
    # A node of the project's own org only, as placement's floor.
    return frozenset(pid for pid, nodes in hosting.items() if any(org_of.get(n) == orgs.get(pid) for n in nodes & capable))


def _blind_review_refusal(vm: MicrovmPage, project: Project) -> Optional[str]:
    """Why Blind review cannot be turned on for `project` now, or None (`blind_review.toggle_refusal`)."""
    return blind_review.toggle_refusal(
        plan_on=bool(vm.routing_plans.get(project.org_id)), on_microvm=project.id in vm.on_microvm,
        capable=project.id in vm.blind_capable,
    )


def _routing_cap(session: Session, org: Org) -> tuple[str, Optional[str]]:
    """`(the highest mode the org's projects may choose, why nothing above it)` — one plan read per org."""
    cap = modes.cap(session, org)
    above = next((mode for mode in ROUTING_MODES if modes.rank(mode) > modes.rank(cap)), None)
    return cap, modes.refusal(session, org, above) if above is not None else None


def live_cast_holders(session: Session, org_ids: Iterable[Optional[UUID]]) -> frozenset[UUID]:
    """Which of these orgs hold Live Cast now (`app/live_cast/entitlement.py::holders`): none in an edition without
    it, so no project there is offered it."""
    if not edition_features.enabled("live_cast"):
        return frozenset()
    from app.live_cast.entitlement import holders

    return holders(session, org_ids)


def _project_out(
    project: Project, *, org: Optional[OrgRef], lane: merge_lane_state.LaneState, vm: MicrovmPage,
    repos: list[ProjectRepo], live_cast: frozenset[UUID], production_qa: frozenset[UUID],
    benchmark: Mapping[int, Budget], job_people: Mapping[UUID, str], jaah_managed: frozenset[int],
    full_gates: Mapping[int, str], port_vars: Mapping[int, list[PortSpec]],
    live_urls: Mapping[int, list[tuple[str, str]]],
) -> ProjectOut:
    """The one constructor for this response — `routes/portfolio.py::list_projects` returns it too.

    `org`, `lane`, `vm`, `repos`, `live_cast` and `production_qa` are resolved by the caller (one `org_refs` call, then
    `org_ref_of`, `app/tenancy/labels.py`; one `merge_lane.lane_states` call; one `microvm_page` call; one
    `project_repos.repos_by_project` call; one `live_cast_holders` call; one `production_qa_gate.orgs_on` call; one
    `offered_benchmark_budgets` call; one `job_people_of` call; one `jaah_repos.managed_ids` call; one
    `full_gate.full_gate_commands` call — `gate_full_cmd` is the merge step's command, never the mirror column; one
    `release_stages.live_urls` call), never
    queried here,
    to avoid an N+1; keyword-only with no default so every call site decides."""
    ref = forges.repo_ref_or_none(project)
    # The community edition has no MicroVMs: the model's defaults, spelled out because `from_row` would otherwise
    # look for them on the row.
    microvm: dict[str, Any] = dict(egress_default=[], egress_effective=[], microvm_sizing={})
    if edition_features.enabled("microvm"):
        from app import microvm_egress, microvm_sizing

        configured = (vm.cp_host, microvm_egress.forge_host(project))
        microvm = dict(
            egress_default=microvm_egress.effective(extra=configured),
            egress_effective=microvm_egress.effective(
                project.egress_allow or (), project.egress_deny or (), extra=configured
            ),
            microvm_sizing=microvm_sizing.sizing_out(None, project, vm.orgs.get(project.org_id), own=project),
        )
    return from_row(
        ProjectOut, project, org=org,
        web_url=ref.web_url() if ref is not None else None,
        clone_url=ref.clone_url() if ref is not None else None,
        merge_lane=lane.active, merge_lane_reason=lane.reason,
        merge_lane_offered=lane.offered,
        # The `PUT` answer's alone (`update_project`).
        move_task_id=None,
        jobs_run_as_name=None, jobs_run_as_warning=None,
        live_cast_offered=project.org_id in live_cast,
        production_qa_offered=project.org_id in production_qa,
        **_benchmark_out(project, benchmark.get(project.id)),
        deploy_workflows=list(project.deploy_workflows or []),
        security_check_patterns=list(project.security_check_patterns or []),
        egress_allow=list(project.egress_allow or []), egress_deny=list(project.egress_deny or []),
        **microvm,
        on_microvm=project.id in vm.on_microvm,
        dispatch_block=DISPATCH_BLOCKS.get(vm.dispatch_blocks.get(project.id, "")),
        dispatch_block_kind=vm.dispatch_blocks.get(project.id),
        repos=[project_repo_out(row) for row in repos],
        routing_latency_cap_hours=(
            float(project.routing_latency_cap_hours) if project.routing_latency_cap_hours is not None else None
        ),
        experiment_budget_share=float(project.experiment_budget_share),
        routing_cap=vm.routing_caps.get(project.org_id, (modes.OFF, None))[0],
        routing_cap_reason=vm.routing_caps.get(project.org_id, (modes.OFF, None))[1],
        blind_review_reason=_blind_review_refusal(vm, project),
        run_jobs_as_name=job_people.get(project.run_jobs_as_id) if project.run_jobs_as_id else None,
        jaah_repo_prefix=jaah_repos.jaah_repo_prefix(org.slug) if org is not None else "",
        jaah_managed=project.id in jaah_managed,
        gate_full_cmd=full_gates.get(project.id, ""),
        port_vars=[spec.name for spec in port_vars.get(project.id, [])],
        live_urls=[LiveUrlOut(stage=name, url=url) for name, url in live_urls.get(project.id, [])],
    )


def jobs_run_as_out(session: Session, project: Project) -> dict[str, Optional[str]]:
    """`ProjectOut.jobs_run_as_name`/`jobs_run_as_warning` for one project: who `project_forge_person` picks, and a
    sentence when that person holds no login on the project's forge root, or when the named pick (the project's,
    else the org's "Run jobs as") was skipped for want of one."""
    person = git_access.project_forge_person(session, project)
    if person is None:
        return {"jobs_run_as_name": None, "jobs_run_as_warning": None}
    named = git_access.explicit_job_people(session, project)
    names = actor_names(session, [person, *named[:1]])
    name = names.get(person)
    try:
        repo = forges.repo_ref(project)
        where = f"{forges.FORGE_LABELS[repo.forge]} login for {repo.root}"
    except forges.ForgeUrlError:
        where = "git login"
    warning = None
    if not git_access.has_forge_login(session, person, project):
        warning = f"{name} has no {where}, so background jobs can't push until they connect one in Profile > Git."
    elif named and named[0] != person:
        warning = f"{names.get(named[0])} has no {where}, so background jobs run as {name}."
    return {"jobs_run_as_name": name, "jobs_run_as_warning": warning}


def job_people_of(session: Session, projects: Iterable[Project]) -> Mapping[UUID, str]:
    """`_project_out`'s `job_people`: every project's "Run jobs as" person named, in one `actor_names` query."""
    return actor_names(session, [p.run_jobs_as_id for p in projects])


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

    Only `uq_projects_org_name` is answered; any other constraint re-raises. The key is per org, so the
    colliding row is always the caller's own org's and the plain sentence reveals nothing across orgs. No id:
    the row committed after this transaction's snapshot, so it is not re-read here."""
    if constraint_name(exc) != "uq_projects_org_name":
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
            raise HTTPException(status_code=422, detail="name is required" if creating else "name cannot be blank")
        fields["name"] = name
    if fields.get("tech_stack") and has_duplicate_tech_stack_entry(fields["tech_stack"]):
        raise HTTPException(status_code=422, detail="tech_stack contains a duplicate (layer, technology) entry")
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
                raise ce_caps.not_in_edition()
    if edition_features.enabled("microvm"):
        from app import microvm_egress, microvm_sizing

        for field_name in ("egress_allow", "egress_deny"):
            if field_name in fields:
                # NOT NULL with `{}`: an explicit null clears the list.
                with refused_as(422, ValueError):
                    fields[field_name] = microvm_egress.validated_names(fields[field_name])
        with refused_as(422, ValueError):
            fields.update(microvm_sizing.validated(fields))
    fields.update(_validated_production_qa(fields, _forge_of(fields, current)))
    with refused_as(422, ValueError):
        fields.update(production_qa_probes.validated(fields, current))
    if fields.get("merge_lane_batch_max") is not None:
        low, high = merge_lane_state.LANE_BATCH_MAX_BOUNDS
        value = fields["merge_lane_batch_max"]
        if isinstance(value, bool) or not low <= value <= high:
            raise HTTPException(status_code=422, detail=f"Set the batch size between {low} and {high}, or clear it.")
    if fields.get("outcome_scan_interval_minutes") is not None:
        problem = outcome_settings.check(fields["outcome_scan_interval_minutes"])
        if problem:
            raise HTTPException(status_code=422, detail=problem)
    if "security_check_patterns" in fields:
        with refused_as(422, ValueError):
            fields["security_check_patterns"] = outcome_security.clean_patterns(fields["security_check_patterns"] or [])

    if creating:
        # After the mandatory seven refuse nulls — see the docstring.
        fields = {key: value for key, value in fields.items() if value is not None}
    return fields


#: Production QA's regression knobs: `(label, low, high)`, each NOT NULL (`models.Project`). A ratio is a multiple of the
#: pre-deploy baseline, so one at or below 1 would call every deploy a regression.
_REGRESSION_BOUNDS: Mapping[str, tuple[str, float, float]] = {
    "regression_window_min": ("Window", 5, 1440),
    "regression_min_events": ("Minimum events", 1, 1_000_000),
    "regression_error_ratio": ("Error rate ratio", 1.01, 100),
    "regression_p95_ratio": ("p95 latency ratio", 1.01, 100),
}
#: The most deploy workflow paths a project watches, the longest one, and its shape: a repo-relative `.yml`/`.yaml`
#: path (later readers match and fetch it, so nothing else is stored) — on an Azure DevOps project a pipeline or release
#: definition name instead (`app/production_qa/azure_devops.py` matches it by name). The longest production
#: environment name.
_DEPLOY_WORKFLOWS_MAX = 20
_DEPLOY_WORKFLOW_MAX_CHARS = 200
_DEPLOY_WORKFLOW_PATH = re.compile(r"[A-Za-z0-9._/-]+\.ya?ml")
_DEPLOY_PIPELINE_NAME = re.compile(r"[A-Za-z0-9._()\[\] -]+")


def _forge_of(fields: dict[str, Any], current: Optional[Project]) -> Optional[str]:
    """The forge the Production QA fields are judged for: the repo URL this write sets, else the project's own."""
    if fields.get("repo_url"):
        try:
            return forges.parse_repo_url(fields["repo_url"], fields.get("forge"), fields.get("forge_base_url")).forge
        except forges.ForgeUrlError:
            return None
    return getattr(current, "forge", None)


def _validated_production_qa(fields: dict[str, Any], forge: Optional[str] = None) -> dict[str, Any]:
    """`ProjectUpdate`'s Production QA fields, normalized: 422 naming the field for a null on a NOT NULL column, a
    revert policy outside `REVERT_POLICIES` or a knob outside `_REGRESSION_BOUNDS`. `forge` is the project's: an
    `azure_devops` project's deploy workflows are pipeline names. The org switch is checked by `apply_project_update`
    (it needs the session)."""
    out: dict[str, Any] = {}
    reject_cleared(fields, ("production_qa_enabled", "revert_policy", *_REGRESSION_BOUNDS))
    if "deploy_workflows" in fields:
        paths = list(dict.fromkeys(p.strip() for p in fields["deploy_workflows"] or [] if p and p.strip()))
        if len(paths) > _DEPLOY_WORKFLOWS_MAX:
            raise HTTPException(status_code=422, detail=f"List at most {_DEPLOY_WORKFLOWS_MAX} release workflows.")
        if any(len(p) > _DEPLOY_WORKFLOW_MAX_CHARS for p in paths):
            raise HTTPException(
                status_code=422, detail=f"A release workflow path is at most {_DEPLOY_WORKFLOW_MAX_CHARS} characters.",
            )
        if forge == "azure_devops":
            if any(not _DEPLOY_PIPELINE_NAME.fullmatch(p) or ".." in p for p in paths):
                raise HTTPException(status_code=422, detail="A release pipeline is its name in Azure DevOps.")
        elif any(not _DEPLOY_WORKFLOW_PATH.fullmatch(p) or ".." in p for p in paths):
            raise HTTPException(
                status_code=422, detail="A release workflow is the path of a .yml file in the repository.",
            )
        out["deploy_workflows"] = paths
    if "revert_policy" in fields and fields["revert_policy"] not in REVERT_POLICIES:
        raise HTTPException(
            status_code=422, detail=f"revert_policy must be one of {', '.join(REVERT_POLICIES)}",
        )
    for name, (label, low, high) in _REGRESSION_BOUNDS.items():
        value = fields.get(name)
        if value is not None and (isinstance(value, bool) or not low <= value <= high):
            raise HTTPException(status_code=422, detail=f"Set {label} between {low:g} and {high:g}.")
    return out


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


def _refuse_jaah_repo_edit(session: Session, project: Project, sent: dict[str, Any],
                           repo_specs: Optional[list[project_repos.RepoSpec]]) -> None:
    """422 when a project Jaah made a repository for (`app/jaah_repos.py`) would re-point its primary to another
    repository of Jaah's GitHub org. Its runners push with a token the console's GitHub App mints for exactly its
    own repository (`app/easy_mode_runner_tokens.py`), so that move would buy a write token for someone else's. A
    resend of the stored value, or a return to its own Jaah repo, passes; a repository outside that org is the
    project's own to pick."""
    row = jaah_repos.row_for(session, project.id)
    if row is None:
        return
    if repo_specs is not None:
        primary = next((spec for spec in repo_specs if spec.is_primary), None)
        target = (primary.repo_url, primary.forge, primary.forge_base_url) if primary is not None else None
    elif "repo_url" in sent:
        target = (sent["repo_url"], sent.get("forge", project.forge), sent.get("forge_base_url", project.forge_base_url))
    else:
        return
    if target is None or not target[0] or target[0] == project.repo_url:
        return
    try:
        ref = forges.parse_repo_url(target[0], target[1], target[2])
    except forges.ForgeUrlError:
        return  # the shape check that follows names it
    if jaah_repos.in_jaah_org(ref) and ref.path.lower() != row.name.lower():
        raise HTTPException(
            status_code=422,
            detail=f"{project.name}'s repository was made by Jaah, so it cannot be re-pointed to another "
                   "repository Jaah made",
        )


#: What an org member still sets on a Jaah-managed project's row (`jaah_repos.managed`); its Access and Budget are
#: their own routes, and the move to a repo of its own is `repos` with a replacement (`_refuse_managed_fields`).
_MEMBER_MANAGED_FIELDS = frozenset({"name", "default_persona", "agent_instructions", "default_agent_id", "default_provider"})


def _refuse_managed_fields(session: Session, project: Project, sent: dict[str, Any],
                           repo_specs: Optional[list[project_repos.RepoSpec]], actor: AdminUser,
                           principal: Principal | None) -> None:
    """403 when an org member's save changes anything of a Jaah-managed project beyond `_MEMBER_MANAGED_FIELDS`. A
    resend of the stored value passes, as `_refuse_system_project_edit`'s. Its repositories change only to put the
    project on one of its own (Transfer repository: `jaah_repo_replace.detect` reads a `replace`), which the
    Jaah-repo question then answers. The operator passes (`jaah_repos.operator_caller`)."""
    if jaah_repos.operator_caller(actor, principal) or not jaah_repos.managed(session, project.id):
        return
    changed = [f for f in sent if f not in _MEMBER_MANAGED_FIELDS and f != "repos" and sent[f] != getattr(project, f, None)]
    if repo_specs is not None or any(f in _LEGACY_REPO_FIELDS for f in changed):
        replacement = jaah_repo_replace.detect(session, project, sent, repo_specs)
        if replacement is not None and replacement.reason == "replace":
            changed = [f for f in changed if f not in _LEGACY_REPO_FIELDS and f not in project_repos.POLICY]
        elif repo_specs is not None and _repos_differ(session, project, repo_specs):
            changed.append("repos")
    if changed:
        raise HTTPException(status_code=403, detail=jaah_repos.MANAGED_REFUSAL)


def _repos_differ(session: Session, project: Project, repo_specs: list[project_repos.RepoSpec]) -> bool:
    """Whether `repo_specs` changes the stored repositories' URLs or which one is primary (a resend does not)."""
    stored = {(row.repo_url, row.is_primary) for row in project_repos.repos_of(session, project.id)}
    return stored != {(spec.repo_url, spec.is_primary) for spec in repo_specs}


def _refuse_system_project_delete(project: Project) -> None:
    """422 for deleting a system project: the platform recreates and relies on it (`app/node_targets.py`)."""
    if project.system_kind is not None:
        raise HTTPException(status_code=422, detail=f"{project.name} is a system project, so it cannot be deleted")


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
# `("projects.py", "has_authority_to_author_governance", "nodes:edit")`.
GOVERNANCE_FIELDS = _GOVERNANCE_FIELDS


def _prefix_key(value: Any) -> str:
    """A branch prefix compared as `_normalized_branch_prefix` would store it, without its refusals (authority is
    judged before shape)."""
    return str(value).strip().rstrip("/")


def _repos_author_the_policy(
    session: Optional[Session], rows: list[dict[str, Any]], project: Optional[Project]
) -> list[str]:
    """The `project_repos.POLICY` fields this `repos` write would change — the same governance as the project's
    `branch_prefix`/`push_enabled`. A change, not authorship: the Edit form resends every row.

    Policy follows the repository, not the row id. Each row is judged by the policy it will STORE against its
    repository's prior — the stored row naming that repository, whatever its id, else what it would inherit
    (`project_repos.prior_policies`, as `replace` applies it); an omitted key stores the prior, so it changes
    nothing. The primary's stored policy is also judged against the project's columns (a primary swap re-projects
    them). On create (`project` None) every comparison is with the column defaults.

    Removing a repository whose policy differs from what a new row would inherit changes those keys (re-added
    later, it would inherit them); removing one whose policy equals the inherited one needs only `projects:edit`."""
    stored = project_repos.repos_of(session, project.id) if session is not None and project is not None else []
    inherited = (
        project_repos.inherited_policy(project) if project is not None
        else {"branch_prefix": "", "push_enabled": True, "allow_force_push": False}
    )
    projected = {key: getattr(project, key) for key in project_repos.POLICY} if project is not None else inherited
    urls = [(row.get("repo_url") or "").strip() for row in rows]
    priors = project_repos.prior_policies(
        stored, [(row.get("id"), url) for row, url in zip(rows, urls, strict=True)], inherited,
    )
    # (before, after) pairs; a removed repository's after is what it would inherit if re-added.
    pairs = [({key: getattr(gone, key) for key in project_repos.POLICY}, inherited)
             for gone in project_repos.removed_repos(stored, urls)]
    for row, prior in zip(rows, priors, strict=True):
        after = {key: prior[key] if row.get(key) is None else row[key] for key in project_repos.POLICY}
        # With no stored row the project's columns are what the first row inherits (force push aside, off).
        pairs += [(before, after) for before in ([prior, projected] if row.get("is_primary") and stored else [prior])]
    changed: set[str] = set()
    for before, after in pairs:
        for key in project_repos.POLICY:
            old, new = before[key], after[key]
            if key == "branch_prefix":
                old, new = _prefix_key(old), _prefix_key(new)
            if new != old:
                changed.add(key)
    return [key for key in project_repos.POLICY if key in changed]


def _governance_named(
    fields: dict[str, Any], project: Optional[Project], session: Optional[Session] = None,
) -> list[str]:
    """The governance fields a payload authors: those it names, plus `integration_branch` when its `repos` set
    would change the primary's branch (`_repos_author_the_branch`) and any row Branch policy field it would change
    (`_repos_author_the_policy`, which reads the stored rows through `session`). `project` None is a create,
    compared with the column defaults."""
    named = [field_name for field_name in _GOVERNANCE_FIELDS if field_name in fields]
    rows = fields.get("repos")
    if rows and "integration_branch" not in named and _repos_author_the_branch(rows, project):
        named.insert(0, "integration_branch")
    if rows:
        named += [key for key in _repos_author_the_policy(session, rows, project) if key not in named]
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
    named = _governance_named(fields, None)
    if named:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{named[0]} may not be set by a node-token caller: an X-Agent-Token create has no "
                "principal, so it may not author this project's governance — the gate and stack "
                "commands are executed by a shell on a runner, and the branch/push fields decide "
                "what that runner may push. Omit it to take the column default, then set it from the "
                "console as a signed-in user."
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

    The node half of the governance door (`has_authority_to_author_governance` says why). The same
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


def has_authority_to_author_governance(
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


def _require_authority_to_author_governance(
    session: Session,
    fields: dict[str, Any],
    actor: AdminUser,
    principal: Principal | None,
    project: Project | None,
) -> None:
    """The raising half: 403 unless `has_authority_to_author_governance` admits a payload that names one
    of the seven. A payload touching none (`notes`, `tech_stack`) is unaffected.

    The payload test comes first (free), so an ordinary edit never pays the grant probe. Called on both
    mutating paths — creating with these values is the same capability. A handler check because the
    condition is a property of the payload; tests authorship (`in fields`), not change. 403, not 422. The
    refusal sentence forks by seat: a tenant reads `app/tenancy/copy.py::governance_authority`."""
    named = _governance_named(fields, project, session)
    if not named:
        return
    if has_authority_to_author_governance(session, actor, principal, project):
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


def _refuse_name_conflict(session: Session, name: str, org_id: Optional[UUID]) -> None:
    """409 when the org already has a project by that name."""
    field, existing = _find_conflict(session, name=name, org_id=org_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"project with {field} '{name}' already exists (id={existing.id}, name={existing.name})",
        )


def _create_project_row(
    session: Session, body: ProjectCreate, principal: Principal | None, actor: AdminUser | None,
    org_id: Optional[UUID], *, server_fields: Optional[dict[str, Any]] = None,
) -> Project:
    """Insert/409 logic for `POST /api/projects` (Connect Existing).

    The one insertion point. No creator grant: the creator holds `projects:create`, which only org-wide
    roles carry, so they already reach the new row.
    `principal` and `actor` have no defaults, so a new create path must state its actor. `org_id` is the
    org the row will be stamped with (the bound one), for gate defaults and cap. `server_fields`
    are columns the server sets (a Jaah-made repository's URL and branches, `app/project_intake.py`): applied after
    the authorship checks, which judge only what the caller sent, and validated like a sent value. With them the
    caller's `repos` is dropped: `project_intake.refuse_invalid` let through only an empty one, and beside the server's
    `repo_url` it would be refused as both."""
    # `exclude_unset`, not `exclude_none`: `_validated_fields` strips None itself, after the mandatory
    # seven refuse explicit nulls.
    # `placement` and `org_id` are not settable columns, and the default agent needs the row's org; `create_project`
    # applies them.
    excluded = {*_CREATE_ONLY_FIELDS, *_DEFAULT_AGENT_FIELDS, *(("repos",) if server_fields else ())}
    sent = _without_repo_fields(body.model_dump(exclude_unset=True, exclude=excluded), None)
    # Authority before shape.
    _reject_governance_from_a_node_token(sent, principal)
    # Same order as `update_project`: no-principal 422 first, then the authority 403. Skipped with no
    # actor — no caller reaches that today (the route takes `require_user`); kept so re-admitting a
    # token caller fails closed. `project=None` is the create path (see the door's docstring).
    if actor is not None:
        _require_authority_to_author_governance(session, sent, actor, principal, None)
    if server_fields:
        sent = {**sent, **server_fields}
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

    _refuse_name_conflict(session, name, org_id)

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
    # Added INSIDE the savepoint: `begin_nested()` flushes what is pending before it opens one.
    try:
        with session.begin_nested():
            session.add(project)
            session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=_race_conflict_detail(exc, name)) from exc
    actor_id = actor.id if actor is not None else None
    if repo_specs is not None:
        project_repos.replace(session, project, repo_specs, actor_id)
    elif legacy_repo:
        project_repos.upsert_primary(session, project, legacy_repo, actor_id)
    # The default pipeline, with no configuration, for a project born with a repo (`app/releases/stages.py`).
    release_stages.materialise(session, project)
    return project


#: Create-only body fields that are no project column: the create path applies each itself.
_CREATE_ONLY_FIELDS = frozenset({"placement", "org_id", "intake", "pipeline_preset"})


def _pipeline_preset(session: Session, slug: Optional[str]) -> Optional[dict[str, Any]]:
    """The preset document a create starts its pipeline from, or None for today's default (absent or `standard`).
    Refused before anything is made: an unknown slug 422; a preset with stages ahead of Dev 409 `pipeline_upgrading`
    while a previous release still serves (`release_stages.old_version_gone`)."""
    if slug is None or slug == pipeline_doc.STANDARD:
        return None
    doc = pipeline_doc.preset_or_422(slug)
    # Ideas only writes no stage ahead of Dev a previous release could misread.
    if slug != pipeline_doc.IDEAS_ONLY and not release_stages.old_version_gone(session):
        raise HTTPException(status_code=409, detail={
            "code": "pipeline_upgrading",
            "message": "Pick Standard for now, or try again in a few minutes, once the update finishes.",
        })
    return doc


def _start_pipeline_from(session: Session, project: Project, slug: str, doc: dict[str, Any], actor: AdminUser,
                         principal: Principal | None) -> None:
    """Write the new project's pipeline as `doc` through the one pipeline write, last in the create's transaction:
    its commit is the create's, so a refused save leaves no project behind."""
    if not release_stages.has_repo(project) and slug != pipeline_doc.IDEAS_ONLY:
        raise HTTPException(status_code=422, detail="A project without a repository can start from Ideas only.")
    mode = basics.mode_of_preset(slug)
    if mode == basics.CUSTOM and not basics.custom_offered(session, project):
        raise HTTPException(status_code=409, detail=basics.NOT_ON_PLAN)
    body = pipeline_doc.instantiate(doc, project, release_pipeline.stages_for(session, project))
    specs, steps = pipeline_doc.specs_of(body)

    def set_mode(_fields: dict[str, Any]) -> None:
        project.pipeline_mode = mode  # in the save's own commit

    release_pipeline.write(session, project, actor, principal, specs, steps, before_commit=set_mode)


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


@router.post("", response_model=ProjectCreateOut, status_code=201)
@feature("projects", "create")
async def create_project(
    body: ProjectCreate,
    db: DbDep,
    # `require_user`: `app.auth.NODE_TOKEN_API_PATHS` grants the token `GET /api/projects` only, and this
    # row carries what runners clone and act on.
    # The same principal `actor` names, in the shape every authz helper takes.
    caller: AsyncCaller = Depends(current_caller),
) -> ProjectCreateOut:
    """Connect Existing: create a DB row for an already-existing repo. No scaffolding is triggered; the
    project's onboarding task is filed and started (`app/project_onboarding.py`).

    `org_id` naming another org is refused (422) unless the caller is the operator, whose create then runs
    in that org (`_create_project_in`) and files its onboarding task there as that org's Owner seat."""
    target = body.org_id
    if target is None or target == caller.actor.org_id:
        return await db.run_sync(_create_project_in, body, caller.actor, caller.principal, caller.actor.org_id)
    if caller.principal is None or not caller.principal.is_operator:
        raise HTTPException(status_code=422, detail="org_id must be your own organization")
    if body.intake is not None:
        # The setup answers file an onboarding task as the creator, who is no member of the named org.
        raise HTTPException(status_code=422, detail=project_intake.NOT_OWN_ORG)
    if not is_tenant_org(target):
        raise HTTPException(status_code=422, detail="org_id must be a tenant organization")
    # The operator seeding a tenant: the guard bound this write to the operator's own org (R5.7), so leave
    # it and enter the NAMED org on a session of its own — every read (name conflict, node,
    # credential, cap) and the stamp are then that org's, as for its own member. Admitted by
    # `principal.is_operator` just above; `named_org` refuses a missing or disabled org with a 422.
    if body.placement is not None:
        # A pool clone authenticates as its declarer, and the operator is no member of the tenant org.
        raise HTTPException(status_code=422, detail="placement is the org's own to declare — create without it")
    # The route session has run nothing yet (it holds no connection), so the named org's own session may open.
    async with db_async.async_session() as bound:
        return await bound.run_sync(_create_project, body, caller.actor, caller.principal, target)


def _create_project(
    bound: Session, body: ProjectCreate, actor: AdminUser, principal: Principal | None, target: UUID,
) -> ProjectCreateOut:
    """`create_project`'s operator arm, on a session of its own entered in the named org `target`."""
    with seam.detached(), seam.bound_to(target):
        return _create_project_in(bound, body, actor, principal, named_org(bound, target), audit_in=target)


def _create_project_in(
    session: Session, body: ProjectCreate, actor: AdminUser, principal: Principal | None, org_id: UUID,
    *, audit_in: Optional[UUID] = None,
) -> ProjectCreateOut:
    """`create_project` on a session bound to `org_id`.

    With `placement`, the slot-writer bar and the nodes are checked before any write, so any refusal leaves no
    project behind; nothing is written for it (`ProjectPlacement`).

    With `intake` (`app/project_intake.py`), its answer picks what is made — an Easy Mode app, a plain Jaah repo, or
    the person's own repository — and is written into the Agent instructions in the same transaction."""
    preset = _pipeline_preset(session, body.pipeline_preset)
    intake = body.intake
    if intake is not None:
        project_intake.refuse_invalid(intake, repo_url=body.repo_url, repo_count=len(body.repos or []))
        if intake.kind in project_intake.JAAH_MADE:
            project_intake.refuse_without_runner_app(operator=_is_operator(principal))
    placement = body.placement
    nodes: list[tuple[str, Node]] = []
    if placement is not None:
        _require_slot_writer(actor, principal)
        nodes = _placement_nodes(session, placement)
    app: Any = None
    if intake is not None and intake.kind == "web_app":
        project, app = _create_web_app_project(session, body, intake, actor, principal, org_id)
        if project.easy_mode:
            nodes = []  # it runs on the org's Easy Mode shared node, where `service.create` placed it
    elif intake is not None and intake.kind in project_intake.JAAH_MADE:
        project = _create_plain_repo_project(session, body, actor, principal, org_id)
    else:
        project = _create_project_row(session, body, principal, actor, org_id)
    setup_answer = _record_setup_answer(session, project, intake, app) if intake is not None else None
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
    if project.has_repo and nodes and jaah_repos.row_for(session, project.id) is None:
        # A Jaah-made repo's clones take its runner login, never the creator's (`routes/agent/task_secrets.py`).
        # The creator's own tasks clone as the creator, so their connection is the one asked about: once,
        # named on the first node. Not the slot rows, which do not exist yet.
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
    if preset is not None:
        # Last before the commit: the pipeline save commits, so it carries the whole create.
        _start_pipeline_from(session, project, body.pipeline_preset or "", preset, actor, principal)
    session.commit()
    # After the commit, best effort: a refused onboarding task never undoes the project.
    # A new web app starts with its App Wizard epic instead, a business project with its Business Wizard task, and
    # the person lands on it (`onboarding_task_id`). The operator seeding a tenant (`audit_in`, never with an intake)
    # files it as that org's Owner seat, as their tab acting as the org would (`app/act_as.py`), and Start names the
    # org's Owner to push as (`project_onboarding._file_and_start`).
    onboarding_task_id: Optional[int] = None
    filer, filer_principal = actor, principal
    if audit_in is not None and principal is not None:
        filer, filer_principal = act_as.seat_user(actor, audit_in), owner_seat(principal, audit_in)
    if intake is not None and intake.kind == "web_app":
        onboarding_task_id = file_app_wizard_epic(
            session, project, filer, filer_principal, description=intake.description or "", setup_answer=setup_answer,
        )
    elif intake is not None and intake.kind in project_intake.BUSINESS_KINDS:
        onboarding_task_id = file_business_wizard_task(
            session, project, filer, filer_principal, description=intake.description or "", setup_answer=setup_answer,
        )
    else:
        onboarding_task_id = file_onboarding_task(
            session, project, filer, filer_principal,
            setup_answer=setup_answer if intake is not None and intake.kind in project_intake.JAAH_MADE else None,
        )
    session.refresh(project)
    # After the refresh: `write_seam.stamp_org` sets `org_id` during the flush, so this resolves the value
    # the row landed with (`app/tenancy/labels.py`).
    out = one_project_out(session, project)
    hosted_app = None
    if app is not None:
        session.refresh(app)
        hosted_app = _hosted_app_out(session, app, operator=_is_operator(principal))
    return ProjectCreateOut(
        **out.model_dump(), placement_warning=placement_warning, onboarding_task_id=onboarding_task_id,
        hosted_app=hosted_app,
    )


def _create_web_app_project(
    session: Session, body: ProjectCreate, intake: ProjectIntake, actor: AdminUser, principal: Principal | None,
    org_id: UUID,
) -> tuple[Project, Any]:
    """A `web_app` intake: the Easy Mode app and its project, uncommitted (`hosted_apps/service.py::create`), for every
    org, placed as `hosted_apps.create` decides. The template catalogue owns the repo-access fields, so of the rest of
    the body only the Agent instructions are kept."""
    if not edition_features.enabled("easy_mode_hosting"):
        raise HTTPException(status_code=503, detail=project_intake.REPO_NOT_SET_UP)
    from app.hosted_apps import service as hosted_apps

    app = hosted_apps.create(
        session, org_id=org_id, name=body.name.strip(), template=project_intake.WEB_APP_TEMPLATE, actor=actor,
        principal=principal,
        commit=False,
    )
    project = session.scalar(select(Project).where(Project.id == app.project_id))
    assert project is not None
    if body.agent_instructions:
        project.agent_instructions = body.agent_instructions
    return project, app


def _is_operator(principal: Principal | None) -> bool:
    """Whether a refusal may name the operator's own setup (`project_intake.refused_not_set_up`)."""
    return principal is not None and principal.is_operator


def _create_plain_repo_project(
    session: Session, body: ProjectCreate, actor: AdminUser, principal: Principal | None, org_id: UUID,
) -> Project:
    """An `other_app`/`business` intake: every refusal the row and its create would raise checked first (shape,
    authority, name, default agent, and the Projects cap read without its lock, so no org-wide lock
    waits on GitHub), then the private repo in the Easy Mode GitHub org (`project_intake.make_plain_repo`), then the
    row on it and its `jaah_repos` row — uncommitted. A create that still fails after GitHub made the repo is adopted
    by its retry."""
    name = body.name.strip()
    server_fields: dict[str, Any] = {
        "integration_branch": project_intake.PLAIN_INTEGRATION_BRANCH,
        "branch_prefix": project_intake.PLAIN_BRANCH_PREFIX,
    }
    sent = _without_repo_fields(
        body.model_dump(exclude_unset=True, exclude={*_CREATE_ONLY_FIELDS, *_DEFAULT_AGENT_FIELDS}), None,
    )
    _reject_governance_from_a_node_token(sent, principal)
    _require_authority_to_author_governance(session, sent, actor, principal, None)
    # The row's own shape checks, on what it will be given (any github.com URL stands in for the one to be made).
    _validated_fields(
        {**sent, **server_fields, "repo_url": "https://github.com/jaah/placeholder"}, creating=True,
    )
    default_agent = body.model_dump(exclude_unset=True, include=set(_DEFAULT_AGENT_FIELDS))
    if default_agent:
        _apply_default_agent(session, dict(default_agent), Project(org_id=org_id))
    _refuse_name_conflict(session, name, org_id)
    org_limits.require_headroom(session, org_id, "projects", lock=False)
    full_name, repo_id = project_intake.make_plain_repo(session, org_id, name, operator=_is_operator(principal))
    project = _create_project_row(session, body, principal, actor, org_id, server_fields={
        **server_fields, "repo_url": f"https://github.com/{full_name}",
    })
    jaah_repos.record(
        session, org_id=org_id, project_id=project.id, name=full_name, kind="plain",
        integration_branch=project_intake.PLAIN_INTEGRATION_BRANCH, actor=actor, repo_id=repo_id,
    )
    return project


def _hosted_app_address(app: Any) -> Optional[str]:
    """The web address of the Easy Mode app a `web_app` intake made (only `_create_web_app_project` makes one)."""
    if not edition_features.enabled("easy_mode_hosting"):
        return None
    from app.hosted_apps import service as hosted_apps

    return hosted_apps.address_or_none(app)


def _record_setup_answer(session: Session, project: Project, intake: ProjectIntake, app: Any) -> str:
    """The intake's answer prepended to the project's Agent instructions (`project_intake.setup_block`); returns the
    answer in words, for the onboarding task."""
    if intake.kind == "web_app":
        what = project_intake.what_it_is("web_app", address=_hosted_app_address(app))
        repository = project_intake.made_by_jaah(app.repo_full_name)
    elif intake.kind in project_intake.JAAH_MADE:
        row = jaah_repos.row_for(session, project.id)
        assert row is not None
        what, repository = project_intake.what_it_is(intake.kind), project_intake.made_by_jaah(row.name)
    else:
        what, repository = project_intake.what_it_is(intake.kind), project.repo_url or ""
    project.agent_instructions = project_intake.with_block(
        project_intake.setup_block(what, repository), project.agent_instructions,
    )
    return what


class GovernanceAuthorityOut(BaseModel):
    """The door's own answer, on the wire — what `has_authority_to_author_governance` said about this
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
@on_session
def project_governance_authority(
    session: Session,
    project_id: int,
    caller: AsyncCaller = Depends(current_caller),
) -> GovernanceAuthorityOut:
    """May this caller author this project's seven governance fields? The Edit form's Governance tab
    reads it; `_require_authority_to_author_governance` enforces the same answer on the PUT.

    The same read `update_project` makes, so a cross-org row is a 404 here as there."""
    project = project_or_404(session, project_id)
    return GovernanceAuthorityOut(
        may_author_governance=has_authority_to_author_governance(
            session, caller.actor, caller.principal, project
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
    #: The nodes whose pool clone cannot read the repository (`git_reach.clone_blocks`): a clone authenticates as
    #: its clone owner, not the caller, so the caller's own `ok` says nothing about it.
    clones: list["CloneReachOut"] = Field(default_factory=list)
    #: Set when the caller's own login cannot clone the repository (`git_reach.cannot_read`: no connection on its
    #: forge, or GitHub says the repository is invisible to them) — Start then offers to run without it.
    read_problem: Optional[str] = None
    read_fix_url: Optional[str] = None


class CloneReachOut(BaseModel):
    """One node whose clone of the project cannot read its repository: who it clones as, why, where GitHub says to
    fix it (`fix_url`), the Worktrees view whose slot drawer offers Change clone owner (`change_path`; None before
    the node has a slot of the project, when that view lists nothing), and Profile > Git when the owner is the
    caller and has no usable connection (`connect_path`)."""

    node: str
    node_id: str
    owner: Optional[str] = None
    owner_id: Optional[str] = None
    problem: str
    fix_url: Optional[str] = None
    change_path: Optional[str] = None
    connect_path: Optional[str] = None


ProjectGitReachOut.model_rebuild()


@router.get("/{project_id}/git-reach", response_model=ProjectGitReachOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@db_async.on_thread(actor="actor")
def project_git_reach(
    session: Session,
    project_id: int,
    actor: AdminUser = Depends(require_user),
) -> ProjectGitReachOut:
    """The project page's and Start's preflight: can the caller's own credentials push and open PRs here? Asked
    of the caller only — a task they start is served their credentials — and never cached past `git_reach`'s own
    answer cache."""
    # `git_reach.writer_for_user` interleaves its reads with GitHub calls and a token renewal (`git_access.token_for`
    # locks the row, commits, renews, re-reads), so the whole read runs on a worker thread, never on the loop.
    project = project_or_404(session, project_id)
    try:
        repo = git_access.project_repo(project, retry="then reload this page")
    except git_access.GitAccessError:
        return ProjectGitReachOut(state="unknown")
    if not project.has_repo or jaah_repos.runner_app_repo(session, project, repo) is not None:
        # A Jaah-made repository's runs and clones log in as the GitHub App: no person's connection decides them.
        return ProjectGitReachOut(state="unknown")
    clones = _clone_reach(session, project, repo, actor.id)
    cannot = git_reach.cannot_read(session, actor.id, repo, retry="then start the task again")
    read = {"read_problem": cannot[0] if cannot else None, "read_fix_url": cannot[1] if cannot else None}
    if repo.forge != "github":
        return ProjectGitReachOut(state="unknown", clones=clones, **read)
    try:
        pick = git_reach.writer_for_user(session, actor.id, repo)
    except git_access.GitAccessError:
        return ProjectGitReachOut(state="unknown", clones=clones, **read)
    if pick.problem:
        return ProjectGitReachOut(state="blocked", credential=pick.credential.name, problem=pick.problem,
                                  fix_url=pick.fix_url, clones=clones, **read)
    return ProjectGitReachOut(state="fallback" if pick.fallback else "ok", credential=pick.credential.name,
                              clones=clones, **read)


#: The console's Profile > Git tab (`apps/web/src/pages/GitAccountNewPage.tsx`'s `PROFILE_GIT`).
PROFILE_GIT_PATH = "/profile?tab=git-accounts"


def _clone_reach(session: Session, project: Project, repo: forges.RepoRef, caller_id: UUID) -> list[CloneReachOut]:
    """`git_reach.clone_blocks` in the page's words: node names and the clone owner's label."""
    blocks = git_reach.clone_blocks(session, project.id, repo)
    if not blocks:
        return []
    nodes = names_by_id(session, [block.node_id for block in blocks])
    people = {person.id: person for person in session.scalars(
        select(AdminUser).where(AdminUser.id.in_({block.owner_id for block in blocks if block.owner_id is not None}))
    )}
    out: list[CloneReachOut] = []
    for block in blocks:
        person = people.get(block.owner_id) if block.owner_id is not None else None
        out.append(CloneReachOut(
            node=nodes.get(block.node_id, str(block.node_id)), node_id=str(block.node_id),
            owner=(display_name(person) or person.email) if person is not None else None,
            owner_id=str(block.owner_id) if block.owner_id is not None else None,
            problem=block.problem, fix_url=block.fix_url,
            change_path=(f"/worktrees?{urlencode({'node': str(block.node_id), 'project': project.name})}"
                         if block.slotted else None),
            connect_path=PROFILE_GIT_PATH if block.connect and block.owner_id == caller_id else None,
        ))
    return out


@router.get("/governance-authority", response_model=GovernanceAuthorityOut)
@feature("projects", "create")
@on_session
def create_governance_authority(
    session: Session,
    caller: AsyncCaller = Depends(current_caller),
) -> GovernanceAuthorityOut:
    """The same question with no row — what the Create form asks, and what `_create_project_row` will
    answer with a 403 if it is `false`.

    `project=None` satisfies the org half by construction but not the grant half, so an org-admin with no
    node of their own gets `false`. `require_user`, like the POST it predicts."""
    return GovernanceAuthorityOut(
        may_author_governance=has_authority_to_author_governance(session, caller.actor, caller.principal, None)
    )


# Per-side cap on the `agent_instructions` before/after in the audit row: the column is unbounded and a
# row is written on every edit. Not `dispatch_compose.placeholders._PROJECT_INSTRUCTIONS_MAX_CHARS`, which bounds what
# a runner is shown.
_AUDIT_INSTRUCTIONS_MAX_CHARS = 2000


#: `_validated_run_jobs_as`'s two refusals, spelled once for the tests and the console.
RUN_JOBS_AS_ADMIN_ONLY = "Only an Org Admin or Owner can change Run jobs as."
RUN_JOBS_AS_UNUSABLE = "Run jobs as must be an enabled member of this project's organization."


def _validated_run_jobs_as(
    session: Session, updates: dict[str, Any], project: Project, principal: Principal | None
) -> None:
    """Gate a changed `run_jobs_as_id`: 403 unless the caller is an Org Admin or above (`Principal.is_org_admin`),
    422 unless it names a real, enabled member of the project's org. The agent route never reaches here with it:
    `persona_registry.PROJECT_SETTINGS_EXCLUDED_FIELDS` refuses the field first. A resent unchanged value passes (the
    form resends every key); an explicit null is a clear (the org default)."""
    if "run_jobs_as_id" not in updates or updates["run_jobs_as_id"] == project.run_jobs_as_id:
        updates.pop("run_jobs_as_id", None)
        return
    require_org_admin(principal, detail=RUN_JOBS_AS_ADMIN_ONLY)
    person = updates["run_jobs_as_id"]
    if person is not None and not git_access.usable_job_person(session, person, project.org_id):
        raise HTTPException(status_code=422, detail=RUN_JOBS_AS_UNUSABLE)


def _access_guard_state(session: Session, project: Project) -> tuple[Any, ...]:
    """What `sync_access_guard` installs for this project (`routes/commands.py::_resolve_command_args`): the
    project's Branch policy and every repository's. A change re-installs the guard."""
    return (
        tuple(getattr(project, key) for key in project_repos.POLICY),
        tuple(
            (row.id, row.integration_branch, *(getattr(row, key) for key in project_repos.POLICY))
            for row in project_repos.repos_of(session, project.id)
        ),
    )


def apply_project_update(
    session: Session,
    project: Project,
    sent: dict[str, Any],
    actor: AdminUser,
    principal: Principal | None,
    *,
    runner: bool = False,
) -> Project:
    """The whole project-row write: both authority checks, every field validation, the conflict
    refusals, the audit row, the commit, and the two post-commit node dispatches. `sent` is
    `model_dump(exclude_unset=True)` (an explicit null is a clear); `project` is already resolved.
    Returns it, refreshed.

    Shared by `PUT /api/projects/{project_id}` and the agent `PUT /api/agent/tasks/{task_id}/project`
    (`app/routes/agent_project_settings.py`, applied to the task's launcher; `runner=True` there, so a runner Jaah
    dispatched still sets up a Jaah-managed project, `_refuse_managed_fields`). A second implementation
    would become the way around the stricter door. The order below
    is load-bearing: no-principal 422 before the authority 403, and every before-read ahead of the
    setattr loop.
    """
    # Before the repository fields are dropped below: a system project refuses a repo change, never ignores it.
    _refuse_system_project_edit(project, sent)
    sent = {field: value for field, value in sent.items() if field != "system_kind"}
    # Not a column: the answer to the Jaah-repo question below (`jaah_repo_replace.require_choice`).
    jaah_repo_choice = sent.pop("jaah_repo", None)
    jaah_repo_confirm = sent.pop("jaah_repo_confirm", None)
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
    if not runner:
        # A runner Jaah dispatched sets up its project (gates, stack) whoever launched it; a member's save cannot.
        _refuse_managed_fields(session, project, sent, repo_specs, actor, principal)
    _refuse_jaah_repo_edit(session, project, sent, repo_specs)
    # Connecting the first repository or removing the last re-sizes every assignment's statics (declares them, or
    # retires them), so it is a placement change and takes the assignment gate (D13).
    if has_repo_after != project.has_repo:
        require_assignment_writer(actor, principal)
    has_repo_before = project.has_repo

    # Read before the setattr loop; the guard-sync dispatch turns on whether any repository's policy changed.
    guard_fields_before = _access_guard_state(session, project)
    # Same reason: the tmux fan-out at the bottom must dispatch nothing for a re-PUT of the same name.
    name_before = project.name
    # Same reason, for the audit: the write overwrites the prior prose, so this local is its only copy.
    agent_instructions_before = project.agent_instructions

    # `exclude_unset`: an explicit null is the edit (the form clears by PUTting one), so defaults do not
    # re-apply.
    updates = _validated_fields(sent, creating=False, current=project)
    _validated_routing(session, updates, project)
    # The repository columns are the primary row's projection, written only by `app/project_repos.py`.
    legacy_repo = {key: updates.pop(key) for key in _LEGACY_REPO_FIELDS if key in updates}
    # The project-level Branch policy writes the primary row (`project_repos.POLICY`); kept in `updates` too, so a
    # project with no repository keeps its own columns. A primary row naming its own value wins.
    primary_policy = {key: updates[key] for key in project_repos.POLICY if key in updates}
    if repo_specs is not None and primary_policy:
        repo_specs = [
            dataclasses.replace(spec, **{key: value for key, value in primary_policy.items()
                                         if getattr(spec, key) is None})
            if spec.is_primary else spec
            for spec in repo_specs
        ]
    if legacy_repo.get("repo_url") and any(
        project_repos.repo_keys(
            legacy_repo["repo_url"], legacy_repo.get("forge"), legacy_repo.get("forge_base_url"),
        )
        & project_repos.repo_keys(row.repo_url, row.forge, row.forge_base_url)
        for row in project_repos.repos_of(session, project.id) if not row.is_primary
    ):
        raise HTTPException(status_code=422, detail=[_repo_error(
            ["repo_url"], "This repository is already connected to this project", "duplicate_repo",
        )])
    _apply_default_agent(session, updates, project)

    # `repo_url` is only where the project is cloned FROM: paths and compose stacks are keyed on the
    # project id (`app/node_clone.py`) and each lease's refresh rewrites a slot clone's `origin` from the URL the
    # CP serves (`apps/jaahd/slot_clone.py`), so it is editable at any time — held slots included.
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

    _validated_run_jobs_as(session, updates, project, principal)
    _apply_production_qa_opt_in(session, updates, project, actor)
    _apply_merge_lane_opt_in(session, updates, project, actor)
    _apply_benchmark(session, updates, project)
    _outcome_settings_on_plan(session, updates, project, principal)
    # Last of the refusals, so a dialog's answer is never followed by another one: a save replacing the project's
    # Jaah-made repo needs `jaah_repo` (409 without it), and a move reads the old head now, before any write.
    replacement = jaah_repo_replace.detect(session, project, sent, repo_specs)
    jaah_repo_replace.require_choice(session, replacement, jaah_repo_choice, jaah_repo_confirm, principal, project.id)
    moving = replacement is not None and jaah_repo_choice == jaah_repo_replace.MOVE
    move_head = jaah_repo_replace.source_head(session, replacement) if moving and replacement else None

    for field_name, value in updates.items():
        setattr(project, field_name, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=_race_conflict_detail(exc, updates.get("name"))) from exc
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
    if repo_specs is None and primary_policy:
        project_repos.set_primary_policy(session, project, primary_policy, actor.id)
    # A first repo connect gives the project its default stages (a no-op once it has them).
    release_stages.materialise(session, project)
    # The one-release shim (architecture §5.2): `gate_full_cmd` sets the merge step's command gate, and the column
    # stays its mirror (`app/pipeline_gates/full_gate.py`).
    if "gate_full_cmd" in updates:
        full_gate.apply_command(session, project, updates["gate_full_cmd"], actor)
    if replacement is not None and jaah_repo_choice is not None:
        jaah_repo_replace.apply(session, project, replacement, jaah_repo_choice, move_head, actor)
    jaah_repo_replace.restore_if_primary_again(session, project, actor)

    # The role columns carry before/after (naming someone confers task-write via
    # `authz.editable_task_clause`), in `routes/tasks/update.py::update_task`'s shape, only for fields that moved.
    written = set(updates) | set(legacy_repo) | ({"repos"} if repo_specs is not None else set())
    data: dict[str, Any] = {"project_id": project.id, "fields": sorted(written)}
    # `agent_instructions` too: trusted directives every runner follows, overwritten by this write
    # (a create's payload is already logged by `UserActivityMiddleware`). Capped per side; change-tested,
    # so a resend records nothing. `test_activity_kinds` pins `data["agent_instructions_change"]` to a 12-line
    # window above the `record_write` below.
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
    record_write(
        session, actor, "project_updated", f"Updated project {project.name}", data=data,
    )
    session.commit()
    if moving:
        # Right after the commit, before any node dispatch, as the onboarding task is filed: a refused filing never
        # undoes the save (the retire cron then marks the move failed).
        jaah_repo_replace.file_move_task(session, project, actor, principal)
    if project.has_repo != has_repo_before:
        # A repository flip converges now, not at the next sweep: with no repo left, every git worktree of the
        # project is retired.
        slot_allocation.converge(session, project_id=project.id, actor=actor)
        session.commit()
    session.refresh(project)

    if _access_guard_state(session, project) != guard_fields_before:
        # These are markers in the clone's git config, not read back from this DB,
        # so a value change is inert until the guard is re-installed.

        # Fire-and-forget, post-commit: idempotent, and `check-access-guard.py` catches a lost dispatch.

        # Every node hosting the project (`slot_allocation.hosting_node_ids`): each holds its own trees, so
        # each re-installs its own guard. `sync_access_guard` may still fail on a node that has not cloned yet
        # (accepted, covered by the conformance checker); `dispatch_and_audit_sync` logs and swallows a
        # refusal, so an offline node costs only its own re-install.

        # The node derives the trees it re-guards from its own home and `project_id`; a node with no recorded home
        # is skipped, as the pool tick rosters it nothing.
        for hosting_id in sorted(slot_allocation.hosting_node_ids(session, project.id), key=str):
            node = session.get(Node, hosting_id)
            node_id = str(hosting_id)
            if node is None or not has_home(node.jaah_home):
                # Never silent: an unsynced tree pushes under its old markers until the node next clones.
                log.warning(
                    "sync_access_guard for project %s not dispatched — node %r has no home (%s)",
                    project.id, node_id, "no such node" if node is None else "the node has no jaah_home",
                )
                continue
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

    if project.name != name_before:
        _rederive_slot_names(session, project, name_before, actor)

    return project


@router.put("/{project_id}", response_model=ProjectOut)
@feature("projects", "edit", object_type="project", object_params=("project_id",))
@db_async.on_thread(actor=lambda caller, **_: caller.actor)
def update_project(
    session: Session,
    project_id: int,
    body: ProjectUpdate,
    # For both authority checks: `principal is None` is the node-token caller, and the org-admin door
    # needs the key, org and node grants off one resolution.
    caller: AsyncCaller = Depends(current_caller),
) -> ProjectOut:
    # `apply_project_update` dispatches to nodes between its commits and writes (`dispatch_and_audit_sync` blocks
    # on the bus round trip, and is audited in this session): the whole write runs on a worker thread.
    project = project_or_404(session, project_id)

    # The HTTP shell: every guard, the audit row and the dispatches are `apply_project_update`'s.
    project = apply_project_update(
        session, project, body.model_dump(exclude_unset=True), caller.actor, caller.principal
    )
    out = one_project_out(session, project)
    out.move_task_id = jaah_repo_replace.open_move_task_id(session, project.id)
    return out.model_copy(update=jobs_run_as_out(session, project))


def one_project_out(session: Session, project: Project) -> ProjectOut:
    """`_project_out` for one project, each of its inputs resolved for that project alone."""
    return _project_out(
        project, org=org_ref_of(org_refs(session, [project.org_id]), project.org_id),
        lane=merge_lane_state.lane_state(session, project), vm=microvm_page(session, [project]),
        repos=project_repos.repos_of(session, project.id), live_cast=live_cast_holders(session, [project.org_id]),
        production_qa=production_qa_gate.orgs_on(session, [project.org_id]),
        benchmark=offered_benchmark_budgets(session, [project]), job_people=job_people_of(session, [project]),
        jaah_managed=jaah_repos.managed_ids(session, [project.id]),
        full_gates={project.id: full_gate.full_gate_command(session, project)},
        port_vars=port_specs_by_project(session, [project.id]),
        live_urls=release_stages.live_urls(session, [project.id]),
    )


def offered_benchmark_budgets(session: Session, projects: Sequence[Project]) -> Mapping[int, Budget]:
    """`outcome_ledger/benchmark.py::offered_budgets`, cloud-only with benchmark mode (`packages/editions/features.toml`):
    the community edition offers it to no project."""
    if not edition_features.enabled("exec_dashboard"):
        return {}
    from app.outcome_ledger import benchmark

    return benchmark.offered_budgets(session, list(projects))


def _benchmark_out(project: Project, spend: Optional[Budget]) -> dict[str, Any]:
    """`ProjectOut`'s benchmark fields: not offered (no budget) reads the stored values and no spend."""
    return dict(
        benchmark_offered=spend is not None, benchmark_sample_rate=float(project.benchmark_sample_rate),
        benchmark_spent_usd=float(spend.spent_usd) if spend is not None else None,
        benchmark_cap_usd=float(spend.cap_usd) if spend is not None else None,
        benchmark_budget_reached=spend is not None and spend.reached,
    )


def _apply_benchmark(session: Session, updates: dict[str, Any], project: Project) -> None:
    """Benchmark mode's two fields in `updates`, in place: neither clears (422); the sample rate is 0..1; turning the
    opt-in ON is refused 409 while the org's switch is off — turning it off always works, and a resend is no change."""
    reject_cleared(updates, ("benchmark_opt_in", "benchmark_sample_rate"))
    if "benchmark_sample_rate" in updates:
        rate = updates["benchmark_sample_rate"]
        if not 0 <= rate <= 1:
            raise HTTPException(status_code=422, detail="Set the sample rate between 0% and 100%.")
        updates["benchmark_sample_rate"] = Decimal(str(round(rate, 4)))
    if updates.get("benchmark_opt_in") and not project.benchmark_opt_in:
        if not plan_terms.switch_on(session, project.org_id, plan_terms.BENCHMARK_MODE):
            raise HTTPException(status_code=409, detail=BENCHMARK_OFF_FOR_ORG)


def _outcome_settings_on_plan(
    session: Session, updates: dict[str, Any], project: Project, principal: Principal | None,
) -> None:
    """The outcome ledger's project settings (scan interval, security check patterns) change only while a tenant's
    plan has Agent performance (`plan_terms.require_tenant_switch`, 403); a resend of the stored value is no change.
    Production QA's settings, by contrast, save while its switch is off: only the opt-in acts
    (`_apply_production_qa_opt_in`)."""
    if any(name in updates and updates[name] != getattr(project, name)
           for name in ("outcome_scan_interval_minutes", "security_check_patterns")):
        plan_terms.require_tenant_switch(session, principal, plan_terms.AGENT_PERFORMANCE)


#: The 409 for opting a project in while its org's switch is off.
BENCHMARK_OFF_FOR_ORG = "Benchmark mode is off for this org."


def _validated_routing(session: Session, updates: dict[str, Any], project: Project) -> None:
    """Validate the intelligent routing fields in `updates` in place. A RAISED mode above what the org allows now
    (`modes.refusal`: its maximum, or none without the plan feature) is a 422 naming why; a resent one passes, so a
    project an org later demotes never wedges the form (the demotion already rewrote it)."""
    if "routing_mode" in updates:
        mode = updates["routing_mode"] or modes.INHERIT
        if mode not in ROUTING_MODES:
            raise HTTPException(status_code=422, detail="Set routing to inherit, recommend or auto.")
        # Lowering is always allowed. Raising reads the org row locked, so a maximum lowered concurrently
        # (`routes/orgs.py::_apply_routing`) is either seen here or demotes this row after.
        if modes.rank(mode) > modes.rank(project.routing_mode):
            org = session.get(Org, project.org_id, with_for_update=True)
            reason = modes.refusal(session, org, mode) if org is not None else modes.NOT_ON_PLAN
            if reason is not None:
                raise HTTPException(status_code=422, detail=reason)
        updates["routing_mode"] = mode
    if updates.get("routing_latency_cap_hours") is not None:
        hours = updates["routing_latency_cap_hours"]
        if isinstance(hours, bool) or not 0 < hours <= ROUTING_LATENCY_CAP_MAX_HOURS:
            raise HTTPException(
                status_code=422,
                detail=f"Set the latency cap between 0 and {ROUTING_LATENCY_CAP_MAX_HOURS} hours, or clear it.",
            )
        updates["routing_latency_cap_hours"] = Decimal(str(hours))
    for flag in ("blind_review", "blind_review_other_vendor"):
        if flag in updates and updates[flag] is None:
            raise HTTPException(status_code=422, detail=f"{flag} is on or off, never empty.")
    if updates.get("blind_review") and not project.blind_review:
        # Turning it on only: a project already on stays on, so a lost node never wedges the form.
        reason = _blind_review_refusal(microvm_page(session, [project]), project)
        if reason is not None:
            raise HTTPException(status_code=422, detail=reason)
    if "experiment_budget_share" in updates:
        share = updates["experiment_budget_share"]
        if share is None or not 0 <= share <= 1:
            raise HTTPException(status_code=422, detail="Set the experiment budget between 0% and 100%.")
        updates["experiment_budget_share"] = Decimal(str(round(share, 3)))


#: The longest latency cap a project may set: a week.
ROUTING_LATENCY_CAP_MAX_HOURS = 168


def _apply_production_qa_opt_in(
    session: Session, updates: dict[str, Any], project: Project, actor: AdminUser,
) -> None:
    """The opt-in in `updates`, in place: turning it on is refused 409 while the org's switch is off
    (`production_qa_gate.org_on`) and stamps the actor in `production_qa_enabled_by`; turning it off clears the stamp,
    and always works. A resend of the stored value is no change (the console resends nothing, an API client may)."""
    if "production_qa_enabled" not in updates or updates["production_qa_enabled"] == project.production_qa_enabled:
        updates.pop("production_qa_enabled", None)
        return
    if updates["production_qa_enabled"]:
        if not production_qa_gate.org_on(session, project.org_id):
            raise HTTPException(status_code=409, detail=production_qa_gate.OFF_FOR_ORG)
        updates["production_qa_enabled_by"] = actor.id
    else:
        updates["production_qa_enabled_by"] = None


#: The 409 for turning a project's merge lane on while the org's plan has none.
MERGE_LANE_NOT_ON_PLAN = "Merge lanes are not on your plan."


def _apply_merge_lane_opt_in(
    session: Session, updates: dict[str, Any], project: Project, actor: AdminUser,
) -> None:
    """The merge lane switch in `updates`, in place, shaped as `_apply_production_qa_opt_in`: turning it on is refused
    409 while the org is not offered the lane (`merge_lane_state.lane_state(...).offered`) and stamps the actor in
    `merge_lane_enabled_by`; turning it off clears the stamp and always works (rows in flight drain). A resend of the
    stored value is no change, and null is refused (422)."""
    reject_cleared(updates, ("merge_lane_enabled",))
    if "merge_lane_enabled" not in updates or updates["merge_lane_enabled"] == project.merge_lane_enabled:
        updates.pop("merge_lane_enabled", None)
        return
    if updates["merge_lane_enabled"]:
        if not merge_lane_state.lane_state(session, project).offered:
            raise HTTPException(status_code=409, detail=MERGE_LANE_NOT_ON_PLAN)
        updates["merge_lane_enabled_by"] = actor.id
    else:
        updates["merge_lane_enabled_by"] = None


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


def _rederive_slot_names(
    session: Session, project: Project, name_before: str, actor: AdminUser
) -> None:
    """After a rename: re-derive every declared slot's derived `name` durably, through the one MUTATED
    writer, attributed to the renaming operator. `name_before` identifies a derived `<old project>/<number>`
    label (`slot_title.stored_opinion`), which would otherwise pass for an operator's choice."""
    slots = session.execute(
        select(
            # The node uuid as text, as `slot_mutate.apply_mutation` addresses a slot.
            cast(Worktree.node_id, String).label("node_id"),
            Worktree.name.label("slot_name"), Worktree.number,
        )
        .where(Worktree.project_id == project.id)
    ).all()
    rederived = False
    for slot in slots:
        if (slot.slot_name or "").strip() == slot_title.default_name(name_before, slot.number):
            slot_mutate.apply_mutation(
                session, slot.node_id, project.id, slot.number,
                actor_id=actor.id, name=slot_title.default_name(project.name, slot.number),
            )
            rederived = True
    if rederived:
        session.commit()


class ProjectMemberOut(BaseModel):
    """One member the Start dialog may offer — identity and label, nothing else.

    Not `routes/users.py::UserOut`, which carries the superadmin admin record (`is_superadmin`,
    provenance, preferences, memberships)."""

    id: UUID
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    # A person is one row per org, so the same name and email can come back twice; the org's name tells them
    # apart. The name only: a viewer needs no other org's id or slug.
    org_name: Optional[str] = None


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def list_project_members(
    session: Session,
    project_id: int,
    may_start: bool = Query(False),
    # Driven in process with an explicit pair (`tests/test_projects_routes.py`), so the two dependencies stay.
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal_async),
) -> list[ProjectMemberOut]:
    """Every user who can reach this project: whom the console may offer as an assignee or a pipeline step's person.

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
        # A tenant gets the caller back, a non-disclosing arm: without it a tenant superadmin (allow-all, no
        # binding) saw empty dropdowns on operator-provisioned projects.
        reach.append(AdminUser.id == actor.id)
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
    orgs = org_refs(session, (row.org_id for row in rows))
    return [
        from_row(ProjectMemberOut, row, org_name=getattr(org_ref_of(orgs, row.org_id), "name", None))
        for row in rows
    ]


def _declared_slot_count(session: Session, project_id: int) -> int:
    """How many pool slots are declared for this project, fleet-wide (every node).

    Keyed on `projects.id`, never the name: this feeds three refusals and must not answer 0 for a renamed
    project."""
    return int(
        session.scalar(select(func.count()).select_from(Worktree).where(Worktree.project_id == project_id)) or 0
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
    # 410: the credential is configured but its GitHub authorization is gone; only its owner can reconnect.
    # 409, not 403, for any other refusal: the caller may ask; the state of the world conflicts.
    with (
        refused_as(409, git_access.GitAccessError, chain=True),
        refused_as(410, git_access.GitAccessNeedsReconnect, chain=True),
    ):
        # The docs browser is GitHub-only (`_docs_repo`), so the person's github.com connection.
        credential = git_access.resolve_for_user(
            session, actor.id, forges.parse_repo_url(f"https://github.com/{owner_repo}", "github"),
            # Not "start the task again" — meaningless on a docs page (`git_access._RETRY_TASK`).
            retry="then reload this page",
        )
        return git_access.token_for(session, credential)


@router.get("/{project_id}/docs", response_model=ProjectDocsOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
async def list_project_docs(
    project_id: int,
    db: DbDep,
    actor: AdminUser = Depends(require_user),
) -> ProjectDocsOut:
    """Every markdown document this project offers, read live from GitHub as the caller.

    Nothing is installed and MkDocs never runs: a committed `mkdocs.yml` is the opt-in, read only for
    `site_name` and `docs_dir`. The list is what the caller may see (`_docs_token`).
    """
    owner_repo, ref, project_name = await db.run_sync(lambda session: _docs_repo(project_id, session))
    # Read-only so far: the connection goes back before the token resolution and the GitHub reads.
    await db_async.release(db)
    try:
        # A worker thread: `git_access.token_for` may renew the token (row lock, commit, renewal, re-read).
        token = await db_async.run_blocking_in_session(_docs_token, actor, owner_repo, actor=actor)
        site_name, docs_dir = await run_in_threadpool(github_api.docs_config, owner_repo, ref, token=token)
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
    with refused_as(502, httpx.HTTPError, chain=True, prefix="GitHub unreachable: "):
        paths = await run_in_threadpool(github_api.docs_tree, owner_repo, ref, docs_dir, token=token)
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
async def read_project_doc(
    project_id: int,
    path: str,
    db: DbDep,
    actor: AdminUser = Depends(require_user),
) -> dict[str, str]:
    """One document's raw markdown, read as the caller (`_docs_token`).

    `path` must be a member of the list `list_project_docs` returns — an allowlist, so traversal and
    encoding tricks need no special handling.
    """
    owner_repo, ref, _name = await db.run_sync(lambda session: _docs_repo(project_id, session))
    # As `list_project_docs`: the connection goes back first, the token on a worker thread, GitHub off the loop.
    await db_async.release(db)
    try:
        token = await db_async.run_blocking_in_session(_docs_token, actor, owner_repo, actor=actor)
        content = await run_in_threadpool(_read_project_doc, owner_repo, ref, path, token)
    except github_api.RepoNotVisible as exc:
        # 404 like its sibling, with the reason true for this caller.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except github_api.DocsNotEnabled as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}") from exc
    content = or_404(content, detail="no such document")
    return {"path": path, "content": content, "ref": ref}


def _read_project_doc(owner_repo: str, ref: str, path: str, token: str) -> Optional[str]:
    """`read_project_doc`'s GitHub reads (no database): the allowlist check, then the document."""
    _, docs_dir = github_api.docs_config(owner_repo, ref, token=token)
    if path not in github_api.docs_tree(owner_repo, ref, docs_dir, token=token):
        raise HTTPException(status_code=404, detail="no such document")
    return github_api.docs_file(owner_repo, ref, path, token=token)


#: The administrator bar on `DELETE /api/projects/{id}` — `authz.org_admin_writer`'s. The route depends on the
#: `_async` dependency; `require_project_deleter` asks the same bar as a plain call (`require_assignment_writer`).
_PROJECT_DELETER_SUBJECT = "Project records"
require_project_deleter_async = org_admin_writer(_PROJECT_DELETER_SUBJECT)


def _require_slot_writer(actor: AdminUser, principal: Principal | None) -> AdminUser:
    """`routes/pool_slots.py::require_slot_writer`'s bar (an async dependency) as a plain call."""
    return org_admin_bar("Worktree slots", actor, principal)


def require_project_deleter(actor: AdminUser, principal: Principal | None) -> AdminUser:
    """`require_project_deleter_async`'s bar as a plain call: 403 unless the caller clears it."""
    return org_admin_bar(_PROJECT_DELETER_SUBJECT, actor, principal)

#: The two keys an assignment write needs beside the deleter bar (D13): placing a project on a node creates
#: and destroys worktrees, so it is both halves of the project's own lifecycle.
_ASSIGNMENT_KEYS = (permission_key("projects", "create"), permission_key("projects", "delete"))


def require_assignment_writer(actor: AdminUser, principal: Principal | None) -> None:
    """403 unless the caller may write an assignment or its static override (D13):
    `projects:create` AND `projects:delete`, plus the project-deleter bar (`require_project_deleter`). The one
    home for that gate — `apply_project_update` asks it."""
    require_project_deleter(actor, principal)
    missing = [key for key in _ASSIGNMENT_KEYS if principal is None or not principal.has(key)]
    if missing:
        raise HTTPException(status_code=403, detail=f"changing where a project runs needs {' and '.join(missing)}")


@router.delete("/{project_id}", status_code=204)
@feature("projects", "delete", object_type="project", object_params=("project_id",))
async def delete_project(
    project_id: int,
    db: DbDep,
    # Superadmin: the single-slot verbs are, and this is strictly more destructive
    # (`require_slot_writer`'s shape).
    actor: AdminUser = Depends(require_project_deleter_async),
    principal: Principal | None = Depends(current_principal_async),
    # An Easy Mode project's app address, typed by the person deleting it (D14): its app goes with it.
    confirm: Optional[str] = None,
) -> Response:
    """Delete the project, never refused for what it holds: every Task in it goes too
    (`_delete_project_tasks`), and the cascade takes the ingest-cache rows (`slot_intents`, `fetch_stamps`).
    Never touches the git repo or the markdown card file on disk (R2 edge case).

    An Easy Mode project's hosted app (its address, function, database and the rest, never its repo) is deleted
    with it, only when `confirm` is the app's address (`_easy_mode_app_to_delete`).

    A project with declared worktree slots is erased as `erase_project` erases it — every slot's directory
    on its node, then the row — behind the slot writer's own bar, which deleting each slot by hand needs
    anyway. Only a slot no node can address still refuses (409), as there."""
    name, has_slots = await db.run_sync(_delete_project, project_id, actor, principal, confirm)
    # Read-only so far: the connection goes back before the cascade or the worker thread.
    await db_async.release(db)
    if has_slots:
        return await _erase_slots_then_delete(db, project_id, name, actor)
    # A worker thread: every task delete commits, pushes its intents and dials the object store
    # (`_delete_project_tasks`).
    await db_async.run_blocking_in_session(_delete_project_in, project_id, name, actor, actor=actor)
    return Response(status_code=204)


def _delete_project(
    session: Session, project_id: int, actor: AdminUser, principal: Principal | None,
    confirm: Optional[str] = None,
) -> tuple[str, bool]:
    """`delete_project`'s refusals: `(name, whether it is erased first)` — true when any node holds a row of it."""
    project = project_or_404(session, project_id)
    _refuse_system_project_delete(project)
    _easy_mode_app_to_delete(session, project, confirm)
    held = session.scalar(select(Worktree.id).where(Worktree.project_id == project.id).limit(1)) is not None
    if held:
        _require_slot_writer(actor, principal)
        return project.name, True
    return project.name, False


def _easy_mode_app_to_delete(session: Session, project: Project, confirm: Optional[str]) -> Optional[Any]:
    """An Easy Mode project's app that is not deleted yet, once `confirm` names its address; 422 asking for the
    address otherwise. None for any other project. The community edition hosts no apps."""
    if not edition_features.enabled("easy_mode_hosting"):
        return None
    from app.hosted_apps import service as hosted_apps

    app = hosted_apps.app_of_project(session, project.id)
    if app is None or app.deleted_at is not None:
        return None
    if app.state == "provisioning":
        raise HTTPException(status_code=409, detail=hosted_apps.STILL_SETTING_UP)
    address = hosted_apps.require_address(app)
    if (confirm or "").strip().lower() != address:
        raise HTTPException(status_code=422, detail=f"Type the app's address, {address}, to delete it.")
    return app


def _start_easy_mode_app_delete(session: Session, project_id: int, actor: AdminUser) -> None:
    """The project's app to `deleting`, its delete workflow enqueued in the transaction that deletes the project."""
    if not edition_features.enabled("easy_mode_hosting"):
        return
    from app.hosted_apps import service as hosted_apps

    app = hosted_apps.app_of_project(session, project_id)
    if app is not None:
        hosted_apps.start_delete(session, app, actor)


def _delete_project_in(session: Session, project_id: int, name: str, actor: AdminUser) -> None:
    """`delete_project` for a project with no slots: its tasks, then the row, audited."""
    tasks_deleted = _delete_project_tasks(session, project_id, actor)
    _start_easy_mode_app_delete(session, project_id, actor)
    tracker_links.unlink_project(session, actor, project_id)
    session.delete(project_or_404(session, project_id))
    record_write(
        session, actor, "project_deleted", f"Deleted project {name}",
        data={"project_id": project_id, "name": name, "tasks_deleted": tasks_deleted},
    )
    session.commit()


async def _erase_slot_on_its_own_session(
    node_id: str, project_id: int, number: int, *, actor_email: str, actor_org: Optional[UUID]
) -> SlotDeleteOut:
    """One cascade leg, on a fresh `AsyncSession` (sessions cannot be shared across concurrent coroutines,
    and each leg holds a row lock across a node round trip).

    Returns the leg's outcome: `delete_one_slot` does not raise on an unreachable node, and this route
    must name orphaned directories. No `force` argument — Delete always forces."""
    async with db_async.async_session() as session:
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
    db: DbDep,
    actor: AdminUser = Depends(require_superadmin),
) -> Response:
    """This deletes the project itself, not merely the slots it declares. Removes every worktree row,
    asks every node to delete its directory, then deletes the `projects` row; getting
    it back means re-creating it under a new id. 204 on success. To keep the project, delete each slot
    (`POST /api/worktrees/{project}/{number}/erase`) instead.

    Cannot be refused on occupancy or liveness: a hold buys a teardown, an unreachable node a report
    (`app/slot_teardown.py`, `pool_slots.py::delete_one_slot`). The final delete cascades away the legs'
    `release_slot` intents — harmless only because the kill rides the awaited erase
    (`slot_teardown.end_holder`).

    Accepted outcome: directories may survive on unreachable nodes. Each is named in a WARNING, in the
    `project_erased` audit payload (`orphaned_directories`), and in a refusal's 409 — the log line is
    emitted before any refusal can return, since those rows are already committed gone.

    Every slot is erased at once. A leg raises only for an unaddressable slot; that refuses with a 409, keeps the
    row, and a retry resumes (the node handler is idempotent). Every Task in the project goes too
    (`_delete_project_tasks`)."""
    name = await db.run_sync(_erase_project, project_id)
    await db_async.release(db)
    return await _erase_slots_then_delete(db, project_id, name, actor)


def _erase_project(session: Session, project_id: int) -> str:
    """`erase_project`'s refusals: the project's name."""
    project = project_or_404(session, project_id)
    _refuse_system_project_delete(project)
    return project.name


def _unexpected_leg_error(error: BaseException, actor: AdminUser) -> str:
    """A leg's non-HTTP failure as the 409 names it: verbatim for the operator, with host paths withheld for a
    tenant (`app/command_read.py`), whose `DELETE` reaches this cascade too."""
    return withhold_host_paths(str(error)) if is_tenant_org(actor.org_id) else str(error)


async def _erase_slots_then_delete(db: AsyncSession, project_id: int, name: str, actor: AdminUser) -> Response:
    """`erase_project`'s work, shared with `delete_project` for a project any node holds a row of: erase every
    slot, then delete its tasks and the row. 409 as `erase_project` documents."""
    rows = await db.run_sync(_erase_slot_rows, project_id)
    # Read-only: the connection goes back before the legs' node round trips.
    await db_async.release(db)
    slots = [(node_id, number) for node_id, number, _name in rows]
    node_names = {node_id: node_name for node_id, _number, node_name in rows}

    async def _erase_all(
        rows: list[tuple[str, int]],
    ) -> tuple[list[tuple[str, int, object]], list[tuple[str, int, str]]]:
        """Every leg, all at once. Returns (legs that raised, directories left orphaned).

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
            for (node_id, number), outcome in zip(rows, outcomes, strict=False)
            if isinstance(outcome, BaseException)
        ]
        orphaned = [
            # The node's own words when given; the fallback only keeps a report from being blank.
            (node_id, number, outcome.node_detail or "the node did not say why")
            for (node_id, number), outcome in zip(rows, outcomes, strict=False)
            if isinstance(outcome, SlotDeleteOut) and not outcome.node_erased
        ]
        return raised, orphaned

    failures, orphaned = await _erase_all(slots)
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

    # A worker thread: every task delete commits, pushes its intents and dials the object store
    # (`_delete_project_tasks`).
    await db_async.run_blocking_in_session(
        _erase_slots_then_delete_in, project_id, name, slots, node_names, orphaned, orphan_sentence, actor,
        actor=actor,
    )
    return Response(status_code=204)


def _erase_slot_rows(session: Session, project_id: int) -> list[tuple[str, int, str]]:
    """Every slot the project declares, as `(node uuid text, number, node name)`."""
    return [tuple(row) for row in session.execute(
        # The node uuid as text, which every leg reads (a name is not an identity); the name only for the
        # sentences and the audit.
        select(cast(Worktree.node_id, String), Worktree.number, Node.name)
        .select_from(Worktree)
        .join(Node, Node.id == Worktree.node_id)
        .where(Worktree.project_id == project_id)
        .order_by(Node.name, Worktree.number)
    ).all()]


def _erase_slots_then_delete_in(
    session: Session, project_id: int, name: str, slots: list[tuple[str, int]], node_names: dict[str, str],
    orphaned: list[tuple[str, int, str]], orphan_sentence: str, actor: AdminUser,
) -> None:
    """`_erase_slots_then_delete` past its legs: the re-count, the tasks, then the row, audited."""
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

    tasks_deleted = _delete_project_tasks(session, project_id, actor)
    _start_easy_mode_app_delete(session, project_id, actor)
    tracker_links.unlink_project(session, actor, project_id)
    session.delete(project_or_404(session, project_id))
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
