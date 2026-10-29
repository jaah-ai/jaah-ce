"""Session lifecycle endpoints."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal, Optional, Union
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import (
    access,
    act_as,
    api_keys,
    ce_caps,
    console_direct,
    edition_features,
    git_connections,
    node_bus,
    nodevault_policy,
    org_gate,
    org_quotas,
    placement_grants,
    plan_terms,
    provider_auth_policy,
    tenant_defaults,
)
from app.operator_settings import defaults as settings_defaults, reads as settings_reads
from app import (
    hipaa, identities, ip_allowlist, login_funnel, login_lockout, login_oauth, mfa, org_invitations, org_locate,
    static_slots_policy,
)
from app.access_catalog import PROJECT_LEVEL_FEATURES, PROJECT_ROLE_ORG_KEYS
from app.auth import (
    COOKIE_NAME,
    clear_session_cookie,
    google_client_id,
    PASSWORD_PROBLEM_MESSAGES,
    hash_password,
    password_problem,
    password_refusal,
    read_session,
    require_user,
    require_user_or_signed_out,
    verify_password,
    verify_session_token,
)
from app.authz.catalog import (
    _FEATURE_LABELS, OWNER_KEYS, PERMISSION_CATALOG, UNHONOURED_PERMISSION_KEYS, Tenancy, permission_key,
)
from app.authz.declarations import always, session_only
from app.authz.principal import effective_access, principal_for_user
from app.authz.visibility import git_identity_select
from app.credentials.registry import type_label
from app.db import get_session
from app.entity_shape import conflict_as_409, constraint_name
from app.git_access import GIT_CREDENTIAL_TYPES, credential_root
from app.login_oauth import ORG_DELETED, ORG_NOT_APPROVED, ORG_PENDING_REVIEW
from app.models.access import Group, UserGroupMembership
from app.models.credentials import Credential
from app.models.identity import AdminUser, UserIdentity, set_identity_fields
from app.models.orgs import ORG_PENDING, ORG_REJECTED, Org
from app.models.projects import Project
from app.notify_targets import available_notification_methods
from app.preferences import (
    UserPreferences,
    hold_unsubscribable_events,
    preferences_document,
    store_preferences,
    stored_preferences,
    subscribable_events,
    validate_notifications_preferences,
)
from app.routes.public import check_rate_limit
from app.tenancy import seam
from app.tenancy.copy import not_available
from app.tenancy.ids import is_operator_plane, is_tenant_org
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
from app.user_activity import record_write
from app.version import TOOL_VERSION

# The community edition has no Slack: the Connect routes refuse before this name is read.
if edition_features.enabled("slack"):
    from app import slack_link
# The community edition has no Telegram: these names are read only on `telegram_router`, which only
# `app/routers_cloud.py` includes.
if edition_features.enabled("telegram"):
    from app import telegram_bot, telegram_link

router = APIRouter(prefix="/api/auth", tags=["auth"])

# A second, prefix-less router: the permission catalog is served at the literal `/api/permissions`,
# which `router`'s `/api/auth` prefix would move — silently emptying the admin permission matrix.
# Registered separately in `app/main.py`.
catalog_router = APIRouter(tags=["auth"])
# The Telegram link (cloud-only, included by `app/routers_cloud.py`), same `/api/auth` prefix.
telegram_router = APIRouter(prefix="/api/auth", tags=["auth"])

log = logging.getLogger("cockpit-auth")


class LoginRequest(BaseModel):
    email: str
    password: str
    # The step-up code, demanded once the identifier is under the lockout's step-up (`app/login_lockout.py`).
    totp: Optional[str] = None


class GoogleLoginRequest(BaseModel):
    credential: str  # the Google ID-token JWT from the GSI button callback
    # An org switch's re-authentication (`POST /api/auth/switch` answered `door: google`): sign in as the
    # person's membership in this org rather than `pick_membership`'s choice (`login_funnel.switch_target`).
    switch_to: Optional[UUID] = None


class MembershipOut(BaseModel):
    id: int
    group: str
    resource_type: str | None = None
    resource_id: str | None = None
    is_everyone: bool = False
    """The org's Everyone group (`groups.is_everyone`): the console labels it and offers no revoke (the server 409s)."""


class OrgMembershipOut(BaseModel):
    """One org the signed-in PERSON is a member of (`AdminOut.memberships`) — the org switcher's row.

    `state`: `active` (signs in and acts there), `pending_approval` (the org awaits operator review), or
    `disabled` (the membership is disabled, or the org is rejected or switched off). `is_current` is the
    membership this session acts as; `is_operator_plane` whether the org is the operator's
    (`app/tenancy/ids.py::is_tenant_org`); `compliance_enabled` `AdminOut.compliance_enabled` for that org, so a
    page the current org lacks can name the org that has it; `staging_of` the parent's name when the org is a
    staging org (`app/staging_org.py`), None otherwise."""

    org_id: UUID
    org_slug: str
    org_name: str
    state: Literal["active", "pending_approval", "disabled"]
    is_current: bool
    is_operator_plane: bool
    compliance_enabled: bool = False
    staging_of: Optional[str] = None


class PendingInvitationOut(BaseModel):
    """An invitation to join another org, awaiting this person's answer (`AdminOut.pending_invitations`)."""

    id: UUID
    org_name: str
    invited_by_name: str
    created_at: datetime


def _compliance_enabled(session: Session, org_id: Optional[UUID]) -> bool:
    """Is Portfolio Compliance on for `org_id` (`app/governance/availability.py`)? False with no org, and in an
    edition without compliance."""
    if not edition_features.enabled("compliance"):
        return False
    if org_id is None:
        return False
    from app.governance import availability

    return availability.enabled(session, org_id)


def _guard_enabled(session: Session, org_id: Optional[UUID]) -> bool:
    """Is the Architectural Guard available for `org_id` (`availability.guard_enabled`)? False with no org, and in
    an edition without compliance."""
    if not edition_features.enabled("compliance"):
        return False
    if org_id is None:
        return False
    from app.governance import availability

    return availability.guard_enabled(session, org_id)


def _membership_state(row: AdminUser, org: Org) -> str:
    """`OrgMembershipOut.state` for one membership row and its org."""
    if row.disabled_at is not None or not org.enabled or org.lifecycle == ORG_REJECTED:
        return "disabled"
    if org.lifecycle == ORG_PENDING:
        return "pending_approval"
    return "active"


def _org_memberships(session: Session, user: AdminUser) -> list[OrgMembershipOut]:
    """Every membership of the caller's OWN person (their identity, never anyone else's), the current one
    first, then by org name. A membership whose org row is missing is left out (no label to show), and so is
    one still waiting on an invitation (`AdminUser.invitation_id`): that org is `pending_invitations`' until
    the person accepts."""
    if user.identity_id is None:
        return []
    rows = [
        row for row in identities.memberships(session, user.identity_id)
        if not row.is_system and row.invitation_id is None
    ]
    # A soft-deleted org is no membership to show or switch into.
    orgs = {org.id: org for org in session.scalars(
        select(Org).where(Org.id.in_({row.org_id for row in rows}), Org.deleted_at.is_(None))
    )}
    parent_ids = {org.parent_org_id for org in orgs.values() if org.parent_org_id is not None}
    parents = dict(session.execute(select(Org.id, Org.name).where(Org.id.in_(parent_ids))).tuples().all()) if parent_ids else {}
    out = [
        OrgMembershipOut(
            org_id=org.id, org_slug=org.slug, org_name=org.name, state=_membership_state(row, org),
            is_current=row.id == user.id, is_operator_plane=not is_tenant_org(org.id),
            compliance_enabled=_compliance_enabled(session, org.id),
            staging_of=parents.get(org.parent_org_id) if org.parent_org_id is not None else None,
        )
        for row in rows
        if (org := orgs.get(row.org_id)) is not None
    ]
    return sorted(out, key=lambda item: (not item.is_current, item.org_name.casefold(), str(item.org_id)))


class TaskCreateDefaultsOut(BaseModel):
    """What an untouched `/tasks` create surface starts at — the three `Tasks > Defaults` knobs, resolved
    (`operator_settings.task_create_defaults`, which owns the why).

    Rides on the identity payload because every authenticated principal gets it before render;
    `GET /api/settings` is `system:view`, which a task-creating user need not hold. Not a user
    preference: this is the operator's choice for everyone."""

    autonomous: bool
    complex: bool
    persona: str
    # `project_memory_default`, for the editor's "Default (On)" label only — never a prefill (see the resolver).
    project_memory: bool


class ProjectCreateDefaultsOut(BaseModel):
    """The gate and stack commands a new project starts with — the four `Projects > Defaults` knobs,
    resolved (`operator_settings.project_create_defaults`). Rides here for `TaskCreateDefaultsOut`'s
    reason: a project creator need not hold `system:view`. Blank means no command."""

    gate_quick_cmd: str
    gate_full_cmd: str
    stack_up_cmd: str
    stack_down_cmd: str


class MeOrgRef(OrgRef):
    """`OrgRef` plus what the viewer's OWN org's lifecycle surfaces read (the pending banner, the setup
    card, the Nodes page) — on `/api/auth/me` only, never on a row's label. `lifecycle` is
    `app/models/orgs.py::Org.lifecycle`'s value. Mirrored in `apps/web/src/types.ts::MeOrgRef`."""

    lifecycle: str
    # The org's plan end (None = never) and whether it has passed — the shell's ended banner — and a priced plan
    # chosen at sign-up awaiting checkout (`app/org_plan_admin.py::summary_data`).
    plan_ends_at: Optional[str] = None
    plan_ended: bool = False
    pending_plan_key: Optional[str] = None
    pending_plan_name: Optional[str] = None


def _me_org_ref(session: Session, org_id: Optional[UUID]) -> Optional[MeOrgRef]:
    """The viewer's org label with its lifecycle and plan; None exactly when `org_ref_of` is."""
    ref = org_ref_of(org_refs(session, [org_id]), org_id)
    org = session.get(Org, org_id) if ref is not None else None
    if ref is None or org is None:
        return None
    plan: dict[str, Any] = {}
    # The community edition has no plans: no end, nothing awaiting checkout.
    if edition_features.enabled("billing"):
        from app import org_plan_admin

        plan = org_plan_admin.summary_data(session, org) if is_tenant_org(org.id) else {}
    return MeOrgRef(**ref.model_dump(), lifecycle=org.lifecycle, **plan)


class HipaaSurfaceOut(BaseModel):
    """One non-BAA surface (a `hipaa.SURFACES` key) and whether an org admin acknowledged its warning."""

    surface: str
    acknowledged: bool


class MeApiKeyOut(BaseModel):
    id: UUID
    name: str
    #: `jaah_…<last4>`: never the secret.
    display: str
    expires_at: Optional[datetime] = None


class AdminOut(BaseModel):
    # No label field: a person's label derives from `first_name` + `last_name`, else `email`
    # (`identities.person_label`), so the client composes it from the pair below.
    id: UUID
    email: str
    # Whether a Google, Microsoft or GitHub sign-in has proven `email` (`AdminUser.email_verified_at`):
    # email notifications go only to a proven address.
    email_verified: bool = False
    role: str = "admin"
    first_name: str | None = None
    last_name: str | None = None
    preferences: dict = Field(default_factory=dict)
    #: The events this user may see and set in Profile > Alerts (`preferences.subscribable_events`).
    notification_events: list[str] = Field(default_factory=list)
    notification_methods_available: list[str] = Field(default_factory=list)
    """WHICH NOTIFICATION METHOD TYPES THIS DEPLOYMENT CAN ACTUALLY DELIVER RIGHT NOW — the subset of
    `preferences.NOTIFICATION_METHOD_TYPES` whose channel is configured.

    A CAPABILITY OF THE DEPLOYMENT, NOT OF THIS USER, and it rides the identity payload for
    `git_credential_id`'s stated reason: the one response every authenticated principal already gets, so
    the shared Notifications panel can tell a user "Slack delivery is not configured — ask an operator"
    WITHOUT `system:view`, which is what reading the operator-settings catalogue would require. Since
    3176 the `slack` credential is designated once for the whole deployment (Settings), so
    without this field the panel's only honest option would be to accept a member id and silently never
    deliver to it — a user staring at a saved setting that does nothing.

    CARRIES NOTHING OF THE CREDENTIAL — not its id, not its name, not its type label. A method type is
    listed when a usable row is designated and absent when one is not, which is the whole of what the
    panel needs and the least this can disclose to a zero-grant account."""
    git_credential_id: UUID | None = None
    """WHICH CREDENTIAL THIS ACCOUNT'S RUNNERS PUSH WITH — their Git Access selection, or
    null for "has connected nothing", which is a real and common state rather than a fault.

    A POINTER, NOT A CAPABILITY, and it rides the identity payload for the same reason
    `task_create_defaults` does: the one response every authenticated principal already gets, so the
    self-service Git Access control on /profile can render its current answer without a second route
    and without `credentials:view`. It names a `credentials.id` and carries NOTHING of the row —
    `app/git_access.py` re-authorizes the pointer on every single resolution, so what a browser reads
    here is a preference, never a grant, and a stale one costs a re-render rather than an access."""
    org: MeOrgRef | None = None
    """WHICH ORG THE SIGNED-IN OPERATOR IS — the viewer's own `admin_users.org_id` (`AdminUser` is
    `OrgScoped`), resolved to the one label shape every tenant-owned surface renders
    (`app/tenancy/labels.py::OrgRef`: the short `slug` the indicator draws, the `name` it says).

    IT RIDES THE IDENTITY PAYLOAD FOR `git_credential_id`'s REASON, one field up: the ONE response every
    authenticated principal already gets. Four of the ten tenant-owned console pages have no record of
    their own to point at — `/projects/new`, `/tasks/new` and `/tasks/new-chat` have created nothing yet,
    and `/clients` is a placeholder with no rows — so their org indicator has no row to read a label off.
    Off this field they can name the tenant with no second route and no permission key; the alternative
    is fetching the org roster, which is a capability a task-creating BA has no business holding in order
    to draw a badge about themselves.

    IT IS THE VIEWER'S OWN ORG AND IS STILL NOT A PROMISE ABOUT WHERE A NEW ROW WILL LAND, THOUGH THE TWO
    USUALLY COINCIDE. A cookie-bearing request binds the PRINCIPAL'S OWN org — `authz._bind_read_scope`
    ends with `if org_id is not None: seam.bind(org_id)` over the `principal.org_id` `_enforce` resolved,
    on an `OPERATOR`-declared route as much as an org-scoped one — and the write seam stamps whatever
    scope the request bound. So a viewer who HAS an org really does get their new row stamped with it.
    That agreement is the WRITE PATH'S to keep and not this field's to guarantee, and it parts on the case
    a create form meets most: a row that DERIVES its org from an unstamped parent is left NULL under a
    bound org (`write_seam.stamp_org`'s derived-unstamped arm), which is the ordinary shape of a subtask
    under a legacy task. So a surface announces this as WHOSE SESSION THIS IS and never as the new row's
    tenant — the reading a reader reaches for first on a create form is the one the write path does not
    owe them.

    NULL IS A REAL STATE, NOT A LOADING ONE. `admin_users.org_id` is NOT NULL like every other tenant-owned
    table (`app/models/base.py::OrgScoped`), so NULL here is a viewer whose `org_id` names no `orgs` row — there
    is still no foreign key to `orgs(id)` — so the label cannot be RESOLVED even though the row is stamped.
    It renders as its own "no org" state and NEVER as the default org: substituting one would put a
    confidently wrong tenant on the very payload that says who the viewer is, which is the failure
    `org_refs` refuses at the source. `app/tenancy/labels.py` is the single home for that rule."""
    version: str = TOOL_VERSION
    is_superadmin: bool = False
    is_operator: bool = False
    """`is_superadmin` AND a member of the operator plane (`app/tenancy/ids.py::is_operator_plane`).

    THE FLAG THE BROWSER READS FOR OPERATOR-ONLY SURFACES (`App.tsx::RequireSuperadmin`, the rail's
    `superadminOnly` rows, the operator-only sections). `is_superadmin` alone would offer a superadmin
    created inside a TENANT org fleet pages the API refuses them, because the per-user flag carries no
    org. This does; the server computes it so
    the browser never has to know which orgs are the operator plane. `is_superadmin` stays beside it
    unchanged: it is still the allow-all that `useCan` answers from, inside whatever the seam binds."""
    tenant: bool = False
    """The viewer's org is a tenant's, not the operator plane (`app/tenancy/ids.py::is_tenant_org`) — the
    same test the server applies to a non-superadmin's host-field and member-list reads, so the browser
    draws exactly what those routes serve."""
    memberships: list[OrgMembershipOut] = Field(default_factory=list)
    """THE ORGS THIS PERSON IS A MEMBER OF (`OrgMembershipOut`) — what the org switcher lists. `org`,
    `is_operator` and `org_role` keep meaning the membership this session acts as (`is_current`)."""
    pending_invitations: list[PendingInvitationOut] = Field(default_factory=list)
    """Invitations to another org awaiting this person's answer (`app/org_invitations.py`): accepted or
    declined at `POST /api/auth/invitations/{id}/accept|decline`. The org is not in `memberships` until then."""
    group_memberships: list[MembershipOut] = Field(default_factory=list)
    """This membership's group memberships (was `memberships` before the org list took the name)."""
    permissions: list[str] = Field(default_factory=list)
    project_permissions: dict[str, list[str]] = Field(default_factory=dict)
    """WHICH KEYS THIS USER HOLDS ON WHICH PROJECT — `{"<project id>": ["worktrees:edit", ...]}`, the
    same key vocabulary as `permissions` above, from the user's role bindings (`app/access.py::expand`).

    IT IS THE FLAT LIST'S COMPANION, NOT ITS REPLACEMENT. `permissions` stays what a route-level key
    check means; this is what a control that then names a PROJECT means. A browser gating a per-project
    affordance on the flat list offers a control the API is about to refuse.

    TWO SOURCES, one answer per project: the project-scope triples a project binding confers, and the
    ORG-WIDE keys an org binding confers, projected onto every project of the caller's org
    (`_scope_maps` says why).

    KEYED BY PROJECT ID, rendered decimal because JSON object keys are strings. The id and not the
    name: names are mutable and a rename would silently empty a browser-side map.

    NOT CONSULTED FOR A SUPERADMIN, who is allow-all off the flag; empty for a user holding no binding,
    which is deny-by-default and never "all".

    IT ANSWERS TASKS TOO: task access is decided on the task's project, so a control that
    gates a task affordance reads this same map."""
    org_role: str | None = None
    """`"owner"`, `"admin"`, or null — the org role an org binding confers (`app/access.py::ORG_OWNER`/
    `ORG_ADMIN`), the same answer the tenant-admin gate (`authz.Access.ORG_ADMIN`) reads."""
    # Not identity, and unfenced by any permission (see `TaskCreateDefaultsOut`); on every response this
    # model shapes, since the browser seeds the form from whichever landed last.
    task_create_defaults: TaskCreateDefaultsOut
    # The same, for the new-project form (`ProjectCreateDefaultsOut`).
    project_create_defaults: ProjectCreateDefaultsOut
    timezone: str
    """THE CONTROL PLANE'S CONFIGURED ZONE — `Settings > General` — as an IANA name
    that is ALREADY RESOLVED: what the server will actually evaluate in, guaranteed loadable, so the
    browser hands it straight to `Intl` with no fallback of its own to keep in step. Not the raw knob,
    because a stored name this host cannot resolve degrades server-side to UTC, and shipping the raw
    string would have the board cut its days in a zone nothing else in the fleet is using.

    IT RIDES HERE FOR `task_create_defaults`' REASON, one field up: the settings catalogue is
    `system:view`-gated, a capability the average task-board reader has no business holding, while every
    one of them needs to know WHEN TODAY STARTED before the `/tasks` board can cut its Today / This week
    buckets. Off the identity payload the browser has only the viewer's own clock, which files the same
    row under yesterday for one reader and today for the next.

    NOT A PER-USER PREFERENCE, despite the company it keeps beside `preferences`: this is the zone the
    OPERATOR chose for the whole control plane — the one every cron expression is read in — and exposing
    it as something a user may set would promise a per-viewer day boundary the server does not have."""
    task_autosave_seconds: int
    """HOW OFTEN THE FULL TASK EDITOR WRITES AN IN-PROGRESS TICKET BACK TO THE SERVER —
    `operator_settings.task_autosave_seconds`, resolved for this principal's own org.

    IT RIDES HERE FOR `task_create_defaults`' REASON, two fields up, and has no reason of its own to
    add: everyone this cadence governs is someone who may write a task, and none of them needs
    `system:view` to do it. The browser's fallback and the clamp on a value it does not recognise are
    `web/src/lib/taskAutosaveInterval.ts`'s, not this model's."""
    task_node_pin_enabled: bool = False
    """Is the task editor's Node field on for this principal's org (`placement_grants.node_pin_enabled`, the
    `task_node_pin_enabled` knob)? Here for `task_create_defaults`' reason: a task writer need not hold
    `system:view`. An operator has the field whatever this says. Presentation only: the pin route refuses on its own."""
    max_file_bytes: int = 0
    """The largest file this principal's org may upload, bytes (`org_quotas.file_cap_bytes`), for the console's
    pick-time check; the upload doors refuse on their own. Read through `web/src/lib/maxFileBytes.ts`."""
    default_theme: str
    """THE THEME THIS USER SEES WHILE THEIR OWN `preferences.theme` IS UNSET — the operator knob
    `default_theme` (`app/operator_settings/`), one value for the whole control plane. Here because a
    preference with no value has to resolve against something, and every signed-in user needs it while
    `GET /api/settings` needs `system:view`. A plain `str`, not the knob's choices: a value this release does not
    know must not 500 the identity payload, and the browser's rule (`web/src/lib/colorScheme.ts`) falls back."""
    playbooks_enabled: bool = False
    """WHETHER PLAYBOOKS ARE ON THIS PRINCIPAL'S ORG'S PLAN — the Playbooks plan switch
    (`app/plan_terms.py::playbooks_enabled`); False with no org. The console's offer only (the Playbook depth,
    the Playbooks tab): the playbook paths re-read the switch for the PROJECT's org and refuse there."""
    org_templates_enabled: bool = False
    """WHETHER THIS PRINCIPAL'S ORG MAY AUTHOR ITS OWN TASK TEMPLATES (`app/plan_terms.py::ORG_TEMPLATES`); False with
    no org. The console's offer only (New template, Duplicate): `routes/task_templates.py` refuses on its own."""
    providers: list[list[str]] = Field(default_factory=list)
    """THE `[row, auth]` CELLS ON THIS PRINCIPAL'S ORG'S PROVIDER GRID (`app/plan_terms.py::providers_of`); none with no
    org. The pickers' offer only (`apps/web/src/lib/providerGrid.ts` reads it): `app/provider_auth_policy.py` refuses
    on its own whatever this says. It also carries the agent rows the console opens the Agents pages on (`sdk`, any
    `acp:*`; `lib/providerGrid.ts::agentsOn`)."""
    microvm_allowed: bool = False
    """WHETHER MICROVM NODES ARE ON THIS PRINCIPAL'S ORG'S PLAN — the MicroVM plan switch
    (`app/plan_terms.py::microvm_allowed`); False with no org. The console's offer only: `app/microvm_mode.py`
    refuses the flip on its own whatever this says."""
    # WHICH SELF-HOSTED NODE TYPES ARE ON THIS PRINCIPAL'S ORG'S PLAN — the three node-type plan switches
    # (`app/plan_terms.py::NODE_OS_SWITCHES`); False with no org. The node wizard's OS choices only:
    # `app/routes/nodes/cloud.py` and `app/routes/node_orders.py` refuse on their own whatever this says.
    ubuntu_nodes_allowed: bool = False
    macos_nodes_allowed: bool = False
    windows_nodes_allowed: bool = False
    github_runners_allowed: bool = False
    """WHETHER GITHUB RUNNERS ARE ON THIS PRINCIPAL'S ORG'S PLAN — its GitHub Runners cap is not 0
    (`app/plan_terms.py::github_runners_allowed`); False with no org. The console's offer only:
    `app/routes/gha_runners.py` refuses on its own whatever this says."""
    project_memory_enabled: bool = False
    """WHETHER PROJECT MEMORY IS ON THIS PRINCIPAL'S ORG'S PLAN — the Project memory plan switch
    (`app/plan_terms.py`); False with no org. The console's offer only: `app/project_memory/switch.py` refuses
    on its own whatever this says."""
    slack_enabled: bool = False
    sso_enabled: bool = False
    scim_enabled: bool = False
    siem_export_enabled: bool = False
    data_residency_enabled: bool = False
    ip_allowlist_enabled: bool = False
    org_retention_enabled: bool = False
    staging_org_enabled: bool = False
    budgets_enabled: bool = False
    """SLACK AND THE EIGHT ENTERPRISE SWITCHES ON THIS PRINCIPAL'S ORG'S PLAN (`app/plan_terms.py::SWITCHES`); False with
    no org. The console's offer only: each door refuses on its own (`plan_terms.require_switch`)."""
    jaah_ai_enabled: bool = False
    """WHETHER THE JAAH AI ACCOUNT IS ON THIS PRINCIPAL'S ORG'S PLAN — the Jaah AI plan switch (`app/plan_terms.py`);
    False with no org. The console's offer only: `app/routes/jaah_ai.py` refuses on its own whatever this says."""
    audit_enabled: bool = False
    session_transcripts_enabled: bool = False
    """WHETHER AUDIT > ACTIVITY AND AUDIT > SESSIONS ARE ON THIS PRINCIPAL'S ORG'S PLAN (`app/plan_terms.py`); False
    with no org. The console's offer only: `routes/audit.py` and `routes/sessions.py` refuse a tenant on their own."""
    # WHICH CLOUDS THIS PRINCIPAL'S ORG'S PLAN LETS IT CONNECT AND LAUNCH ON — Your Cloud per provider
    # (`app/plan_terms.py::CLOUD_SWITCHES`); False with no org. The console's offer only: `clouds.require_provider`
    # refuses on its own whatever this says.
    aws_cloud_allowed: bool = False
    gcp_cloud_allowed: bool = False
    azure_cloud_allowed: bool = False
    tenant_email_allowed: bool = False
    """WHETHER THIS PRINCIPAL'S ORG'S PLAN LETS IT CONNECT ITS OWN GMAIL (`app/plan_terms.py::TENANT_EMAIL`); False
    with no org. The console's offer only: `routes/org_email.py` refuses on its own."""
    compliance_enabled: bool = False
    """WHETHER PORTFOLIO COMPLIANCE EXISTS FOR THIS PRINCIPAL'S ORG — the Compliance plan parameter, read
    through its only reader `app/governance/availability.py` (pinned contract S7). The sidebar and the route
    guard read it; `/api/compliance/*` refuses (404) on its own whatever this says."""
    guard_enabled: bool = False
    """WHETHER THE ARCHITECTURAL GUARD IS AVAILABLE FOR THIS PRINCIPAL'S ORG — Compliance AND Advanced Compliance
    both on, read through its only reader `app/governance/availability.py::guard_enabled` (pinned contract S1). The
    console shows guard UI only when true; every guard API refuses (404) on its own whatever this says."""
    nodelink_enabled: bool = False
    """WHETHER NODELINK IS ON FOR THIS PRINCIPAL'S ORG — the NodeLink plan term, always on for the operator plane,
    read through `app/tenant_defaults.py::shared_store_allowed`. The console shows its NodeLink controls only when
    true; every NodeLink write refuses on its own whatever this says."""
    nodevault_enabled: bool = False
    """MAY THIS PRINCIPAL'S ORG READ NODEVAULT AT ALL — `app/nodevault_policy.py::readable`, the GET's own gate
    (allowed, set up, or mid-operation); False with no org. Settings skips the `/api/nodevault` read while it is
    off; the routes refuse on their own whatever this says."""
    org_hipaa: bool = False
    """WHETHER THIS PRINCIPAL'S ORG IS IN HIPAA MODE (`app/hipaa.py::org_is_on`), so the console can apply the HIPAA
    safeguards without the org roster. Every server-side safeguard reads the org itself, whatever this says."""
    acting_as: Optional[OrgRef] = None
    """THE ORG THIS TAB ACTS AS (`app/act_as.py`), or null. While set, this whole payload is that org's
    Owner seat — `org`, `permissions`, `org_role` and every per-org knob are the org's, `is_superadmin` and
    `is_operator` are false — and only `id`/`email`/`memberships` are still the operator's own. The console draws
    its act-as bar from it."""
    terms_pending: bool = False
    """THIS SESSION STILL OWES THE TERMS ACCEPTANCE (`auth.SessionClaims.terms`, PRD D7): every other route answers
    403 `terms_required` until `POST /api/auth/terms/accept`, so the console opens its acceptance page first."""
    api_key: Optional["MeApiKeyOut"] = None
    """THE API KEY THIS REQUEST PRESENTS (`app/api_keys.py`), or null for a console session. With a key every
    permission field above is the key's narrowed set, as the guard decides it."""
    hipaa_warnings: list[HipaaSurfaceOut] = Field(default_factory=list)
    """THE NON-BAA SURFACES THIS PRINCIPAL'S ORG IS WARNED ABOUT (`app/hipaa.py::org_warnings`), each with whether an
    org admin acknowledged it; empty while HIPAA is off. Here, not only on the org row, because every member sees the
    warning at the point of use and only an admin may read the org. Presentation only: nothing is ever blocked."""
    static_slots: Optional[int] = 0
    """HOW MANY STATIC WORKTREES THIS PRINCIPAL'S ORG'S PLAN LETS A PROJECT HOLD ON ONE NODE — 0 = none, None = no
    cap (the operator plane), read through `app/static_slots_policy.py`. At 0 the console hides every control that
    adds a static slot; the writes refuse on their own whatever this says."""


def _load_memberships(session: Session, user_id: UUID) -> list[MembershipOut]:
    rows = session.execute(
        select(UserGroupMembership, Group.name, Group.is_everyone)
        .join(Group, UserGroupMembership.group_id == Group.id)
        .where(UserGroupMembership.user_id == user_id)
        .order_by(UserGroupMembership.id)
    ).all()
    return [
        MembershipOut(
            id=membership.id, group=group_name,
            resource_type=membership.resource_type, resource_id=membership.resource_id,
            is_everyone=bool(is_everyone),
        )
        for membership, group_name, is_everyone in rows
    ]


def _is_scope_key(key: str) -> bool:
    """A key a project triple can carry — what a scoped browser control may ask of the map."""
    return key.split(":", 1)[0] in PROJECT_LEVEL_FEATURES or key in PROJECT_ROLE_ORG_KEYS


def _scope_maps(session: Session, user: AdminUser) -> dict:
    """`AdminOut`'s `permissions`, `project_permissions` and `org_role`, from one `authz.effective_access` call, so
    `/api/auth/me` and the guard cannot disagree.

    ORG-WIDE KEYS ARE PROJECTED ONTO EVERY PROJECT THE CALLER'S ORG SEES. The browser's
    scope seam (`web/src/auth.tsx::useCanOnProject`/`useCanOnAnyProject`) answers
    only from a keyed entry, and `Expansion.org_keys` are not triples, so without the projection an Org
    Admin would see every project/task control hidden while the API admits them (`has_on_scope`'s org
    arm). Only keys a scoped control can ask (`_is_scope_key`) are projected, which bounds the payload.
    The read runs under `_admin_out`'s bound org, so the projection is that org's rows and no other's."""
    # An acting seat (`act_as.seat_user`) is the org's Owner: no bindings of its own, the Owner's keys org-wide.
    expansion = (
        access.Expansion(org_keys=OWNER_KEYS, scope_permissions=frozenset(), org_role=access.ORG_OWNER, valid_until=None)
        if act_as.operator_org(user) is not None
        else effective_access(session, user.id)[1]
    )
    by_project: dict[str, set[str]] = {}
    for scope_type, scope_id, key in expansion.scope_permissions:
        # The `account` arm is dropped: no browser map is keyed on an account.
        if scope_type == "project":
            by_project.setdefault(str(scope_id), set()).add(key)
    org_scope_keys = {key for key in expansion.org_keys if _is_scope_key(key)}
    if org_scope_keys:
        for project_id in session.scalars(select(Project.id)).all():
            by_project.setdefault(str(project_id), set()).update(org_scope_keys)

    return {
        "permissions": sorted(expansion.permissions),
        # Keys sorted so the payload is byte-stable.
        "project_permissions": {pid: sorted(keys) for pid, keys in sorted(by_project.items())},
        "org_role": expansion.org_role,
    }


def _admin_out(user: AdminUser, session: Session) -> AdminOut:
    """The `/api/auth/me` payload — READ UNDER THE CALLER'S OWN ORG.

    Its four callers are `@always(Tenancy.BOOTSTRAP)`, so nothing is bound, and under the enforcing seam
    every read here would return an identity with no groups, permissions or projects.

    The caller's org, not the identity escape: escape the reads that resolve the caller, scope the reads
    that describe one (`seam.IDENTITY_CLASSES`); `Group` is not an identity class.

    `bind_from`, not `bind(user.org_id)`: this `def` runs on a worker thread, where a bare `ContextVar`
    set is lost (see `seam.bind`). It derives the org from the row. A user with NULL `org_id` runs
    unbound and sees nothing — no invented tenant."""
    # Imported locally, as in `routes/settings.py::put_settings`: `cron_runtime` drags in the ORM, activity
    # registry and schedule parser. Cached after the first response.
    from app import cron_runtime

    if act_as.operator_org(user) is not None:
        # An acting seat's org is the one it acts as, not its row's (`act_as.seat_user`).
        with seam.bound_to(user.org_id):
            return _admin_out_bound(user, session, cron_runtime)
    with seam.bind_from(session, AdminUser, user.id):
        return _admin_out_bound(user, session, cron_runtime)


def _admin_out_bound(user: AdminUser, session: Session, cron_runtime) -> AdminOut:
    """`_admin_out`'s body, under the scope it bound. Split so the `with` wraps one expression and a later
    edit cannot land an argument's read outside the block."""
    return AdminOut(
        id=user.id, email=user.email, email_verified=user.email_verified_at is not None, role=user.role,
        first_name=user.first_name, last_name=user.last_name,
        # The document shape `preferences_to_store` writes.
        preferences=preferences_document(UserPreferences.from_stored(stored_preferences(user))),
        notification_events=subscribable_events(session, user),
        notification_methods_available=available_notification_methods(user.org_id),
        git_credential_id=user.git_credential_id,
        # Through the shared resolver (`app/tenancy/labels.py`), which decides that an unknown or NULL id is
        # None, not the default org. The one `AdminOut` constructor, so every auth response carries it.
        org=_me_org_ref(session, user.org_id),
        is_superadmin=user.is_superadmin,
        is_operator=is_operator_plane(user.org_id, user.is_superadmin),
        tenant=is_tenant_org(user.org_id),
        memberships=_org_memberships(session, user),
        pending_invitations=[
            PendingInvitationOut(
                id=row.id, org_name=row.org_name, invited_by_name=row.invited_by_name, created_at=row.created_at,
            )
            for row in org_invitations.pending_for(session, user.identity_id)
        ],
        group_memberships=_load_memberships(session, user.id),
        **_scope_maps(session, user),
        # Resolved per response (the knobs are live-editable), from `operator_settings`' short TTL cache; the
        # zone likewise. The principal's own org is passed: these are per-org knobs and the resolver refuses an
        # ambient scope once any org overrides, which here would be a login outage. `task_autosave_seconds` too.
        task_create_defaults=TaskCreateDefaultsOut(
            **settings_defaults.task_create_defaults(user.org_id)
        ),
        project_create_defaults=ProjectCreateDefaultsOut(
            **settings_defaults.project_create_defaults(user.org_id)
        ),
        timezone=cron_runtime.resolved_timezone_name(),
        task_autosave_seconds=settings_reads.get_int(
            "task_autosave_seconds", org=user.org_id
        ),
        task_node_pin_enabled=placement_grants.node_pin_enabled(user.org_id),
        max_file_bytes=org_quotas.file_cap_bytes(session, user.org_id),
        # Operator-tier: one value, read with no org.
        default_theme=settings_reads.get_str("default_theme"),
        **_plan_switches(session, user.org_id),
        providers=(
            provider_auth_policy.encoded(provider_auth_policy.allowed(session, user.org_id))
            if user.org_id is not None else []
        ),
        compliance_enabled=_compliance_enabled(session, user.org_id),
        guard_enabled=_guard_enabled(session, user.org_id),
        nodelink_enabled=user.org_id is not None and tenant_defaults.shared_store_allowed(session, user.org_id),
        nodevault_enabled=_nodevault_enabled(session, user.org_id),
        static_slots=static_slots_policy.cap(session, user.org_id),
        org_hipaa=hipaa.org_is_on(session, user.org_id),
        hipaa_warnings=[
            HipaaSurfaceOut(surface=surface, acknowledged=acked)
            for surface, acked in hipaa.org_warnings(session, user.org_id)
        ],
        acting_as=(
            org_ref_of(org_refs(session, [user.org_id]), user.org_id) if act_as.operator_org(user) is not None else None
        ),
    )


def _nodevault_enabled(session: Session, org_id: Optional[UUID]) -> bool:
    org = session.get(Org, org_id) if org_id is not None else None
    return org is not None and nodevault_policy.readable(session, org, nodevault_policy.status(session, org))


def _plan_switches(session: Session, org_id: Optional[UUID]) -> dict[str, bool]:
    """The identity payload's plan switches, the org and its plan row read once (`plan_terms.switch_on`'s rule:
    no org, or an id naming none, reads every switch off), and the GitHub Runners flag (its cap not 0)."""
    names = (*plan_terms.CONSOLE_SWITCHES, plan_terms.GITHUB_RUNNERS)
    if org_id is None:
        return dict.fromkeys(names, False)
    return plan_terms.switches_on(session, org_id, names)


def _revoke_direct_console_grants(admin_id: UUID, retired_epoch: int) -> None:
    """Tell every connected node to drop the direct-console grants issued to a session that just ended.

    The epoch passed is the retired one — see `revoke_for_admin_session`, the single home for that
    rule and its call sites. Called on both `session_epoch` bumps here (logout, password change).
    Synchronous (via `revoke_for_admin_session_sync`) so callers stay `def` and bcrypt stays off the
    event loop.
    """
    console_direct.revoke_for_admin_session_sync(node_bus.registry, admin_id, retired_epoch)


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """The hash `login` compares against when the email resolves to no loginable row.

    Minted once per process through `hash_password`, so it costs `BCRYPT_ROUNDS` like a stored
    credential. Random plaintext, so no submitted password can ever match it.
    """
    return hash_password(secrets.token_urlsafe(32))


class MfaChallengeOut(BaseModel):
    """A first factor that was accepted but mints no session yet (202): the second factor is due
    (`app/login_funnel.py`). `verify`: enter a code; `enrol`: set one up first. The pending cookie is set."""

    mfa: Literal["verify", "enrol"]


_CHALLENGES = {"mfa_required": "verify", "enrol_required": "enrol"}


@router.post("/login", response_model=Union[AdminOut, MfaChallengeOut], dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def login(
    body: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)
):
    # The PERSON first (`app/identities.py`), then which of their memberships this sign-in opens
    # (`login_funnel.pick_membership`, under its identity escape: the route is `@always(Tenancy.BOOTSTRAP)`).
    # `_admin_out` binds the chosen membership's org afterwards.
    person = identities.by_email(session, body.email)
    if person is not None and identities.is_system(session, person):
        person = None
    # None with a person: they hold no membership any more, refused `disabled` once the password is right.
    admin = login_funnel.pick_membership(session, person, "password", request=request) if person is not None else None
    # Three arms have no hash to check: an unknown email, the system actor (`entity_shape.SYSTEM_ACTOR_ID`,
    # never a login identity) and a Google-only invitee. Each still pays one bcrypt compare against the dummy
    # before the same 401, so response timing cannot enumerate which emails have an account.
    loginable = person is not None
    attributed = admin

    def refuse(status: int, detail, reason: str) -> HTTPException:
        login_funnel.record_login_failure(
            session, request, attributed, method="password", reason=reason, identifier=body.email
        )
        return HTTPException(status_code=status, detail=detail)

    # The lockout answers first, keyed on the row's own email when there is one, and the same whether or not
    # the email has an account: a locked identifier tests no password at all.
    key = person.email if loginable else body.email
    lockout = login_lockout.state(session, key, login_funnel.source_ip(request))
    if lockout == login_lockout.LOCKED:
        raise refuse(403, {"code": login_lockout.LOCKED}, login_lockout.LOCKED)
    # The step-up binds only an account that HAS a confirmed authenticator (an unknown email is treated as
    # one): an invitee yet to enrol keeps the per-source lock alone, else a stranger's 30 failures would shut
    # their only door. Accepted cost: under a step-up, such an account answers `invalid credentials` where the
    # rest answer `step_up_required`, so it is distinguishable — availability over that narrow enumeration.
    stepped_up = lockout == login_lockout.STEP_UP_REQUIRED and (
        admin is None or mfa.is_enrolled(session, admin)
    )
    # Under the step-up every refusal is the same 401: neither a right password nor a right code is ever
    # confirmed alone, and the code is checked only for a caller who proved the password, so a stranger can
    # never spend the owner's authenticator failure count.
    step_up_refusal = {"code": login_lockout.STEP_UP_REQUIRED}

    def denied(detail, reason: str) -> HTTPException:
        return refuse(401, step_up_refusal if stepped_up else detail, reason)

    if not loginable or person.password_hash is None:
        verify_password(body.password, _dummy_password_hash())
        raise denied("invalid credentials", "unknown_identifier" if not loginable else "no_password")
    if admin is None:
        # No membership left (R7): the account is deactivated — said only to a caller who knows the password.
        if not verify_password(body.password, person.password_hash):
            raise denied("invalid credentials", "bad_password")
        if stepped_up:
            # Under the step-up a right password is never confirmed alone.
            raise denied(None, login_lockout.STEP_UP_REQUIRED)
        raise refuse(403, {"code": login_oauth.ACCOUNT_DISABLED}, "disabled")
    password_refused = password_refusal(admin)
    if password_refused is not None:
        # Refused whatever was typed, after the same bcrypt cost: answering only a RIGHT password with this
        # 403 would make the door an oracle for the passwords of rows with no password door (a superadmin by
        # PRD D7, or one an operator shut — `auth.password_refusal`).
        verify_password(body.password, _dummy_password_hash())
        raise refuse(403, {"code": password_refused}, password_refused)
    _refuse_sso_member_password(session, request, admin, body.password)
    if not verify_password(body.password, person.password_hash):
        raise denied("invalid credentials", "bad_password")
    second_factor = None
    if stepped_up:
        if not body.totp:
            raise denied(None, login_lockout.STEP_UP_REQUIRED)
        if not mfa.verify_current_code(session, admin, body.totp, sign_in=True).ok:
            raise denied(None, "bad_factor")
        second_factor = "otp"
    _refuse_unapproved(session, request, admin, "password")
    challenge = finish_first_factor(session, request, response, admin, "password", second_factor=second_factor)
    return challenge or _admin_out(admin, session)


# A switch target that is not the person's live membership: the same 404 `POST /api/auth/switch` answers. Not
# a refused sign-in (the Google identity was proven), so not recorded or counted toward the lockout.
_SWITCH_NOT_FOUND = "not found"


@router.post("/google", response_model=Union[AdminOut, MfaChallengeOut], dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def google_login(
    body: GoogleLoginRequest, request: Request, response: Response, session: Session = Depends(get_session)
):
    """Alternative login via a Google Sign-In ID token — invite-only, but for a registered email domain.

    Verifies the GSI credential (JWT) against Google's public keys with our OAuth Client ID as the
    expected audience, then authenticates an existing admin_users row by email, linking google_sub on first
    match. With no row, a verified email on a domain an org registered creates the user in that org
    (`_join_by_domain`, access-model C6) while `signup_domain_join` is on; any other email is refused as before.

    A `google_sub` link counts only while the linked row's email still equals the token's: a row whose email
    was changed after linking would otherwise keep logging that Google account into the WRONG person.
    Such a stale link is released and the email arm decides, as for a never-linked account.
    """
    try:
        idinfo = verified_google_idinfo(body.credential)
    except HTTPException as exc:
        if exc.status_code == 401:
            login_funnel.record_login_failure(session, request, None, method="google", reason="bad_credential")
        raise
    try:
        admin, challenge = google_sign_in(session, request, response, idinfo, switch_to=body.switch_to)
    except HTTPException as exc:
        # The refusals `google_sign_in` raises itself; the funnel's and the rejected org's are recorded already.
        reason = _GOOGLE_REFUSALS.get(exc.detail) if isinstance(exc.detail, str) else None
        if reason is not None:
            session.rollback()  # nothing the refused resolution staged is kept
            login_funnel.record_login_failure(
                session, request, None, method="google", reason=reason, identifier=idinfo.get("email")
            )
        raise
    if body.switch_to is not None and challenge is None:
        login_funnel.announce_replaced_session(session, request, admin)
    return challenge or _admin_out(admin, session)


_GOOGLE_REFUSALS = {
    "no Jaah account for this Google email": "no_account",
    "account already linked to another Google identity": "already_linked",
    "Google identity already linked": "already_linked",
    "Sign-in conflicted with another. Try again.": "conflict",
}


def verified_google_idinfo(credential: str) -> dict:
    """The claims of a Google Sign-In ID token Google verified for our client id, with a verified email and
    a `sub`; the 503/401 refusals otherwise. Shared by the sign-in and the sign-up door
    (`app/routes/signup.py`)."""
    client_id = google_client_id()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    try:
        idinfo = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), client_id
        )
    except (ValueError, GoogleAuthError):
        raise HTTPException(status_code=401, detail="invalid Google credential")
    # `is not True`: a sign-in may now CREATE an account, so a stringly `"false"` must not pass.
    if idinfo.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="Google email not verified")
    # Guard explicitly: an unguarded `google_sub == None` would render as `IS NULL` and match an arbitrary
    # unlinked admin instead of failing closed.
    sub = idinfo.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="invalid Google credential")
    return idinfo


def google_sign_in(
    session: Session, request: Request, response: Response, idinfo: dict, *, switch_to: Optional[UUID] = None
) -> tuple[AdminUser, Optional[MfaChallengeOut]]:
    """`google_login`'s resolution of verified claims: the user, with the session cookie set on `response`
    or the second-factor challenge (`finish_first_factor`), or the refusal it has always given. `switch_to`
    signs in the person's membership in that org instead (`_picked`), or 404s."""
    sub = idinfo["sub"]
    email = (idinfo.get("email") or "").strip().lower()
    no_account = HTTPException(status_code=403, detail="no Jaah account for this Google email")
    # The PERSON linked to this Google account (`app/identities.py`). The system actor cannot log in: the
    # refusal is the same 403 an unknown email gets, checked on both arms — on the email arm before
    # `google_sub` is linked. Not a 401: Google verified this credential.
    person = identities.by_google_sub(session, sub)
    if person is not None and identities.is_system(session, person):
        raise no_account
    stale_link = None
    if person is not None and person.email.lower() != email:
        # Released only once the email arm has found this identity's real person: a refusal below writes nothing.
        stale_link, person = person, None
    if person is None:
        person = identities.by_email(session, email)
        if person is not None and identities.is_system(session, person):
            raise no_account
        if person is None:
            # Nobody has this email: a registered domain's join creates the person and their membership.
            admin = _join_by_domain(session, idinfo, email=email, sub=sub, stale_link=stale_link)
            # A join races a reject that commits after `org_for_email_domain` read the org enabled.
            _refuse_unapproved(session, request, admin, "google")
            return admin, finish_first_factor(session, request, response, admin, "google", idp_claims=idinfo)
        admin = _picked(session, request, person, idinfo, switch_to)
        # Before the link write below: a refused sign-in leaves nothing behind.
        _refuse_unapproved(session, request, admin, "google")
        if person.google_sub and person.google_sub != sub:
            raise HTTPException(
                status_code=409, detail="account already linked to another Google identity"
            )
        if stale_link is not None:
            identities.set_fields(session, stale_link, google_sub=None)
            session.flush()  # `google_sub` is unique: the release must reach the DB before the relink
        set_identity_fields(admin, google_sub=sub)
        with conflict_as_409(session, "Google identity already linked"):
            session.commit()
    else:
        admin = _picked(session, request, person, idinfo, switch_to)
    # The `google_sub` arm (the email arm refused above).
    _refuse_unapproved(session, request, admin, "google")
    # Both arms matched the person's email to the Google-verified email: this sign-in proves it.
    if admin.email_verified_at is None:
        from app.login_oauth import stamp_email_verified

        stamp_email_verified(session, admin, email)
        session.commit()
    return admin, finish_first_factor(session, request, response, admin, "google", idp_claims=idinfo)


def _picked(
    session: Session, request: Request, person: UserIdentity, idinfo: dict, switch_to: Optional[UUID] = None
) -> AdminUser:
    """The membership a Google sign-in of `person` opens (`login_funnel.pick_membership`), or the
    `account_disabled` refusal, recorded, when they hold none any more (R7). With `switch_to` (an org
    switch's re-authentication) it is their live membership in that org, or a 404 (`google_login` records it)."""
    admin = login_funnel.pick_membership(session, person, "google", idp_claims=idinfo, request=request)
    if admin is None:
        login_funnel.record_login_failure(
            session, request, None, method="google", reason="disabled", identifier=person.email
        )
        raise HTTPException(status_code=403, detail={"code": login_oauth.ACCOUNT_DISABLED})
    if switch_to is not None:
        admin = login_funnel.switch_target(session, admin, switch_to)
        if admin is None:
            raise HTTPException(status_code=404, detail=_SWITCH_NOT_FOUND)
    return admin


def finish_first_factor(
    session: Session,
    request: Request,
    response: Response,
    admin: AdminUser,
    method: login_funnel.FirstFactor,
    *,
    idp_claims: Optional[dict] = None,
    second_factor: Optional[str] = None,
) -> Optional[MfaChallengeOut]:
    """A POST door's end: the funnel sets the session cookie (None), or the second factor is due — the
    pending cookie set, `response` turned 202, the challenge returned for the door to answer — or this
    raises its refusal (403 — the factor itself was verified). Shared by the password, Google and sign-up
    doors."""
    outcome = login_funnel.complete_first_factor(
        session, request, response, admin, method, idp_claims=idp_claims, second_factor=second_factor
    )
    if outcome.kind == "session":
        return None
    if outcome.kind in _CHALLENGES:
        response.status_code = 202
        return MfaChallengeOut(mfa=_CHALLENGES[outcome.kind])
    if outcome.reason in _CODED_REFUSALS:
        from app.login_oauth import ACCOUNT_DISABLED

        # `{code}` as `org_gate`'s refusal is, so the login page words it (`Login.tsx::postRefusalMessage`).
        code = ACCOUNT_DISABLED if outcome.reason == "disabled" else outcome.reason
        if code == ip_allowlist.REFUSAL_CODE:
            # The address refused, so the page can name the one an admin must add.
            raise HTTPException(status_code=403, detail={"code": code, "reason": login_funnel.source_ip(request)})
        raise HTTPException(status_code=403, detail={"code": code})
    raise HTTPException(status_code=403, detail=f"sign-in refused ({outcome.reason or outcome.kind})")


def _refuse_sso_member_password(session: Session, request: Request, admin: AdminUser, password: str) -> None:
    """403 a password sign-in by a member of an SSO-only org (`app/sso_policy.py`) or a Require-SSO one
    (`app/org_sso.py`): neither has a password door, so it is refused whatever was typed, after the same bcrypt
    cost (a refusal only a RIGHT password earned would make the door an oracle). An edition without single sign-on
    has neither."""
    if not edition_features.enabled("sso"):
        return
    from app import org_sso, sso_policy

    if sso_policy.sso_org(session, admin) is not None:
        verify_password(password, _dummy_password_hash())
        sso_policy.refuse(session, request, admin, "password")
        raise HTTPException(status_code=403, detail={"code": login_oauth.SSO_REQUIRED})
    if org_sso.sso_enforced_policy(session, request, admin, admin.email, "password", None) is not None:
        verify_password(password, _dummy_password_hash())
        raise HTTPException(status_code=403, detail={"code": login_oauth.SSO_ENFORCED})


# The funnel's refusals a POST door answers as `{code}` (the org gate's two included: its backstop hook).
_CODED_REFUSALS = frozenset({
    "disabled", "superadmin_password", "password_off", login_oauth.SSO_REQUIRED, login_lockout.LOCKED,
    ORG_PENDING_REVIEW, ORG_NOT_APPROVED, ORG_DELETED, ip_allowlist.REFUSAL_CODE, login_oauth.SSO_ENFORCED,
})


def _refuse_unapproved(session: Session, request: Request, admin: AdminUser, door: str) -> None:
    """A POST door's pending- or rejected-org refusal (`org_gate.refuse_unapproved_sign_in`), recorded as a
    `login_failed` like every refused sign-in. Call only after the identity is verified: the refusal names the
    org's fate."""
    try:
        org_gate.refuse_unapproved_sign_in(session, admin.org_id)
    except HTTPException as exc:
        reason = exc.detail["code"]
        log.info("%s-login: refused %s user=%s", door, reason, admin.id)
        login_funnel.record_login_failure(session, request, admin, method=door, reason=reason)
        raise


def _join_by_domain(
    session: Session, idinfo: dict, *, email: str, sub: str, stale_link: Optional[UserIdentity]
) -> AdminUser:
    """A first Google sign-in with no account: create it when the VERIFIED email's domain is registered
    on an enabled org (`org_domains`) and domain joins are on (`signup_domain_join`), else the
    invite-only 403 — a paused join first records `domain_join_skipped` in the org it would have
    entered. `google_login` has already refused an
    unverified email. The domain is read off the email, never the `hd` claim, which only says which
    Workspace issued the token.

    The user is made by the one user-create helper (`routes/users.py::create_org_user`), so they join the
    org's Everyone group, then bound to the org's default role at org scope, if it has one — all as the
    system actor, in one transaction under the org the domain names. A stale link (see `google_login`) is
    released first, outside that org: its row may live in another one."""
    # An edition without sign-up admits nobody by email domain: the invite-only 403.
    if not edition_features.enabled("signup"):
        raise HTTPException(status_code=403, detail="no Jaah account for this Google email")
    # Local: `routes/users.py` imports this module.
    from app import signup
    from app.routes.users import create_org_user

    domain = access.email_domain(email)
    org_id = signup.domain_join_target(session, email)
    if org_id is None:
        raise HTTPException(status_code=403, detail="no Jaah account for this Google email")
    if not settings_reads.get_bool("signup_domain_join"):
        # Paused (`signup_domain_join` off): the matched org hears of it, the person gets the invite-only 403.
        signup.record_domain_join_skipped(session, org_id, email)
        session.commit()
        raise HTTPException(status_code=403, detail="no Jaah account for this Google email")
    if stale_link is not None:
        identities.set_fields(session, stale_link, google_sub=None)
        session.flush()  # `google_sub` is unique: the release must reach the DB before the new row takes it
    # The org the verified domain decided; this route binds nothing, and the rows below are that org's.
    with seam.bound_to(org_id):
        try:
            user = create_org_user(
                session, access.system_actor(org_id), org_id=org_id, email=email,
                first_name=idinfo.get("given_name"), last_name=idinfo.get("family_name"),
            )
            # Created FROM the Google-verified email, so it is proven from the start.
            set_identity_fields(user, google_sub=sub, email_verified_at=datetime.now(timezone.utc))
            access.bind_default_role(session, user)
            record_write(
                session, access.system_actor(org_id), "user_domain_join",
                f"{email} joined through the email domain {domain}",
                data={"user_id": user.id, "email": email, "domain": domain},
            )
            session.commit()
            # Reloaded under the org: the caller reads `id`/`session_epoch` unbound, where an expired
            # attribute would reload as `false()`.
            session.refresh(user)
        except IntegrityError:
            # A concurrent first sign-in for the same email or Google identity won the unique key.
            session.rollback()
            raise HTTPException(status_code=409, detail="Sign-in conflicted with another. Try again.")
        except HTTPException as exc:
            # The org's Users cap (`create_org_user`): said to the person signing in, not the buyer.
            if exc.status_code != 409:
                raise
            session.rollback()
            raise HTTPException(
                status_code=409, detail="This organization has no seats left. Ask its admin to make room.",
            ) from exc
    # Committed: the org's `user_registered` triggers hear of the join.
    from app import trigger_user_registered

    trigger_user_registered.on_member_joined(user.id, org_id, via=trigger_user_registered.VIA_DOMAIN_JOIN)
    return user


@router.post("/logout", dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """End this session — bump the IDENTITY epoch (which ends this person's cookie in every org, on every
    device) and clear it."""
    admin_id = verify_session_token(request.cookies.get(COOKIE_NAME))
    if admin_id is not None:
        # Identity escape, for `app/auth.py::_session_user`'s reason: unbound, the lookup finds nothing and the
        # logout silently leaves every issued cookie live. The write that follows needs no escape.
        with seam.identity_scope():
            user = session.get(AdminUser, admin_id)
        if user is not None:
            ended = identities.end_every_session(session, user)
            session.commit()
            # After the commit: a rolled-back retirement must never revoke a session that is still live.
            for member_id, epoch in ended:
                _revoke_direct_console_grants(member_id, epoch)
    clear_session_cookie(response)
    return {"ok": True}


class SwitchIn(BaseModel):
    org_id: UUID
    # A TOTP or a recovery code, when a previous answer was `reauth_required` with `door: otp`.
    code: Optional[str] = Field(default=None, max_length=64)


class SwitchOut(BaseModel):
    """`POST /api/auth/switch`'s answer (pinned for the web switcher and the deep-link interstitial).
    `session`: the cookie was re-minted for `org`. `reauth_required`: nothing changed; meet the target org's
    policy at `door` — `otp` (resend with `code`; `error` says why a sent one failed), `enrol` (no factor yet:
    the pending cookie is set, continue at the MFA enrol page) or an IdP: a redirect
    to `start_url`, or for `google` the Sign-In button posting `switch_to` to `/api/auth/google`."""

    outcome: Literal["session", "reauth_required"]
    org: Optional[OrgRef] = None
    door: Optional[Literal["otp", "enrol", "google", "microsoft", "github"]] = None
    start_url: Optional[str] = None
    error: Optional[Literal["bad_factor", "mfa_throttled"]] = None


#: The redirect IdP doors whose `/start` takes `switch_to` (`routes/login_oauth.py`); Google's door is a POST.
_SWITCH_START_DOORS = frozenset({"github", "microsoft"})


@router.post(
    "/switch", response_model=SwitchOut, response_model_exclude_none=True, dependencies=[Depends(check_rate_limit)]
)
@always(Tenancy.BOOTSTRAP)
@session_only
def switch_org(
    body: SwitchIn,
    request: Request,
    response: Response,
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Act as another of the caller's own orgs from now on, in every tab of this browser session (R2/R4):
    the session cookie is re-minted for the person's membership in `org_id` once that org's sign-in policy
    holds for this session (`login_funnel.switch_membership`, R10). 404 for an org the caller holds no live
    membership in (unknown, not theirs, or disabled — one answer for all); 409 `org_pending` /
    `org_not_approved` for an org that may not be signed into, and `ip_not_allowed` (with `ip`) when its IP
    allowlist leaves out this address. Not audited (PRD §5)."""
    claims = read_session(request.cookies.get(COOKIE_NAME))
    if claims is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    target = identities.membership_in(session, admin.identity_id, body.org_id)
    outcome = login_funnel.switch_membership(session, request, response, claims, target, code=body.code)
    if outcome.kind == "refused":
        if outcome.reason == "not_found":
            raise HTTPException(status_code=404, detail="not found")
        if outcome.reason == ip_allowlist.REFUSAL_CODE:
            # The address the target org's allowlist refused, so the switcher names the one its admin must add.
            return JSONResponse(
                {"detail": outcome.reason, "ip": login_funnel.source_ip(request)}, status_code=409
            )
        raise HTTPException(status_code=409, detail=outcome.reason)
    if outcome.kind == "reauth_required":
        start_url = (
            f"{login_oauth.start_path(outcome.door)}?{urlencode({'switch_to': str(body.org_id)})}"
            if outcome.door in _SWITCH_START_DOORS else None
        )
        return SwitchOut(outcome="reauth_required", door=outcome.door, start_url=start_url, error=outcome.error)
    return SwitchOut(outcome="session", org=org_ref_of(org_refs(session, [body.org_id]), body.org_id))


class LocatedOut(BaseModel):
    """`GET /api/auth/locate/...`'s answer: the org and NOTHING of the object (R9). Pinned by its key set."""

    org_id: UUID
    org_name: str


@router.get("/locate/{object_type}/{object_id}", response_model=LocatedOut, dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def locate_object(
    object_type: str,
    object_id: str,
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> LocatedOut:
    """Which of the caller's OTHER orgs holds this object (`object_type` is an `authz.OBJECT_SPECS` name), so
    a detail page that 404s under the active org can offer "This belongs to B — Switch to B" (R9/AC13). 200
    only when a live, switchable membership of the caller's person could open it there; everything else is
    the same 404 as a missing id. `BOOTSTRAP` like `switch_org`: the answer is about orgs the session is not
    bound to, each entered for its own probe (`app/org_locate.py`)."""
    org = org_locate.locate(session, admin, object_type, object_id)
    if org is None:
        raise HTTPException(status_code=404, detail="not found")
    return LocatedOut(org_id=org.id, org_name=org.name)


@router.get("/me", response_model=AdminOut)
@always(Tenancy.BOOTSTRAP)
def me(
    request: Request,
    admin: AdminUser = Depends(require_user_or_signed_out),
    session: Session = Depends(get_session),
):
    out = _admin_out(admin, session)
    key = api_keys.of(request)
    if key is not None:
        return _narrowed_to_key(out, key)
    claims = read_session(request.cookies.get(COOKIE_NAME))
    return out.model_copy(update={"terms_pending": True}) if claims is not None and claims.terms else out


def _narrowed_to_key(out: AdminOut, key: api_keys.ApiKeyContext) -> AdminOut:
    """`/me` for a request made with an API key: the owner's permission fields cut to the key's (`api_keys.narrow`'s
    rule) and the key itself named."""
    display, expires_at, hidden = api_keys.summary(key)
    return out.model_copy(update={
        "permissions": sorted(set(out.permissions) & key.keys),
        "project_permissions": {
            pid: kept for pid, keys in out.project_permissions.items()
            if int(pid) not in hidden and (kept := sorted(set(keys) & key.keys))
        },
        "org_role": out.org_role if key.org_admin else None,
        "is_superadmin": out.is_superadmin and key.superadmin,
        "is_operator": out.is_operator and key.superadmin,
        "api_key": MeApiKeyOut(id=key.key_id, name=key.name, display=display, expires_at=expires_at),
    })


class InvitationAcceptedOut(BaseModel):
    org_id: UUID


@router.post("/invitations/{invitation_id}/accept", response_model=InvitationAcceptedOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def accept_invitation(
    invitation_id: UUID,
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> InvitationAcceptedOut:
    """Join the org that invited this PERSON (R5): their new membership there (never `is_superadmin`), the
    invitation's grant applied, the org now in `/api/auth/me`'s `memberships`. The session keeps acting as
    the membership it was. `BOOTSTRAP` like `/me`: the invitation is in another org than the caller's, found
    by person, and written under `bound_to` its own org (`app/org_invitations.py`). 404 unless it is the
    caller's own pending invitation; 409 when they are a member there already or the org is full."""
    return InvitationAcceptedOut(org_id=org_invitations.accept(session, admin, invitation_id))


@router.post("/invitations/{invitation_id}/decline", status_code=204)
@always(Tenancy.BOOTSTRAP)
@session_only
def decline_invitation(
    invitation_id: UUID,
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Turn an invitation down: marked declined, no membership made. 404 as `accept_invitation`."""
    org_invitations.decline(session, admin, invitation_id)
    return Response(status_code=204)


# ── Connect Slack by a one-time code DMed to the bot (`app/slack_link.py`) ────────────────


class SlackLinkCodeOut(BaseModel):
    """A fresh code: what to send the bot, when it stops working, and a link that opens the bot's DM (`None` when
    Slack will not say who the bot is)."""

    code: str
    expires_at: datetime
    url: Optional[str] = None


class SlackLinkStatusOut(BaseModel):
    """The caller's newest code (`slack_link.link_state`) and the Slack member id and display name now stored (the
    bot writes them on redeem); the console polls this while a code is shown."""

    state: Literal["none", "pending", "redeemed", "expired"]
    slack_user_id: Optional[str] = None
    slack_display_name: Optional[str] = None


@router.post("/me/slack-link-code", response_model=SlackLinkCodeOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def issue_slack_link_code(
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Self-service only: the code connects whoever sends it to THIS account, so it is issued to the
    signed-in user and shown only to them. A new code replaces any pending one."""
    if not edition_features.enabled("slack"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    # Identity escape, as in `update_me`: unbound, this re-read of the caller's own row would 401.
    with seam.identity_scope():
        user = session.get(AdminUser, admin.id)
        if user is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        org_id = user.org_id
        try:
            code, expires_at = slack_link.issue_code(session, user)
        except slack_link.SlackNotConnected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
    return SlackLinkCodeOut(code=code, expires_at=expires_at, url=slack_link.open_url(org_id))


@router.get("/me/slack-link-code", response_model=SlackLinkStatusOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def slack_link_status(
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not edition_features.enabled("slack"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    with seam.identity_scope():
        user = session.get(AdminUser, admin.id)
        if user is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        # A fresh read: the bot's redeem commits on another session.
        session.refresh(user)
        method = UserPreferences.from_stored(user.preferences).notifications.method_for("slack")
        return SlackLinkStatusOut(
            state=slack_link.link_state(user),
            slack_user_id=(method.user_id or None) if method is not None else None,
            slack_display_name=(method.display_name or None) if method is not None else None,
        )


# ── Connect Telegram by the bot's deep link (`app/telegram_link.py`) ────────────────────────


class TelegramLinkCodeOut(BaseModel):
    """A fresh code and the deep link that carries it to the bot."""

    code: str
    expires_at: datetime
    url: str


class TelegramLinkOut(BaseModel):
    """The caller's person's link (`telegram_link.link_state`); the console polls this while a link is shown."""

    state: Literal["none", "pending", "linked", "expired"]
    linked_at: Optional[datetime] = None
    #: Who Telegram says is linked (`@username` or a name), so the person can tell it is their own account.
    linked_as: Optional[str] = None
    bot_username: Optional[str] = None


def _telegram_off(admin: AdminUser) -> str:
    """Only the operator is told where (`app/tenancy/copy.py` never serves "Settings" to a tenant)."""
    where = " in Settings > Integrations > Telegram" if is_operator_plane(admin.org_id, admin.is_superadmin) else ""
    return f"Telegram is not set up here. An operator picks a bot{where}."


def _telegram_link_out(session: Session, user: AdminUser) -> TelegramLinkOut:
    link = telegram_link.link_for(session, user)
    token, _ = telegram_bot.bot_token()
    return TelegramLinkOut(
        state=telegram_link.link_state(link),
        linked_at=link.linked_at if link is not None else None,
        linked_as=link.chat_label if link is not None and link.chat_id is not None else None,
        bot_username=telegram_bot.bot_username(token) if token else None,
    )


def _own_row(session: Session, admin: AdminUser) -> AdminUser:
    user = session.get(AdminUser, admin.id)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


@telegram_router.post("/me/telegram-link-code", response_model=TelegramLinkCodeOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def issue_telegram_link_code(
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Self-service only, as Slack's: the link connects whoever opens it to THIS person. No plan gates it."""
    token, _ = telegram_bot.bot_token()
    if token is None:
        raise HTTPException(status_code=409, detail=_telegram_off(admin))
    username = telegram_bot.bot_username(token)
    if username is None:
        raise HTTPException(
            status_code=409,
            detail="Telegram did not answer for the bot. Try again; if it persists, an operator re-checks the "
                   "bot credential.",
        )
    with seam.identity_scope():
        user = _own_row(session, admin)
        if user.identity_id is None or user.invitation_id is not None:
            raise HTTPException(status_code=409, detail="Accept the invitation to this organization first.")
        code, expires_at = telegram_link.issue_code(session, user)
        session.commit()
    return TelegramLinkCodeOut(code=code, expires_at=expires_at, url=f"https://t.me/{username}?start={code}")


@telegram_router.get("/me/telegram-link", response_model=TelegramLinkOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def telegram_link_status(
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    with seam.identity_scope():
        # The webhook's redeem commits on another session; this request's session has read nothing yet.
        return _telegram_link_out(session, _own_row(session, admin))


@telegram_router.post("/me/telegram-link/enable", response_model=TelegramLinkOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def enable_telegram_here(
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Turn Telegram on for this membership with the chat the person already linked (in any org): one click on
    Profile, no new code. 409 when nothing is linked."""
    with seam.identity_scope():
        user = _own_row(session, admin)
        if not telegram_link.turn_on(session, user):
            raise HTTPException(status_code=409, detail="Connect Telegram first.")
        session.commit()
        return _telegram_link_out(session, user)


@telegram_router.delete("/me/telegram-link", response_model=TelegramLinkOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def unlink_telegram(
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Delete the person's link (every org stops sending) and turn Telegram off on this membership."""
    with seam.identity_scope():
        user = _own_row(session, admin)
        telegram_link.unlink(session, user)
        session.commit()
        return _telegram_link_out(session, user)


# ── Git Access: the credential a person's runners push with ────────────────────────────
# One validator for both write paths (`PATCH /api/users/{id}` in `routes/users.py`, which imports this
# module, and self-service `PUT /api/auth/me`), so neither accepts a pointer the other refuses.


class GitCredentialOut(BaseModel):
    """One credential a person may select under Git Access — IDENTITY ONLY, no `config` and no secret.

    `config` is absent rather than stripped, as in `routes/agent/task_secrets.py::TaskCredentialSummary`: a second
    serializer to keep in step with `registry.secret_keys()` is the one that would leak.

    `type_label` because the UI may not render a raw slug; derived from the server's registry per response."""

    id: UUID
    name: str
    type: str
    type_label: str = ""


def git_credential_scope(session: Session, subject: AdminUser) -> Select:
    """The `select(Credential)` narrowed to what `subject` may push as (`authz.git_identity_select`) — the
    same rule `git_access._readable_credential` resolves by, so the dropdown and resolution agree.

    The subject is always the person the selection is about, never the operator making it; hence
    `GET /api/users/{id}/git-credentials`."""
    return git_identity_select(principal_for_user(session, subject), current_id=subject.git_credential_id)


def selectable_git_credentials(
    session: Session, subject: AdminUser, *, root: str | None = None
) -> list[GitCredentialOut]:
    """Every git-capable credential `subject` may reach, newest first — the Git Access dropdown's roster;
    with `root`, only those that authenticate that forge root (`git_access.credential_root`), which is
    what a per-forge connection may name.

    Narrowed in the query so unrelated rows' `config` is never loaded and decrypted.
    `GIT_CREDENTIAL_TYPES` is sorted so the SQL is stable across processes."""
    stmt = git_credential_scope(session, subject).where(
        Credential.type.in_(sorted(GIT_CREDENTIAL_TYPES))
    )
    rows = session.scalars(
        stmt.order_by(Credential.created_at.desc(), Credential.id.desc())
    )
    return [
        GitCredentialOut(
            id=row.id, name=row.name, type=row.type, type_label=type_label(row.type)
        )
        for row in rows
        if root is None or credential_root(row) == root
    ]


def validated_git_credential_id(
    session: Session, subject: AdminUser, credential_id: UUID | None
) -> UUID | None:
    """`credential_id` if `subject` may genuinely select it, else a 422 SAYING WHICH of the two it is.

    None ("connected nothing") passes through: clearing is how a person disconnects.

    Two refusals with separate fixes: an unreachable row (deleted credential or revoked grant) versus a
    wrong type (a Jira row in a git field). A 422 either way — a bad value in a well-formed body.

    Not trusted later: `git_access.resolve_for_user` re-runs the scope on every resolution, so a revoked
    grant stops serving without cleanup."""
    if credential_id is None:
        return None
    credential = session.scalars(
        git_credential_scope(session, subject).where(Credential.id == credential_id)
    ).first()
    if credential is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "That credential is not available to this account — it does not exist, or the grant "
                "on it was revoked. Pick one of the credentials offered here."
            ),
        )
    if credential.type not in GIT_CREDENTIAL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{credential.name}' is a {type_label(credential.type)} credential and cannot "
                "authenticate a git remote. Pick a GitHub, GitLab, Bitbucket or Azure DevOps credential "
                "instead."
            ),
        )
    return credential_id


SELF_EMAIL_CHANGE_DETAIL = "Ask an org admin to change your email, or Support if you are one."


class MeUpdate(BaseModel):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    # The rule is `auth.password_problem`, checked in `update_me` (it needs the account's email).
    password: str | None = None
    preferences: UserPreferences | None = None
    git_credential_id: UUID | None = None
    """THE SELF PATH FOR GIT ACCESS, AND IT CARRIES NO PERMISSION KEY — a person choosing which of THEIR
    OWN credentials their runners push with is not an administrative act, and /profile is the one
    deliberately unguarded surface in the console (see `apps/web/src/pages/ProfilePage.tsx`'s header and
    `authz.Declaration.own_credential`, which admits a keyless session to its own credential rows).

    `None` IS MEANINGFUL HERE, unlike every other field on this model, so this one is read through
    `model_fields_set` rather than an `is not None` test: an explicit null DISCONNECTS, and a body that
    never mentions the field must leave the selection alone. Reading it the way its neighbours are read
    would make disconnecting impossible from this route."""


@router.put("/me", response_model=AdminOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def update_me(
    body: MeUpdate,
    request: Request,
    response: Response,
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Stays `def` deliberately: `hash_password` is bcrypt (~180ms CPU) and a sync endpoint runs in a
    worker thread; `async` would stall the event loop's node-bus heartbeats and relay frames."""
    # Identity escape, as in `logout`: unbound, this re-read of the caller's own row would 401.
    with seam.identity_scope():
        user = session.get(AdminUser, admin.id)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if body.email is not None and body.email.strip().lower() != (user.email or "").lower():
        # Nobody changes their own email: it is where the deployment mailbox sends, and an
        # org admin changes it for others (`routes/users.py::set_superadmin`). An unchanged value, which the
        # profile form re-submits, is accepted.
        raise HTTPException(status_code=403, detail=SELF_EMAIL_CHANGE_DETAIL)
    if body.password is not None:
        problem = password_problem(body.password, user.email)
        if problem is not None:
            # Before any assignment, like the git check below, so a 422 never leaves a partial write staged.
            raise HTTPException(status_code=422, detail=PASSWORD_PROBLEM_MESSAGES[problem])
    # `model_fields_set`: null means disconnect, absent means unspoken (see `MeUpdate.git_credential_id`).
    # Validated before any other assignment so a 422 never leaves a partial write staged.
    if "git_credential_id" in body.model_fields_set:
        # Bound to the caller's own org: this route binds nothing, and an unbound `Credential` read is
        # `false()`, which refused every row the (org-bound) roster offers.
        with seam.bound_to(user.org_id):
            # Through the connection store, so the github.com/gitlab.com row follows the pointer.
            git_connections.select_pointer(
                session, user, user, validated_git_credential_id(session, user, body.git_credential_id)
            )
    if body.first_name is not None:
        set_identity_fields(user, first_name=body.first_name or None)
    if body.last_name is not None:
        set_identity_fields(user, last_name=body.last_name or None)
    ended: list[tuple[UUID, int]] = []
    if body.password is not None:
        set_identity_fields(user, password_hash=hash_password(body.password))
        ended = identities.end_every_session(session, user)
        # A new password also forgets every trusted device: a stolen one must not keep skipping the code.
        mfa.revoke_trusted_devices(session, user)
    if body.preferences is not None:
        # R2.6/R2.6a: the only write path a non-superadmin has, so it uses the same shared validator as
        # PATCH /api/users/{id}. Only when `notifications` changed: the Settings tab re-submits the whole
        # document, and an untouched block referencing a deleted credential must not block unrelated saves.
        body.preferences = hold_unsubscribable_events(
            body.preferences, stored_preferences(user), subscribable_events(session, user)
        )
        stored_notifications = UserPreferences.from_stored(user.preferences).notifications
        if (
            "notifications" in body.preferences.model_fields_set
            and body.preferences.notifications.model_dump() != stored_notifications.model_dump()
        ):
            # `target=user` equals `actor` here; passed because the validator's rule is stated in both terms.
            validate_notifications_preferences(body.preferences, actor=admin, target=user)
        # The shared store helper (`app/preferences.py`), never a bare `model_dump()`: it carries the
        # task rail fields forward when this body never mentioned them.
        store_preferences(user, body.preferences)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # `identities.email` is unique deployment-wide but rows are org-scoped, so "already exists" would be a
        # cross-org existence oracle. Only the operator seat (`is_operator_plane`: org and superadmin flag) keeps
        # "already exists"; everyone else reads `not_available`. Only when Postgres names the email constraint
        # (`identities_email_key`); other violations keep the generic sentence. This is the only door a tenant
        # can hit that constraint from (`routes/users.py` is `@superadmin_only(Tenancy.OPERATOR)`).
        constraint = constraint_name(exc)
        if constraint == "identities_email_key" and not is_operator_plane(admin.org_id, admin.is_superadmin):
            raise HTTPException(status_code=409, detail=not_available("email"))
        raise HTTPException(status_code=409, detail="email already exists")
    session.refresh(user)
    if body.password is not None:
        login_funnel.reissue_session(request, response, user)
    # After the commit, so a rolled-back rotation never revokes grants whose session is still valid.
    for member_id, epoch in ended:
        _revoke_direct_console_grants(member_id, epoch)
    # An acting tab is answered as the seat it acts as, like `/me` (`act_as.seat_user`).
    seated = act_as.operator_org(admin) is not None and admin.org_id is not None
    return _admin_out(act_as.seat_user(user, admin.org_id) if seated else user, session)


class PermissionOut(BaseModel):
    """One catalog entry as the admin screens consume it: the key a grant row names, plus the (feature,
    action) pair placing it in the matrix and its description."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    feature: str
    action: str
    description: str
    #: The feature as the grid names it ("credential accounts").
    feature_label: str
    #: Whether any route honours the key (`catalog.UNHONOURED_PERMISSION_KEYS`); an API key's picker hides the rest.
    honoured: bool


@catalog_router.get("/api/permissions", response_model=list[PermissionOut])
@always(Tenancy.BOOTSTRAP)
def list_permissions():
    """The permission catalog, in catalog order — the vocabulary every grant screen renders.

    `@always`, not superadmin-gated: a fixed constant from `app.authz.PERMISSION_CATALOG` that reveals
    nothing about any user, and the admin screens need it for their labels. An undeclared route fails
    closed, hence the marker.

    Order is the catalog's (feature, then action), so the matrix lines up with the seeded rows. No
    session and no auth dependency — it reads a module constant."""
    return [
        PermissionOut(
            key=spec.key, feature=spec.feature, action=spec.action, description=spec.description,
            feature_label=_FEATURE_LABELS[spec.feature], honoured=spec.key not in UNHONOURED_PERMISSION_KEYS,
        )
        for spec in PERMISSION_CATALOG
    ]
