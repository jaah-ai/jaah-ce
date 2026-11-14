"""Who may deploy to a stage, and who may approve a release. No new access key.

`may_deploy`: a stage with no `deployers` list takes the default — holders of `projects:edit` on the project
(Project Maintainer, Org Owner/Admin). A stage with one admits exactly its roles (a binding of that role on the project
or org-wide) and its users.

`may_approve_step` (R1): a step whose `approvers` are set admits exactly those roles and users — being allowed to press
grants no approval right then, and being an approver grants no right to press; with none set, approvers are who may
press (`may_deploy`). `may_approve` is that, on the release's snapshot rule, refusing the requester only while the
step's `requester_cant_approve` is on (R2). A refusal is a 403 with no prose; the API's `can_*` flags let the UI omit the action instead, judged as `as_writer` (the principal the write
runs as), so a flag never offers what the write refuses.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from app.access_catalog import SCOPE_ORG, SCOPE_PROJECT
from app.authz.catalog import permission_key
from app.authz.principal import Principal, owner_seat
from app.authz.visibility import has_project_key
from app.models.releases import Release, ReleaseStage
from app.pipeline.rules import StepRules

PROJECTS_EDIT = permission_key("projects", "edit")


def as_writer(principal: Optional[Principal], org_id: Optional[UUID]) -> Optional[Principal]:
    """Who a write to a project of `org_id` runs as: an unacting operator writes another org's rows only acting as it
    (`app/act_as.py`, the console's `lib/actAs.ts::useActAsRows`), so as that org's Owner seat; anyone else as
    themselves. A read's `can_*` flags ask this, so they answer as the write will."""
    if principal is None or org_id is None or not principal.is_operator or str(principal.org_id) == str(org_id):
        return principal
    return owner_seat(principal, org_id)


def deployer_roles(stage: ReleaseStage) -> list[str]:
    return [str(r) for r in (stage.deployers or {}).get("roles") or []]


def deployer_users(stage: ReleaseStage) -> list[str]:
    return [str(u) for u in (stage.deployers or {}).get("users") or []]


def _holds_role(principal: Principal, roles: list[str], project_id: int) -> bool:
    """An org role the principal holds, or a live binding of a listed role on the project or org-wide."""
    if principal.org_role is not None and principal.org_role in roles:
        return True
    return any(
        binding.role_slug in roles and (
            binding.scope_type == SCOPE_ORG or (binding.scope_type == SCOPE_PROJECT and binding.scope_id == project_id)
        )
        for binding in getattr(principal, "bindings", ()) or ()
    )


def may_deploy(principal: Optional[Principal], project_id: int, stage: ReleaseStage, *,
               default_key: str = PROJECTS_EDIT) -> bool:
    """May `principal` take the step into `stage` (a release to it, by default)? `default_key` is the key the
    step's default `who` holds (`app/releases/pipeline.py::default_who`): `projects:edit` for a release."""
    if principal is None:
        return False
    if stage.deployers is None:
        return has_project_key(principal, default_key, project_id)
    return _admits(principal, project_id, {"roles": deployer_roles(stage), "users": deployer_users(stage)}, default_key)


def _admits(principal: Principal, project_id: int, who: dict[str, list[str]], default_key: str) -> bool:
    """Whether `principal` is one of `who`'s users or holds one of its roles (`may_deploy`'s rule for a set list)."""
    if principal.is_superadmin:
        return True
    # An API key is narrowed to the keys it was minted with: it acts here only with that key on the project.
    if principal.api_key_id is not None and not has_project_key(principal, default_key, project_id):
        return False
    return str(principal.user_id) in who["users"] or _holds_role(principal, who["roles"], project_id)


def may_approve_step(principal: Optional[Principal], project_id: int, stage: ReleaseStage, *,
                     default_key: str = PROJECTS_EDIT) -> bool:
    """May `principal` approve the step into `stage` (R1)? Its `approvers` when set, else who may press it. Says
    nothing about the requester (`may_approve`, `gate.may_approve`)."""
    if principal is None:
        return False
    approvers = StepRules.of(stage).approvers
    if approvers is None:
        return may_deploy(principal, project_id, stage, default_key=default_key)
    return _admits(principal, project_id, approvers, default_key)


def may_approve(principal: Optional[Principal], project_id: int, stage: ReleaseStage, release: Release) -> bool:
    """May `principal` approve `release` (`stage`: its snapshot rule)? An approver of the step, never its requester
    while `requester_cant_approve` is on (R2)."""
    if principal is None:
        return False
    if StepRules.of(stage).requester_cant_approve and _same_user(principal.user_id, release.requested_by_id):
        return False
    return may_approve_step(principal, project_id, stage)


def may_cancel(principal: Optional[Principal], project_id: int, stage: ReleaseStage, release: Release) -> bool:
    """The requester or anyone who may deploy to the target."""
    if principal is None:
        return False
    return _same_user(principal.user_id, release.requested_by_id) or may_deploy(principal, project_id, stage)


def _same_user(a: Any, b: Optional[UUID]) -> bool:
    return b is not None and str(a) == str(b)
