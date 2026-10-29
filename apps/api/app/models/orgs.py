"""Orgs — the tenancy axis — and their branding, settings, sign-up, SSO and membership-transfer tables."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ColumnElement,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    exists,
    func,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CIDR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.app_defaults import DEFAULT_APP_TITLE, default_app_title
from app.models.base import (
    _actor_column,
    Base,
    EntityMixin,
    _in_check,
    org_unique,
    OrgScoped,
    StampedMixin,
    timestamp_column,
    UuidPkMixin,
)


#: `orgs.lifecycle` (`ck_orgs_lifecycle`). `app/org_gate.py` reads it.
ORG_PENDING, ORG_ACTIVE, ORG_REJECTED = "pending", "active", "rejected"


ORG_LIFECYCLES = (ORG_PENDING, ORG_ACTIVE, ORG_REJECTED)


# The IdPs an org may be SSO-only through (`Org.sso_provider`): the console's own Google and Microsoft
# sign-in doors. GitHub is never one: it carries no tenant to pin.
SSO_PROVIDERS = ("microsoft", "google")


#: The four per-org cap columns, in `app/org_limits.py::LIMIT_KINDS` order less Agents, whose override is Max agents
#: (a test pins the pairing). `limit_runner_slots` and `limit_active_worktrees` are retired: unmapped,
#: hidden by `migrations/env.py::_RETIRING_COLUMNS`, dropped with their CHECKs by the next contraction revision.
ORG_LIMIT_COLUMNS = ("limit_nodes", "limit_projects", "limit_users", "limit_github_runners")


#: The four per-org quota columns, in `app/org_quotas.py::QUOTA_KINDS` order (a test pins the pairing).
ORG_QUOTA_COLUMNS = (
    "limit_agent_requests_per_minute", "limit_task_attachment_mb", "limit_org_attachment_gb",
    "limit_attachment_file_mb",
)


#: The quota columns a plan carries too (`app/org_quotas.py::PLAN_QUOTA_KINDS` order): NULL = `_default`'s value.
BILLING_PLAN_QUOTA_COLUMNS = ORG_QUOTA_COLUMNS


#: `org_signups.provider` — how the owner proved their identity: one of the three IdPs, or `email` (a code or
#: link sent to the address, `email_signups`).
SIGNUP_PROVIDERS = ("google", "github", "microsoft", "email")


#: `orgs.account_type` (`ck_orgs_account_type`): what the applicant chose at sign-up. Every org born before the
#: column, and every operator-created one, reads `organization` through the default.
ACCOUNT_INDIVIDUAL, ACCOUNT_ORGANIZATION = "individual", "organization"


ACCOUNT_TYPES = (ACCOUNT_INDIVIDUAL, ACCOUNT_ORGANIZATION)


class Org(EntityMixin, Base):
    """An org — the tenancy axis, and the table every `org_id` points at.

    The operator is a row (org zero, `app/tenancy/ids.py::ORG_ZERO_ID`), not a NULL, so every `org_id`
    can be NOT NULL and a forgotten predicate cannot fall through a nullable arm. No `org_id` here (the
    one `Axis` verdict in `app/tenancy/manifest.py`).

    `slug` is the external handle (a tenant org's tailnet tag `tag:org-<slug>`), `name` the display
    label. Grammar: `app/routes/commands.py::_SLUG_RE`, enforced by
    `app/routes/orgs.py::_validated_slug`; lowercase-only under a case-sensitive unique index. A self-service
    org's is generated (`app/org_slug.py`). Only an operator renames it, and only a tenant org's:
    `app/routes/orgs.py::_rename_slug` moves its tailnet tag with it.

    `enabled` is step one of a disable-then-destroy lifecycle; it gates dispatch and the domain join but not
    an existing user's login (`app/routes/orgs.py` says so). NOT NULL, default true.

    `lifecycle` is the self-service approval state (`ORG_LIFECYCLES`): a self-service org is born
    `pending`, an operator approves it `active` or rejects it `rejected` (with `enabled=false`), and
    re-enabling a rejected org re-admits it `active`; every operator-created org is `active`.
    `app/org_gate.py` is what reads it, including the rejected member's refusal at every sign-in door. A self-service org is one with an
    `OrgSignup` row, never a flag here.

    `limit_*` are the operator's per-org caps (`app/org_limits.py` owns what each counts): NULL = the
    platform default knob, 0 = unlimited. Operator-owned columns, never `tier="org"` knobs.
    `cron_min_period_seconds` is the operator's per-org minimum cron period, `cron_min_period_own_seconds`
    the org administrator's own (raise-only); NULL = none. `app/cron_min_period.py` owns the hierarchy.
    `plan_chosen_at` stamps when the org's owner picked a plan (`app/org_plans.py`); NULL = not yet.
    `plan_key` is the org's plan (a `billing_plans.key`; every insert names it — there is no default plan), kept
    in step with Stripe both ways; `stripe_plan_key` is the plan Stripe was last seen to entitle (`-` = none,
    `app/billing/status.py::ENTITLED_NONE`; NULL = never observed). `app/org_plan_admin.py` owns both.

    The per-org plan overrides (revision 0374; read from `app/plan_terms.py` once it
    lands): `plan_started_at` stamps when `plan_key` last CHANGED, the base of a live plan end and of credit months;
    `plan_end_overridden` with `plan_end_override` NULL = never ends, with a date = ends then; `hosting_allowed_override`
    NULL = the plan's, and so `compliance_enabled_override` (revision 0378), `nodevault_allowed_override`
    (revision 0381), `nodelink_allowed_override` (revision 0405) and `merge_lane_enabled_override` (revision 0407);
    `products_overridden` makes the org's `org_plan_products` rows its whole product list (empty is valid);
    `pending_plan_key` is a priced plan chosen at sign-up and awaiting checkout.

    `colocation_allowed` is the operator's exception to a tenant org's isolation defaults
    (`app/tenant_defaults.py`): a node on another org's Multi-Node Host. Operator-owned like `limit_*`; false for
    every new org, and read only for a tenant. `shared_store_allowed` is RETIRED (revision 0405): NodeLink is now the
    plan term `nodelink_allowed`; the column stays while two releases overlap and nothing reads it.

    `hipaa_enabled`/`hipaa_baa_effective_date`/`hipaa_acks` are HIPAA mode (`app/hipaa.py`): operator-only, off by default.

    `sso_provider` set makes the org SSO-only (one control, no separate flag): its members sign in only
    through that IdP (`SSO_PROVIDERS`) from the tenant `sso_tenant` names — an Entra tenant id, or a Google
    Workspace `hd` domain. `sso_mfa_attested_at`/`_by_id` record who attested that the IdP enforces MFA; only
    then does the sign-in count as the second factor. `app/sso_policy.py` is what reads all four.
    """

    __tablename__ = "orgs"
    __table_args__ = (
        # Named so an IntegrityError is greppable; unique because the slug is the external handle.
        UniqueConstraint("slug", name="uq_orgs_slug"),
        CheckConstraint(
            "lifecycle IN (" + ", ".join(f"'{s}'" for s in ORG_LIFECYCLES) + ")",
            name="ck_orgs_lifecycle",
        ),
        _in_check("account_type", ACCOUNT_TYPES, "ck_orgs_account_type"),
        *(
            CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=f"ck_orgs_{column}")
            for column in ORG_LIMIT_COLUMNS + ORG_QUOTA_COLUMNS
        ),
        *(
            CheckConstraint(f"{column} IS NULL OR {column} > 0", name=f"ck_orgs_{column}")
            for column in ("cron_min_period_seconds", "cron_min_period_own_seconds", "min_agents_override")
        ),
        *(
            CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=f"ck_orgs_{column}")
            for column in ("max_agents_override", "static_slots_override")
        ),
        CheckConstraint(
            "sso_provider IS NULL OR sso_provider IN (" + ", ".join(f"'{p}'" for p in SSO_PROVIDERS) + ")",
            name="ck_orgs_sso_provider",
        ),
        CheckConstraint("(sso_provider IS NULL) = (sso_tenant IS NULL)", name="ck_orgs_sso_tenant"),
        CheckConstraint("sso_mfa_attested_at IS NULL OR sso_provider IS NOT NULL", name="ck_orgs_sso_attested"),
        CheckConstraint(
            "billing_interval IS NULL OR billing_interval IN ('month', 'year')", name="ck_orgs_billing_interval",
        ),
        CheckConstraint("parent_org_id IS NULL OR parent_org_id <> id", name="ck_orgs_parent_not_self"),
        # One live staging org per parent (`app/staging_org.py`, D-F7).
        Index(
            "uq_orgs_parent_org_id", "parent_org_id", unique=True,
            postgresql_where=text("parent_org_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        # The credential roster's Org sort and its operator search (revision 202610232130).
        Index("ix_orgs_lname_id", text("lower(name)"), "id"),
        Index(
            "ix_orgs_name_trgm", text("lower(name)"),
            postgresql_using="gin", postgresql_ops={"lower(name)": "gin_trgm_ops"},
        ),
        Index(
            "ix_orgs_slug_trgm", text("lower(slug)"),
            postgresql_using="gin", postgresql_ops={"lower(slug)": "gin_trgm_ops"},
        ),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # The role a domain-joined user is bound to at org scope; NULL = none (they reach nothing until bound).
    # `use_alter`: `roles` is declared below this class.
    default_role_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="SET NULL", use_alter=True, name="fk_orgs_default_role_id"),
        nullable=True,
    )
    lifecycle: Mapped[str] = mapped_column(
        Text, nullable=False, default=ORG_ACTIVE, server_default=ORG_ACTIVE
    )
    # Soft delete (revision 202610130600): set = deleted, hidden from every org list and refused at
    # sign-in and dispatch, its rows kept until Compliance Delete purges them. No FK: the user may be purged.
    deleted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    deleted_by_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # A staging org's parent (revision 202610191400): set = this org shares the parent's plan and bill
    # (`app/staging_org.py`; its terms are the parent's, `plan_terms.terms_org`). NULL = an ordinary org.
    parent_org_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("orgs.id", name="fk_orgs_parent_org_id"), nullable=True,
    )
    plan_chosen_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    limit_nodes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_projects: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_users: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_github_runners: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # `app/org_quotas.py`: NULL = the platform default, 0 = unlimited.
    limit_agent_requests_per_minute: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_task_attachment_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_org_attachment_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_attachment_file_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cron_min_period_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cron_min_period_own_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    colocation_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    shared_store_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The org's provider grid override: `[row, auth]` cells (`plan_terms.providers_term`); NULL = the plan's.
    providers_override: Mapped[Optional[list]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    sso_provider: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sso_tenant: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sso_mfa_attested_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # SET NULL: deleting the attester must not block the org; the attestation stands until changed.
    sso_mfa_attested_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", use_alter=True, name="fk_orgs_sso_mfa_attested_by_id"),
        nullable=True,
    )
    # Skip the console's second factor for every member: an operator-only setting for orgs used to
    # record videos and documentation. Never honoured for an operator-plane org (`app/mfa.py::org_skips`).
    mfa_skip: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # HIPAA mode (`app/hipaa.py`, the single home of what it changes): the operator's switch, the BAA effective date it
    # was turned on under (required while on, `routes/orgs.py::_apply_hipaa`), and each non-BAA surface warning an org
    # admin acknowledged, `{surface: {"by": <admin_user uuid>, "at": <iso ts>}}`.
    hipaa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    hipaa_baa_effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    hipaa_acks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # What the org's per-agent plan subscription bought (revision 202610172101): the plan item's quantity and its
    # interval, written from the subscription mirror only (`app/org_plan_admin.py::on_entitlement`); the agents
    # bought are the runner-slot cap of a per-agent plan (`app/plan_terms.py::caps_of`).
    agents_bought: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    billing_interval: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The per-agent plan subscription's `trial_end` while it is `trialing`, else NULL (revision 202610220037): the
    # mirror the trial agent cap reads (`plan_terms.trial_live`), written with `agents_bought`. `agents_trial_peak` is
    # the most agents the org wanted at once this trial (`app/trial_agents.py`), reset when a new trial starts.
    agents_trial_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    agents_trial_peak: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Slack messages on the org's behalf dropped because its plan leaves Slack out (revision 202610172330),
    # counted by `app/slack_egress.py::record_plan_drop` and shown on the customer's Plan tab.
    slack_plan_drops: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    plan_key: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="-", server_default="-")
    plan_started_at: Mapped[datetime] = timestamp_column()
    plan_end_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    plan_end_override: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    hosting_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    compliance_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Advanced Compliance (revision 202610120000): the plan switch's per-org override (`plan_terms._switch`).
    advanced_compliance_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    nodevault_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    nodelink_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    merge_lane_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # MicroVM nodes (revision 202610111900): the plan switch's per-org override (`plan_terms._switch`), and
    # the org's VM size ceilings — NULL = no org value, the next tier down decides (`app/microvm_sizing.py`).
    microvm_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    microvm_vcpus: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    microvm_ram_mib: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    microvm_disk_gib: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Playbooks (revision 202610122200): the plan switch's per-org override (`plan_terms._switch`); NULL = the plan's.
    playbooks_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Templates (org) (revision 202610281500): the plan switch's per-org override; NULL = the plan's.
    org_templates_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Node types (revision 202610131600): the three plan switches' per-org overrides, one per `nodes.os`
    # (`plan_terms.NODE_OS_SWITCHES`); NULL = the plan's.
    ubuntu_nodes_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    macos_nodes_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    windows_nodes_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Project memory (revision 202610171200): the plan switch's per-org override (`plan_terms._switch`); NULL = the plan's.
    project_memory_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Slack and the eight enterprise switches (revision 202610171800, `plan_terms.SWITCHES`): per-org overrides;
    # NULL = the plan's.
    slack_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    sso_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    scim_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    siem_export_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    data_residency_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    ip_allowlist_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    org_retention_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    staging_org_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    budgets_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Jaah AI (revision 202610200017): the plan switch's per-org override (`plan_terms._switch`); NULL = the plan's.
    jaah_ai_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Audit and Session transcripts (revision 202610210100): the plan switches' per-org overrides; NULL = the plan's.
    audit_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    session_transcripts_enabled_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Your Cloud per provider (revision 202610211437): the plan switches' per-org overrides; NULL = the plan's.
    aws_cloud_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    gcp_cloud_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    azure_cloud_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Tenant email (revision 202610240100): the plan switch's per-org override; NULL = the plan's.
    tenant_email_allowed_override: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # The plan's other limits (revision 202610200100, `plan_terms`): the agent range a per-agent purchase must fall
    # in (NULL = the plan's; 0 on the max = no upper bound), static slots, and the then plan — while
    # `then_plan_overridden`, `then_plan_key_override` (NULL then = rolls into no plan), else the plan's.
    min_agents_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_agents_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    static_slots_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    then_plan_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # A renamed plan key follows; a deleted plan leaves the override at none.
    then_plan_key_override: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "billing_plans.key", onupdate="CASCADE", ondelete="SET NULL", use_alter=True,
            name="fk_orgs_then_plan_key_override",
        ),
        nullable=True,
    )
    # The org's own retention windows in days (revision 202610190100): transcripts, activity events, task
    # artifacts. NULL = the deployment's window. Shorten only; bounds and the sweep: `app/org_retention.py`.
    retention_transcripts_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retention_events_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retention_artifacts_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The org's data region (revision 202610191600): an AWS region code from the operator's
    # `data_regions` knob. NULL = no choice made (the control-plane region). Rules: `app/data_residency.py`.
    data_region: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    products_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    pending_plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The onboarding latch (D15): stamped the first time every "Set up your company" step is done (hiding the
    # card does not end it), and never cleared. While NULL a new node or project is auto-assigned
    # (`app/slot_allocation.py::auto_assign`); once set, assignment is the operator's alone.
    onboarding_completed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # `ACCOUNT_TYPES`: what the applicant chose at sign-up (revision 0351). Never NULL.
    account_type: Mapped[str] = mapped_column(
        Text, nullable=False, default=ACCOUNT_ORGANIZATION, server_default=ACCOUNT_ORGANIZATION
    )


def in_live_org(org_id: ColumnElement[UUID]) -> ColumnElement[bool]:
    """A row's `org_id` names no soft-deleted org: an org's members, projects and nodes are deleted
    with it, so their reads filter on this (`authz/visibility.py`'s scoped selects, the People directory) and
    Restore shows them again. A correlated NOT EXISTS on the org's primary key."""
    return ~exists().where(Org.id == org_id, Org.deleted_at.is_not(None))


class OrgDomain(UuidPkMixin, OrgScoped, Base):
    """An email domain whose verified Google sign-ins join this org. A domain belongs to at most one org
    (`uq_org_domains_domain` is global). Stored lowercase (`ck_org_domains_lowercase`) — the case-insensitive
    uniqueness `citext` would give, without an extension."""

    __tablename__ = "org_domains"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("domain", name="uq_org_domains_domain"),
        CheckConstraint("domain = lower(domain) AND domain <> ''", name="ck_org_domains_lowercase"),
        Index("ix_org_domains_org_id", "org_id"),
    )

    domain: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[UUID] = _actor_column()
    created_at: Mapped[datetime] = timestamp_column()


class OrgIpRange(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """One range of an org's IP allowlist: with any row saved, the org's people may use the console and
    API only from inside one of its ranges. `app/ip_allowlist.py` owns every rule. `label` says whose network it
    is. A line of the org's one list, edited only as that list, so not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "org_ip_ranges"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "cidr", name="uq_org_ip_ranges_org_id_cidr"),
    )

    cidr: Mapped[str] = mapped_column(CIDR, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class OrgSiemDestination(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """An org's SIEM export destination: one HTTPS endpoint per org that the `siem-export` cron POSTs the
    org's activity events to as JSON. `app/siem_export.py` owns every rule — the URL check, the signing, the cursor.
    `secret_sealed` is the HMAC key or the auth header's value, a tier-1 envelope bound to the row id
    (`siem_export.seal_secret`), never shown after save. `cursor_event_id` is the last `activity_events.id` delivered;
    `last_*` describe the latest attempt. The org's one setting, not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "org_siem_destinations"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_org_siem_destinations_org_id"),
        CheckConstraint("auth_mode IN ('header', 'hmac')", name="ck_org_siem_destinations_auth_mode"),
        CheckConstraint("url LIKE 'https://%'", name="ck_org_siem_destinations_url_https"),
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    #: The header carrying the secret in `header` mode; NULL in `hmac` mode.
    header_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    secret_sealed: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cursor_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The HTTP status of the latest attempt; NULL when no response came back.
    last_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: Why the latest attempt failed; NULL after a delivery succeeded.
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ScimToken(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """One SCIM bearer token of an org: what its identity provider presents on `/api/public/scim/v2`.
    `app/scim.py` owns every rule. Only the SHA-256 of the token is stored (`token_hash`); `prefix` is its first
    characters, shown so an admin can tell two apart. Revoked by setting `revoked_at`; deleted only once revoked, by an
    admin clearing it from the list, or with the admin who made it (`scim.forget_creator`). A child of the org's SCIM setting, not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "scim_tokens"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("token_hash", name="uq_scim_tokens_token_hash"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class OrgSsoDomain(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """One email domain an org claims for SAML/OIDC sign-in. `app/org_sso.py` owns every rule. It counts
    only once `verified_at` is set: the org proved it with a DNS TXT record `jaah-verification=<verification_token>`
    at `_jaah.<domain>`. Many orgs may claim a domain; one may verify it (`uq_org_sso_domains_verified_domain`).
    Stored lowercase, as `org_domains`. A line of the org's SSO setting, not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "org_sso_domains"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "domain", name="uq_org_sso_domains_org_id_domain"),
        CheckConstraint("domain = lower(domain) AND domain <> ''", name="ck_org_sso_domains_lowercase"),
        Index(
            "uq_org_sso_domains_verified_domain", "domain", unique=True,
            postgresql_where=text("verified_at IS NOT NULL"),
        ),
    )

    domain: Mapped[str] = mapped_column(Text, nullable=False)
    verification_token: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class OrgSsoConnection(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """An org's one SAML 2.0 or OIDC identity provider. `app/org_sso.py` owns every rule. SAML keeps the
    IdP metadata (pasted, or fetched from `saml_idp_metadata_url` on save); OIDC names an `oidc_client` credential
    (issuer, client id, sealed client secret). `tested_at` is the last passing test sign-in, cleared whenever the IdP
    changes; `enforced` (SSO only) may be set only while it is. The org's one setting, not an entity
    (`tests/test_entity_shape.py`)."""

    __tablename__ = "org_sso_connections"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_org_sso_connections_org_id"),
        CheckConstraint("protocol IN ('saml', 'oidc')", name="ck_org_sso_connections_protocol"),
    )

    protocol: Mapped[str] = mapped_column(String(8), nullable=False)
    saml_idp_metadata_xml: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    saml_idp_metadata_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    oidc_credential_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credentials.id", ondelete="RESTRICT", name="fk_org_sso_connections_oidc_credential_id"),
        nullable=True,
    )
    jit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    tested_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_org_sso_connections_tested_by_id"),
        nullable=True,
    )


class SsoSpentRequest(Base):
    """A SAML AuthnRequest id an ACS has already answered: a Response must answer an outstanding request
    (`allow_unsolicited` is off), so a spent id refuses the replay of a stolen flow cookie plus SAMLResponse pair
    (`app/sso_signin.py::spend_request`). Kept only while a flow cookie could still carry the id; older rows are
    pruned as new ones land. No tenant data: an opaque id the SP minted."""

    __tablename__ = "sso_spent_requests"
    __table_args__ = (
        PrimaryKeyConstraint("request_id", name="pk_sso_spent_requests"),
        Index("ix_sso_spent_requests_spent_at", "spent_at"),
    )

    request_id: Mapped[str] = mapped_column(Text)
    spent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OrgSignup(OrgScoped, Base):
    """The self-service sign-up behind one org: one row per self-service org, keyed by the org (the PK is
    the "one per org" guarantee). Its existence IS what makes an org self-service. `owner_id` is the user
    who signed up; the composite `(owner_id, org_id)` key (`app/tenancy/manifest.py`) holds them in it.

    `decided_*`/`rejection_reason` record the operator's approve/reject; `slack_*`/`hubspot_*` the two
    best-effort notifications' outcomes. `occupation` (an individual's) / `job_title` (an organization
    applicant's) and `intended_use` are the application's answers, held here and nowhere else (revision 0351);
    NULL on a sign-up made before they were asked. `welcome_*` summarise the LATEST welcome email: when, by
    whom, and its failure (NULL = sent). Not an entity (no name; the org carries it)."""

    __tablename__ = "org_signups"
    __table_args__ = (
        _in_check("provider", SIGNUP_PROVIDERS, "ck_org_signups_provider"),
        Index("ix_org_signups_owner_id", "owner_id"),
    )

    org_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_org_signups_owner_id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    signed_up_at: Mapped[datetime] = timestamp_column(server_now=False)
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    decided_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_org_signups_decided_by_id"), nullable=True
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_posted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    slack_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hubspot_contact_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hubspot_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    occupation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    job_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intended_use: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    welcome_sent_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    welcome_sent_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_org_signups_welcome_sent_by_id"), nullable=True
    )
    welcome_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The plan the applicant chose (a `billing_plans.key`, revision 0374); NULL = none chosen (the default plan).
    plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The multi-step sign-up (revision 202610201527). ISO 3166-1 alpha-2.
    country: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # NULL = a draft the applicant has not submitted yet; a row from before drafts carries its `signed_up_at`.
    submitted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When the applicant confirmed their plan at Sign up; it drives the guided setup's plan step.
    plan_confirmed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Stripe ids only, never a client secret.
    setup_intent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_method_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When the sign-up's billing details were last saved (the applicant's step, or the operator's details save while
    # it is unsubmitted; revision 202610262100); NULL = not saved. The roster's Stage reads it (`app/signup_stage.py`).
    billing_saved_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Why subscribing at approval failed, shown on the approval screen; NULL = no failure.
    billing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EmailSignup(UuidPkMixin, OrgScoped, Base):
    """One email address proving it is reachable before it may apply for an org (revision 0351).

    Always org zero: nobody owns the address yet, so the row is the operator's pre-tenant data, like
    `SiteEnquiry`. `email` is stored lower-cased. Only HMAC digests of the code and the link token are kept
    (`code_hash`, `link_hash`); a successful verify clears both and stamps `verified_at`, and `password_hash`
    is the bcrypt hash the applicant chose after it. `consumed_at` is stamped when the application creates the
    org and its owner. At most one unconsumed row per address (`uq_email_signups_open_email`) is what lets an
    applicant resume rather than duplicate. Machine state, append-and-stamp, so no entity shape
    (`tests/test_entity_shape.py`)."""

    __tablename__ = "email_signups"
    __table_args__ = (
        org_unique(__tablename__),
        # The open-row unique index compares addresses as stored, so they are stored lower-cased.
        CheckConstraint("email = lower(email)", name="ck_email_signups_email_lower"),
        Index(
            "uq_email_signups_open_email", "org_id", "email",
            unique=True, postgresql_where=text("consumed_at IS NULL"),
        ),
        Index(
            "uq_email_signups_link_hash", "link_hash",
            unique=True, postgresql_where=text("link_hash IS NOT NULL"),
        ),
    )

    email: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    code_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    code_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    link_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    verified_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_sent_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    consumed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    # The answers given before verification, plus `plan_key` (revision 202610201527); cleared once the draft org
    # is created from them.
    profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class AppSettings(OrgScoped, Base):
    """One org's branding: one row per org (`UNIQUE(org_id)`), holding `app_title`. The row is created
    with its org, never on a read miss (`routes/settings.py`)."""

    __tablename__ = "app_settings"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_app_settings_org_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    app_title: Mapped[str] = mapped_column(
        String(120), nullable=False, default=default_app_title,
        # Derived, not retyped: a literal here would be a second home for the string.
        server_default=DEFAULT_APP_TITLE,
    )


class OrgSetting(OrgScoped, Base):
    """One org's override of one operator knob, the tier above the operator's own.

    `app/operator_settings/` owns the catalogue, which knobs are `org`-tier, and resolution order
    (org > operator > env > default). A table rather than keys in `system_state` so the read seam can
    scope it and it can be indexed. `UNIQUE(org_id, key)` is the business key. Validated by
    `operator_settings` on write and clamped on read, not by the schema.

    Not the standard entity shape: a row has no identity of its own, so only `updated_at` (who changed
    it lives in `activity_events`). Non-`org`-tier keys are refused by the writer (they would be inert)."""

    __tablename__ = "org_settings"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "key", name="uq_org_settings_org_id_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 64 characters, matching `system_state.key`.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # The value alone, never a `{key: value}` blob — this tier is one row PER KNOB.
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class OrgConsentHash(OrgScoped, Base):
    """One org's managed-settings consent hash, learned from its own nodes and served only to them.

    Per org because the hash derives from the Claude account's Anthropic org managed settings, so two
    tenants can legitimately disagree. `app/node_config.py` owns learning and serving.
    `UNIQUE(org_id)`: one row per org. Machinery, not user-authored: no entity shape."""

    __tablename__ = "org_consent_hashes"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_org_consent_hashes_org_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The winning consent record's `updatedAt` (epoch ms, the node's clock) — what "newest" is judged by.
    #: NULL: taught by a daemon too old to report it, or carried over from the fleet-wide row.
    consented_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: The node name that taught it, as a lead for an operator chasing a prompt.
    node: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class TenancyExclusion(Base):
    """The exclusion log: what the org boundary refused, aggregated, in a table an operator can query.

    `app/tenancy/seam.py`'s console trail dedups per process and answers only "which paths"; this answers
    "how often, and still", the standing evidence the enforcing boundary is not quietly cutting callers
    off. Operator-global by design (no `org_id`; `Shared` in `app/tenancy/manifest.py`): its use is
    comparing orgs.

    Aggregated per (minute, class, scope_kind, org, route, mode) by the unique key, never per row: a
    per-query insert on the read path would be its own availability risk. The writer buffers and upserts
    (`app/tenancy/exclusion_log.py`). NULLS NOT DISTINCT is required: `bound_org` is NULL for unbound
    reads, the population that matters most (precedent: `RoleBinding.uq_role_bindings`).

    `scope_kind` tells a correct cross-org refusal (`org`) from a forgotten escape (`unbound`); `mode`
    separates excluded reads from unbound writes (nothing writes `unbound_write` yet). Not an entity
    (`_EXEMPT_TABLES`), like `ActivityEvent`. Retention rides `activity_events_retention_days`
    (`app/cron_retention.py`). Rows scale with the active key set, not traffic (dev measured ~40/min,
    ~21M/year flat, mostly `scope_kind='org'`); re-tune the knob once measured live.
    """

    __tablename__ = "tenancy_exclusions"
    __table_args__ = (
        # The bucket; its single runtime home (`exclusion_log`'s flush resolves it off this table).
        # Named so the flush can use `ON CONFLICT ON CONSTRAINT`, which skips index inference.
        UniqueConstraint(
            "occurred_at", "mapped_class", "scope_kind", "bound_org", "route_or_job", "mode",
            name="uq_tenancy_exclusions_bucket", postgresql_nulls_not_distinct=True,
        ),
        # No second index: the unique's index leads on `occurred_at` and serves the page's window and
        # order.
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Truncated to the minute by the writer (that is the aggregation); hence no `func.now()` default.
    occurred_at: Mapped[datetime] = timestamp_column(server_now=False)
    # The mapped class's `__name__`: the seam and census decide per class.
    mapped_class: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # NULL means unbound (never "unknown"). No FK to `orgs`: the trail outlives the org, as
    # `ActivityEvent.user_id`.
    bound_org: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # Route template plus method, `cron:<activity_type>`, or `unattributed`; never a full path with ids,
    # which would make buckets per request.
    route_or_job: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    # BigInteger so a busy bucket cannot saturate. No server default: the upsert always names its count.
    excluded_count: Mapped[int] = mapped_column(BigInteger, nullable=False)


ORG_INVITATION_STATES = ("pending", "accepted", "declined")


class OrgInvitation(EntityMixin, OrgScoped, Base):
    """An org's invitation to a person who ALREADY has an account elsewhere (multi-org membership R5, 0362).

    `org_id` is the inviting org; `identity_id` the invitee. Nothing is a member until the invitee accepts:
    the accept makes their `admin_users` row in `org_id` (`app/org_invitations.py`), never `is_superadmin`,
    and applies `grant` — the access the inviter chose, `{"groups": [<group name>, ...]}` — verbatim. `grant`
    also carries `typed_names`, the name pair the inviter typed, shown on the row that waits on it
    (`org_invitations.typed_names`). One
    pending invitation per person per org (`uq_org_invitations_pending`). The invitee reads their own under
    `seam.identity_scope()` filtered by `identity_id`, never by org; `name` is the invitee's email."""

    __tablename__ = "org_invitations"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("state", ORG_INVITATION_STATES, "ck_org_invitations_state"),
        Index(
            "uq_org_invitations_pending", "org_id", "identity_id", unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_org_invitations_identity_id", "identity_id"),
        Index("ix_org_invitations_org_id_created_at", "org_id", "created_at"),
    )

    identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("identities.id", ondelete="CASCADE", name="fk_org_invitations_identity_id"), nullable=False
    )
    grant: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class OrgTransferCarryover(UuidPkMixin, Base):
    """One live runner's sanctioned tail across an operator's project move: the org-A node that was running
    `task_id` when its project moved to org B may keep finishing that task in org B (`app/carryover.py`).
    Operator machinery read BEFORE any org is bound, so no `org_id` and a `Shared` verdict
    (`app/tenancy/manifest.py`); `from_org_id`/`to_org_id` record the move, never a scope.

    Bounded per (node, task, session): `worktree_id` names the slot the runner holds; `launch_session_id` is
    the session the grant was written for. `ended_at` is stamped once, by an existing end edge (`carryover.close`), and an
    open row is what `carryover.scope_for` answers. No foreign key reaches a tenant-owned table: the slot row is
    erased after the carry-over ends and the history must outlive it, and a NOT NULL key into `tasks` or `nodes`
    would make the row one org's (`app/tenancy/manifest.py`) when it spans two by design. The reads
    join the live task, so a row whose task or node is gone answers nothing. Written by the transfer engine only,
    never updated except `ended_at`, so no entity shape (`tests/test_entity_shape.py`)."""

    __tablename__ = "org_transfer_carryovers"
    __table_args__ = (
        # One open carry-over per task: a task has one runner, and a second grant would widen the arm.
        Index(
            "uq_org_transfer_carryovers_open_task",
            "task_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        # `scope_for`'s probe, on every agent call that declares a task.
        Index(
            "ix_org_transfer_carryovers_open_node_task",
            "node_id",
            "task_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    transfer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    worktree_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    launch_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    from_org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False)
    to_org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    ended_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
