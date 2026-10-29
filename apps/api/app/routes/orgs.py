"""Org CRUD: `/api/orgs`, the operator's surface for the tenancy axis itself (Customers).

`app/models/orgs.py::Org` owns what an org is; `app/tenancy/ids.py` owns the operator org's row. This is the
write path into `orgs`, and `_validated_slug` is its slug control.

Every route, reads included, is `@superadmin_only(Tenancy.OPERATOR)` but five (access-model C6): the
PATCH, `@org_admin(Tenancy.CALLER_PLANE)` so an Org Owner/Admin may set THEIR OWN org's `default_role`
and nothing else (every other field stays an operator's, `_OPERATOR_FIELDS`), and the read of an org's
email domains. Adding or removing a domain is an operator's (D4: no DNS proof, so the operator is the
proof). Access: creating an org creates a tenant, and a grantable `orgs:*` feature key would let an
org-admin create sibling tenants (a tenancy escape), so the gate is the fleet-global flag. Tenancy: the
subject is the fleet, and `orgs` is the one `Axis` class in `app/tenancy/manifest.py` (no `org_id`), so
the two org-admin routes check the org is the caller's own themselves (`administered_org_or_404`). The verdicts are
recorded in `test_authz_policy.py::_OPERATOR_PLANE_ALLOWLIST` / `_ORG_ADMIN_ALLOWLIST`.
The third is `POST /api/orgs/self-create`, `@always(Tenancy.BOOTSTRAP)`: any signed-in person applies for
a NEW org through the self-service sign-up's own admission (`app/signup.py::create_signup`) — born
`pending`, so an operator's approval, not this route, is what admits the tenant.
The last two are the Owner's Delete account on Billing (`GET`/`POST /api/org/delete`,
`@org_admin(Tenancy.CALLER_PLANE)`): the caller's own org only, its Owner only, the org's name typed back and a 2FA
code when the Owner has one — the same soft delete as `DELETE`.

Lifecycle: `PATCH {enabled: false}` stops new dispatch and deletes nothing; `DELETE` is a soft delete of any
tenant org — it stops what the org runs and hides it, rows kept; Restore lists it again, and Compliance
Delete is the only purge, 2FA-authorized. `app/tenancy/org_lifecycle.py` owns the wind-down and the
purge, and `app/tenancy/org_delete.py`'s trigger is the floor; this module is only the wire (`restrict_violation`
-> 409).
`enabled` gates dispatch (`app/pool_state.py`) and the domain join (`access.org_for_email_domain`),
but not an existing user's login (see `Org`).

Approval: a self-service org is born `pending`; `approve`/`reject` are its only exits and the enable
PATCH re-admits a rejected one (the section above the routes names the three edges). A rejected org IS
refused at login, at every sign-in door (`app/org_gate.py::refuse_unapproved_sign_in`); a merely disabled
one is not.

Org zero, the operator plane, may be renamed but never disabled, deleted or re-slugged; every other org is
an ordinary tenant. A tenant org's slug is an operator's to rename
(`_rename_slug`): it is the org's tailnet tag outside the database, so a rename moves its devices to the new tag
durably (`app/durable/org_retag.py`). Org zero's slug never changes. An operator may also
converge a tenant org's existing devices onto its tags (`retag_org_tailnet`).
"""
from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, RootModel, StrictInt
from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import (
    access, cron_min_period, edition_features, hipaa, ce_caps, mfa, node_bus, org_limits,
    org_quotas, org_slug, org_welcome, plan_terms, provider_auth_policy, staging_org,
)
from app.operator_settings import (
    cache as settings_cache,
    defaults as settings_defaults,
    errors as settings_errors,
    writes as settings_writes,
)
from app.access_admin import revoke_direct_grants_retired_epoch
from app.access_catalog import ROLES_BY_SLUG
from app.actors import actor_names, actor_names_for, stamp_actor
from app.auth import require_operator, require_superadmin, require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import always, org_admin, session_only, superadmin_only
from app.authz.principal import Principal, current_principal
from app.clock import as_utc, now_iso
from app.db import SessionLocal, get_session
from app.entity_shape import (
    EntityOut,
    StrictModel,
    administered_org_or_404,
    conflict_as_409,
    count_by,
    entity_fields,
    flush_or_409,
    get_or_404,
    iso_or_none,
    row_by_uuid_or_404,
    tenant_org_or_404,
    validated_description,
    validated_name,
)
from app.durable import notify_outbox
from app.events import emit_console_event
from app.list_page import Q_MAX, Page, PageQuery, like_pattern, page_query, sql_page
from app.microsoft_oauth import PERSONAL_TENANT_ID
from app.models.billing import BillingPlan
from app.models.identity import AdminUser, UserIdentity
from app.models.orgs import (
    ACCOUNT_ORGANIZATION,
    ORG_ACTIVE,
    ORG_PENDING,
    ORG_REJECTED,
    SSO_PROVIDERS,
    Org,
    OrgDomain,
    OrgSignup,
)
from app.models.support import SiteEnquiry
from app.org_gate import REASON_MAX
from app.org_slug import SLUG_UNIQUE
from app.user_activity import record_write
from app.routes.commands import _SLUG_RE, SLUG_RULE
from app.routes.settings import SettingKnobOut
from app.tenancy import offboard, seam
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS, ORG_ZERO_ID, is_tenant_org
from app.tenancy.labels import OrgRef
from app.tenancy.org_lifecycle import (
    ORG_DELETE_REFUSED_SQLSTATES,
    detach_outside_references,
    org_owned_attachment_keys,
    purge_org_rows,
    undetachable_references,
    wind_down,
)

# No `prefix=`: the public/private ALB split test reads each route's path off the decorator literal.
router = APIRouter(tags=["orgs"])

#: The enable PATCH's lifecycle rows in the customer history (`app/customer_history.py::CATEGORY_OF`).
ORG_ENABLED = "org_enabled"
ORG_DISABLED = "org_disabled"

#: A soft-deleted org changes only by Restore or Compliance Delete.
_DELETED_READ_ONLY = "'{name}' ({slug}) is deleted — restore it first."

#: Why the operator plane (`tenancy/ids.py::is_tenant_org` false) is never disabled; every tenant may be.
_OPERATOR_UNDISABLABLE = (
    "org zero is the operator plane — the fleet machinery, the operator staff and the system rows "
    "that run the control plane itself all belong to it, so disabling it would mark the control "
    "plane not-dispatchable to itself"
)



# ── The wire ──────────────────────────────────────────────────────────────────────────────────────


class WelcomeSendOut(BaseModel):
    """One welcome email attempt (`app/org_welcome.py::Sent`): when, who, and `org_welcome.Outcome`."""

    at: str
    by: Optional[str] = None
    outcome: str


class OrgSignupOut(BaseModel):
    """The self-service sign-up behind an org (`OrgSignup`), as the approval decision needs it. The two
    notification outcomes are served as words the page can render, never the HubSpot record id.
    `decided_*`/`rejection_reason` are the approve/reject decision, kept as history when a rejected org is
    re-admitted (read them with `lifecycle`). `account_type` is the org's (`Org.account_type`); the
    application's answers are NULL on a sign-up made before they were asked. `welcome_*` summarise the latest
    welcome email (`welcome_error` None = sent) and `welcome_sends` lists every one, newest first
    (`app/org_welcome.py`)."""

    provider: str
    email: str
    account_type: str = ACCOUNT_ORGANIZATION
    owner_name: Optional[str] = None
    occupation: Optional[str] = None
    job_title: Optional[str] = None
    intended_use: Optional[str] = None
    signed_up_at: Optional[str] = None
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    rejection_reason: Optional[str] = None
    slack_posted_at: Optional[str] = None
    slack_error: Optional[str] = None
    hubspot_synced: bool = False
    hubspot_error: Optional[str] = None
    welcome_sent_at: Optional[str] = None
    welcome_sent_by: Optional[str] = None
    welcome_error: Optional[str] = None
    welcome_sends: list[WelcomeSendOut] = []
    # The plan applied for (`org_signups.plan_key`; None = none chosen), by name.
    plan_key: Optional[str] = None
    plan_name: Optional[str] = None
    # A card saved at sign-up (`org_signups.payment_method_id`; false once rejected, which detaches it): approval
    # starts the plan on it (`org_approval.start_signup_billing`), and `billing_error` is why that failed, served
    # while the plan is still pending (None = it did not fail, or the owner has since started it).
    card_on_file: bool = False
    billing_error: Optional[str] = None


class OrgEnquiryOut(BaseModel):
    """The newest marketing-site enquiry sharing the sign-up's email (`SiteEnquiry`), read, never written."""

    company: str
    role: str
    note: str
    created_at: Optional[str] = None


class HipaaWarningOut(BaseModel):
    """One integration whose vendor has no BAA on file (`hipaa.warned_surfaces`), and who accepted it."""

    surface: str
    vendor: str
    acknowledged_at: Optional[str] = None
    acknowledged_by: Optional[str] = None


class HipaaOut(BaseModel):
    """An org's HIPAA mode: the operator's switch, its BAA date (`YYYY-MM-DD`), and the open warnings."""

    enabled: bool = False
    baa_effective_date: Optional[str] = None
    warnings: list[HipaaWarningOut] = Field(default_factory=list)


class HipaaAckIn(StrictModel):
    """An org admin's acknowledgement of one non-BAA surface (a `hipaa.SURFACES` key)."""

    surface: str = Field(max_length=64)


class OrgOut(EntityOut):
    """One org as the console renders it: the seven entity columns, the slug, `enabled`, and the two
    things an operator cannot type: whether the row is the operator plane, and how many users are bound to it.

    `is_operator_plane` (`tenancy/ids.py::is_tenant_org`, inverted) is served so the browser does not hardcode
    org zero's uuid, and so the UI can explain
    the refusal before the 409. `user_count` answers "may I disable this"; a count, never a roster.

    `lifecycle` is the approval state (`Org.lifecycle`); `signup` is present exactly when the org is
    self-service, and `enquiry` is the matching site enquiry, if any — both operator-plane reads.
    """

    slug: str
    enabled: bool
    is_operator_plane: bool
    user_count: int
    # The role every domain-joined user is bound to at org scope: a project role slug, or None (the default:
    # they reach nothing until bound).
    default_role: Optional[str] = None
    lifecycle: str = ORG_ACTIVE
    # A self-service org the system approved at sign-up (`app/org_approval.py::approve_at_signup`): active,
    # its sign-up decided by the system actor. False for every other org, and on an Org Admin's own answer.
    auto_approved: bool = False
    signup: Optional[OrgSignupOut] = None
    enquiry: Optional[OrgEnquiryOut] = None
    # A tenant org's plan and its effective end (`app/plan_terms.py::ends_of`, overrides included), for the
    # operator's roster: `plan_name` None = no plan (an operator org), `plan_ends_at` None = never ends.
    plan_name: Optional[str] = None
    plan_ends_at: Optional[str] = None
    # A coding agent can run for it (`org_limits.orgs_with_a_coder`): an enabled account, or on the Jaah account.
    # The operator's "act as an org that can" offer reads it; operator view only, else None (not read).
    has_coder: Optional[bool] = None
    # Soft delete (`Org.deleted_at`): when the operator deleted it; None = live. A deleted org is listed nowhere but
    # the Customers roster's Deleted filter, and is restored or Compliance-Deleted from its page.
    deleted_at: Optional[str] = None
    # SSO-only (`Org.sso_provider`): the IdP and tenant its members must sign in through, and who attested
    # that the IdP enforces MFA, when. All None for an org that is not SSO-only.
    sso_provider: Optional[str] = None
    sso_tenant: Optional[str] = None
    sso_mfa_attested_at: Optional[str] = None
    sso_mfa_attested_by: Optional[str] = None
    # The operator's isolation exception (`app/tenant_defaults.py`); False is a tenant's default.
    colocation_allowed: bool = False
    # The operator's two-factor exemption (`Org.mfa_skip`): its members sign in with no second factor.
    mfa_skip: bool = False
    # HIPAA mode (`app/hipaa.py`): `{enabled, baa_effective_date, warnings}`; `warnings` is empty while off.
    hipaa: Optional[HipaaOut] = None
    # What THIS request's welcome email did (`org_welcome.Outcome`): set only on the answer of approve and of
    # `POST /welcome-email`, None everywhere else and when approve was asked not to send.
    welcome_outcome: Optional[str] = None
    # MicroVM (architecture §4.9): whether the org may mark nodes MicroVM, `{value, source}` with source `plan`
    # or `override` (`plan_terms.microvm_allowed_term`; the operator sets the override on the Plan tab), and the
    # org's VM size: `{vcpus|ram_mib|disk_gib: {value, source}, override: {...}}` (org > system).
    microvm_allowed: Optional[dict[str, Any]] = None
    microvm_sizing: Optional[dict[str, Any]] = None
    # Portfolio Compliance (`governance/availability.py`) is on for the org: the operator's /compliance picker
    # offers these (`pages/compliance/RequireCompliance.tsx`). False on an Org Admin's own answer.
    compliance_enabled: bool = False
    # The org's console plan switches (`plan_terms.CONSOLE_SWITCHES`), operator view only: the act-as picker on a
    # plan-gated page (`lib/planSwitches.tsx::RequirePlanSwitch`) offers the orgs whose switch is on. Empty on an Org
    # Admin's own answer.
    plan_switches: dict[str, bool] = Field(default_factory=dict)
    # The org's provider grid cells (`provider_auth_policy.encoded`), operator view only: the act-as picker on the
    # Agents pages offers the orgs with an agent row on (`lib/providerGrid.ts::agentsOn`). Empty on an Org Admin's own
    # answer.
    providers: list[list[str]] = Field(default_factory=list)
    # The staging link (`app/staging_org.py`), operator view only: the parent a staging org follows, and a parent's
    # live staging org. None on the other side, on an ordinary org, and on an Org Admin's own answer.
    parent_org: Optional[OrgRef] = None
    staging_org: Optional[OrgRef] = None


class OrgApproveIn(BaseModel):
    """An approval. `send_welcome` (default on, as with no body at all) emails the owner the welcome. `plan_key`
    (any active plan) replaces the plan applied for; absent keeps it."""

    send_welcome: bool = True
    plan_key: Optional[str] = None


class OrgRejectIn(BaseModel):
    """A rejection. The reason is optional; when given it is shown to the org's members at sign-in."""

    reason: Optional[str] = None


class PendingCountOut(BaseModel):
    count: int


def _validated_slug(value: str) -> str:
    """The slug grammar `POST /api/commands` enforces, applied at the org's only write path.

    The grammar is imported, never restated (`routes/accounts.py::_validated_slug`); lowercase-only keeps
    `Operator` from sitting beside `operator` under a case-sensitive unique index.

    An `HTTPException`, not a pydantic `ValueError`: a validator's 422 carries a list `detail`, which
    `api.ts::apiDetail` cannot attribute to a field, so the operator would lose the message.
    """
    slug = (value or "").strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=422, detail=SLUG_RULE)
    return slug


class OrgCreateIn(BaseModel):
    """A new tenant. `slug` is required with no default; its grammar is checked in the handler
    (`_validated_slug` says why)."""

    slug: str
    name: str
    description: Optional[str] = None
    # Defaults to enabled, matching the column's `server_default`; absence is not a state here.
    enabled: bool = True
    # The org's plan: any active plan (`signup_plans.active_row`), never `(default)` — there is no default plan.
    plan_key: str



class OrgUpdateIn(BaseModel):
    """A PATCH: every field optional, and only the ones present are applied.

    `slug` is an operator's rename of a tenant org's handle (`_rename_slug`).
    """

    slug: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    # `null` clears it; one of `access.DEFAULT_ROLE_SLUGS` sets it. An Org Admin may send it.
    default_role: Optional[str] = None
    # SSO-only (`_apply_sso`): a provider in `SSO_PROVIDERS` with its tenant makes the org SSO-only; `null`
    # turns it off. `sso_mfa_attested` true records the caller's attestation that the IdP enforces MFA.
    # An Org Admin may send these for their own org.
    sso_provider: Optional[str] = None
    sso_tenant: Optional[str] = None
    sso_mfa_attested: Optional[bool] = None
    # The operator's isolation exception for a tenant org (`app/tenant_defaults.py`): a node on another
    # org's Multi-Node Host. Operator only; refused for an operator-plane org. NodeLink is a plan term
    # (`nodelink_allowed` on the plan routes), not an exception.
    colocation_allowed: Optional[bool] = None
    # Skip the second factor for every member (`_apply_mfa_skip`). Operator only; refused for an
    # operator-plane org.
    mfa_skip: Optional[bool] = None
    # HIPAA mode (`_apply_hipaa`). Operator only; turning it on needs a BAA effective date.
    hipaa_enabled: Optional[bool] = None
    hipaa_baa_effective_date: Optional[date] = None
    # The org's MicroVM size override (`app/microvm_sizing.py`; null clears one). An Org Admin may send these.
    microvm_vcpus: Optional[int] = None
    microvm_ram_mib: Optional[int] = None
    microvm_disk_gib: Optional[int] = None


#: The org's MicroVM size override (the `microvm_sizing.column`s), applied in `update_org`.
_MICROVM_SIZE_FIELDS = frozenset(("microvm_vcpus", "microvm_ram_mib", "microvm_disk_gib"))
#: The operator's isolation exceptions, applied in `update_org`.
_EXCEPTION_FIELDS = ("colocation_allowed",)
#: The PATCH fields only an operator may change: renaming or disabling a tenant is fleet administration,
#: and so is widening its isolation.
_OPERATOR_FIELDS = frozenset({"slug", "name", "description", "enabled", "mfa_skip", *_EXCEPTION_FIELDS})
_SSO_FIELDS = frozenset({"sso_provider", "sso_tenant", "sso_mfa_attested"})
#: HIPAA mode: the operator's alone, refused to anyone else with its own reason (`update_org`).
_HIPAA_FIELDS = frozenset({"hipaa_enabled", "hipaa_baa_effective_date"})
#: The refusals `_apply_hipaa` and `update_org` give, spelled once for the tests and the console.
HIPAA_NEEDS_DATE = "Enter the BAA effective date to turn on HIPAA."
HIPAA_OPERATOR_ONLY = "Only Jaah support can change HIPAA."
HIPAA_OFF = "HIPAA is off for this organization, so there is no warning to acknowledge."
_ENTRA_TENANT_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_WORKSPACE_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class OrgDomainIn(BaseModel):
    domain: str


class OrgDomainOut(BaseModel):
    domain: str
    created_at: Optional[str] = None
    created_by: Optional[str] = None


# ── Serialization ─────────────────────────────────────────────────────────────────────────────────


def _user_counts(session: Session, org_ids: list[UUID]) -> dict[UUID, int]:
    """How many `admin_users` each org has, for many orgs (`count_by`; an org with no users is absent).
    Unscoped on the operator plane; an Org Admin's PATCH reads under their own org, the only org it serves
    (`administered_org_or_404`).

    Flip-time consumer: `AdminUser` is `OrgScoped`, and if the enforcing seam binds the caller's own org
    this reads 0 for every other org, silently wrong on the number read before disabling a tenant.
    Whoever flips the seam must read this in an explicit operator scope or drop the column.
    """
    return count_by(session, AdminUser.org_id, org_ids)


def _signups(session: Session, org_ids: list[UUID]) -> dict[UUID, OrgSignup]:
    """The sign-up row of each self-service org among `org_ids`, in one query."""
    if not org_ids:
        return {}
    return {row.org_id: row for row in session.scalars(select(OrgSignup).where(OrgSignup.org_id.in_(org_ids)))}


def _enquiries(session: Session, emails: set[str]) -> dict[str, SiteEnquiry]:
    """The newest site enquiry per lowercased email, in one query. A read-time join on the email alone
    (nothing links an enquiry to a sign-up); `site_enquiries` is never written here."""
    if not emails:
        return {}
    newest: dict[str, SiteEnquiry] = {}
    rows = session.scalars(
        select(SiteEnquiry)
        .where(func.lower(SiteEnquiry.email).in_(emails))
        .order_by(SiteEnquiry.received_at.desc())
    )
    for row in rows:
        newest.setdefault(row.email.lower(), row)
    return newest


def _signup_out(
    row: Org, signup: OrgSignup, names: dict[UUID, str], welcomes: list[org_welcome.Sent], plans: dict[str, str]
) -> OrgSignupOut:
    return OrgSignupOut(
        plan_key=signup.plan_key,
        plan_name=plans.get(signup.plan_key, signup.plan_key) if signup.plan_key else None,
        provider=signup.provider,
        email=signup.email,
        account_type=row.account_type or ACCOUNT_ORGANIZATION,
        owner_name=names.get(signup.owner_id),
        occupation=signup.occupation,
        job_title=signup.job_title,
        intended_use=signup.intended_use,
        signed_up_at=iso_or_none(signup.signed_up_at),
        decided_at=iso_or_none(signup.decided_at),
        decided_by=names.get(signup.decided_by_id),
        rejection_reason=signup.rejection_reason,
        slack_posted_at=iso_or_none(signup.slack_posted_at),
        slack_error=signup.slack_error,
        hubspot_synced=signup.hubspot_contact_id is not None,
        hubspot_error=signup.hubspot_error,
        welcome_sent_at=iso_or_none(signup.welcome_sent_at),
        welcome_sent_by=names.get(signup.welcome_sent_by_id),
        welcome_error=signup.welcome_error,
        # A rejected org's card is detached; an error is moot once the plan has started.
        card_on_file=signup.payment_method_id is not None and row.lifecycle != ORG_REJECTED,
        billing_error=signup.billing_error if row.pending_plan_key else None,
        welcome_sends=[
            WelcomeSendOut(
                at=iso_or_none(sent.at) or "", by=names.get(sent.by_id) or sent.by_label, outcome=sent.outcome
            )
            for sent in welcomes
        ],
    )


def _enquiry_out(row: Optional[SiteEnquiry]) -> Optional[OrgEnquiryOut]:
    if row is None:
        return None
    return OrgEnquiryOut(company=row.company, role=row.role, note=row.note, created_at=iso_or_none(row.received_at))


def _microvm_sizing_out(row: Org) -> Optional[dict[str, Any]]:
    """The org's VM size (`microvm_sizing.sizing_out`); None where MicroVM is not shipped (the community edition)."""
    if not edition_features.enabled("microvm"):
        return None
    from app import microvm_sizing

    return microvm_sizing.sizing_out(None, None, row, own=row)


def _rows_out(session: Session, rows: list[Org], *, operator_view: bool = True) -> list[OrgOut]:
    """Eight queries for the whole page whatever the row count: user counts, actor names, sign-ups, the
    people those name, enquiries, welcome emails, plan names, and the SSO attesters. `signup`/`enquiry` are the operator's (a lead and the notification
    outcomes are not the tenant's business), so an Org Admin's PATCH answer carries neither."""
    if not rows:
        return []
    counts = _user_counts(session, [row.id for row in rows])
    names = actor_names_for(session, rows)
    signups = _signups(session, [row.id for row in rows]) if operator_view else {}
    welcomes = org_welcome.sends(session, signups.values())
    people = actor_names(
        session,
        [
            pid for s in signups.values() for pid in (s.owner_id, s.decided_by_id, s.welcome_sent_by_id)
            if pid is not None
        ] + [sent.by_id for sends in welcomes.values() for sent in sends if sent.by_id is not None],
    )
    enquiries = _enquiries(session, {s.email.lower() for s in signups.values()})
    plans = dict(session.execute(select(BillingPlan.key, BillingPlan.name)).tuples().all()) if signups else {}
    attesters = actor_names(session, [row.sso_mfa_attested_by_id for row in rows if row.sso_mfa_attested_by_id])
    plan_rows = _plans_by_key(session)
    ends = _plan_ends(session, rows, plan_rows) if operator_view else {}
    switches = _switch_terms(session, rows, plan_rows)
    hipaa_out = _hipaa_out(session, rows)
    parents, stagings = _staging_links(session, rows) if operator_view else ({}, {})
    dflt = plan_terms.defaults(session) if operator_view else None
    coders = org_limits.orgs_with_a_coder(session, {row.id for row in rows}) if operator_view else None
    auto_approved: dict[UUID, bool] = {}
    # The community edition has no sign-up approval.
    if edition_features.enabled("signup"):
        from app import org_approval

        auto_approved = {row.id: org_approval.is_auto_approved(row, signups.get(row.id)) for row in rows}
    return [
        OrgOut(
            **entity_fields(row, names),
            slug=row.slug,
            enabled=bool(row.enabled),
            is_operator_plane=not is_tenant_org(row.id),
            user_count=counts.get(row.id, 0),
            default_role=_default_role_slug(row),
            lifecycle=row.lifecycle,
            auto_approved=auto_approved.get(row.id, False),
            signup=(
                _signup_out(row, signups[row.id], people, welcomes.get(row.id, []), plans) if row.id in signups else None
            ),
            enquiry=_enquiry_out(enquiries.get(signups[row.id].email.lower())) if row.id in signups else None,
            plan_name=ends[row.id][0] if row.id in ends else None,
            plan_ends_at=ends[row.id][1] if row.id in ends else None,
            has_coder=row.id in coders if coders is not None else None,
            deleted_at=iso_or_none(row.deleted_at),
            sso_provider=row.sso_provider,
            sso_tenant=row.sso_tenant,
            sso_mfa_attested_at=iso_or_none(row.sso_mfa_attested_at),
            sso_mfa_attested_by=attesters.get(row.sso_mfa_attested_by_id),
            colocation_allowed=bool(row.colocation_allowed),
            mfa_skip=bool(row.mfa_skip),
            hipaa=hipaa_out[row.id],
            microvm_allowed={
                "value": switches[row.id]["microvm_allowed"].value, "source": switches[row.id]["microvm_allowed"].source,
            },
            microvm_sizing=_microvm_sizing_out(row),
            compliance_enabled=operator_view and switches[row.id]["compliance_enabled"].value,
            plan_switches=(
                {
                    **{name: switches[row.id][name].value for name in plan_terms.CONSOLE_SWITCHES},
                    plan_terms.GITHUB_RUNNERS: _runners_on(session, row, plan_rows),
                } if operator_view else {}
            ),
            providers=(
                provider_auth_policy.encoded(_grid(session, row, plan_rows, dflt)) if operator_view else []
            ),
            parent_org=parents.get(row.parent_org_id) if row.parent_org_id is not None else None,
            staging_org=stagings.get(row.id),
        )
        for row in rows
    ]


def _staging_links(session: Session, rows: list[Org]) -> tuple[dict[UUID, OrgRef], dict[UUID, OrgRef]]:
    """`({parent id: ref}, {parent id: its live staging org's ref})` for `rows`, in one query."""
    ids = [row.id for row in rows]
    parent_ids = [row.parent_org_id for row in rows if row.parent_org_id is not None]
    related = session.scalars(
        select(Org).where(or_(Org.id.in_(parent_ids), and_(Org.parent_org_id.in_(ids), Org.deleted_at.is_(None))))
    ).all()
    parents: dict[UUID, OrgRef] = {}
    stagings: dict[UUID, OrgRef] = {}
    for org in related:
        ref = OrgRef(id=org.id, slug=org.slug, name=org.name)
        if org.id in parent_ids:
            parents[org.id] = ref
        if org.parent_org_id in ids and org.deleted_at is None:
            stagings[org.parent_org_id] = ref
    return parents, stagings


def _hipaa_out(session: Session, rows: list[Org]) -> dict[UUID, HipaaOut]:
    """Each org's HIPAA mode. The warned surfaces are read once, and the acknowledgers' names in one query."""
    on = [row for row in rows if hipaa.is_on(row)]
    warned = hipaa.warned_surfaces(session) if on else {}
    names = actor_names(session, [by for row in on for by in map(_ack_by, (row.hipaa_acks or {}).values()) if by])
    out: dict[UUID, HipaaOut] = {}
    for row in rows:
        acks = row.hipaa_acks or {}
        warnings = []
        for surface, vendor in (warned.items() if hipaa.is_on(row) else ()):
            ack = acks.get(surface)
            by = _ack_by(ack)
            warnings.append(HipaaWarningOut(
                surface=surface,
                vendor=vendor,
                acknowledged_at=ack.get("at") if isinstance(ack, dict) else None,
                acknowledged_by=names.get(by) if by else None,
            ))
        out[row.id] = HipaaOut(
            enabled=bool(row.hipaa_enabled),
            baa_effective_date=iso_or_none(row.hipaa_baa_effective_date),
            warnings=warnings,
        )
    return out


def _ack_by(ack: Any) -> Optional[UUID]:
    """Who acknowledged a warning (`Org.hipaa_acks`), or None for a malformed entry."""
    try:
        return UUID(str(ack["by"]))
    except (TypeError, KeyError, ValueError):
        return None


def _plans_by_key(session: Session) -> dict[str, BillingPlan]:
    """Every plan row by key, read once for a page of orgs (a key no row has reads as the default plan,
    `plan_terms.plan_row`'s rule, at the caller)."""
    return {plan.key: plan for plan in session.scalars(select(BillingPlan))}


def _switch_terms(
    session: Session, rows: list[Org], plans: dict[str, BillingPlan],
) -> dict[UUID, dict[str, plan_terms.Term[bool]]]:
    """Each org's MicroVM and Portfolio Compliance switches and its console plan switches
    (`plan_terms.CONSOLE_SWITCHES`), over the page's plan rows (`_plans_by_key`), each org's plan chain walked once:
    what an operator's org pickers offer (/compliance, a plan-gated page)."""
    names = (*plan_terms.CONSOLE_SWITCHES, "compliance_enabled")
    return {row.id: plan_terms.org_switch_terms(session, row, names, plans.get(row.plan_key)) for row in rows}


def _grid(
    session: Session, row: Org, plans: dict[str, BillingPlan], dflt: Optional[BillingPlan],
) -> frozenset[provider_auth_policy.Cell]:
    """The org's provider grid cells (`plan_terms.providers_term`) over the page's plan rows: no read per org."""
    org = plan_terms.terms_org(session, row)
    return plan_terms.providers_term(org, plans.get(org.plan_key) or dflt, dflt).value


def _runners_on(session: Session, row: Org, plans: dict[str, BillingPlan]) -> bool:
    """The org's GitHub Runners flag (`plan_terms.GITHUB_RUNNERS`, its cap not 0), over the page's plan rows."""
    org = plan_terms.terms_org(session, row)
    plan = plans.get(org.plan_key) or plan_terms.plan_row(session, org.plan_key)
    return plan_terms.caps_of(org, plan)["github_runners"].value != 0


def _plan_ends(
    session: Session, rows: list[Org], plans: dict[str, BillingPlan],
) -> dict[UUID, tuple[str, Optional[str]]]:
    """Each tenant org's plan name and effective end, the plan rows read once for the page (`plan_terms.plan_row`'s
    rule: a key no row has reads as the default plan)."""
    fallback: list[Optional[BillingPlan]] = []  # the default plan's row, read once, for a key no row has
    out: dict[UUID, tuple[str, Optional[str]]] = {}
    for row in rows:
        if not is_tenant_org(row.id):
            continue
        plan = plans.get(row.plan_key)
        if plan is None:
            if not fallback:
                fallback.append(plan_terms.plan_row(session, row.plan_key))
            plan = fallback[0]
        end = plan_terms.ends_of(row, plan).value
        ends_at = as_utc(end).isoformat() if end is not None else None
        out[row.id] = (plan.name if plan is not None else row.plan_key, ends_at)
    return out


def _default_role_slug(row: Org) -> Optional[str]:
    role = access.ROLES_BY_ID.get(row.default_role_id) if row.default_role_id else None
    return role.slug if role is not None else None


def _one(session: Session, org_id: str) -> Org:
    return row_by_uuid_or_404(session, Org, org_id, detail="no such org")


def _live(row: Org) -> Org:
    """`row`, unless it is soft-deleted: a deleted org changes only by Restore or Compliance Delete, so
    every other write route refuses it (409) — a plan push would bill a customer whose billing the delete ended."""
    if row.deleted_at is not None:
        raise HTTPException(status_code=409, detail=_DELETED_READ_ONLY.format(name=row.name, slug=row.slug))
    return row


def _live_one(session: Session, org_id: str) -> Org:
    return _live(_one(session, org_id))


# ── The routes ────────────────────────────────────────────────────────────────────────────────────


#: `?ids=` resolves the labels of picked values, never a page of the roster: as wide as one page can be.
_IDS_MAX = 200
_ORG_SORTS = {"name": func.lower(Org.name), "slug": Org.slug, "created": Org.created_at}


def _org_ids(ids: Optional[str]) -> Optional[list[UUID]]:
    """`?ids=a,b,c` as uuids; a malformed id or more than `_IDS_MAX` is a 422, never a silent partial answer."""
    if ids is None:
        return None
    parts = [part.strip() for part in ids.split(",") if part.strip()]
    if len(parts) > _IDS_MAX:
        raise HTTPException(status_code=422, detail=f"at most {_IDS_MAX} ids")
    try:
        return [UUID(part) for part in parts]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated uuids") from None


def _not_a_draft():
    """SQL: the org is no unsubmitted sign-up draft's (`signup.not_a_draft`); an edition without sign-up has none."""
    if edition_features.enabled("signup"):
        from app import signup

        return signup.not_a_draft()
    return true()


@router.get("/api/orgs", response_model=Page[OrgOut])
@superadmin_only(Tenancy.OPERATOR)
def list_orgs(
    q: str = Query("", max_length=Q_MAX),
    ids: Optional[str] = Query(None, max_length=_IDS_MAX * 40),
    query: PageQuery = Depends(page_query(tuple(_ORG_SORTS))),
    _: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> Page[OrgOut]:
    """Every live org, a page at a time: those awaiting approval first, then the operator plane, then
    alphabetically by name. A soft-deleted org is listed only by the Customers roster's Deleted filter
    (`routes/customer_roster.py`). `q` matches name or slug; `ids` narrows to exactly those live orgs.

    Not newest-first: the list is scanned by eye for a named tenant; the operator plane is the landmark.
    """
    # An unsubmitted sign-up draft is nobody's application yet (`app/signup_draft.py`).
    stmt = select(Org).where(Org.deleted_at.is_(None), _not_a_draft())
    if q.strip():
        needle = like_pattern(q)
        stmt = stmt.where(or_(Org.name.ilike(needle, escape="\\"), Org.slug.ilike(needle, escape="\\")))
    wanted = _org_ids(ids)
    if wanted is not None:
        stmt = stmt.where(Org.id.in_(wanted))
    pending = Org.lifecycle == ORG_PENDING
    rows, total = sql_page(session, stmt, query, sorts=_ORG_SORTS, default=[
        # Orgs awaiting approval first (the decision the operator owes), oldest-created first among them.
        case((pending, 0), else_=1), case((pending, Org.created_at)).asc().nullsfirst(),
        case((Org.id.in_(tuple(OPERATOR_PLANE_ORG_IDS)), 0), else_=1), func.lower(Org.name), Org.slug,
    ], tie=Org.id)
    return Page[OrgOut](items=_rows_out(session, rows), total=total, page=query.page, per=query.per)


@router.post("/api/orgs", response_model=OrgOut, status_code=201)
@superadmin_only(Tenancy.OPERATOR)
def create_org(
    body: OrgCreateIn,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> OrgOut:
    """File a new tenant.

    The slug is checked before the insert so the ordinary duplicate gets an actionable 409 instead of a
    flush-time 500; the constraint stays the authority (`app/org_create.py::create_org_row`).

    Creates nothing else: no admin, nodes or worktrees; those are separate gated decisions.

    `plan_key` must be an active plan (422 otherwise); the self-serve rules (`app/self_serve_rules.py`) do not bind
    an operator. An edition without multi-org tenancy has its one org only (404).
    """
    if not edition_features.enabled("multi_org_tenancy"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import signup_plans
    from app.org_create import create_org_row

    if body.plan_key == plan_terms.DEFAULTS_KEY or signup_plans.active_row(session, body.plan_key) is None:
        raise HTTPException(status_code=422, detail="Choose one of the plans in Products › Plans.")
    slug = _validated_slug(body.slug)
    name = validated_name(body.name)
    description = validated_description(body.description)
    taken = session.scalar(select(Org.id).where(Org.slug == slug))
    if taken is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"an org already answers to the slug '{slug}' — the handle is the stable external "
                "name and cannot be shared, so pick another one"
            ),
        )
    # The shared core (Everyone group, console event, the race's 409); an operator-created org is unlimited.
    row = create_org_row(
        session, slug=slug, name=name, description=description, enabled=body.enabled, actor=user,
        self_service=False, plan_key=body.plan_key,
    )
    # The customer's History opens with its creation (`app/customer_history.py`), as the operator who filed it.
    record_write(session, user, "org_created", f"Created the organization {name}",
                 data={"org_id": row.id, "slug": slug})
    session.commit()
    session.refresh(row)
    return _rows_out(session, [row])[0]


class SelfCreateIn(BaseModel):
    """The sign-up application's answers minus the person (`signup.application_from`): the names
    default to the signed-in person's own, and are asked only of a person who has none on record (an
    invited, email-only account) — they name the applicant on this application, never the person. Every
    answer typed `Any`, so `signup.application_from` names each problem beside its field."""

    account_type: Any = None
    first_name: Any = None
    last_name: Any = None
    occupation: Any = None
    job_title: Any = None
    org_name: Any = None
    intended_use: Any = None
    accept_terms: Any = None
    legal_version: Any = None
    plan_key: Any = None


class SelfCreateOut(BaseModel):
    org_id: UUID
    state: str


@router.post("/api/orgs/self-create", response_model=SelfCreateOut, status_code=201)
@always(Tenancy.BOOTSTRAP)
@session_only
def self_create_org(
    body: SelfCreateIn,
    request: Request,
    admin: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> SelfCreateOut:
    """A signed-in person applies for ANOTHER org (account menu > Create organization; R6). The same
    admission as the self-service sign-up (`signup.create_signup`'s identity path): the org lands
    `pending` with the caller's person as its Org Owner — a new membership, never `is_superadmin` — and an
    operator approves it (`approve` below). Until then it lists as `pending_approval` in `/api/auth/me`'s
    `memberships` and cannot be acted as. Approved automatically only by `signup_auto_approve_all`
    (`org_approval.approve_at_signup`, never by domain): then `state` is `active` and the welcome follows.

    `BOOTSTRAP` like `/api/auth/me`: nothing is bound, the person is read under the identity escape, and
    the new org's rows are written under `seam.bound_to` the org it creates — the caller's current org is
    never read or written.

    No applicant email: the caller is signed in and sees the org as pending in their own list.

    `plan_key` is required: one the sign-up offers (`signup_plans.offered`), kept for approval as a sign-up's is.

    Refusals: 403 `signup_closed`; 422 `invalid_fields` (`plan_key` among them); 409 `legal_version_changed`; 409
    `self_serve_rules.SECOND_FREE` for a second Free org of the person's email; 409 `signup_pending` while an org of
    this person still awaits review; 409 `signup_conflict` when the create lost a race."""
    # The community edition has no sign-up.
    if not edition_features.enabled("signup"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import identities, org_approval, self_serve_rules, signup, signup_plans
    from app.routes import signup as signup_routes

    signup.require_open()
    with seam.identity_scope():
        identity = session.get(UserIdentity, admin.identity_id) if admin.identity_id is not None else None
    if identity is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    answers = {
        **body.model_dump(),
        "first_name": identity.first_name or body.first_name,
        "last_name": identity.last_name or body.last_name,
    }
    try:
        application = signup.application_from(answers)
    except signup.ApplicationInvalid as invalid:
        raise signup_routes.invalid_fields(invalid.problems) from None
    plan = signup_plans.offered_plan(session, body.plan_key if isinstance(body.plan_key, str) else None)
    if plan is None:
        raise signup_routes.invalid_fields([signup.FieldProblem("plan_key", "Choose one of the plans shown.")])
    acceptance = signup_routes.acceptance_of(request, body.legal_version)
    # The slug first: its LLM call (`signup.abbreviation`, bounded but slow) must not run under the email lock, which
    # would hold every other self-create of this email for its length.
    slug_base = signup.abbreviation(application.org_name)
    # Held from the Free check to `create_signup`'s commit of the org (`self_serve_rules.lock_email`).
    self_serve_rules.lock_email(session, identity.email)
    self_serve_rules.refuse_second_free(session, identity.email, plan.key)
    owner = signup.create_signup(
        session, None, application, acceptance, identity=identity, plan_key=plan.key, slug_base=slug_base,
    )
    approved = org_approval.approve_at_signup(owner.org_id, identity.email, by_domain=False)
    signup_routes.notify_operators(owner, application, identities.identity_provider(identity), approved=approved)
    if not approved:
        return SelfCreateOut(org_id=owner.org_id, state="pending_approval")
    notify_outbox.after_answer("signup_approved", str(owner.org_id))
    return SelfCreateOut(org_id=owner.org_id, state="active")


class SlugSuggestionOut(BaseModel):
    slug: str


@router.get("/api/orgs/slug-suggestion", response_model=SlugSuggestionOut)
@superadmin_only(Tenancy.OPERATOR)
def suggest_org_slug(
    name: str,
    _: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> SlugSuggestionOut:
    """A free generated slug for an org called `name` (`app/org_slug.py`, the sign-up's own), for the Create
    dialog to pre-fill. Declared before `/api/orgs/{org_id}`, which would otherwise claim the path."""
    return SlugSuggestionOut(slug=org_slug.generate_slug(session, validated_name(name)))


@router.get("/api/orgs/pending-count", response_model=PendingCountOut)
@superadmin_only(Tenancy.OPERATOR)
def pending_org_count(
    _: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> PendingCountOut:
    """How many orgs await approval — the rail's badge, polled. Declared before `/api/orgs/{org_id}`,
    which would otherwise claim the path."""
    return PendingCountOut(
        count=session.scalar(
            select(func.count()).select_from(Org).where(
                Org.lifecycle == ORG_PENDING, Org.deleted_at.is_(None), _not_a_draft(),
            )
        ) or 0
    )


@router.get("/api/orgs/{org_id}", response_model=OrgOut)
@org_admin(Tenancy.CALLER_PLANE)
def get_org(
    org_id: str,
    _: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> OrgOut:
    """One org, in the same shape the list serves it: an operator any org, an Org Owner/Admin their own
    (`administered_org_or_404` — another org is the same 404 as none), in the same view `update_org` answers them
    (no `signup`/`enquiry`: those are the operator's)."""
    row = administered_org_or_404(session, org_id, principal)
    return _rows_out(session, [row], operator_view=principal is not None and principal.is_operator)[0]


@router.patch("/api/orgs/{org_id}", response_model=OrgOut)
@org_admin(Tenancy.CALLER_PLANE)
def update_org(
    org_id: str,
    body: OrgUpdateIn,
    user: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> OrgOut:
    """Rename an org, retitle its description, disable/re-enable it, or set its default role.

    A PATCH because the form and the toggle are different controls; `model_fields_set` distinguishes
    "not sent" from "sent as null".

    An Org Owner/Admin reaches their own org only, and only `default_role`, the SSO fields and the MicroVM size
    (403 for any other field, before any mutation); an operator any org and every field. Seeded rows may be renamed but not
    disabled, refused before any mutation so a combined body changes nothing. Re-enabling is not guarded.
    """
    row = _live(administered_org_or_404(session, org_id, principal))
    fields = body.model_fields_set
    operator = principal is not None and principal.is_operator
    if ("enabled" in fields or fields & _HIPAA_FIELDS) and operator:
        # Locked and re-read (HIPAA too: its date-while-on rule is checked against this row), as approve/reject lock it: an enable racing a reject would otherwise write
        # `enabled=true` over a freshly rejected org without re-admitting it.
        session.get(Org, row.id, with_for_update=True, populate_existing=True)
    if fields & _HIPAA_FIELDS and not operator:
        raise HTTPException(status_code=403, detail=HIPAA_OPERATOR_ONLY)
    if fields & _OPERATOR_FIELDS and not operator:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only an operator can rename, retitle or disable an organization, or change its isolation "
                "or two-factor setting."
            ),
        )
    if fields & set(_EXCEPTION_FIELDS) and not is_tenant_org(row.id):
        raise HTTPException(
            status_code=422,
            detail="The operator's own organizations are not isolated, so there is no exception to record.",
        )
    if "mfa_skip" in fields and not is_tenant_org(row.id):
        raise HTTPException(status_code=422, detail="The operator's own organizations always require two-factor.")
    hipaa_after = _hipaa_after(row, body, fields)
    new_default_role = row.default_role_id
    if "default_role" in fields:
        if body.default_role is not None and body.default_role not in access.DEFAULT_ROLE_SLUGS:
            raise HTTPException(
                status_code=422,
                detail="The default role is none, Project Viewer or Project Developer.",
            )
        new_default_role = ROLES_BY_SLUG[body.default_role].id if body.default_role else None

    if "enabled" in fields and body.enabled is False and not is_tenant_org(row.id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{row.name}' ({row.slug}) cannot be disabled — {_OPERATOR_UNDISABLABLE}. Rename it if the "
                "label is wrong; there is no supported way to take it out of service."
            ),
        )

    renamed: Optional[tuple[str, list[str]]] = None
    if "slug" in fields:
        renamed = _rename_slug(session, row, body.slug, user)

    changed = renamed is not None
    if "name" in fields:
        name = validated_name(body.name or "")
        changed = changed or name != row.name
        row.name = name
    if "description" in fields:
        description = validated_description(body.description)
        changed = changed or description != row.description
        row.description = description
    if "enabled" in fields and body.enabled is not None:
        if bool(body.enabled) != bool(row.enabled):
            changed = True
            # A lifecycle row in the customer history (`app/customer_history.py::CATEGORY_OF`).
            record_write(
                session, user, ORG_ENABLED if body.enabled else ORG_DISABLED,
                f"{'Enabled' if body.enabled else 'Disabled'} org {row.name}",
                data={"org_id": str(row.id)},
            )
        row.enabled = body.enabled
        if body.enabled and row.lifecycle == ORG_REJECTED:
            # Re-enabling a rejected org is the operator re-admitting it: rejected -> active, the one way
            # out of `rejected`. Its members sign in again; nothing else of theirs was removed.
            row.lifecycle = ORG_ACTIVE
            changed = True
            record_write(
                session, user, "org_readmitted", f"Re-admitted org {row.name} after its rejection",
                data={"org_id": str(row.id)},
            )
    if new_default_role != row.default_role_id:
        changed = True
        before = _default_role_slug(row)
        row.default_role_id = new_default_role
        # Load-bearing audit: this decides what every future domain sign-in is granted.
        record_write(
            session, user, "org_default_role",
            f"Set the default role of org {row.name} to {body.default_role or 'none'}",
            data={"org_id": str(row.id), "from": before, "to": body.default_role},
        )

    for field in _EXCEPTION_FIELDS:
        value = getattr(body, field)
        if field in fields and value is not None and bool(value) != bool(getattr(row, field)):
            # Audited by request capture (the PATCH body names the field and its new value) and stamped
            # below with `updated_by`; the risk register lists every tenant org that holds one.
            setattr(row, field, bool(value))
            changed = True

    if fields & _MICROVM_SIZE_FIELDS:
        # The community edition has no MicroVMs: a resent value is no change, a changed one is not in it.
        if not edition_features.enabled("microvm") and any(
            getattr(body, field) != getattr(row, field) for field in fields & _MICROVM_SIZE_FIELDS
        ):
            raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
        if edition_features.enabled("microvm"):
            from app import microvm_sizing

            try:
                sizes = microvm_sizing.validated(
                    {field: getattr(body, field) for field in fields & _MICROVM_SIZE_FIELDS})
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            for field, value in sizes.items():
                if getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True

    retired: list[tuple[UUID, int]] = []
    if fields & _SSO_FIELDS:
        sso_changed, retired = _apply_sso(session, row, body, user)
        changed = changed or sso_changed
    if "mfa_skip" in fields and body.mfa_skip is not None and body.mfa_skip != bool(row.mfa_skip):
        retired += _apply_mfa_skip(session, row, body.mfa_skip, user)
        changed = True
    hipaa_changed, hipaa_retired = _apply_hipaa(session, row, hipaa_after, user, frozenset(m for m, _ in retired))
    if hipaa_changed:
        retired += hipaa_retired
        changed = True

    if not changed:
        # A no-op PATCH writes nothing, so `updated_by`/`updated_at` keep meaning "who last changed this"
        # (`routes/groups.py`'s rule) and no open tab is notified.
        return _rows_out(session, [row], operator_view=operator)[0]

    stamp_actor(row, user)
    # A slug taken between `_rename_slug`'s read and here is the ordinary duplicate's 409, not a 500.
    flush_or_409(session, _SLUG_TAKEN, constraints=frozenset({SLUG_UNIQUE}))
    # Same event and `PROBED_ENTITIES` requirement as the create path.
    emit_console_event(session, entity="org", id=row.id, action="upserted")
    session.commit()
    # After the commit, as `reject_org` does: a node acting on a rolled-back change would sign out a live console.
    for member_id, retired_epoch in retired:
        revoke_direct_grants_retired_epoch(member_id, retired_epoch)
    session.refresh(row)
    # The community edition has no tailnet to retag.
    if edition_features.enabled("tailnet"):
        from app.durable import org_retag

        if renamed is not None:
            old_slug, device_ids = renamed
            org_retag.enqueue_retag(row.id, old_slug, row.slug, device_ids)
    return _rows_out(session, [row], operator_view=operator)[0]


_SLUG_TAKEN = "Another organization already has that slug."


def _rename_slug(
    session: Session, row: Org, value: Optional[str], user: AdminUser
) -> Optional[tuple[str, list[str]]]:
    """Rename `row`'s slug to `value`; `(old slug, the org's tailnet device ids)` when it changed, else None.
    The caller enqueues the retag of those devices after its commit (`app/durable/org_retag.py`).

    The row is locked first, so two renames of one org serialize instead of leaving the slug and the tags
    disagreeing. The devices are read HERE, before the old slug is free, so the retag can only ever
    move devices this org held — never one another org enrols later under the reused old slug. For the
    same reason a slug whose tag devices still carry is refused: an org renamed onto it would inherit them.
    An unreadable tailnet refuses the rename (503) rather than renaming blind.

    Refused for org zero (its slug is the operator plane's own handle) and for a slug that is not also a
    tailnet tag name (`cloud_node_launch.tailnet_tags_for` would refuse the org's next node). Taken → 409,
    checked first so the ordinary duplicate is actionable; `uq_orgs_slug` stays the authority."""
    if not is_tenant_org(row.id):
        raise HTTPException(status_code=422, detail="The operator plane's slug cannot be changed.")
    slug = _validated_slug(value or "")
    session.get(Org, row.id, with_for_update=True, populate_existing=True)
    if slug == row.slug:
        return None
    # The community edition has no tailnet: its slug is no tag, and no device moves with it.
    if edition_features.enabled("tailnet"):
        from app.cloud_node_launch import _TAG_SAFE_SLUG

        if _TAG_SAFE_SLUG.fullmatch(slug) is None:
            raise HTTPException(
                status_code=422,
                detail="An organization's slug is also its tailnet tag, so it takes letters, digits and '-' only.",
            )
    if session.scalar(select(Org.id).where(Org.slug == slug, Org.id != row.id)) is not None:
        raise HTTPException(status_code=409, detail=f"Another organization already has the slug '{slug}'.")
    old = row.slug
    on_old: list[str] = []
    if edition_features.enabled("tailnet"):
        from app.durable import org_retag

        try:
            devices = org_retag.tailnet_devices(old, slug)
        except Exception as exc:  # noqa: BLE001 — any headscale failure: nothing renamed, the operator retries
            raise HTTPException(
                status_code=503,
                detail=f"Could not read the tailnet to move this organization's devices ({type(exc).__name__}); "
                "nothing was renamed. Try again.",
            ) from None
        if devices.on_new:
            raise HTTPException(
                status_code=409,
                detail=f"Tailnet devices still carry tag:org-{slug}; remove them before giving an organization that slug.",
            )
        on_old = devices.on_old
    row.slug = slug
    record_write(
        session, user, "org_slug_renamed", f"Renamed the slug of org {row.name} from {old} to {slug}",
        data={"org_id": str(row.id), "from": old, "to": slug, "devices": len(on_old)},
    )
    return old, on_old


@router.post("/api/orgs/{org_id}/tailnet-retag", status_code=202)
@superadmin_only(Tenancy.OPERATOR)
def retag_org_tailnet(
    org_id: str,
    user: AdminUser = Depends(require_operator),
    session: Session = Depends(get_session),
) -> dict:
    """Converge a tenant org's tailnet devices onto its own tags (`app/durable/org_retag.py::enqueue_converge`):
    `devices` is how many the tailnet read here would retag, `workflow_id` the durable converge. Idempotent —
    already-correct devices are no-ops. 409 for an operator-plane org (its devices keep the fleet tags), 422 for
    a slug that is no tag name; an unreadable tailnet or a refused enqueue is a 503 that queued nothing. Audited
    only once queued, with the workflow id."""
    # The community edition has no tailnet.
    if not edition_features.enabled("tailnet"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app.cloud_node_launch import CloudNodeError
    from app.durable import org_retag

    row = _live_one(session, org_id)
    if not is_tenant_org(row.id):
        raise HTTPException(status_code=409, detail="An operator-plane organization keeps the fleet's tailnet tags.")
    try:
        devices = org_retag.drifted_devices(row.id)
    except CloudNodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — any headscale failure: nothing retagged, the operator retries
        raise HTTPException(
            status_code=503,
            detail=f"Could not read the tailnet ({type(exc).__name__}); nothing was retagged. Try again.",
        ) from None
    workflow_id = org_retag.enqueue_converge(row.id)
    if workflow_id is None:
        raise HTTPException(status_code=503, detail="Could not queue the retag; nothing was retagged. Try again.")
    record_write(
        session, user, "org_tailnet_retagged", f"Queued a tailnet retag of org {row.name}",
        data={"org_id": str(row.id), "slug": row.slug, "devices": devices, "workflow_id": workflow_id},
    )
    session.commit()
    return {"devices": devices, "workflow_id": workflow_id}


def _validated_sso(row: Org, body: OrgUpdateIn) -> tuple[Optional[str], Optional[str]]:
    """The `(provider, tenant)` the PATCH asks for, normalised; 422 for anything that could not pin a tenant.
    A field not sent keeps the row's value; a null provider turns SSO-only off and clears the tenant."""
    fields = body.model_fields_set
    provider = body.sso_provider if "sso_provider" in fields else row.sso_provider
    if provider is None:
        if body.sso_tenant is not None:
            raise HTTPException(status_code=422, detail="Choose an SSO provider for this tenant.")
        return None, None
    if not edition_features.enabled("sso"):
        raise HTTPException(status_code=422, detail="Single sign-on is not in this edition.")
    from app import sso_policy

    if provider not in SSO_PROVIDERS:
        raise HTTPException(status_code=422, detail="The SSO provider is Microsoft or Google.")
    tenant = sso_policy.normalized_tenant(
        (body.sso_tenant if "sso_tenant" in fields else row.sso_tenant) or ""
    )
    if provider == "microsoft" and not _ENTRA_TENANT_RE.match(tenant):
        raise HTTPException(status_code=422, detail="The Microsoft tenant is the Entra tenant ID (a GUID).")
    if provider == "microsoft" and tenant == PERSONAL_TENANT_ID:
        # Personal Microsoft accounts: no organization's IdP, so nothing to pin and no MFA to attest.
        raise HTTPException(status_code=422, detail="Personal Microsoft accounts are not an organization's tenant.")
    if provider == "google" and not _WORKSPACE_DOMAIN_RE.match(tenant):
        raise HTTPException(status_code=422, detail="The Google tenant is the Workspace domain, like example.com.")
    return provider, tenant


def _apply_sso(
    session: Session, row: Org, body: OrgUpdateIn, user: AdminUser
) -> tuple[bool, list[tuple[UUID, int]]]:
    """Apply the PATCH's SSO fields: whether anything changed, and the `(member, retired epoch)` pairs whose
    sessions it ended. Records `sso_config_changed` on a change.

    A change that narrows how members may sign in — SSO-only turned on, a new provider or tenant, the MFA
    attestation withdrawn — ends every member's session (the caller's included) by bumping `session_epoch`,
    as `reject_org` does: a session a now-refused door minted, or an `sso` one minted under a withdrawn
    attestation, must not outlive the change. Turning SSO-only off, or attesting, narrows nothing.

    Org zero is refused: the operator plane signs in through its own doors (superadmins have no password at
    all). An attestation is about one IdP tenant, so changing the provider or tenant clears it unless the
    same PATCH attests again; turning SSO-only off clears it too. Attesting needs a provider."""
    if row.id == ORG_ZERO_ID:
        raise HTTPException(status_code=422, detail="Org zero is the operator plane; it cannot be SSO-only.")
    provider, tenant = _validated_sso(row, body)
    attest = body.sso_mfa_attested if "sso_mfa_attested" in body.model_fields_set else None
    if attest and provider is None:
        raise HTTPException(status_code=422, detail="Choose an SSO provider before attesting its MFA.")
    before = {
        "provider": row.sso_provider, "tenant": row.sso_tenant, "mfa_attested": row.sso_mfa_attested_at is not None,
    }
    if (provider, tenant) != (row.sso_provider, row.sso_tenant) and attest is None:
        attest = False
    row.sso_provider, row.sso_tenant = provider, tenant
    if attest is True and (not before["mfa_attested"] or (before["provider"], before["tenant"]) != (provider, tenant)):
        row.sso_mfa_attested_at, row.sso_mfa_attested_by_id = datetime.now(timezone.utc), user.id
    elif attest is False:
        row.sso_mfa_attested_at, row.sso_mfa_attested_by_id = None, None
    after = {"provider": provider, "tenant": tenant, "mfa_attested": row.sso_mfa_attested_at is not None}
    if after == before:
        # Re-attesting what is already attested keeps the first attestation's who and when.
        return False, []
    # Turning SSO-only off clears the attestation too, but reopens doors rather than closing any.
    narrows = provider is not None and (
        (provider, tenant) != (before["provider"], before["tenant"])
        or (before["mfa_attested"] and not after["mfa_attested"])
    )
    retired: list[tuple[UUID, int]] = []
    if narrows:
        for member in session.scalars(select(AdminUser).where(AdminUser.org_id == row.id)):
            retired.append((member.id, member.session_epoch or 0))
            member.session_epoch = (member.session_epoch or 0) + 1
    # Load-bearing audit (SOC 2 G-01): this decides which doors every member of the org may sign in by.
    state = f"SSO-only via {provider} ({tenant})" if provider else "not SSO-only"
    record_write(
        session, user, "sso_config_changed", f"Set org {row.name} {state}",
        data={"org_id": str(row.id), "from": before, "to": after, "members_signed_out": len(retired)},
    )
    return True, retired


def _apply_mfa_skip(session: Session, row: Org, skip: bool, user: AdminUser) -> list[tuple[UUID, int]]:
    """Turn the org's two-factor exemption on or off, recording `mfa_skip_changed`. Turning it OFF ends every
    member's session (the `(member, retired epoch)` pairs returned), as `_apply_sso` does when it narrows: a
    session minted with no second factor must not outlive the exemption. Turning it on narrows nothing."""
    row.mfa_skip = skip
    retired: list[tuple[UUID, int]] = []
    if not skip:
        for member in session.scalars(select(AdminUser).where(AdminUser.org_id == row.id)):
            retired.append((member.id, member.session_epoch or 0))
            member.session_epoch = (member.session_epoch or 0) + 1
    # Load-bearing audit (SOC 2 G-01): this decides whether the org's members prove a second factor at all.
    record_write(
        session, user, "mfa_skip_changed",
        f"{'Turned off' if skip else 'Turned on'} two-factor for org {row.name}",
        data={"org_id": str(row.id), "mfa_skip": skip, "members_signed_out": len(retired)},
    )
    return retired


def _hipaa_after(row: Org, body: OrgUpdateIn, fields: set[str]) -> tuple[bool, Optional[date]]:
    """The org's HIPAA mode once this PATCH applies, refused before any mutation when it would be on with no
    BAA date. A field sent as null keeps `hipaa_enabled` (it has no null state) and clears the date."""
    enabled = bool(row.hipaa_enabled)
    if "hipaa_enabled" in fields and body.hipaa_enabled is not None:
        enabled = body.hipaa_enabled
    effective = body.hipaa_baa_effective_date if "hipaa_baa_effective_date" in fields else row.hipaa_baa_effective_date
    if enabled and effective is None:
        raise HTTPException(status_code=422, detail=HIPAA_NEEDS_DATE)
    return enabled, effective


def _apply_hipaa(
    session: Session, row: Org, after: tuple[bool, Optional[date]], user: AdminUser, already_retired: frozenset[UUID],
) -> tuple[bool, list[tuple[UUID, int]]]:
    """Set the org's HIPAA mode and BAA date, recording `hipaa_changed` with the before and after; False when
    nothing changed. Filed in the org itself, so its own admins see it in their Audit too.

    Turning it ON ends the session of every member who may hold one minted on a skip — every member while the
    org skips the second factor, else each exempt one — since a HIPAA org honours no skip (`mfa.skip_reason`,
    C6). Those `(member, retired epoch)` pairs are returned, as `_apply_mfa_skip` returns its own; a member in
    `already_retired` (this PATCH already ended their session) is not retired twice."""
    enabled, effective = after
    before = {"enabled": bool(row.hipaa_enabled), "date": iso_or_none(row.hipaa_baa_effective_date)}
    now = {"enabled": enabled, "date": iso_or_none(effective)}
    retired: list[tuple[UUID, int]] = []
    if before == now:
        return False, retired
    if enabled and not before["enabled"]:
        for member in session.scalars(select(AdminUser).where(AdminUser.org_id == row.id)):
            if member.id not in already_retired and (row.mfa_skip or member.mfa_exempt):
                retired.append((member.id, member.session_epoch or 0))
                member.session_epoch = (member.session_epoch or 0) + 1
    if before["enabled"] != enabled:
        # A new HIPAA term starts with no warning accepted: an acknowledgement stands only for the term it was given in.
        row.hipaa_acks = {}
    row.hipaa_enabled = enabled
    row.hipaa_baa_effective_date = effective
    if before["enabled"] != enabled:
        summary = f"Turned {'on' if enabled else 'off'} HIPAA for org {row.name}"
    else:
        summary = f"Changed the BAA date of org {row.name}"
    if now["date"]:
        summary += f" (BAA {now['date']})"
    # Load-bearing audit: the setting decides which safeguards the org's members are held to.
    record_write(
        session, user, "hipaa_changed", summary,
        data={"org_id": str(row.id), "before": before, "after": now, "members_signed_out": len(retired)},
        in_org=row.id,
    )
    return True, retired


@router.post("/api/orgs/{org_id}/hipaa/acknowledgements", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
def acknowledge_hipaa_warning(
    org_id: str,
    body: HipaaAckIn,
    user: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """An org admin (or the operator) accepts a HIPAA warning: this org may keep using a surface whose vendor
    has no BAA on file (`hipaa.SURFACES`). Idempotent: a surface already acknowledged keeps its first record. 409
    while HIPAA is off; turning it on or off clears every acknowledgement (`_apply_hipaa`)."""
    row = _live(administered_org_or_404(session, org_id, principal))
    vendor = hipaa.SURFACES.get(body.surface)
    if vendor is None:
        raise HTTPException(status_code=422, detail=f"Unknown surface: {body.surface}.")
    session.get(Org, row.id, with_for_update=True, populate_existing=True)
    if not hipaa.is_on(row):
        raise HTTPException(status_code=409, detail=HIPAA_OFF)
    acks = dict(row.hipaa_acks or {})
    if body.surface in acks:
        return Response(status_code=204)
    acks[body.surface] = {"by": str(user.id), "at": now_iso()}
    # A new dict, so the JSONB column is seen as changed.
    row.hipaa_acks = acks
    record_write(
        session, user, "hipaa_warning_acknowledged",
        f"Acknowledged the HIPAA warning for {body.surface} in org {row.name}",
        data={"org_id": str(row.id), "surface": body.surface, "vendor": vendor},
        in_org=row.id,
    )
    emit_console_event(session, entity="org", id=row.id, action="upserted")
    session.commit()
    return Response(status_code=204)


# ── Approval (self-service orgs) ──────────────────────────────────────────────────────────────────
# A self-service org is born `pending` (`app/org_create.py`) and these two routes move it out: pending ->
# active (approve), pending -> rejected (reject). The third edge, rejected -> active, is the enable PATCH
# above; the fourth, pending -> active by the system at sign-up, is `app/org_approval.py::approve_at_signup`.
# `app/org_gate.py` owns what each state refuses.


def _pending_for_decision(
    session: Session, org_id: str, *, allow_draft: bool = False,
) -> tuple[Org, Optional[OrgSignup], bool]:
    """The org, locked, when it is awaiting a decision, its sign-up, and whether that is an unsubmitted draft
    (`app/signup_draft.py`); 409 otherwise. A draft is nobody's application yet, so only an approve that submits it
    (`allow_draft`) takes one. The lock serialises two operators deciding the same org, so the second reads the
    first's outcome and is refused."""
    row = _live_one(session, org_id)
    # `populate_existing`: a bare `session.get` is served from the identity map and locks nothing.
    session.get(Org, row.id, with_for_update=True, populate_existing=True)
    if row.lifecycle != ORG_PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"'{row.name}' is not awaiting approval, so there is nothing to decide. Reload the list.",
        )
    signup = session.scalar(select(OrgSignup).where(OrgSignup.org_id == row.id))
    draft = False
    # The community edition has no sign-up drafts.
    if edition_features.enabled("signup"):
        from app.signup import drafts as signup_drafts

        draft = session.scalar(signup_drafts().where(OrgSignup.org_id == row.id)) is not None
    if draft and not allow_draft:
        raise HTTPException(
            status_code=409, detail=f"'{row.name}' has not finished signing up, so there is nothing to decide.",
        )
    return row, signup, draft


def _submit_draft(session: Session, row: Org, signup: OrgSignup, user: AdminUser, claim: Session) -> tuple[UUID, int]:
    """An operator's approve of a draft submits it first (`signup_draft.submit_by_operator`), staged for the
    approval's commit; 409 `signup_incomplete` while its profile is unanswered (stage `profile`)."""
    # The community edition has no sign-up drafts (its caller, `approve_org`, already refuses there).
    if not edition_features.enabled("signup"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import signup_draft, signup_plans, signup_stage

    owner = session.scalar(select(AdminUser).where(AdminUser.id == signup.owner_id))
    stage = signup_stage.stage_of_org(
        row, signup, owner, draft=True, plan_paid=(signup.plan_key or "") in signup_plans.paid_keys(session),
    )
    if stage == "profile":
        raise HTTPException(status_code=409, detail={
            "code": "signup_incomplete",
            "message": f"Fill the sign-up first: '{row.name}' still misses profile answers or a country.",
        })
    return signup_draft.submit_by_operator(session, row, signup, user, claim)


@router.post("/api/orgs/{org_id}/approve", response_model=OrgOut)
@superadmin_only(Tenancy.OPERATOR)
def approve_org(
    org_id: str,
    body: Optional[OrgApproveIn] = None,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> OrgOut:
    """Admit a pending self-service org: `active`, so its creation doors and dispatch unlock
    (`app/org_gate.py`). 409 unless pending. The approval is audited in the same transaction, and wakes the
    reconciler that provisions its plan's products (`app/plan_bundles.py`). Then, unless `send_welcome` is false,
    its owner is sent the welcome email (`app/org_welcome.py`); it never un-does or fails the approval, and the
    answer's `welcome_outcome` says what the email did. The sequence is `app/org_approval.py::approve`, shared with the
    automatic approval at sign-up."""
    # The community edition has no sign-up approval.
    if not edition_features.enabled("signup"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import org_approval, signup_plans

    send_welcome = body.send_welcome if body is not None else True
    plan_key = body.plan_key if body is not None else None
    row, signup, draft = _pending_for_decision(session, org_id, allow_draft=True)
    # Checked under the org's lock, so a plan retired meanwhile is refused rather than silently replaced.
    if plan_key is not None and signup_plans.active_row(session, plan_key) is None:
        raise HTTPException(status_code=422, detail="Choose one of the plans in Products › Plans.")
    if draft and signup is not None:
        # Locked, then re-read: an applicant's own Sign up (`signup_draft.submit`, which locks this row, not the org)
        # that landed meanwhile is an ordinary application now, approved as one.
        session.scalar(
            select(OrgSignup).where(OrgSignup.org_id == row.id).with_for_update()
            .execution_options(populate_existing=True)
        )
        draft = signup.submitted_at is None
    if draft and signup is not None:
        # A draft is submitted for the applicant and approved in one transaction; its email's open
        # sign-up row (org zero's) is consumed on a session of its own, committed as soon as the approval has.
        with SessionLocal() as claim:
            retired = _submit_draft(session, row, signup, user, claim)
            outcome = org_approval.approve(
                session, row, signup, user, send_welcome=send_welcome, plan_key=plan_key, committed=claim.commit,
            )
        revoke_direct_grants_retired_epoch(*retired)
    else:
        outcome = org_approval.approve(session, row, signup, user, send_welcome=send_welcome, plan_key=plan_key)
    out = _rows_out(session, [row])[0]
    out.welcome_outcome = outcome
    return out


@router.post("/api/orgs/{org_id}/welcome-email", response_model=OrgOut)
@superadmin_only(Tenancy.OPERATOR)
def send_welcome_email(
    org_id: str,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> OrgOut:
    """Send (or resend) an approved self-service org's owner the welcome email (`app/org_welcome.py`). 409
    unless the org is active and self-service. A mail fault is not an error: `welcome_outcome` and the
    sign-up's `welcome_error` say it."""
    row = _live_one(session, org_id)
    if row.lifecycle != ORG_ACTIVE:
        raise HTTPException(
            status_code=409, detail=f"'{row.name}' is not active, so it cannot be sent a welcome email."
        )
    outcome = org_welcome.send(session, row, user)
    if outcome == "skipped":
        raise HTTPException(
            status_code=409, detail=f"'{row.name}' has no sign-up, so there is no owner to welcome."
        )
    session.refresh(row)
    out = _rows_out(session, [row])[0]
    out.welcome_outcome = outcome
    return out


class BundleKeepOut(BaseModel):
    #: Whether this copy may be kept as a paid hosting now, and at what monthly price (cents; None = not for sale).
    offered: bool
    monthly_cents: Optional[int] = None


class BundleOut(BaseModel):
    """One copy of a node the org's plan includes (`app/plan_bundles.py::bundles_out`, Architecture §10). `status`
    is §4.8's vocabulary; `note` its sentence (the operator's detail here, the tenant-safe one on `/api/org/bundles`);
    `stops_at` when a copy the plan no longer includes stops. Mirrored in `apps/web/src/types.ts`."""

    id: str
    kind: str
    label: str
    ordinal: int
    status: str
    note: Optional[str] = None
    node_id: Optional[str] = None
    hosting_id: Optional[str] = None
    stops_at: Optional[str] = None
    keep: BundleKeepOut


@router.get("/api/orgs/{org_id}/bundles", response_model=list[BundleOut])
@superadmin_only(Tenancy.OPERATOR)
def list_org_bundles(
    org_id: str,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> list[BundleOut]:
    """The nodes the org's plan includes, with the operator's detail of a failure."""
    row = _one(session, org_id)
    # The community edition has no plans bundles: the plan includes no node.
    if not edition_features.enabled("billing"):
        return []
    from app import plan_bundles

    return [BundleOut(**item) for item in plan_bundles.bundles_out(session, row.id, tenant=False)]




@router.post("/api/orgs/{org_id}/bundles/{bundle_id}/retry", response_model=list[BundleOut])
@superadmin_only(Tenancy.OPERATOR)
async def retry_org_bundle(
    org_id: str,
    bundle_id: UUID,
    user: AdminUser = Depends(require_superadmin),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[BundleOut]:
    """Set up again a node product that failed, or whose node was deleted, from its one row — never a second
    node."""
    # The community edition has no plans bundles.
    if not edition_features.enabled("billing"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import plan_bundles
    from app.routes import billing_org_plan

    row = _live_one(session, org_id)
    org_uuid = row.id
    await billing_org_plan.retry_bundle(session, org_uuid, bundle_id, user, principal)
    return [BundleOut(**item) for item in plan_bundles.bundles_out(session, org_uuid, tenant=False)]


@router.post("/api/orgs/{org_id}/reject", response_model=OrgOut)
@superadmin_only(Tenancy.OPERATOR)
def reject_org(
    org_id: str,
    body: OrgRejectIn,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> OrgOut:
    """Turn a pending self-service org away: `rejected` AND disabled, the optional reason stored for its
    members' sign-in refusal (`app/org_gate.py::refuse_unapproved_sign_in`), and every member's session
    ended by bumping `session_epoch` in the same transaction. Deletes nothing. After the commit the applicant
    is emailed, without the reason, and any card saved at sign-up is detached. 409 unless pending;
    422 a reason over `REASON_MAX` characters (the refusal would cut it)."""
    # The community edition has no sign-up approval.
    if not edition_features.enabled("signup"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import org_approval

    reason = (body.reason or "").strip()
    if len(reason) > REASON_MAX:
        raise HTTPException(status_code=422, detail=f"reason is at most {REASON_MAX} characters")
    row, signup, _ = _pending_for_decision(session, org_id)
    row.lifecycle = ORG_REJECTED
    row.enabled = False
    org_approval.decided(signup, user)
    if signup is not None:
        signup.rejection_reason = reason or None
    # Every member's session ends now (`app/auth.py` refuses a cookie carrying a retired epoch); the
    # retired epochs are kept for the post-commit direct-console revoke.
    retired: list[tuple[UUID, int]] = []
    for member in session.scalars(select(AdminUser).where(AdminUser.org_id == row.id)):
        retired.append((member.id, member.session_epoch or 0))
        member.session_epoch = (member.session_epoch or 0) + 1
    stamp_actor(row, user)
    record_write(
        session, user, "org_rejected", f"Rejected org {row.name} ({row.slug})",
        data={"org_id": str(row.id), "slug": row.slug, "reason": reason or None, "members_signed_out": len(retired)},
    )
    emit_console_event(session, entity="org", id=row.id, action="upserted")
    session.commit()
    # After the commit (a node acting on a rolled-back reject would sign out a live console).
    for member_id, retired_epoch in retired:
        revoke_direct_grants_retired_epoch(member_id, retired_epoch)
    # The applicant's rejection email and the detach of any card saved at sign-up, durable and after the commit
    # (`org_approval.after_rejection`); the reason stays the operator's (D7).
    if signup is not None:
        notify_outbox.after_answer("org_rejected", str(row.id))
    session.refresh(row)
    return _rows_out(session, [row])[0]


# ── Limits ────────────────────────────────────────────────────────────────────────────────────────


#: The largest override an operator may set — the platform-default knobs' own ceiling
#: (`app/operator_settings/`, "Organization limits"); unlimited on GitHub Runners (`plan_terms.CAP_MAX`).
_MAX_LIMIT = plan_terms.CAP_MAX


class LimitOut(BaseModel):
    """One of an org's caps or abuse quotas on the wire (`app/org_limits.py::Limit`, `app/org_quotas.py::Quota`).
    `limit` null = unlimited; `source` is `override` or `plan` for a cap (`app/plan_terms.py`) and `override` or
    `default` for a quota (a plan quota's may be `plan`, `app/org_quotas.py::PLAN_QUOTA_KINDS`); `override` is the
    org's own column as stored (null = none, 0 = unlimited — off on GitHub Runners, `plan_terms.OFFABLE_CAPS`, whose unlimited is `maximum`), so the form can show and clear exactly what was set; `default` is what clearing it falls back to (0 or null = unlimited, but 0 = off on GitHub Runners) and
    `fallback_source` whose value that is — a cap's `plan`, a quota's plan's or `(default)`'s (`default`) —
    whatever `source` says now."""

    kind: str
    label: str
    limit: Optional[int]
    source: str
    usage: int
    override: Optional[int]
    default: Optional[int]
    fallback_source: Literal["plan", "default"]
    #: The largest override this kind takes.
    maximum: int
    #: What `limit`, `usage` and `default` count in ("MB", "GB"); empty for a plain count.
    unit: str = ""


class LimitsIn(RootModel[dict[str, Optional[int]]]):
    """`{kind: value}` for the caps to change: an int (0 = unlimited; on GitHub Runners 0 = off and the maximum =
    unlimited, `plan_terms.CAP_MAX`) sets an override, null clears it back
    to the plan's value (a quota: its plan's, else `(default)`'s). A kind not sent is left as it is."""


#: Every kind this tab reads and writes: the caps, then the abuse quotas (`app/org_quotas.py`), which share
#: the `orgs.limit_<kind>` and `org_limit_<kind>` spellings (only `PLAN_QUOTA_KINDS` have a plan tier).
_KINDS = org_limits.LIMIT_KINDS + org_quotas.QUOTA_KINDS


def _column(kind: str) -> str:
    """The `orgs` column this tab reads and writes for `kind`: the Agents cap's one override is Max agents
    (`plan_terms.caps_of`), every other kind its own column."""
    return "max_agents_override" if kind == "runner_slots" else org_limits.column(kind)


def _limits_out(session: Session, row: Org) -> list[LimitOut]:
    return [
        LimitOut(
            kind=limit.kind,
            label=limit.label,
            limit=limit.limit,
            source=limit.source,
            usage=limit.usage,
            override=getattr(row, _column(limit.kind)),
            default=default,
            fallback_source=fallback_source,
            maximum=org_quotas.maximum(limit.kind) or _MAX_LIMIT,
            unit=limit.unit if isinstance(limit, org_quotas.Quota) else "",
        )
        for limit in [
            *org_limits.effective_limits(session, row.id),
            *org_quotas.effective_quotas(session, row.id),
        ]
        for default, fallback_source in [_fallback(session, row, limit)]
    ]


def _fallback(session: Session, row: Org, limit: org_limits.Limit | org_quotas.Quota) -> tuple[Optional[int], str]:
    """What a cleared override hands the cap back to (None or 0 = unlimited; 0 is off on an `OFFABLE_CAPS` kind) and
    whose value that is: its plan's for a cap, the plan's else `(default)`'s for a quota (`org_quotas.fallback`)."""
    if isinstance(limit, org_limits.Limit):
        return limit.plan_limit, "plan"
    return org_quotas.fallback(session, row, limit.kind)


@router.get("/api/orgs/{org_id}/limits", response_model=list[LimitOut])
@superadmin_only(Tenancy.OPERATOR)
def get_org_limits(
    org_id: str,
    _: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> list[LimitOut]:
    """The org's caps, each with its effective value, whether an override or the default decided it,
    and its usage. An operator's: limits are operator-owned (never a tenant-writable knob)."""
    return _limits_out(session, _one(session, org_id))


@router.put("/api/orgs/{org_id}/limits", response_model=list[LimitOut])
@superadmin_only(Tenancy.OPERATOR)
def put_org_limits(
    org_id: str,
    body: LimitsIn,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> list[LimitOut]:
    """Override or clear any of the org's caps. Refused whole (422) on an unknown kind or a value
    outside 0..100000, before any write. Lowering a cap below usage deletes and stops nothing: it only
    holds back new creation and dispatch (`app/org_limits.py`)."""
    row = _live_one(session, org_id)
    wanted = body.root
    unknown = sorted(set(wanted) - set(_KINDS))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown limit: {', '.join(unknown)}. Use {', '.join(_KINDS)}.",
        )
    for kind, value in wanted.items():
        top = org_quotas.maximum(kind) or _MAX_LIMIT
        if value is not None and not 0 <= value <= top:
            raise HTTPException(
                status_code=422,
                detail=f"A limit is between 0 ({'off' if kind == 'github_runners' else 'no limit'}) and {top}.",
            )
    changed = False
    for kind, value in wanted.items():
        if getattr(row, _column(kind)) != value:
            setattr(row, _column(kind), value)
            changed = True
    # Max agents also bounds the org's buy range: never below its Min agents. The community edition sells
    # nothing.
    if edition_features.enabled("billing"):
        from app import org_plan_admin

        if "runner_slots" in wanted:
            org_plan_admin.check_agent_range(session, row)
    if changed:
        stamp_actor(row, user)
        session.flush()
        emit_console_event(session, entity="org", id=row.id, action="upserted")
        session.commit()
        session.refresh(row)
        org_quotas.invalidate_budget(row.id)
    return _limits_out(session, row)


# ── Settings overrides ────────────────────────────────────────────────────────────────────────────────


class OrgSettingsIn(StrictModel):
    """A partial update, `PUT /api/settings`' shape: only the keys sent are touched, `null` clears the org's
    override (the org then inherits the operator's value)."""

    values: dict[str, Any]


def _org_settings_out(session: Session, row: Org) -> list[SettingKnobOut]:
    """Every org-tier knob as it governs this org: `value` is what applies, `fallback` what it would inherit
    with no override, `overridden` whether the org holds its own row (`operator_settings.defaults.effective_settings`)."""
    return [
        SettingKnobOut(**knob)
        for knob in settings_defaults.effective_settings(session, org=row.id)
        if knob["tier"] == "org"
    ]


@router.get("/api/orgs/{org_id}/settings", response_model=list[SettingKnobOut])
@superadmin_only(Tenancy.OPERATOR)
def get_org_settings(
    org_id: str,
    _: AdminUser = Depends(require_operator),
    session: Session = Depends(get_session),
) -> list[SettingKnobOut]:
    """The org's settings overrides. An operator's: a tenant sees the effective values read-only in Settings."""
    return _org_settings_out(session, _one(session, org_id))


@router.put("/api/orgs/{org_id}/settings", response_model=list[SettingKnobOut])
@superadmin_only(Tenancy.OPERATOR)
def put_org_settings(
    org_id: str,
    body: OrgSettingsIn,
    user: AdminUser = Depends(require_operator),
    session: Session = Depends(get_session),
) -> list[SettingKnobOut]:
    """Override or clear org-tier settings for one org (`operator_settings.apply_org_overrides`: refused whole,
    422, on an unknown or operator-global key or an out-of-range value, before any write). This route is the
    org authorization that writer asks of its caller."""
    row = _live_one(session, org_id)
    # Before the writer, which commits: the audit row lands in the same transaction or not at all.
    record_write(
        session, user, "org_settings_changed", f"Changed settings overrides of org {row.name}",
        data={"org_id": str(row.id), "values": body.values},
    )
    try:
        # Two saves raced to create the same key (`uq_org_settings_org_id_key`): a 409, and nothing of this one landed.
        with conflict_as_409(session, "Changed by someone else just now. Reload and save again."):
            settings_writes.apply_org_overrides(session, row.id, body.values)
    except settings_errors.OperatorSettingsError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    settings_cache.invalidate_cache()
    emit_console_event(session, entity="org", id=row.id, action="upserted")
    session.commit()
    return _org_settings_out(session, row)


# ── Cron minimum period ───────────────────────────────────────────────────────────────────────────────


class CronMinPeriodOut(BaseModel):
    """An org's minimum cron period, every tier (`app/cron_min_period.py::CronMinPeriod`), in seconds.
    `override` and `own` are the org's two columns as stored (null = unset), so a form can show and clear
    exactly what was set; `fallback` is what the org follows with no override; `upstream` is the lowest
    `own` may be."""

    operator_floor: int
    tenant_default: int
    fallback: int
    fallback_source: str
    override: Optional[int]
    upstream: int
    upstream_source: str
    own: Optional[int]
    effective: int


class CronMinPeriodIn(StrictModel):
    """The tiers to change; a field not sent is left alone, null clears it. `override` is the operator's;
    `own` the org administrator's (an operator may set either)."""

    # Strict: lax coercion would read `true` as 1 and "3600" as 3600.
    override: Optional[StrictInt] = None
    own: Optional[StrictInt] = None


def _cron_min_period_out(session: Session, row: Org) -> CronMinPeriodOut:
    return CronMinPeriodOut(**dataclasses.asdict(cron_min_period.resolve(session, row.id)))


@router.get("/api/orgs/{org_id}/cron-min-period", response_model=CronMinPeriodOut)
@org_admin(Tenancy.CALLER_PLANE)
def get_org_cron_min_period(
    org_id: str,
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CronMinPeriodOut:
    """The org's minimum cron period: an operator's for any org, an Org Owner/Admin's for their own."""
    return _cron_min_period_out(session, administered_org_or_404(session, org_id, principal))


@router.patch("/api/orgs/{org_id}/cron-min-period", response_model=CronMinPeriodOut)
@org_admin(Tenancy.CALLER_PLANE)
def patch_org_cron_min_period(
    org_id: str,
    body: CronMinPeriodIn,
    user: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CronMinPeriodOut:
    """Set or clear the operator's per-org override (operator only; never under the operator minimum) and
    the org administrator's own value (never under the upstream the override leaves). Refused whole, before
    any write. Raising either changes no cron row: a faster row is throttled when it fires
    (`app/cron_runtime.py`) and refused on its next schedule save."""
    row = _live(administered_org_or_404(session, org_id, principal))
    sent = body.model_fields_set
    if "override" in sent and not (principal is not None and principal.is_operator):
        raise HTTPException(
            status_code=403,
            detail="An operator sets your organization's minimum — you may only raise it with your own.",
        )
    try:
        if "override" in sent:
            override = cron_min_period.check_override(body.override)
            row.cron_min_period_seconds = override
        if "own" in sent:
            # Judged against the upstream the override above leaves, so one request can lower the
            # override and the own value together.
            row.cron_min_period_own_seconds = cron_min_period.check_own(
                body.own, cron_min_period.resolve(session, row.id)
            )
    except cron_min_period.MinPeriodError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if session.is_modified(row):
        stamp_actor(row, user)
        session.flush()
        emit_console_event(session, entity="org", id=row.id, action="upserted")
        session.commit()
        session.refresh(row)
    return _cron_min_period_out(session, row)


def _erase_billing(org_id: UUID) -> None:
    """An org being deleted stops paying (`routes/billing_org_plan.py::erase_billing`); the community edition has
    no billing."""
    if not edition_features.enabled("billing"):
        return
    from app.routes import billing_org_plan

    billing_org_plan.erase_billing(org_id)


def _sign_out_members(session: Session, org_id: UUID) -> list[tuple[UUID, int]]:
    """Retire every member's session epoch (`app/auth.py` refuses a cookie carrying one); the retired epochs, for
    the post-commit direct-console revoke."""
    retired: list[tuple[UUID, int]] = []
    for member in session.scalars(select(AdminUser).where(AdminUser.org_id == org_id)):
        retired.append((member.id, member.session_epoch or 0))
        member.session_epoch = (member.session_epoch or 0) + 1
    return retired


@router.delete("/api/orgs/{org_id}", status_code=204)
@superadmin_only(Tenancy.OPERATOR)
async def delete_org(
    org_id: str,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> Response:
    """Delete an org — SOFTLY: nothing is purged. Any tenant org, enabled or not, the caller's own
    included; never the operator plane, whose members' sign-in it would refuse with no way back.

    Billing ends (PRD §6, `_erase_billing`; a Stripe failure there is the delete's failure, 503/400) and
    `org_lifecycle.wind_down` stops everything the org runs — disabled, live tasks failed, hostings' machines
    ended, nodes torn down (their rows kept, disabled and uncredentialed) — committing an `org_wound_down` row. Then, under the org row's `FOR UPDATE`, the row is
    stamped `deleted_at`/`deleted_by_id`, its members' sessions end, and `org_deleted` is filed (in the actor's
    org, the operator plane's when the actor's own org is the one deleted) naming the org, so its customer history
    records it. 409 while the org has a live staging org (`staging_org.refuse_parent_delete`). The org is hidden from every org list and refused at sign-in and dispatch, and its members,
    projects and nodes from theirs (`app/models/orgs.py::in_live_org`); Restore
    (`restore_org`) lists it again, Compliance Delete (`compliance_delete_org`) is the only purge. 204."""
    row = _one(session, org_id)
    if not is_tenant_org(row.id):
        raise HTTPException(
            status_code=409,
            detail=f"'{row.name}' ({row.slug}) is the operator plane and cannot be deleted — every operator signs in to it.",
        )
    if row.deleted_at is not None:
        raise HTTPException(status_code=409, detail=f"'{row.name}' ({row.slug}) is already deleted.")
    staging_org.refuse_parent_delete(session, row)
    await _soft_delete(session, user, row)
    return Response(status_code=204)


async def _soft_delete(session: Session, user: AdminUser, row: Org) -> None:
    """The soft delete both doors share (`delete_org`, `delete_own_org`): billing ended, the org wound down, then
    stamped deleted with its members signed out. `session` must see the whole org (the operator plane)."""
    key, name, slug = row.id, row.name, row.slug
    audit_org = user.org_id if user.org_id != key else ORG_ZERO_ID
    await run_in_threadpool(_erase_billing, key)
    # Its nodes' rows stay, like its members and projects: hidden with the org, back on Restore.
    wound_down = await wind_down(session, row, user, keep_node_rows=True)
    await run_in_threadpool(_record_wind_down, session, user, key, name, slug, audit_org, wound_down)
    retired = await run_in_threadpool(_mark_deleted, session, user, key, audit_org)
    for member_id, retired_epoch in retired:
        # Off the loop: the revoke hops back onto it from a worker thread (`console_direct`).
        await run_in_threadpool(revoke_direct_grants_retired_epoch, member_id, retired_epoch)


class OwnOrgDeleteOut(BaseModel):
    """What the Owner's Delete account dialog asks for: the name to type, and whether a 2FA code is due."""

    name: str
    needs_code: bool


class OwnOrgDeleteIn(BaseModel):
    """The Owner's confirmation: the org's name typed back, and a current 2FA code (TOTP or a recovery code) when
    they have a second factor."""

    name: str = Field(max_length=200)
    code: str = Field(default="", max_length=64)


#: Refused own-org deletes are security events, filed like Compliance Delete's.
ORG_DELETE_REFUSED = "org_delete_refused"


def _own_org_for_owner(principal: Principal | None, session: Session) -> Org:
    """The caller's own tenant org, for its Owner only: 404 on the operator plane, 403 for an Admin."""
    org_id = tenant_org_or_404(principal, detail="No organization to delete.")
    if principal is None or principal.org_role != access.ORG_OWNER:
        raise HTTPException(status_code=403, detail="Only the Owner can delete the account.")
    return get_or_404(session, Org, org_id, detail="No organization to delete.")


@router.get("/api/org/delete", response_model=OwnOrgDeleteOut)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def own_org_delete_check(
    principal: Principal | None = Depends(current_principal),
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> OwnOrgDeleteOut:
    """What deleting the caller's own org asks of its Owner (`delete_own_org`)."""
    row = _own_org_for_owner(principal, session)
    return OwnOrgDeleteOut(name=row.name, needs_code=mfa.is_enrolled(session, user))


@router.post("/api/org/delete", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
async def delete_own_org(
    body: OwnOrgDeleteIn,
    principal: Principal | None = Depends(current_principal),
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Delete account — the Owner's own soft delete of their org (Billing's Delete account), the same as the
    operator's `delete_org`: a live subscription ends now with no refund, everything the org runs stops, every member
    is signed out. The operator can still Restore or Compliance Delete it.

    Owner only (403 for an Admin), signed in — never an API key (`@session_only`). The org's exact name confirms
    (422 otherwise). An Owner with a second factor also gives a current code (`mfa.verify_current_code`, recovery
    codes accepted): a wrong one 403, counted and filed as `org_delete_refused`. Without one, the name alone
    confirms. 409 while the org has a live staging org (`staging_org.refuse_parent_delete`), before the code. 204."""
    row = _own_org_for_owner(principal, session)
    if body.name.strip() != row.name.strip():
        raise HTTPException(status_code=422, detail="Type the organization's name exactly to confirm.")
    staging_org.refuse_parent_delete(session, row)  # before the code, so a refused delete spends none
    if mfa.is_enrolled(session, user):
        check = mfa.verify_current_code(session, user, body.code, allow_recovery=True)
        if not check.ok:
            outcome = "throttled" if check.throttled else "wrong"
            record_write(
                session, user, ORG_DELETE_REFUSED, f"Delete of org {row.name} ({row.slug}) refused: {outcome} 2FA code",
                data={"org_id": str(row.id), "slug": row.slug, "totp": outcome},
            )
            session.commit()
            raise HTTPException(
                status_code=403, detail="Too many wrong codes. Wait, then retry." if check.throttled else "Wrong code.",
            )
        # The used step is spent now, so the code cannot authorize a second delete.
        session.commit()
    key = row.id
    session.rollback()  # nothing more on the request's session: it holds no transaction through the wind-down
    # The wind-down reaches past the caller's org (a host's carried nodes, the audit row in the operator plane).
    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        org = fleet.get(Org, key)
        if org is None or org.deleted_at is not None:
            raise HTTPException(status_code=409, detail="That org is already deleted.")
        await _soft_delete(fleet, user, org)
    return Response(status_code=204)


def _mark_deleted(session: Session, user: AdminUser, key: UUID, audit_org: UUID) -> list[tuple[UUID, int]]:
    """`delete_org`'s last step, one transaction under the org row's `FOR UPDATE`: the soft-delete stamp, every
    member signed out, `org_deleted` filed. The retired epochs, for the post-commit direct-console revoke."""
    row = session.get(Org, key, with_for_update=True, populate_existing=True)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=409, detail="That org is already deleted.")
    row.deleted_at = datetime.now(timezone.utc)
    row.deleted_by_id = user.id
    stamp_actor(row, user)
    retired = _sign_out_members(session, row.id)
    record_write(
        session, user, "org_deleted", f"Deleted org {row.name} ({row.slug})",
        data={"org_id": str(row.id), "slug": row.slug, "members_signed_out": len(retired)}, in_org=audit_org,
    )
    emit_console_event(session, entity="org", id=row.id, action="deleted")
    session.commit()
    return retired


@router.post("/api/orgs/{org_id}/restore", response_model=OrgOut)
@superadmin_only(Tenancy.OPERATOR)
def restore_org(
    org_id: str,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> OrgOut:
    """Undo a soft delete: `deleted_at` cleared, the org listed again — and still DISABLED (re-enabling is the
    existing Enable, a separate decision). What the delete's wind-down ended (tasks, hostings, nodes) stays ended.
    409 unless deleted. Audit `org_restored`."""
    row = _one(session, org_id)
    session.get(Org, row.id, with_for_update=True, populate_existing=True)
    if row.deleted_at is None:
        raise HTTPException(status_code=409, detail=f"'{row.name}' ({row.slug}) is not deleted.")
    if row.parent_org_id is not None and staging_org.staging_of(session, row.parent_org_id) is not None:
        # One live staging org per parent (`uq_orgs_parent_org_id`): its parent made another since.
        raise HTTPException(status_code=409, detail=f"'{row.name}' is a staging org, and its parent already has another. Delete that one first.")
    row.deleted_at = None
    row.deleted_by_id = None
    stamp_actor(row, user)
    record_write(
        session, user, ORG_RESTORED, f"Restored org {row.name} ({row.slug})",
        data={"org_id": str(row.id), "slug": row.slug},
    )
    emit_console_event(session, entity="org", id=row.id, action="upserted")
    session.commit()
    session.refresh(row)
    return _rows_out(session, [row])[0]


#: The audit kinds of the soft-delete lifecycle (`org_deleted` is the soft delete itself, and the purge's).
ORG_RESTORED = "org_restored"
ORG_COMPLIANCE_DELETED = "org_compliance_deleted"
ORG_COMPLIANCE_DELETE_REFUSED = "org_compliance_delete_refused"


async def purge_org(org_id: str, user: AdminUser, session: Session) -> None:
    """The purge: a soft-deleted org and everything it owns. Reached ONLY through Compliance Delete
    (`compliance_delete_org` → `app/tenancy/offboard.py::execute`), never a route of its own.

    The one refusal is up front, before anything is touched: a row OUTSIDE the org that points into it and cannot
    be detached (`org_lifecycle.undetachable_references` — in practice only the operator plane's system actor,
    which other orgs' rows are authored by). Then billing ends and `org_lifecycle.wind_down` runs again — both
    idempotent, so whatever came back since the soft delete is stopped too — and `_purge` removes every row in one
    transaction under the org row's `FOR UPDATE`, refusing an org restored meanwhile.

    The audit is the load-bearing kind (`record_write` raises rather than commit unaudited): the purge destroys
    the tenant's own history, so this row is the only surviving record. It is filed in the actor's org, or in
    the operator plane when the actor's own org is the one deleted, and staged AFTER the `orgs` row's delete
    flushes (the trigger fires there). `tests/test_org_delete.py` asserts it survives. The database work runs off
    the event loop (`run_in_threadpool`)."""
    row = _one(session, org_id)
    # Re-read: a Restore that landed during the offboarding must not have its org wound down again.
    session.refresh(row)
    if row.deleted_at is None:
        raise HTTPException(status_code=409, detail=f"'{row.name}' ({row.slug}) was restored — nothing was purged.")
    key, name, slug = row.id, row.name, row.slug
    actor_org = user.org_id
    audit_org = actor_org if actor_org != key else ORG_ZERO_ID
    blocking = await run_in_threadpool(undetachable_references, session, key)
    session.rollback()
    if blocking:
        if "orgs.parent_org_id" in blocking:
            raise HTTPException(status_code=409, detail=(
                f"'{name}' ({slug}) has a staging org. Delete and purge the staging org first. Nothing was stopped or deleted."
            ))
        listed = ", ".join(f"{column} ({count})" for column, count in sorted(blocking.items()))
        raise HTTPException(
            status_code=409,
            detail=(
                f"rows in other orgs point at '{name}' ({slug})'s rows and cannot be reassigned: {listed}. "
                "Nothing was stopped or deleted."
            ),
        )
    await run_in_threadpool(_erase_billing, key)
    wound_down = await wind_down(session, row, user)
    await run_in_threadpool(_record_wind_down, session, user, key, name, slug, audit_org, wound_down)
    doomed_object_keys = await run_in_threadpool(_purge, session, user, key, audit_org, wound_down)
    # Objects last, after the commit: the only third-party call must not sit inside the row lock. Each
    # key goes through the single-attachment guard, so a key shared with a surviving row is safe. Best
    # effort: the worst case is a logged orphan. Function-local import to break the
    # `routes/attachments.py` -> `routes/tasks/delete.py` cycle (as `delete_task_core` does).
    from app.attachment_storage import reap_unreferenced_object

    for bucket, object_key in doomed_object_keys:
        await run_in_threadpool(reap_unreferenced_object, session, object_key, bucket)


def _record_wind_down(
    session: Session, user: AdminUser, key: UUID, name: str, slug: str, audit_org: UUID, wound_down: dict
) -> None:
    """Committed on its own, before the purge: what the wind-down ended stays on the record even if the purge
    is then refused. Loads the actor too, before the purge can take its row (its org may be the one deleted)."""
    record_write(
        session, user, "org_wound_down", f"Stopped everything org {name} ({slug}) ran",
        data={"org_id": str(key), "slug": slug, **wound_down}, in_org=audit_org,
    )
    session.commit()
    _ = user.email


def _purge(
    session: Session, user: AdminUser, key: UUID, audit_org: UUID, wound_down: dict
) -> list[tuple[Optional[str], str]]:
    """`purge_org`'s last step: one transaction under the org row's `FOR UPDATE` — outside references detached
    (`detach_outside_references`), every owned row purged, the `orgs` row deleted, the `org_deleted` audit row
    staged after the trigger ran. Returns the attachment objects' `(bucket, key)` to reap after the commit.

    The `DBAPIError` arm is the floor speaking (a table the purge missed, a reference it could not detach);
    `ORG_DELETE_REFUSED_SQLSTATES` owns the shared-SQLSTATE trap. Anything else re-raises."""
    # Re-read under the lock: `populate_existing` forces the round trip; a bare `session.get` would be served from
    # the identity map and lock nothing. Serialises concurrent deletes and a concurrent re-enable.
    row = session.get(Org, key, with_for_update=True, populate_existing=True)
    if row is None:
        return []
    # A Restore that landed during the offboarding wins: only a deleted org is purged.
    if row.deleted_at is None:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"'{row.name}' ({row.slug}) was restored — nothing was purged.")
    name, slug = row.name, row.slug
    try:
        # Object keys first, while the rows naming them exist (`org_owned_attachment_keys`).
        doomed_object_keys = org_owned_attachment_keys(session, key)
        # The org's signed compliance manifests: their erase is enqueued in this transaction, before the purge
        # takes the index rows that name a moved-in task's objects (`compliance_seal.erase_for_org`). An edition
        # without compliance signs none.
        if edition_features.enabled("compliance"):
            from app.durable import compliance_seal

            compliance_seal.erase_for_org(session, key, user, in_org=audit_org)
        detached = detach_outside_references(session, key)
        deleted = purge_org_rows(session, key)
        # The ORM delete, so the instance leaves the identity map. Safe past the write seam: `Org` is an
        # `Axis` class, absent from the derivation map. Flushed here so the trigger has run before the audit row.
        session.delete(row)
        session.flush()
        record_write(
            session,
            user,
            "org_deleted",
            f"Deleted org {name} ({slug})",
            data={
                "org_id": str(key), "slug": slug, "rows_deleted": deleted, "references_detached": detached,
                **wound_down,
            },
            in_org=audit_org,
        )
        # Same `PROBED_ENTITIES` requirement as create/update; `deleted` like every other delete path.
        emit_console_event(session, entity="org", id=key, action="deleted")
        session.commit()
    except DBAPIError as exc:
        orig = getattr(exc, "orig", None)
        # `diag` is psycopg's, so reads are defensive; a driver without it leaves the exception alone.
        diag = getattr(orig, "diag", None)
        if getattr(orig, "sqlstate", None) not in ORG_DELETE_REFUSED_SQLSTATES:
            raise
        session.rollback()
        # Decided on `constraint_name`, not SQLSTATE: both cases share 23001, and only referential integrity
        # names a constraint.
        constraint = getattr(diag, "constraint_name", None)
        if constraint:
            table = getattr(diag, "table_name", None)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a row outside this org still references one of its rows ({constraint}"
                    + (f" on {table}" if table else "")
                    + ") and could not be detached from it, so the org's rows were not purged. Its work was "
                    "stopped and its nodes deleted; running Compliance Delete again retries the purge."
                ),
            ) from None
        # `message_primary`, not `str(orig)`, which appends the plpgsql CONTEXT trace.
        primary = getattr(diag, "message_primary", None) or str(orig)
        raise HTTPException(status_code=409, detail=primary) from None
    return doomed_object_keys


class OffboardIn(BaseModel):
    """`execute: false` (the default) is the dry run: the census and its blockers, nothing touched. `execute: true`
    is refused: the purge's one door is Compliance Delete (`compliance_delete_org`)."""

    execute: bool = False


def _census_out(row: Org, found: offboard.Census, blockers: list[str]) -> dict:
    return {
        "execute": False, "org": {"id": str(row.id), "slug": row.slug, "name": row.name},
        "blockers": blockers, "stores": found.counts(),
    }


@router.post("/api/orgs/{org_id}/offboard")
@superadmin_only(Tenancy.OPERATOR)
async def offboard_org(
    org_id: str,
    body: OffboardIn,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> dict:
    """The offboarding dry run: every store holding the org's data, counted, and every reason an execution would
    be refused now (`app/tenancy/offboard.py::census`). Executing is Compliance Delete's alone — one door to the
    purge, 2FA-authorized — so `execute: true` is 409 here."""
    if body.execute:
        raise HTTPException(
            status_code=409,
            detail="Offboarding runs as Compliance Delete: delete the org, then POST /compliance-delete with a 2FA code.",
        )
    row = _one(session, org_id)
    found = await run_in_threadpool(offboard.census, session, row, user)
    connected = set(await run_in_threadpool(node_bus.registry.connected_nodes))
    blockers = found.blockers + offboard.node_blockers(found, connected)
    session.rollback()
    return _census_out(row, found, blockers)


class ComplianceDeleteIn(BaseModel):
    """`execute: false` (the default) is the offboarding dry run; `execute: true` purges, authorized by `code`, a
    current TOTP code of the operator's own second factor."""

    code: str = Field(default="", max_length=64)
    execute: bool = False


@router.post("/api/orgs/{org_id}/compliance-delete")
@superadmin_only(Tenancy.OPERATOR)
async def compliance_delete_org(
    org_id: str,
    body: ComplianceDeleteIn,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> dict:
    """Compliance Delete — the ONLY purge: offboard a soft-deleted tenant, every store holding its data
    emptied and re-read for residue (`app/tenancy/offboard.py`), then its rows purged (`purge_org`).

    409 unless the org is soft-deleted. The dry run answers the census. Executing needs the operator's own TOTP
    (`mfa.verify_current_code`, no recovery codes): an operator with no second factor is refused "Set up 2FA
    first."; a wrong code 403, its failure counted. Then the offboarding preconditions (409 on any blocker).

    The surviving record is the `org_compliance_deleted` audit row, filed in the actor's org (the operator plane's
    when that is the org purged), so it escapes the purge as `org_deleted` does: who authorized it, when, the TOTP
    outcome, and the offboarding record. Filed whether the
    offboarding finished or stopped, in its own transaction; a stop is a 409 carrying the partial record."""
    row = _one(session, org_id)
    if row.deleted_at is None:
        raise HTTPException(
            status_code=409, detail=f"'{row.name}' ({row.slug}) is not deleted — delete it before Compliance Delete.",
        )
    started_at = now_iso(timespec="seconds")
    found = await run_in_threadpool(offboard.census, session, row, user)
    connected = set(await run_in_threadpool(node_bus.registry.connected_nodes))
    blockers = found.blockers + offboard.node_blockers(found, connected)
    if not body.execute:
        session.rollback()
        return _census_out(row, found, blockers)

    if blockers:
        session.rollback()
        raise HTTPException(status_code=409, detail=" ".join(blockers))
    org = {"id": str(row.id), "slug": row.slug, "name": row.name}
    if not mfa.is_enrolled(session, user):
        session.rollback()
        raise HTTPException(status_code=403, detail="Set up 2FA first.")
    check = mfa.verify_current_code(session, user, body.code, allow_recovery=False)
    if not check.ok:
        outcome = "throttled" if check.throttled else "wrong"
        # A refused step-up on the purge is a security event; committed with the failure count.
        record_write(
            session, user, ORG_COMPLIANCE_DELETE_REFUSED,
            f"Compliance Delete of org {org['name']} ({org['slug']}) refused: {outcome} 2FA code",
            data={"org_id": org["id"], "slug": org["slug"], "totp": outcome},
        )
        session.commit()
        raise HTTPException(
            status_code=403, detail="Too many wrong codes. Wait, then retry." if check.throttled else "Wrong code.",
        )
    authorized_at = now_iso(timespec="seconds")
    # The used step is spent now, so the code cannot authorize a second purge; committed before the slow hops.
    session.commit()

    stores: dict = {}
    failure: Optional[BaseException] = None
    try:
        await offboard.execute(session, row, found, user, connected, stores)
        record = offboard.record(org, user, started_at, stores)
    except offboard.OffboardStopped as stop:
        record = offboard.record(org, user, started_at, stores, stopped=stop.sentence)
        # The partial record rides the 409, so the tool files it as evidence too.
        failure = HTTPException(status_code=409, detail={"message": stop.sentence, "record": record})
    except BaseException as exc:  # noqa: BLE001 — every stop past the first step is filed, then re-raised
        # `purge_org` refusing the purge (a missed table, an outside reference) is the expected one.
        session.rollback()
        reason = str(exc.detail) if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        record = offboard.record(org, user, started_at, stores, stopped=reason)
        failure = exc
    # Filed whether it finished or stopped, in its own transaction: the tenant's own history is gone.
    record_write(
        session, user, ORG_COMPLIANCE_DELETED,
        # From `org`, not `row`: after the purge the expired instance would re-SELECT a deleted row.
        f"Compliance-deleted org {org['name']} ({org['slug']})" if record["complete"]
        else f"Compliance Delete of org {org['name']} ({org['slug']}) incomplete",
        data={
            "org_id": org["id"], "slug": org["slug"],
            "authorized_by": {"user_id": str(user.id), "email": user.email},
            "authorized_at": authorized_at, "totp": "verified", "offboard": record,
        },
        in_org=user.org_id if str(user.org_id) != org["id"] else ORG_ZERO_ID,
    )
    session.commit()
    if failure is not None:
        raise failure
    return {"execute": True, "record": record}


# ── Email domains (access-model C6, D4) ────────────────────────────────────────────────────────────
# A verified Google sign-in whose email is on one of these domains joins the org (`routes/auth.py`). A
# domain belongs to at most one org (`uq_org_domains_domain`, global) and is stored lowercase.

_DOMAIN_UNIQUE = "uq_org_domains_domain"


def _domains_out(session: Session, rows: list[OrgDomain]) -> list[OrgDomainOut]:
    # `actor_names`, not `actor_names_for`: a domain row is created, never updated (no `updated_by_id`).
    names = actor_names(session, [row.created_by_id for row in rows])
    return [
        OrgDomainOut(
            domain=row.domain, created_at=iso_or_none(row.created_at), created_by=names.get(row.created_by_id)
        )
        for row in rows
    ]


@router.get("/api/orgs/{org_id}/domains", response_model=list[OrgDomainOut])
@org_admin(Tenancy.CALLER_PLANE)
def list_org_domains(
    org_id: str,
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[OrgDomainOut]:
    """The org's email domains, alphabetically — for the Org page, so an Org Admin sees who may join."""
    row = administered_org_or_404(session, org_id, principal)
    rows = list(session.scalars(
        select(OrgDomain).where(OrgDomain.org_id == row.id).order_by(OrgDomain.domain)
    ))
    return _domains_out(session, rows)


@router.post("/api/orgs/{org_id}/domains", response_model=OrgDomainOut, status_code=201)
@superadmin_only(Tenancy.OPERATOR)
def add_org_domain(
    org_id: str,
    body: OrgDomainIn,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> OrgDomainOut:
    """Register an email domain on an org. 422 a malformed domain or org zero (the control plane's own org
    takes no sign-ups), 409 a domain already on any org."""
    row = _live_one(session, org_id)
    domain = access.normalized_domain(body.domain)
    if row.id == ORG_ZERO_ID:
        raise HTTPException(status_code=422, detail="Org zero is the operator plane; it takes no email domain.")
    conflict = "That domain already belongs to an organization. Remove it there first."
    if session.scalar(select(OrgDomain.id).where(OrgDomain.domain == domain)) is not None:
        raise HTTPException(status_code=409, detail=conflict)
    created = OrgDomain(org_id=row.id, domain=domain, created_by_id=user.id)
    session.add(created)
    flush_or_409(session, conflict, constraints=frozenset({_DOMAIN_UNIQUE}))
    record_write(
        session, user, "org_domain_add", f"Added email domain {domain} to org {row.name}",
        data={"org_id": str(row.id), "domain": domain},
    )
    session.commit()
    session.refresh(created)
    return _domains_out(session, [created])[0]


@router.delete("/api/orgs/{org_id}/domains/{domain}", status_code=204)
@superadmin_only(Tenancy.OPERATOR)
def remove_org_domain(
    org_id: str,
    domain: str,
    user: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> Response:
    """Remove an email domain from an org — 404 when it is not on this org. Users who joined through it
    stay; only future sign-ins stop joining."""
    row = _live_one(session, org_id)
    normalized = access.canonical_domain(domain)
    found = session.scalar(
        select(OrgDomain).where(OrgDomain.org_id == row.id, OrgDomain.domain == normalized)
    )
    if found is None:
        raise HTTPException(status_code=404, detail="no such domain on this org")
    session.delete(found)
    record_write(
        session, user, "org_domain_remove", f"Removed email domain {normalized} from org {row.name}",
        data={"org_id": str(row.id), "domain": normalized},
    )
    session.commit()
    return Response(status_code=204)


# The org's plan and its overrides (`GET`/`PUT /api/orgs/{org_id}/plan`) live in `routes/billing_org_plan.py`, on this
# router: the community edition runs no plans, so they are the cloud's alone. Imported last: that module reads this one.
if edition_features.enabled("billing"):
    from app.routes import billing_org_plan  # noqa: E402, F401 — registers its routes on `router`
