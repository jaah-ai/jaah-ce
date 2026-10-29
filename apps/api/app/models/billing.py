"""Billing: plans and their products, comps, subscriptions, invoices, checkouts and billing notices."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    desc,
    func,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, EntityMixin, _in_check, org_unique, OrgScoped, timestamp_column, UuidPkMixin
from app.models.orgs import BILLING_PLAN_QUOTA_COLUMNS, ORG_LIMIT_COLUMNS


# ── Billing (`app/billing/`) ─────────────────────────────────────────────────────────────────────────
# Mirrors of Stripe state, written ONLY by `app/billing/mirrors.py` from objects re-fetched at the pinned API
# version. Stripe owns money: no amount here is a source of truth, only the last thing Stripe said. Every
# mirror row carries the `(stripe_account_id, livemode)` it was read from, and every reader filters by the
# connected pair (`app/billing/state.py`), so switching accounts never lets the old account's rows count.
# No column holds card data: `tests/billing/test_no_card_data.py` pins the payment-method display set.

BILLING_EVENT_OUTCOMES = ("applied", "ignored", "rejected", "error")


#: `billing_checkouts.state` (`ck_billing_checkouts_state`); the first three are a LIVE checkout.
BILLING_CHECKOUT_STATES = ("started", "card_saved", "subscribing", "subscribed", "abandoned")


BILLING_CHECKOUT_LIVE = ("started", "card_saved", "subscribing")


BILLING_STATE_ID = 1


class _BillingMirror:
    """The account pair and timestamps every billing mirror row carries."""

    stripe_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    livemode: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


class BillingState(Base):
    """The one row of billing machinery state: which account is connected, and how far the event poll got.

    `account_id`/`livemode` are the PINNED pair (`app/billing/state.py::ensure_pinned`), re-pinned when the
    knob names another credential or the credential row changes (`credential_updated_at`). `events_cursor` is
    the epoch-second `created` of the newest event a COMPLETED poll saw; NULL until the first connect."""

    __tablename__ = "billing_state"
    __table_args__ = (CheckConstraint(f"id = {BILLING_STATE_ID}", name="ck_billing_state_singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=BILLING_STATE_ID)
    account_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    livemode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    credential_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    credential_updated_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    pinned_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    marker_customer_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    events_cursor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_poll_ok_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_allowed_event_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_poll_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


class BillingCustomer(UuidPkMixin, OrgScoped, _BillingMirror, Base):
    """An org's Stripe Customer on one account. One live row per (org, account, mode); a deleted customer
    keeps its row with `deleted_at`, so a new one can be created beside it."""

    __tablename__ = "billing_customers"
    __table_args__ = (
        org_unique(__tablename__),
        Index(
            "uq_billing_customers_org_account", "org_id", "stripe_account_id", "livemode",
            unique=True, postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    deleted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


def _plan_key_fk(column: str) -> ForeignKeyConstraint:
    """A plan naming another plan by key (revision 202610171800): a renamed key follows, a deleted plan clears it.
    Outside the class: `ON UPDATE CASCADE` is a key's, not a second `updated_at` writer
    (`tests/test_entity_shape.py::test_no_entity_model_declares_an_onupdate`)."""
    return ForeignKeyConstraint(
        [column], ["billing_plans.key"], onupdate="CASCADE", ondelete="SET NULL", name=f"fk_billing_plans_{column}",
    )


class BillingPlan(EntityMixin, Base):
    """A plan an org can be entitled to: its limits (NULL = platform default, 0 = unlimited) and the
    Stripe Product/Price it sells as. `price_unit_amount` mirrors the IMMUTABLE Stripe Price, so it can only
    ever equal what Stripe charges. Seeded `team` and `enterprise`, unpriced, and `trial` (revision 0374).

    `duration_days` is how long an org stays on the plan once assigned (NULL = never ends); `hosting_allowed`
    whether its orgs may order hosting; `compliance_enabled` whether Portfolio Compliance is on for its orgs
    (revision 0378, off by default — read only through `app/governance/availability.py`); `nodevault_allowed`
    whether its orgs may use NodeVault (revision 0381, off by default — read through `app/nodevault_policy.py`);
    `nodelink_allowed` whether its orgs may use NodeLink (revision 0405, off by default — read through
    `app/tenant_defaults.py`); `merge_lane_enabled` whether its orgs' runner PRs go through the merge lane (revision
    0407, off by default — read only through `app/plan_terms.py`); `static_slots` how many static worktrees a project may hold on one node (revision 0390, 0 = none — read through
    `app/static_slots_policy.py`). Its bundled products are `billing_plan_products` rows.

    The row keyed `_default` (`app/plan_terms.py::DEFAULTS_KEY`, revision 202610211200) is no plan an org buys: it
    is every plan's fallback. A field a plan names in `inherits` (`plan_terms.INHERITABLE`) reads `_default`'s
    value, and a plan's switch left NULL reads `_default`'s; on `_default` itself every switch is set."""

    __tablename__ = "billing_plans"
    __table_args__ = (
        UniqueConstraint("key", name="uq_billing_plans_key"),
        *(
            CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=f"ck_billing_plans_{column}")
            for column in ORG_LIMIT_COLUMNS + BILLING_PLAN_QUOTA_COLUMNS
        ),
        CheckConstraint("duration_days IS NULL OR duration_days > 0", name="ck_billing_plans_duration_days"),
        CheckConstraint(
            "cron_min_period_seconds IS NULL OR cron_min_period_seconds >= 1",
            name="ck_billing_plans_cron_min_period_seconds",
        ),
        CheckConstraint("static_slots >= 0", name="ck_billing_plans_static_slots"),
        CheckConstraint("min_agents IS NULL OR min_agents >= 1", name="ck_billing_plans_min_agents"),
        CheckConstraint(
            "max_agents IS NULL OR max_agents >= coalesce(min_agents, 1)", name="ck_billing_plans_max_agents",
        ),
        CheckConstraint(
            "trial_agent_ceiling IS NULL OR trial_agent_ceiling > 0", name="ck_billing_plans_trial_agent_ceiling",
        ),
        _plan_key_fk("includes_plan_key"),
        _plan_key_fk("then_plan_key"),
    )

    key: Mapped[str] = mapped_column(Text, nullable=False)
    blurb: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    limit_nodes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_projects: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_users: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_github_runners: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # `app/org_quotas.py`: NULL = `_default`'s value (0 = unlimited).
    limit_task_attachment_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_attachment_file_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_org_attachment_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    limit_agent_requests_per_minute: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # `app/cron_min_period.py`: the plan's tenant default; NULL = `_default`'s (revision 202610211200).
    cron_min_period_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The fields this plan reads from `_default` (`plan_terms.INHERITABLE` names; revision 202610211200).
    inherits: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    # The provider grid: `[row, auth]` cells this plan's orgs may run (`plan_terms.providers_term`); NULL with
    # `providers` not inherited = `provider_auth_policy.SEED` (revision 202610251500).
    providers: Mapped[Optional[list]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    self_serve: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    stripe_product_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_price_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_unit_amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_account_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    livemode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    duration_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The free trial a purchase of this plan opens with, once per org (`app/billing/subscriptions.py::trial_days`);
    # NULL = none (revision 202610201437).
    trial_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The agent cap a per-agent plan's trial runs under when the plan has no Max agents (`plan_terms.trial_agents_of`);
    # NULL = `plan_terms.TRIAL_AGENT_CEILING_DEFAULT` (revision 202610220037).
    trial_agent_ceiling: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The plan switches (`plan_terms.SWITCHES`): NULL = `_default`'s value (revision 202610211200).
    hosting_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("true"))
    compliance_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Advanced Compliance, the Architectural Guard on top of Compliance (revision 202610120000, off by default —
    # read only through `app/governance/availability.py::guard_enabled`).
    advanced_compliance_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    nodevault_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    nodelink_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    merge_lane_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    static_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Whether this plan's orgs may mark nodes MicroVM (revision 202610111900, ON by default).
    microvm_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("true"))
    # Whether this plan's orgs may use playbooks (revision 202610122200, OFF by default — read only through
    # `app/plan_terms.py`).
    playbooks_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Whether this plan's orgs may author their own task templates (revision 202610281500, OFF by default — read only
    # through `app/plan_terms.py`).
    org_templates_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Which self-hosted node types this plan's orgs may register (revision 202610131600), one per `nodes.os`:
    # Ubuntu ON, macOS and Windows OFF by default — read only through `app/plan_terms.py::NODE_OS_SWITCHES`.
    ubuntu_nodes_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("true"))
    macos_nodes_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    windows_nodes_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Whether this plan's orgs run with project memory (revision 202610171200, OFF by default — read only through
    # `app/project_memory/switch.py`).
    project_memory_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Slack and the eight enterprise switches (revision 202610171800, OFF by default; every plan that existed then
    # got Slack ON) — read only through `app/plan_terms.py`.
    slack_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    sso_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    scim_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    siem_export_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    data_residency_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    ip_allowlist_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    org_retention_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    staging_org_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    budgets_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Whether this plan's orgs may run on the Jaah AI account (revision 202610200017, OFF by default — read only
    # through `app/token_billing/settings.py`).
    jaah_ai_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Whether this plan's orgs see Audit > Activity and Audit > Sessions (revision 202610210100, ON by default —
    # read only through `app/plan_terms.py`).
    audit_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("true"))
    session_transcripts_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("true"))
    # Your Cloud per provider (revision 202610211437): AWS ON, GCP and Azure OFF — read only through `app/plan_terms.py`.
    aws_cloud_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("true"))
    gcp_cloud_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    azure_cloud_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("false"))
    # Tenant email (revision 202610240100): Gmail connections and hosted trigger addresses, ON — read only through
    # `app/plan_terms.py`.
    tenant_email_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, server_default=text("true"))
    # The plan whose switches this plan also turns on, transitively (`plan_terms.plan_chain`; caps, prices,
    # duration and the then plan are never inherited), and the plan an org moves to once this plan's end passes
    # (`app/plan_rollover.py`). NULL = none.
    includes_plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    then_plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Per-agent pricing (revision 202610172101): the Price is per agent — the plan item's quantity is the agents
    # bought, within [min_agents (NULL = 1), max_agents (NULL = no upper bound)] — and the optional yearly Price,
    # mirrored like the monthly pair (`app/billing/plans.py::push_price`). Read through `app/org_plans.py`.
    per_agent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    min_agents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_agents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    yearly_stripe_price_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    yearly_price_unit_amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


class PlanTermsSnapshot(OrgScoped, Base):
    """Every org's effective plan terms as they stood when the plan engine gained inheritance and rollover
    (revision 202610171800): the before-image Customers › Plan terms check compares `plan_terms.effective` with,
    so a deploy that changed an existing org's terms shows. Written once by that migration, never after."""

    __tablename__ = "plan_terms_snapshots"
    __table_args__ = (
        org_unique("plan_terms_snapshots"),
        UniqueConstraint("org_id", name="uq_plan_terms_snapshots_org_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    plan_key: Mapped[str] = mapped_column(Text, nullable=False)
    plan_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The effective end; NULL = never.
    plan_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The caps (`plan_terms.CAP_KINDS`; a snapshot may still carry a retired kind), NULL = unlimited, and every switch,
    #: by name.
    caps: Mapped[dict] = mapped_column(JSONB, nullable=False)
    switches: Mapped[dict] = mapped_column(JSONB, nullable=False)
    stripe_price_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    #: The migration revision that wrote it.
    revision: Mapped[str] = mapped_column(Text, nullable=False)


#: `billing_plan_products.kind` / `org_plan_products.kind`: what a plan bundles (PRD D-5) — a Shared node of a
#: tier, a Dedicated node of a size, Jaah AI credit each billing month, or Live Cast (how many projects may be
#: published, `app/live_cast/entitlement.py`).
PLAN_PRODUCT_KINDS = ("shared_node", "dedicated_node", "jaah_ai_credit", "live_cast")


#: The kinds that make a node, so an `org_bundled_products` instance (Jaah AI credit's record is the ledger).
BUNDLED_NODE_KINDS = ("shared_node", "dedicated_node")


def _plan_product_checks(table: str) -> tuple[CheckConstraint, ...]:
    """One product line's shape: its kind, a positive quantity, and the catalogue field(s) its kind needs."""
    return (
        _in_check("kind", PLAN_PRODUCT_KINDS, f"ck_{table}_kind"),
        CheckConstraint("quantity >= 1", name=f"ck_{table}_quantity"),
        CheckConstraint("credit_cents IS NULL OR credit_cents >= 0", name=f"ck_{table}_credit_cents"),
        CheckConstraint("kind <> 'shared_node' OR tier_key IS NOT NULL", name=f"ck_{table}_shared_node"),
        CheckConstraint(
            "kind <> 'dedicated_node' OR (instance_type IS NOT NULL AND storage_gb IS NOT NULL)",
            name=f"ck_{table}_dedicated_node",
        ),
        CheckConstraint("kind <> 'jaah_ai_credit' OR credit_cents IS NOT NULL", name=f"ck_{table}_jaah_ai_credit"),
        CheckConstraint("kind <> 'live_cast' OR coalesce(backfill_tasks, 0) >= 1", name=f"ck_{table}_live_cast"),
        CheckConstraint("backfill_tasks IS NULL OR kind = 'live_cast'", name=f"ck_{table}_backfill_tasks"),
    )


class _PlanProductLine:
    """A product line's own columns, shared by a plan's line and an org's override line. The catalogue keys
    (`tier_key`, `instance_type` + `storage_gb`) name `rental_prices` rows without a foreign key: the catalogue
    is read, never changed, by bundles (PRD NC-2), so a write is validated against its active rows instead."""

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    tier_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instance_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    credit_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: A Live Cast line's backfill N: how many of a project's latest tasks are evaluated when it is published
    #: (revision 202610130400). NULL on every other kind.
    backfill_tasks: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class BillingPlanProduct(_PlanProductLine, EntityMixin, Base):
    """One product a plan bundles, included in its price (PRD D-1, revision 0374). `name` is the line's label."""

    __tablename__ = "billing_plan_products"
    __table_args__ = (
        *_plan_product_checks(__tablename__),
        Index("ix_billing_plan_products_plan_id", "plan_id"),
    )

    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("billing_plans.id", ondelete="CASCADE", name="fk_billing_plan_products_plan_id"), nullable=False
    )


class OrgPlanProduct(_PlanProductLine, EntityMixin, OrgScoped, Base):
    """One line of an org's own product list, which replaces its plan's WHOLE list while
    `orgs.products_overridden` is set (PRD D-6, revision 0374). `name` is the line's label."""

    __tablename__ = "org_plan_products"
    __table_args__ = (
        org_unique(__tablename__),
        *_plan_product_checks(__tablename__),
        Index("ix_org_plan_products_org_id", "org_id"),
    )


#: `org_comps.end_mode`: never ends, ends at `ends_at`, or ends with the org's plan.
COMP_END_MODES = ("never", "date", "plan")


class OrgComp(_PlanProductLine, EntityMixin, OrgScoped, Base):
    """A product the operator gives an org for free (a comp is a plan-bundle line with its
    own end): one product line ADDED to its plan's or its override's list — it never sets `products_overridden` —
    with an end of its own. `app/plan_bundles.py` claims a comp's node copies by `comp_id` and grants its Jaah AI
    credit monthly, as a plan line's; at its end its copies follow the released lifecycle (warn → stop after
    grace → delete, with Keep). `ended_at` is an operator's removal (`ended_by_id` who). `name` is the label."""

    __tablename__ = "org_comps"
    __table_args__ = (
        org_unique(__tablename__),
        *_plan_product_checks(__tablename__),
        _in_check("end_mode", COMP_END_MODES, "ck_org_comps_end_mode"),
        CheckConstraint("(end_mode = 'date') = (ends_at IS NOT NULL)", name="ck_org_comps_ends_at"),
        Index("ix_org_comps_org_id", "org_id"),
    )

    end_mode: Mapped[str] = mapped_column(Text, nullable=False)
    ends_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    ended_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: Provenance only, no foreign key (a user may be purged; the comp's record stays).
    ended_by_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


#: `customer_events.category` (`ck_customer_events_category`): the History feed's filter chips.
CUSTOMER_EVENT_CATEGORIES = ("billing", "products", "plan", "access", "lifecycle")


#: `customer_events.actor_kind`: a person, Stripe, or the system acting on its own.
CUSTOMER_EVENT_ACTOR_KINDS = ("user", "stripe", "system")


#: `customer_events.mode`; NULL is "not applicable".
CUSTOMER_EVENT_MODES = ("with_billing", "without_billing")

#: `billing_subscriptions.kind`: the subscription that carries the org's plan, or the one that carries its hostings.
SUBSCRIPTION_KINDS = ("plan", "hosting")


class CustomerEvent(OrgScoped, Base):
    """One thing that happened to a customer org, kept forever (customer history is its own
    never-pruned record). APPEND-ONLY and filed under the org it happened TO — unlike `activity_events`, filed
    under the actor's. Written only by `app/customer_history.py`: from the audit funnel (`record_write`) and from
    Stripe's events. No retention rule touches it. `source_key` (`activity:<id>`, `stripe:<event id>`) makes a
    replay a no-op."""

    __tablename__ = "customer_events"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("source_key", name="uq_customer_events_source_key"),
        _in_check("category", CUSTOMER_EVENT_CATEGORIES, "ck_customer_events_category"),
        _in_check("actor_kind", CUSTOMER_EVENT_ACTOR_KINDS, "ck_customer_events_actor_kind"),
        CheckConstraint(
            "mode IS NULL OR mode IN (" + ", ".join(f"'{v}'" for v in CUSTOMER_EVENT_MODES) + ")",
            name="ck_customer_events_mode",
        ),
        Index("ix_customer_events_org_at", "org_id", desc("at"), desc("id")),
        Index("ix_customer_events_org_category_at", "org_id", "category", desc("at")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = timestamp_column()
    category: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: Provenance only, no foreign key (a user may be purged; the history stays).
    actor_user_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    actor_label: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    source_key: Mapped[str] = mapped_column(Text, nullable=False)


class BillingSubscription(UuidPkMixin, OrgScoped, _BillingMirror, Base):
    """An org's Stripe Subscription, as last fetched. The PLAN ITEM is the item whose product carries
    `metadata.jaah_kind = plan` (`app/billing/status.py`); its period is the subscription's period. At most
    one non-terminal subscription per (org, account, mode) and kind (a plan and a hosting one coexist)."""

    __tablename__ = "billing_subscriptions"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", SUBSCRIPTION_KINDS, "ck_billing_subscriptions_kind"),
        # One live plan (a `plan` row WITH its plan item) and one live hosting subscription per org.
        Index(
            "uq_billing_subscriptions_org_plan_live", "org_id", "stripe_account_id", "livemode",
            unique=True,
            postgresql_where=text("kind = 'plan' AND plan_item_id IS NOT NULL AND status NOT IN ('canceled', 'incomplete_expired')"),
        ),
        Index(
            "uq_billing_subscriptions_org_hosting_live", "org_id", "stripe_account_id", "livemode",
            unique=True, postgresql_where=text("kind = 'hosting' AND status NOT IN ('canceled', 'incomplete_expired')"),
        ),
    )

    stripe_subscription_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    plan_item_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_price_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_unit_amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    current_period_start: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    current_period_end: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    schedule_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scheduled_plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    pending_update: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    collection_method: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The plan item's Price interval (`month` | `year`), revision 202610172101.
    interval: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The plan whose trial the subscription began with (its `jaah_trial` metadata) and Stripe's `trial_end`,
    #: revision 202610201437.
    trial_plan_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trial_end: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: `plan` | `hosting` (`SUBSCRIPTION_KINDS`), revision 202610281200: which subscription this is. Set from the
    #: `jaah_kind` metadata we stamp on create, else kept, else derived from the plan item (`mirrors.write_subscription`).
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'plan'"))
    fetched_at: Mapped[datetime] = timestamp_column(server_now=False)


class BillingPaymentMethod(UuidPkMixin, OrgScoped, _BillingMirror, Base):
    """A saved payment method's DISPLAY fields only — never a number or a CVC (C5, AC-11.1).

    `last_charge_failed_at` is stamped by the `invoice.payment_failed` refresher on the card that failed and
    cleared by `invoice.paid` or an update of this method."""

    __tablename__ = "billing_payment_methods"
    __table_args__ = (org_unique(__tablename__),)

    stripe_payment_method_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    wallet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last4: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    exp_month: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    exp_year: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    stripe_created: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_charge_failed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    detached_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class BillingInvoice(UuidPkMixin, OrgScoped, _BillingMirror, Base):
    """An org's Stripe Invoice as last fetched: amounts in the currency's minor unit, exactly as Stripe
    reported them. `amount_refunded` is the sum of the refunds on the charges that paid it."""

    __tablename__ = "billing_invoices"
    __table_args__ = (org_unique(__tablename__),)

    stripe_invoice_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amount_due: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    amount_paid: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    amount_refunded: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    post_payment_credit_notes_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    payment_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_created: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    period_start: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    period_end: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    hosted_invoice_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    invoice_pdf: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credit_note_pdfs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    next_payment_attempt: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Stripe's `charge_automatically` or `send_invoice`; NULL until re-read after the column landed.
    collection_method: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BillingSetupAttempt(UuidPkMixin, OrgScoped, _BillingMirror, Base):
    """One SetupIntent an org opened to save a card, and how often confirming it failed — the card-testing
    defence (`app/billing/payment_methods.py`): at `SETUP_FAILURE_LIMIT` failures the SetupIntent is
    cancelled, so a stolen-card list cannot be walked through one client secret. Written by the
    setup-intent route (the row) and the `setup_intent.setup_failed` handler (the count)."""

    __tablename__ = "billing_setup_attempts"
    __table_args__ = (org_unique(__tablename__),)

    setup_intent_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cancelled_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class BillingCheckout(UuidPkMixin, OrgScoped, _BillingMirror, Base):
    """An org's self-serve purchase of a paid plan, from the confirm step to its first paid invoice
    (`app/billing/subscriptions.py`). At most one LIVE checkout per (org, account, mode), so two tabs share
    one row — and so one `attempt`, and one idempotency key `checkout:{id}:subscribe:{attempt}`. `attempt`
    advances only when a first payment is declined, so a retry with a new card is a new request."""

    __tablename__ = "billing_checkouts"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("state", BILLING_CHECKOUT_STATES, "ck_billing_checkouts_state"),
        Index(
            "uq_billing_checkouts_org_live", "org_id", "stripe_account_id", "livemode",
            unique=True, postgresql_where=text("state IN ('started', 'card_saved', 'subscribing')"),
        ),
    )

    plan_key: Mapped[str] = mapped_column(Text, nullable=False)
    setup_intent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'started'"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: What the purchase in flight was sent to Stripe with (revision 202610172330): the plan item's quantity and
    #: interval. A retry under the same attempt must send the same, so a replayed key never meets other params.
    agents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    interval: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class BillingPreview(UuidPkMixin, OrgScoped, _BillingMirror, Base):
    """One plan-change preview Stripe computed (`invoices.create_preview`), kept so the change that follows
    is charged at the SAME `proration_date` — which is what makes the amount shown equal the amount charged
    (AC-4.1). The tenant names it by id only; the date and the amount never come from the browser. Bound to
    the subscription and plan price it was computed against, good for ten minutes, and used once."""

    __tablename__ = "billing_previews"
    __table_args__ = (org_unique(__tablename__),)

    plan_key: Mapped[str] = mapped_column(Text, nullable=False)
    subscription_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The plan price the quote moved FROM, and what it was (`subscribe` | `upgrade` | `downgrade`): an
    #: upgrade is charged only if the subscription still sits on that price.
    from_price_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    proration_date: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_due: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = timestamp_column(server_now=False)
    used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: What an `agents` or `interval` quote quoted (revision 202610172101): the agent count, the interval, and the
    #: count it quoted FROM (a quote is spent only while the subscription still has it).
    quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    interval: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    from_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


#: `billing_notices.kind` (`ck_billing_notices_kind`, revision 0333). `app/billing/notices.py` composes each.
BILLING_NOTICE_KINDS = (
    "payment.failed", "payment.action_required", "refund.issued",
    "card.expires_30d", "card.expires_7d", "card.expired",
    # A Jaah AI auto-recharge whose charge failed (`app/token_billing/topups.py`, revision 0341).
    "recharge.failed",
    # Jaah AI wallet alerts (`app/token_billing/alerts.py`, revision 0343): under the low-balance threshold,
    # work paused, work resumed.
    "jaah.low", "jaah.paused", "jaah.resumed",
    # A hosting's machine-side warnings (`app/hosting_lifecycle.py`, revision 0356): renewal unpaid, about to stop,
    # stopped, about to be deleted; and a bundled node's plan end (revision 0374).
    "rental.past_due", "rental.stopping", "rental.stopped", "rental.terminating", "rental.bundle_ending",
)


class BillingNotice(UuidPkMixin, OrgScoped, Base):
    """One thing an org's Owner/Admins are told about its billing (`app/billing/notices.py`): a failed or
    unauthenticated payment, a refund, a card about to expire. `dedupe_key` is UNIQUE, so a notice is raised
    exactly once; `subject` is the Stripe id it is about, so the event that settles it can resolve it.
    `payload` is display fields only — brand, last four, amounts, never a card number (C5). The bell and the
    banner read unresolved rows; `emailed_at` is stamped at insert, when the notice's durable email is owed."""

    __tablename__ = "billing_notices"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", BILLING_NOTICE_KINDS, "ck_billing_notices_kind"),
        Index("ix_billing_notices_org_open", "org_id", "created_at", postgresql_where=text("resolved_at IS NULL")),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    banner: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = timestamp_column()
    resolved_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    emailed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class BillingNoticeRead(OrgScoped, Base):
    """When one Owner/Admin read one billing notice in the bell — `ticket_reads`' shape, without the foreign
    keys no billing row carries."""

    __tablename__ = "billing_notice_reads"
    __table_args__ = (org_unique(__tablename__),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, server_default=text("uuidv7()"))
    notice_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    read_at: Mapped[datetime] = timestamp_column()


class BillingEvent(UuidPkMixin, Base):
    """One Stripe event the poll kept (allow-listed types only), deduplicated on Stripe's id. `outcome` is
    NULL until processed; `error` rows are retried each tick. Pruned 90 days after `processed_at`."""

    __tablename__ = "billing_events"
    __table_args__ = (
        _in_check("outcome", BILLING_EVENT_OUTCOMES, "ck_billing_events_outcome"),
        Index("ix_billing_events_pending", "created", "stripe_event_id",
              postgresql_where=text("outcome IS NULL OR outcome = 'error'")),
    )

    stripe_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stripe_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    livemode: Mapped[bool] = mapped_column(Boolean, nullable=False)
    object_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


#: `org_bundled_products.state` (revision 0374): claimed and launching, failed, running, released by the plan (and
#: in the keep-or-stop lifecycle), kept as a paid hosting, or gone.
BUNDLED_PRODUCT_STATES = ("provisioning", "failed", "live", "released", "kept", "ended")


class OrgBundledProduct(EntityMixin, OrgScoped, Base):
    """One instance of a node an org's plan bundles (revision 0374).

    `fingerprint` names the configuration (`shared_node:<tier_key>` · `dedicated_node:<instance_type>:<storage_gb>`)
    and `ordinal` which of its `quantity` copies this is; the partial unique index holds at most one live instance
    per (org, fingerprint, ordinal), so never more than `quantity`. `node_id` is a recorded pointer WITHOUT a
    foreign key: a node deleted later leaves it dangling, which reads "removed".
    `hosting_id` is the free hosting carrying it (`SET NULL`: the instance outlives a deleted hosting)."""

    __tablename__ = "org_bundled_products"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", BUNDLED_NODE_KINDS, "ck_org_bundled_products_kind"),
        _in_check("state", BUNDLED_PRODUCT_STATES, "ck_org_bundled_products_state"),
        CheckConstraint("ordinal >= 1", name="ck_org_bundled_products_ordinal"),
        # A plan's copies are one per (org, fingerprint, ordinal), a comp's one per (comp, fingerprint, ordinal):
        # a comp and a plan line of the same configuration never claim each other's machine.
        Index(
            "uq_org_bundled_products_plan_live", "org_id", "fingerprint", "ordinal",
            unique=True, postgresql_where=text("state NOT IN ('kept', 'ended') AND comp_id IS NULL"),
        ),
        Index(
            "uq_org_bundled_products_comp_live", "comp_id", "fingerprint", "ordinal",
            unique=True, postgresql_where=text("state NOT IN ('kept', 'ended') AND comp_id IS NOT NULL"),
        ),
        Index("ix_org_bundled_products_hosting_id", "hosting_id"),
        Index("ix_org_bundled_products_comp_id", "comp_id"),
    )

    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    hosting_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("node_rentals.id", ondelete="SET NULL", name="fk_org_bundled_products_hosting_id"), nullable=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    released_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: The comp this copy belongs to (`org_comps`); None for a copy of the plan's (or the override's) list.
    #: CASCADE: a nulled pointer would make a comp's copy a plan copy. A comp is only ever ended, never deleted,
    #: save by its org's purge.
    comp_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("org_comps.id", ondelete="CASCADE", name="fk_org_bundled_products_comp_id"), nullable=True
    )
