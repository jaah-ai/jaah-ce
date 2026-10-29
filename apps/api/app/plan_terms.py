"""What an org's plan gives it — THE SINGLE HOME of an org's effective terms.

Every term resolves **override → plan → `(default)` → code constant**. `(default)` is the reserved plan
row keyed `DEFAULTS_KEY` (`defaults`): no plan an org buys, but every plan's fallback. A plan reads a field from it
when the field is in the plan's `inherits` (`INHERITABLE` names; `field`); otherwise the plan's own value stands, and
an empty one keeps its meaning — unlimited / never / none (the quotas and the cron minimum excepted: theirs falls to
`(default)` too, `app/org_quotas.py`, `app/cron_min_period.py`). An org whose plan row is missing reads `(default)`.

The plan SWITCHES (`SWITCHES`, the one registry) are tri-state on a plan: On, Off, or NULL = `(default)`'s value. A
switch is ON when the plan OR any plan it includes has it ON (`includes_plan_key`, transitively — `plan_chain`);
otherwise the plan's own NULL reads `(default)`'s value, its Off reads off. With no plan — an operator-plane org —
each reads `(default)`'s value. With no `(default)` row at all, its code constant (`SwitchSpec.default`).

A NEW MAJOR FEATURE SHIPS OFF, BEHIND ONE MORE SWITCH HERE: a `SWITCHES` row, a plan column defaulting
false, an org override, `(default)` OFF, enforced server-side at every door that reaches
the feature (`require_switch`: 403 "<Label> is not on your plan. Upgrade in Billing.") and hidden from tenants in the console when
off (no upsell: an off-plan feature renders nothing). Never an org setting: what an org may use is the plan's decision.

* caps — `orgs.limit_<kind>` when non-NULL, else the plan's `billing_plans.limit_<kind>`; NULL and 0 both
  mean unlimited (served as None) — except GitHub Runners (`OFFABLE_CAPS`), where 0 means off and only NULL is
  unlimited: that one cap is the whole feature (`github_runners_allowed`), with no switch beside it. An
  org's NULL there is "the plan's", so its unlimited override is stored as `CAP_MAX` and served as None.
* end — `orgs.plan_end_override` while `orgs.plan_end_overridden` (NULL then = never ends), else LIVE:
  `orgs.plan_started_at + billing_plans.duration_days` (DA-3), None when the plan has no duration. `ended` once
  it has passed (D-7): the org's nodes leave dispatch (`ended_clause`, the SQL twin) and members see a banner.
* hosting — `orgs.hosting_allowed_override`, else `billing_plans.hosting_allowed`.
* compliance — `orgs.compliance_enabled_override`, else `billing_plans.compliance_enabled` (Portfolio Compliance;
  read only by `app/governance/availability.py`).
* advanced compliance — `orgs.advanced_compliance_enabled_override`, else `billing_plans.advanced_compliance_enabled`
  (the Architectural Guard, on top of Compliance; read only by `app/governance/availability.py::guard_enabled`).
* nodevault — `orgs.nodevault_allowed_override`, else `billing_plans.nodevault_allowed` (NodeVault; read only by
  `app/nodevault_policy.py`).
* nodelink — `orgs.nodelink_allowed_override`, else `billing_plans.nodelink_allowed` (NodeLink, the fleet's shared
  account store; read only by `app/tenant_defaults.py`, which keeps the operator plane always on).
* merge lane — `orgs.merge_lane_enabled_override`, else `billing_plans.merge_lane_enabled` (the control-plane
  verification queue that batch-tests and merges runner PRs; off on every plan; `merge_lane_enabled` is its reader).
* MicroVM — `orgs.microvm_allowed_override`, else `billing_plans.microvm_allowed` (whether the org may mark a node
  MicroVM; ON on every plan by default; read only by `app/microvm_mode.py`).
* playbooks — `orgs.playbooks_enabled_override`, else `billing_plans.playbooks_enabled` (the playbook engine; off on
  every plan; replaces the retired org knob `playbooks_enabled`; `playbooks_enabled` is its reader).
* org templates — `orgs.org_templates_enabled_override`, else `billing_plans.org_templates_enabled` (whether the org may
  author its own task templates, `app/routes/task_templates.py`; off on every plan; off hides the org's templates from
  the gallery and from Start without deleting them; `org_templates_enabled` is its reader).
* node types — `orgs.<os>_nodes_allowed_override`, else `billing_plans.<os>_nodes_allowed`, one switch per `nodes.os`
  (`NODE_OS_SWITCHES`: whether the org may register a NEW self-hosted node of that OS, or a NEW Windows cloud or
  Dedicated hosting node; Ubuntu ON, macOS and Windows OFF by default; nodes the org already has keep running, and an
  Ubuntu cloud, hosting or node-group node is not gated; read by `app/routes/nodes/cloud.py` and
  `app/routes/node_orders.py` through `node_os_allowed`).
* project memory — `orgs.project_memory_enabled_override`, else `billing_plans.project_memory_enabled` (whether the
  org's runners, crons and console use project memory at all; read only by `app/project_memory/switch.py`, which puts
  it after the kill switch and before every other memory setting). ON on every plan that existed when it shipped,
  with the five plans' OFF pins lifted (revision 202610181900); a new plan defaults OFF.
* Slack — `slack_enabled`: every Slack surface of the org (connecting a workspace, notifications, question cards
  and replies, the shipped-task card, unfurls; read by `app/slack_egress.py`, `app/alert_relay.py` and the inbound
  doors). ON on every plan that existed when it shipped.
* enterprise features — `sso_enabled`, `scim_enabled`, `siem_export_enabled`, `data_residency_enabled`,
  `ip_allowlist_enabled`, `org_retention_enabled`, `staging_org_enabled`, `budgets_enabled`: OFF everywhere; each
  feature wires its own doors through `require_switch`.
* Jaah AI — `orgs.jaah_ai_enabled_override`, else `billing_plans.jaah_ai_enabled` (whether the org may run on the
  Jaah AI account; off on every plan; read only by `app/token_billing/settings.py`, which keeps an org it turns
  off on its own accounts).
* audit / session transcripts — `orgs.audit_enabled_override` / `orgs.session_transcripts_enabled_override`, else
  `billing_plans.audit_enabled` / `billing_plans.session_transcripts_enabled`: whether a tenant reads Audit > Activity
  (`routes/audit.py`) and Audit > Sessions with its transcripts (`routes/sessions.py`). ON on every plan; the
  operator always reads both.
* Your Cloud — `orgs.<provider>_cloud_allowed_override`, else `billing_plans.<provider>_cloud_allowed`, one switch per
  cloud provider (`CLOUD_SWITCHES`): whether the org may connect and launch nodes in its own AWS, GCP or Azure
  account (`app/clouds/__init__.py::require_provider`). AWS ON on every plan that existed when it shipped (revision
  202610211437), GCP and Azure OFF. Jaah Hosted is not gated here (`hosting_allowed`).
* tenant email — `orgs.tenant_email_allowed_override`, else `billing_plans.tenant_email_allowed`: whether the org may
  connect its own Gmail and send through it (`app/tenant_gmail/`), and hold hosted trigger addresses. ON on every plan
  (revision 202610240100). Off keeps connections and their history; sending and connecting refuse.
* then plan — `orgs.then_plan_key_override` while `orgs.then_plan_overridden` (NULL then = none), else
  `billing_plans.then_plan_key`: the plan an org moves to once its end passes (`app/plan_rollover.py`); none = the
  org's service ends as before.
* quotas — requests per minute, task storage, org storage and max file size: `orgs.limit_<kind>`, else the plan's
  `billing_plans.limit_<kind>`, else `(default)`'s, else a constant (`app/org_quotas.py` resolves them).
* static slots — `orgs.static_slots_override`, else `billing_plans.static_slots` (how many static worktrees a
  project may hold on one node; 0 = none; read only by `app/static_slots_policy.py`).
* agent range — `orgs.min_agents_override` / `orgs.max_agents_override` (0 = no upper bound), else the plan's
  `min_agents` (NULL = 1) / `max_agents` (NULL = no upper bound): what a per-agent purchase of the org's own plan
  may buy (`app/org_plans.py::bounds`). On a per-agent plan the agents bought are the Agents cap; on any other the
  Max agents is (`caps_of`).
* providers — the provider grid: which `(row, auth)` cells the org may run — built-in providers by auth type, the
  SDK agent row (`sdk`/`own`: manifest agents and catalog connectors, and with any agent row on, the Agents pages) and
  one row per ACP-registry agent (`acp:<id>`/`api_key`: browse, install and dispatch, the bound key account's own
  pair included) — `app/provider_auth_policy.py` names the rows and auth types and is every door's API. `orgs.providers_override` (NULL = the plan's; a list replaces it
  outright), else the plan's `billing_plans.providers` (or `(default)`'s when the plan inherits `providers`), else
  `provider_auth_policy.SEED`. An operator-plane org reads `(default)` plus its own override, never a plan. Unlike a
  switch, it never follows `includes_plan_key`: an included plan's grid does not flow to the including plan. A cell
  is ON only while stored, so one a later build or the registry adds reads OFF everywhere until an operator stores it
  (`providers_of`).
* products — the org's `org_plan_products` rows (the WHOLE list; empty is valid) while
  `orgs.products_overridden`, else the plan's `billing_plan_products`.

A STAGING ORG (`app/staging_org.py`, D-F7) has no terms of its own: every read here resolves it through its parent
first (`terms_org`), so it shares the parent's plan row AND the parent's overrides, and loses a term the moment the
parent does. It receives no products (`receives`): the bundled nodes and credit are the parent's.

The plan row is read whether or not it is `active`: retiring a plan stops new assignment only (AC1.4). An org
whose `plan_key` names no row is read as on `(default)` — never as unrestricted.

`assign` is the one way an org changes plan; `wake_bundles` the one wake of the bundle reconciler (the
`plan-bundles` cron, `app/plan_bundles.py`). `describe` is the one label builder for a product line.
Nothing here commits.
"""
from __future__ import annotations

import functools
import logging
from collections import Counter
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta
from typing import Callable, Final, Generic, Literal, Mapping, Optional, Sequence, TypeVar
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, case, delete, exists, func, select, update
from sqlalchemy.orm import Session, aliased, object_session
from sqlalchemy.sql.elements import ColumnElement

from app import node_platform, provider_auth_policy
from app.operator_settings import reads as settings_reads
from app.actors import stamp_actor
from app.entity_shape import iso_or_none
from app.models.billing import BillingPlan, BillingPlanProduct, OrgBundledProduct, OrgPlanProduct
from app.models.hosting import HostingPrice
from app.models.identity import AdminUser
from app.models.orgs import ORG_ACTIVE, Org
from app.tenancy.ids import is_tenant_org
from app.user_activity import record_write
from app.clock import as_utc as _as_utc, utcnow as _now

log = logging.getLogger("plan-terms")

T = TypeVar("T")
Cell = provider_auth_policy.Cell
#: Who set a term: the plan, an operator's override, or (runner slots of a per-agent plan) the agents bought or the
#: trial's lifted cap (`trial_agents_of`).
Source = Literal["plan", "override", "purchase", "trial"]
ProductKind = Literal["shared_node", "dedicated_node", "jaah_ai_credit", "live_cast"]
#: Live Cast's one price row in the catalogue (`rental_prices` kind/key): the price per project a month.
LIVE_CAST_PRICE: Final = ("live_cast", "project")
#: THE agent cap of a per-agent plan's trial when the plan sets neither Max agents nor a trial agent ceiling
#: (`trial_agents_of`).
TRIAL_AGENT_CEILING_DEFAULT: Final = 10
#: The most a Live Cast line's backfill N may be: it bounds what one publish spends on evaluation.
MAX_BACKFILL: Final = 1000
#: The Operator Settings knob a new Live Cast line's backfill N starts at (Products › Live Cast).
LIVE_CAST_BACKFILL_KNOB: Final = "live_cast_default_backfill"

#: The reserved plan row `(default)`: every plan's fallback (`defaults`), never offered or assigned by a buyer.
DEFAULTS_KEY: Final = "_default"
#: The fields a plan may read from `(default)` (`billing_plans.inherits` holds these names), each with the
#: `billing_plans` columns it carries — the first is its value (`field`). `products` is the plan's product lines.
INHERITABLE: Final[Mapping[str, tuple[str, ...]]] = {
    "price": ("price_unit_amount", "yearly_price_unit_amount", "per_agent"),
    "trial_days": ("trial_days",),
    "min_agents": ("min_agents",),
    "max_agents": ("max_agents",),
    **{kind: (f"limit_{kind}",) for kind in ("nodes", "projects", "users", "github_runners")},
    **{kind: (f"limit_{kind}",) for kind in (
        "task_attachment_mb", "attachment_file_mb", "org_attachment_gb", "agent_requests_per_minute",
    )},
    "cron_min_period_seconds": ("cron_min_period_seconds",),
    "static_slots": ("static_slots",),
    "duration_days": ("duration_days",),
    "then_plan_key": ("then_plan_key",),
    "products": (),
    "providers": ("providers",),
}


@dataclass(frozen=True)
class SwitchSpec:
    """One plan switch: `billing_plans.<name>`, `orgs.<name>_override`, the identity payload's key, its console
    `label`, and `default` — the value read with no `(default)` row at all (`_switch`)."""

    name: str
    label: str
    default: bool = False


#: THE PLAN SWITCHES, in display and history order: the one registry the terms, the plans API, the org overrides,
#: the identity payload and the console read (`apps/web/src/lib/planSwitches.tsx` is its twin).
SWITCHES: Final[tuple[SwitchSpec, ...]] = (
    SwitchSpec("hosting_allowed", "Hosting", True),
    SwitchSpec("compliance_enabled", "Compliance"),
    SwitchSpec("advanced_compliance_enabled", "Advanced Compliance"),
    SwitchSpec("nodevault_allowed", "NodeVault"),
    SwitchSpec("nodelink_allowed", "NodeLink"),
    SwitchSpec("merge_lane_enabled", "Merge lane"),
    SwitchSpec("microvm_allowed", "MicroVM nodes", True),
    SwitchSpec("playbooks_enabled", "Playbooks"),
    SwitchSpec("org_templates_enabled", "Templates (org)"),
    SwitchSpec("ubuntu_nodes_allowed", "Ubuntu nodes", True),
    SwitchSpec("macos_nodes_allowed", "macOS nodes"),
    SwitchSpec("windows_nodes_allowed", "Windows nodes"),
    SwitchSpec("project_memory_enabled", "Project memory", True),
    # Slack (ON on every plan that existed then) and the eight enterprise features (OFF).
    SwitchSpec("slack_enabled", "Slack", True),
    SwitchSpec("sso_enabled", "SSO"),
    SwitchSpec("scim_enabled", "SCIM"),
    SwitchSpec("siem_export_enabled", "SIEM export"),
    SwitchSpec("data_residency_enabled", "Data residency"),
    SwitchSpec("ip_allowlist_enabled", "IP allowlist"),
    SwitchSpec("org_retention_enabled", "Retention"),
    SwitchSpec("staging_org_enabled", "Staging org"),
    SwitchSpec("budgets_enabled", "Budgets"),
    # The Jaah AI account, once a per-org operator checkbox (OFF).
    SwitchSpec("jaah_ai_enabled", "Jaah AI"),
    # Audit > Activity and Audit > Sessions (ON).
    SwitchSpec("audit_enabled", "Audit", True),
    SwitchSpec("session_transcripts_enabled", "Session transcripts", True),
    # Your Cloud per provider — AWS ON (every plan could launch Your AWS before), GCP and Azure OFF.
    SwitchSpec("aws_cloud_allowed", "Your Cloud: AWS", True),
    SwitchSpec("gcp_cloud_allowed", "Your Cloud: GCP"),
    SwitchSpec("azure_cloud_allowed", "Your Cloud: Azure"),
    # An org's own Gmail and hosted trigger addresses, one "tenant email" term (ON).
    SwitchSpec("tenant_email_allowed", "Tenant email", True),
)
SWITCH_BY_NAME: Final[Mapping[str, SwitchSpec]] = {spec.name: spec for spec in SWITCHES}


def switch_fields(annotation: object, default: object = ...) -> dict[str, tuple[object, object]]:
    """`pydantic.create_model` fields for every switch — `{name: (annotation, default)}` in `SWITCHES` order — so
    the plan and org-plan API models (`routes/billing_plans.py`, `routes/orgs.py`) list no switch by name."""
    return {spec.name: (annotation, default) for spec in SWITCHES}


#: An org's own task templates (`app/routes/task_templates.py`).
ORG_TEMPLATES: Final = "org_templates_enabled"
#: The Slack switch: every Slack surface of the org (`app/slack_egress.py`, `app/alert_relay.py`, inbound, unfurls).
SLACK: Final = "slack_enabled"
#: The Jaah AI switch: whether the org may run on the Jaah AI account (`app/token_billing/settings.py`).
JAAH_AI: Final = "jaah_ai_enabled"
#: An org's own Gmail connections and hosted trigger addresses (`app/tenant_gmail/`).
TENANT_EMAIL: Final = "tenant_email_allowed"
#: Audit > Activity (`routes/audit.py`) and Audit > Sessions with its transcripts (`routes/sessions.py`).
AUDIT: Final = "audit_enabled"
SESSION_TRANSCRIPTS: Final = "session_transcripts_enabled"
#: The node-type switches, one per `nodes.os` (`node_platform.OS_VALUES`): whether an org may register a new
#: self-hosted node of that OS. The console's twin: `apps/web/src/lib/planSwitches.tsx::NODE_OS_SWITCH`.
NODE_OS_SWITCHES: Final[Mapping[str, str]] = {
    node_platform.OS_UBUNTU: "ubuntu_nodes_allowed",
    node_platform.OS_MACOS: "macos_nodes_allowed",
    node_platform.OS_WINDOWS: "windows_nodes_allowed",
}
#: The Your Cloud switches, one per cloud provider (`app/clouds/base.py::ProviderKey`): whether the org may connect and
#: launch on that cloud (`app/clouds/__init__.py::require_provider`). The console's twin:
#: `apps/web/src/lib/planSwitches.tsx::CLOUD_SWITCH`.
CLOUD_SWITCHES: Final[Mapping[str, str]] = {
    "aws": "aws_cloud_allowed",
    "gcp": "gcp_cloud_allowed",
    "azure": "azure_cloud_allowed",
}
#: The switches the console reads per org (`apps/web/src/lib/planSwitches.tsx::PlanSwitch`): the identity payload's
#: (`routes/auth.py`) and the operator org roster's `plan_switches` (`routes/orgs.py`), which narrows the operator's
#: act-as picker on a plan-gated page to the orgs that have the feature.
CONSOLE_SWITCHES: Final = (
    "playbooks_enabled", ORG_TEMPLATES, "microvm_allowed",
    *NODE_OS_SWITCHES.values(), "project_memory_enabled",
    # Slack and the eight enterprise switches: the console shows each feature's controls or its upgrade
    # notice; every door refuses on its own (`require_switch`).
    "slack_enabled", "sso_enabled", "scim_enabled", "siem_export_enabled", "data_residency_enabled", "ip_allowlist_enabled", "org_retention_enabled", "staging_org_enabled", "budgets_enabled",
    "jaah_ai_enabled", "audit_enabled", "session_transcripts_enabled",
    *CLOUD_SWITCHES.values(), TENANT_EMAIL,
)
#: The terms an operator-plane org may override (`routes/billing_org_plan.py::put_org_plan`): these switches and the provider grid
#: (`PROVIDERS`). Hosting, NodeLink, Slack, Jaah
#: AI, Audit and Session transcripts are not among them: the operator plane is exempt from the first three and the
#: last two in code (`hosting_catalog.offerings`, `tenant_defaults.shared_store_allowed`, the Slack gate reads tenants
#: only, `require_tenant_switch`) and never runs on the Jaah AI account (`token_billing.settings.set_mode`), so a
#: switch there would decide nothing.
OPERATOR_PLANE_SWITCHES: Final = tuple(
    spec.name for spec in SWITCHES
    if spec.name not in ("hosting_allowed", "nodelink_allowed", SLACK, JAAH_AI, AUDIT, SESSION_TRANSCRIPTS)
)
#: The provider grid term's name: the `INHERITABLE` field, `PlanTerms.providers`, the org-plan API's field.
PROVIDERS: Final = "providers"
#: The five caps, `app/org_limits.py::LIMIT_KINDS` order (`app/models/orgs.py::ORG_LIMIT_COLUMNS` pairs with it, less
#: Agents, whose override is Max agents).
CAP_KINDS: Final = ("runner_slots", "nodes", "projects", "users", "github_runners")
#: The caps with an `orgs.limit_<kind>` / `billing_plans.limit_<kind>` column of their own: every one but Agents.
COLUMN_CAP_KINDS: Final = tuple(kind for kind in CAP_KINDS if kind != "runner_slots")
#: The caps whose 0 means off rather than unlimited: GitHub Runners, the feature and its count in one term.
OFFABLE_CAPS: Final = frozenset({"github_runners"})
#: The largest cap an operator may set (`routes/orgs.py`'s limits route): on an `OFFABLE_CAPS` kind it is unlimited.
CAP_MAX: Final = 100000
#: The console's GitHub Runners flag (`routes/auth.py`, the operator org roster's `plan_switches`): the cap not 0.
GITHUB_RUNNERS: Final = "github_runners_allowed"
#: The bundle reconciler's cron (`app/cron_system_activities.py`), woken after a commit that changes an org's terms.
BUNDLES_ACTIVITY: Final = "plan-bundles"
#: Instance states that still hold a configuration's place (`uq_org_bundled_products_live`).
_HELD_STATES: Final = ("provisioning", "failed", "live", "released")
_ACTIVE_STATES: Final = ("provisioning", "failed", "live")

#: Activity kinds `assign` writes (`app/customer_history.py::CATEGORY_OF` files them in the customer history).
OVERRIDE_CLEARED: Final = "org_plan_override_cleared"
OVERRIDE_CHANGED: Final = "org_plan_override_changed"

#: The numeric plan limits beyond the caps, as `(label, orgs column)`: one override each (NULL = the plan's).
_LIMIT_OVERRIDES: Final = (
    ("Min agents", "min_agents_override"), ("Max agents", "max_agents_override"),
    ("Static slots", "static_slots_override"),
)
THEN_PLAN: Final = "Then plan"

_CAP_LABELS: Final = {
    "runner_slots": "Agents",
    "nodes": "Nodes",
    "projects": "Projects",
    "users": "Users",
    "github_runners": "GitHub Runners",
}


# ── The shapes ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Term(Generic[T]):
    """One term as it governs an org: `value` applies, `source` says who set it, `plan_value` is what the plan
    alone says (for "Overridden — plan: X")."""

    value: T
    source: Source
    plan_value: T
    #: A switch the plan has ON only through a plan it includes (`plan_chain`): that plan's key. None otherwise.
    inherited_from: Optional[str] = None

    @property
    def overridden(self) -> bool:
        return self.source == "override"


@dataclass(frozen=True)
class ProductLine:
    """One bundled product line: a plan's (`billing_plan_products`) or an org's override (`org_plan_products`).
    `id` is None for an unsaved draft."""

    id: Optional[UUID]
    kind: ProductKind
    quantity: int
    tier_key: Optional[str] = None
    instance_type: Optional[str] = None
    storage_gb: Optional[int] = None
    credit_cents: Optional[int] = None
    backfill_tasks: Optional[int] = None

    def fingerprint(self) -> Optional[str]:
        """The configuration a node line makes (`org_bundled_products.fingerprint`); None for Jaah AI credit and
        Live Cast, which make no node."""
        if self.kind == "shared_node":
            return f"shared_node:{self.tier_key}"
        if self.kind == "dedicated_node":
            return f"dedicated_node:{self.instance_type}:{self.storage_gb}"
        return None

    @classmethod
    def of(cls, row: BillingPlanProduct | OrgPlanProduct) -> "ProductLine":
        return cls(
            id=row.id, kind=row.kind, quantity=row.quantity, tier_key=row.tier_key,  # type: ignore[arg-type]
            instance_type=row.instance_type, storage_gb=row.storage_gb, credit_cents=row.credit_cents,
            backfill_tasks=row.backfill_tasks,
        )


@dataclass(frozen=True)
class PlanTerms:
    plan_key: str
    plan_name: str
    #: `CAP_KINDS` → its term; None = unlimited.
    caps: Mapping[str, Term[Optional[int]]]
    #: None = never ends.
    ends_at: Term[Optional[datetime]]
    ended: bool
    #: Every `SWITCHES` name → its term; each also reads as an attribute (`terms.slack_enabled`).
    switches: Mapping[str, Term[bool]]
    products: Term[tuple[ProductLine, ...]]
    #: The provider grid's cells (`providers_of`).
    providers: Term[frozenset[Cell]]

    def __getattr__(self, name: str) -> Term[bool]:
        if name != "switches" and name in SWITCH_BY_NAME:
            return self.switches[name]
        raise AttributeError(name)


@dataclass(frozen=True)
class BundleDiff:
    """What the reconciler would do to an org's bundled nodes, per fingerprint: instances to `provision`,
    instances to `release`, and wanted instances it already has (`carry`, a released one included — A-7)."""

    provision: dict[str, int] = dataclass_field(default_factory=dict)
    release: dict[str, int] = dataclass_field(default_factory=dict)
    carry: dict[str, int] = dataclass_field(default_factory=dict)


# ── The plan row ─────────────────────────────────────────────────────────────────────────────────────


#: Where `defaults` keeps the `(default)` row it read, per session.
_DEFAULTS_INFO: Final = "plan_terms.defaults"


def defaults(session: Session) -> Optional[BillingPlan]:
    """The `(default)` row (`DEFAULTS_KEY`), read once per session while the row stays in it (`session.info`). None
    with no such row (a database the revision 202610211200 never ran on): every term then reads its constant."""
    row = session.info.get(_DEFAULTS_INFO)
    if row is not None and row in session:
        return row
    row = session.scalars(select(BillingPlan).where(BillingPlan.key == DEFAULTS_KEY)).one_or_none()
    if row is not None:
        session.info[_DEFAULTS_INFO] = row
    return row


def field_row(plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan], name: str) -> Optional[BillingPlan]:
    """The row whose value `plan` has for `INHERITABLE` field `name`: `defaults_row` when `plan` inherits it (or
    there is no plan), else `plan` itself."""
    if plan is None or name in (plan.inherits or ()):
        return defaults_row
    return plan


def field(
    plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan], name: str, column: Optional[str] = None,
) -> object:
    """`plan`'s value for `INHERITABLE` field `name` (`field_row`'s), read from `column` (one of the field's
    columns; default its first). None when neither row has it."""
    row = field_row(plan, defaults_row, name)
    return getattr(row, column or INHERITABLE[name][0]) if row is not None else None


def _defaults_for(
    plan: Optional[BillingPlan], org: Optional[Org], given: Optional[BillingPlan],
) -> Optional[BillingPlan]:
    """`given`, else — only when `plan` may need it (none, or one that inherits) — `defaults` on the session the
    plan or org row belongs to. The terms below take `(default)` as an optional argument so a caller holding only
    the rows still resolves inherited fields."""
    if given is not None or (plan is not None and not plan.inherits):
        return given
    for row in (plan, org):
        session = object_session(row) if row is not None else None
        if session is not None:
            return defaults(session)
    return None


def _sized_by(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan],
) -> tuple[Optional[BillingPlan], Optional[BillingPlan]]:
    """The plan row and `(default)` row `org`'s caps, agent range and static slots read: none for an operator-plane
    org (`is_tenant_org`) — it has no plan, so those stay unlimited / 1 / 0 whatever `(default)` holds, as before
    `(default)` existed (its quotas still read `(default)`, `app/org_quotas.py`) — else `plan` and `_defaults_for`'s."""
    if not is_tenant_org(org.id):
        return None, None
    return plan, _defaults_for(plan, org, defaults_row)


#: The operator's pick of where a lapse lands (`operator_settings/catalog_billing.py`).
LAPSE_PLAN_KNOB: Final = "billing_lapse_plan_key"


def lapse_pick(session: Session) -> str:
    """`LAPSE_PLAN_KNOB` while it names an active plan; blank otherwise (unset, or a plan retired since)."""
    picked = settings_reads.get_str(LAPSE_PLAN_KNOB)
    if picked and session.scalar(select(BillingPlan.active).where(BillingPlan.key == picked)):
        return picked
    return ""


def lapse_plan_key(session: Session, lapsing: Optional[str] = None) -> str:
    """The plan an org lands on when its paid plan (`lapsing`) lapses: the operator's `LAPSE_PLAN_KNOB` while it
    names an active plan other than `lapsing` (blank is none — and landing on the plan that lapsed would keep it
    unpaid); else `(default)` — or, when `(default)` itself runs out (a duration and a then plan), the plan it ends
    into, so a lapse never starts a clock that already ran."""
    picked = lapse_pick(session)
    if picked and picked != lapsing:
        return picked
    row = defaults(session)
    if row is not None and row.duration_days is not None and row.then_plan_key is not None:
        return row.then_plan_key
    return DEFAULTS_KEY


def plan_row(session: Session, plan_key: str) -> Optional[BillingPlan]:
    """`plan_key`'s row, active or retired; `(default)` when no row has that key (never fail-open)."""
    row = session.scalars(select(BillingPlan).where(BillingPlan.key == plan_key)).one_or_none()
    return row if row is not None else defaults(session)


def plan_name(session: Session, key: Optional[str]) -> Optional[str]:
    """`key`'s display name; the key itself for an unknown plan, None for None."""
    if key is None:
        return None
    return session.scalar(select(BillingPlan.name).where(BillingPlan.key == key)) or key


def plan_lines(session: Session, plan: BillingPlan) -> tuple[ProductLine, ...]:
    rows = session.scalars(
        select(BillingPlanProduct).where(BillingPlanProduct.plan_id == plan.id)
        .order_by(BillingPlanProduct.sort_order, BillingPlanProduct.created_at, BillingPlanProduct.id)
    ).all()
    return tuple(ProductLine.of(r) for r in rows)


def org_lines(session: Session, org_id: UUID) -> tuple[ProductLine, ...]:
    rows = session.scalars(
        select(OrgPlanProduct).where(OrgPlanProduct.org_id == org_id)
        .order_by(OrgPlanProduct.sort_order, OrgPlanProduct.created_at, OrgPlanProduct.id)
    ).all()
    return tuple(ProductLine.of(r) for r in rows)


# ── Effective terms ──────────────────────────────────────────────────────────────────────────────────


def _unlimited(raw: Optional[int], kind: str = "") -> Optional[int]:
    """A stored cap as served: None = unlimited. 0 is unlimited too, save on an `OFFABLE_CAPS` kind, where it is off
    and `CAP_MAX` is unlimited."""
    if raw is None:
        return None
    if kind in OFFABLE_CAPS:
        return None if raw >= CAP_MAX else raw
    return None if raw == 0 else raw


def caps_of(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None,
) -> dict[str, Term[Optional[int]]]:
    """The five caps of `org` on `plan` — the cheap path the dispatch caps take (one cached `(default)` read at most,
    `_defaults_for`). Runner slots (Agents) are the plan's Max agents (`max_agents_of`), and on a per-agent
    plan the agents bought (`orgs.agents_bought`, source `purchase`); while a trial of a per-agent plan is live they
    are lifted to `trial_agents_of` (source `trial`, never below the agents bought). Their one override is Max agents
    (`orgs.max_agents_override`, source `override` when it is what governs: with no agents bought, or pinned below
    them) — migration 202610221300 folded the old `limit_runner_slots` into it; that column and
    `limit_active_worktrees` are retired. Every other cap has a column of its own (`COLUMN_CAP_KINDS`)."""
    plan, dflt = _sized_by(org, plan, defaults_row)
    out: dict[str, Term[Optional[int]]] = {}
    for kind in CAP_KINDS:
        if kind == "runner_slots":
            # No plan (the operator plane): unlimited, save the org's own Max agents.
            own_most = org.max_agents_override
            most = max_agents_of(org, plan, dflt) if plan is not None else (
                Term(_unlimited(own_most), "override", None) if own_most is not None else Term(None, "plan", None)
            )
            planned = most.value if plan is not None else None
            trial = trial_agents_of(org, plan, dflt)
            if trial is not None:
                out[kind] = Term(max(org.agents_bought or 0, trial), "trial", planned)
            elif field(plan, dflt, "price", "per_agent") and org.agents_bought is not None:
                # An operator's Max agents pinned below the agents bought still throttles them, as the runner-slot
                # override it replaced did.
                pinned = most.overridden and most.value is not None and most.value < org.agents_bought
                out[kind] = (
                    Term(most.value, "override", most.plan_value) if pinned
                    else Term(org.agents_bought, "purchase", planned)
                )
            elif most.overridden:
                out[kind] = Term(most.value, "override", most.plan_value)
            else:
                out[kind] = Term(planned, "plan", planned)
            continue
        planned = _unlimited(field(plan, dflt, kind), kind)  # type: ignore[arg-type]
        own = getattr(org, f"limit_{kind}")
        out[kind] = Term(_unlimited(own, kind), "override", planned) if own is not None else Term(planned, "plan", planned)
    return out


def trial_live(org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None) -> bool:
    """`org` is in the free trial of its per-agent plan now: the subscription mirror `orgs.agents_trial_until` (written
    by `app/org_plan_admin.py::_follow_purchase`, never a subscription read here — the dispatch caps' hot path) is in
    the future and the plan is priced per agent."""
    until = org.agents_trial_until
    if until is None or _as_utc(until) <= _now():
        return False
    plan, dflt = _sized_by(org, plan, defaults_row)
    return plan is not None and bool(field(plan, dflt, "price", "per_agent"))


def trial_agents_of(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None,
) -> Optional[int]:
    """The agent cap a live trial lifts `org` to (`trial_live`): the plan's Max agents (`max_agents_of`), else its
    trial agent ceiling, else `TRIAL_AGENT_CEILING_DEFAULT`. None when no trial is live. Node and worktree capacity
    still bound what runs."""
    if not trial_live(org, plan, defaults_row):
        return None
    plan, dflt = _sized_by(org, plan, defaults_row)
    most = max_agents_of(org, plan, dflt).value
    if most is not None:
        return most
    ceiling = plan.trial_agent_ceiling if plan is not None else None
    return ceiling if ceiling is not None else TRIAL_AGENT_CEILING_DEFAULT


def min_agents_of(org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None) -> Term[int]:
    """The fewest agents the org may buy on `plan` (its own plan)."""
    planned: int = field(*_sized_by(org, plan, defaults_row), "min_agents") or 1  # type: ignore[assignment]
    own = org.min_agents_override
    return Term(own, "override", planned) if own is not None else Term(planned, "plan", planned)


def max_agents_of(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None,
) -> Term[Optional[int]]:
    """The most agents the org may buy on `plan` (its own plan); None = no upper bound."""
    planned: Optional[int] = field(*_sized_by(org, plan, defaults_row), "max_agents")  # type: ignore[assignment]
    own = org.max_agents_override
    return Term(_unlimited(own), "override", planned) if own is not None else Term(planned, "plan", planned)


def static_slots_of(org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None) -> Term[int]:
    planned: int = field(*_sized_by(org, plan, defaults_row), "static_slots") or 0  # type: ignore[assignment]
    own = org.static_slots_override
    return Term(own, "override", planned) if own is not None else Term(planned, "plan", planned)


def then_plan_of(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None,
) -> Term[Optional[str]]:
    """The plan key the org moves to once its plan ends; None = none."""
    planned: Optional[str] = field(plan, _defaults_for(plan, org, defaults_row), "then_plan_key")  # type: ignore[assignment]
    if org.then_plan_overridden:
        return Term(org.then_plan_key_override, "override", planned)
    return Term(planned, "plan", planned)


def providers_term(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None,
) -> Term[frozenset[Cell]]:
    """The provider grid of `org` (already `terms_org`'s) on `plan`: its override, else the plan's (or `(default)`'s
    when the plan inherits it), else `provider_auth_policy.SEED`. An operator-plane org has no plan: `(default)`'s."""
    if not is_tenant_org(org.id):
        plan = None
    raw = field(plan, _defaults_for(plan, org, defaults_row), PROVIDERS)
    planned = provider_auth_policy.SEED if raw is None else provider_auth_policy.decoded(raw)
    if org.providers_override is not None:
        return Term(provider_auth_policy.decoded(org.providers_override), "override", planned)
    return Term(planned, "plan", planned)


def providers_of(session: Session, org_id: Optional[UUID], *, memo: Optional[dict] = None) -> frozenset[Cell]:
    """`effective(...).providers.value` for a caller holding only the id — the hot path every door and every account
    pick reads (`app/provider_auth_policy.py`); `memo` (keyed by org id) serves a pass's repeat reads. No org id reads
    `(default)`'s value (else `SEED`); an id naming no org reads nothing allowed (fail closed)."""
    if memo is not None and org_id in memo:
        return memo[org_id]
    if org_id is None:
        raw = getattr(defaults(session), PROVIDERS, None)
        cells = provider_auth_policy.SEED if raw is None else provider_auth_policy.decoded(raw)
    else:
        org = session.get(Org, org_id)
        if org is None:
            cells = frozenset()
        else:
            org = terms_org(session, org)
            cells = providers_term(org, plan_row(session, org.plan_key), defaults(session)).value
    if memo is not None:
        memo[org_id] = cells
    return cells


def terms_org(session: Session, org: Org) -> Org:
    """The org whose plan and overrides decide `org`'s terms: a staging org's parent (one plan, one bill — D-F7),
    else `org` itself. Every term read below starts here."""
    if org.parent_org_id is None:
        return org
    return session.get(Org, org.parent_org_id) or org


def caps(session: Session, org: Org) -> dict[str, Term[Optional[int]]]:
    """`effective(...).caps` without reading the products."""
    org = terms_org(session, org)
    return caps_of(org, plan_row(session, org.plan_key))


def plan_end(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None,
) -> Optional[datetime]:
    """What the plan alone says the org's end is: its start plus the plan's duration (DA-3); None = never."""
    days = field(plan, _defaults_for(plan, org, defaults_row), "duration_days")
    return None if not days else _as_utc(org.plan_started_at) + timedelta(days=days)  # type: ignore[arg-type]


def ends_of(
    org: Org, plan: Optional[BillingPlan], defaults_row: Optional[BillingPlan] = None,
) -> Term[Optional[datetime]]:
    planned = plan_end(org, plan, defaults_row)
    if org.plan_end_overridden:
        own = org.plan_end_override
        return Term(_as_utc(own) if own is not None else None, "override", planned)
    return Term(planned, "plan", planned)


def plan_chain(session: Session, plan: Optional[BillingPlan]) -> tuple[BillingPlan, ...]:
    """`plan` then every plan it includes, transitively (`billing_plans.includes_plan_key`), each once: a cycle the
    save refuses (`routes/billing_plans.py`) still ends here. Empty for no plan."""
    chain: list[BillingPlan] = []
    seen: set[str] = set()
    current = plan
    while current is not None and current.key not in seen:
        chain.append(current)
        seen.add(current.key)
        key = current.includes_plan_key
        current = session.scalars(select(BillingPlan).where(BillingPlan.key == key)).one_or_none() \
            if key is not None else None
    return tuple(chain)


def _default_switch(defaults_row: Optional[BillingPlan], name: str) -> bool:
    """`(default)`'s value for switch `name`; its code constant with no `(default)` row (or, defensively, a NULL)."""
    value = getattr(defaults_row, name) if defaults_row is not None else None
    return SWITCH_BY_NAME[name].default if value is None else bool(value)


def _switch(org: Org, chain: Sequence[BillingPlan], name: str, defaults_row: Optional[BillingPlan]) -> Term[bool]:
    """One plan switch: `orgs.<name>_override`, else ON when the plan or any plan it includes has it ON (`chain`,
    `plan_chain`'s; `inherited_from` names the first including plan that does), else `(default)`'s value when the
    plan's own is NULL, else off. With no plan — an operator-plane org has none for this (`routes/orgs.py` refuses
    its plan) — `(default)`'s value."""
    inherited: Optional[str] = None
    if chain and is_tenant_org(org.id):
        donor = next((plan for plan in chain if getattr(plan, name)), None)
        if donor is not None:
            planned = True
            if donor is not chain[0]:
                inherited = donor.key
        else:
            planned = getattr(chain[0], name) is None and _default_switch(defaults_row, name)
    else:
        planned = _default_switch(defaults_row, name)
    own = getattr(org, f"{name}_override")
    if own is not None:
        return Term(own, "override", planned, inherited)
    return Term(planned, "plan", planned, inherited)


def _switches(
    org: Org, chain: Sequence[BillingPlan], names: Sequence[str], defaults_row: Optional[BillingPlan],
) -> dict[str, Term[bool]]:
    return {name: _switch(org, chain, name, defaults_row) for name in names}


def org_chain(session: Session, org: Org, plan: Optional[BillingPlan] = None) -> tuple[BillingPlan, ...]:
    """`plan_chain` of the org's plan row; `plan` is that row when the caller already read it."""
    return plan_chain(session, plan if plan is not None else plan_row(session, org.plan_key))


def effective(session: Session, org: Org, now: Optional[datetime] = None) -> PlanTerms:
    """Everything `org`'s plan gives it, each term override → plan (a switch also → an included plan). A staging
    org's are its parent's (`terms_org`)."""
    org = terms_org(session, org)
    plan = plan_row(session, org.plan_key)
    dflt = defaults(session)
    ends = ends_of(org, plan, dflt)
    lines_row = field_row(plan, dflt, "products")
    planned_lines = plan_lines(session, lines_row) if lines_row is not None else ()
    products = (
        Term(org_lines(session, org.id), "override", planned_lines) if org.products_overridden
        else Term(planned_lines, "plan", planned_lines)
    )
    return PlanTerms(
        plan_key=org.plan_key,
        plan_name=plan.name if plan is not None and plan.key == org.plan_key else org.plan_key,
        caps=caps_of(org, plan, dflt),
        ends_at=ends,
        ended=ends.value is not None and ends.value <= (now or _now()),
        switches=_switches(org, plan_chain(session, plan), [spec.name for spec in SWITCHES], dflt),
        products=products,
        providers=providers_term(org, plan, dflt),
    )


def _org_switch(session: Session, org: Org, name: str) -> bool:
    org = terms_org(session, org)
    return _switch(org, org_chain(session, org), name, defaults(session)).value


def hosting_allowed(session: Session, org: Org) -> bool:
    """`effective(...).hosting_allowed.value` without reading the products."""
    return _org_switch(session, org, "hosting_allowed")


def compliance_enabled(session: Session, org: Org, plan: Optional[BillingPlan] = None) -> bool:
    """`effective(...).compliance_enabled.value` without reading the products. `plan` is the org's plan row when
    the caller already read it (a page of orgs), else it is read here."""
    parent = terms_org(session, org)
    chain = org_chain(session, parent, plan if parent is org else None)
    return _switch(parent, chain, "compliance_enabled", defaults(session)).value


def org_switch_terms(
    session: Session, org: Org, names: Sequence[str], plan: Optional[BillingPlan] = None,
) -> dict[str, Term[bool]]:
    """Several switches of an org row already read, its plan chain walked once; `plan` is its plan row when the
    caller already read it (a page of orgs), else it is read here."""
    parent = terms_org(session, org)
    return _switches(parent, org_chain(session, parent, plan if parent is org else None), names, defaults(session))


def advanced_compliance_enabled(session: Session, org: Org) -> bool:
    """`effective(...).advanced_compliance_enabled.value` without reading the products."""
    return _org_switch(session, org, "advanced_compliance_enabled")


def nodevault_allowed(session: Session, org: Org) -> bool:
    """`effective(...).nodevault_allowed.value` without reading the products."""
    return _org_switch(session, org, "nodevault_allowed")


def nodelink_allowed(session: Session, org: Org) -> bool:
    """`effective(...).nodelink_allowed.value` without reading the products."""
    return _org_switch(session, org, "nodelink_allowed")


def merge_lane_enabled(session: Session, org: Org) -> bool:
    """`effective(...).merge_lane_enabled.value` without reading the products."""
    return _org_switch(session, org, "merge_lane_enabled")


def microvm_allowed(session: Session, org: Org) -> bool:
    """`effective(...).microvm_allowed.value` without reading the products."""
    return microvm_allowed_term(session, org).value


def microvm_allowed_term(session: Session, org: Org, plan: Optional[BillingPlan] = None) -> Term[bool]:
    """`effective(...).microvm_allowed` without reading the products. `plan` is the org's plan row when the
    caller already read it (a page of orgs), else it is read here."""
    parent = terms_org(session, org)
    chain = org_chain(session, parent, plan if parent is org else None)
    return _switch(parent, chain, "microvm_allowed", defaults(session))


def playbooks_enabled(session: Session, org: Org) -> bool:
    """`effective(...).playbooks_enabled.value` without reading the products."""
    return _org_switch(session, org, "playbooks_enabled")


def org_templates_enabled(session: Session, org: Org) -> bool:
    """`effective(...).org_templates_enabled.value` without reading the products."""
    return _org_switch(session, org, ORG_TEMPLATES)


def github_runners_allowed(session: Session, org_id: Optional[UUID]) -> bool:
    """Whether the org `org_id` may run GitHub Runners at all: its GitHub Runners cap is not 0 (`OFFABLE_CAPS`),
    with `switches_on`'s rules for no org and an unknown one."""
    return switches_on(session, org_id, (GITHUB_RUNNERS,))[GITHUB_RUNNERS]


def node_os_allowed(session: Session, org_id: Optional[UUID], os: str) -> bool:
    """Whether the org `org_id` may register a new self-hosted node of `os` (its `NODE_OS_SWITCHES` switch), with
    `switch_on`'s rules: no org id reads `(default)`'s value, an unknown org reads off."""
    return switch_on(session, org_id, NODE_OS_SWITCHES[os])


def switch_on(session: Session, org_id: Optional[UUID], name: str) -> bool:
    """Plan switch `name` (a `SWITCHES` name) for the org `org_id`, for a caller holding only the id, included
    plans counted. No org id reads `(default)`'s value; an id naming no org reads off (fail closed)."""
    return switches_on(session, org_id, (name,))[name]


def switches_on(session: Session, org_id: Optional[UUID], names: Sequence[str]) -> dict[str, bool]:
    """`switch_on` for several switches, the org and its plan chain read once. `GITHUB_RUNNERS` may be among
    `names`: the org's GitHub Runners cap not 0, ON with no org (no plan, no cap)."""
    switches = [name for name in names if name != GITHUB_RUNNERS]
    if org_id is None:
        dflt = defaults(session)
        return {name: True if name == GITHUB_RUNNERS else _default_switch(dflt, name) for name in names}
    org = session.get(Org, org_id)
    if org is None:
        return dict.fromkeys(names, False)
    org = terms_org(session, org)
    chain = org_chain(session, org)
    dflt = defaults(session)
    out = {name: term.value for name, term in _switches(org, chain, switches, dflt).items()}
    if GITHUB_RUNNERS in names:
        plan = chain[0] if chain else None
        out[GITHUB_RUNNERS] = caps_of(org, plan, dflt)["github_runners"].value != 0
    return out


def not_on_plan(name: str) -> str:
    """The refusal a door answers while the org's plan leaves switch `name` out: what, and what to do next."""
    return f"{SWITCH_BY_NAME[name].label} is not on your plan. Upgrade in Billing."


def require_switch(session: Session, org_id: UUID, name: str) -> None:
    """403 naming the plan feature (`not_on_plan`) unless switch `name` is on for the org `org_id` (`switch_on`)."""
    if not switch_on(session, org_id, name):
        raise HTTPException(status_code=403, detail=not_on_plan(name))


def require_tenant_switch(session: Session, principal: Optional[object], name: str) -> None:
    """`require_switch` for a tenant caller's own org; the operator (and a caller with no org) passes: the operator
    reads every org whatever its own plan says, as the console shows it every surface."""
    org_id = getattr(principal, "org_id", None)
    if org_id is not None and not getattr(principal, "is_operator", False):
        require_switch(session, org_id, name)


def static_slots(session: Session, org: Org) -> int:
    """The org's static slots (`static_slots_of`; 0 = none); 0 when no plan row is found and none is overridden."""
    org = terms_org(session, org)
    return static_slots_of(org, plan_row(session, org.plan_key)).value


# Built once: a clause is immutable, and building it proxies every `BillingPlan` column twice (the alias and the
# fallback), a few ms on each board read.
@functools.cache
def ended_clause() -> ColumnElement[bool]:
    """`PlanTerms.ended` in SQL, correlated to `orgs` (`app/pool_state.py::_node_org_in_service`). The duration is
    the org's plan row's, else `(default)`'s — when the row inherits it or is missing (a missing row behaves like
    `(default)`, never fail-open) — parity with `effective` is `tests/test_plan_terms.py`'s matrix."""
    own, fallback = aliased(BillingPlan), aliased(BillingPlan)
    own_row = and_(own.key == Org.plan_key, ~own.inherits.any("duration_days"))
    duration = case(
        (exists().where(own_row), select(own.duration_days).where(own_row).scalar_subquery()),
        else_=select(fallback.duration_days).where(fallback.key == DEFAULTS_KEY).scalar_subquery(),
    )
    return case(
        (
            Org.plan_end_overridden.is_(True),
            and_(Org.plan_end_override.is_not(None), Org.plan_end_override <= func.now()),
        ),
        else_=and_(
            duration.is_not(None),
            Org.plan_started_at + func.make_interval(0, 0, 0, duration) <= func.now(),
        ),
    )


# ── Changing an org's plan ───────────────────────────────────────────────────────────────────────────


def was_text(what: str, was: object) -> str:
    """A cleared override's value as people read it (`what` its label, `was` its history `data["from"]`): a switch
    On or Off, a cap its number, the end its day or never, the products their lines."""
    if isinstance(was, bool):
        return "On" if was else "Off"
    if isinstance(was, list):
        return ", ".join(str(item) for item in was) or "none"
    if was is None:
        return "never"
    if what == "Max agents" and was == 0:
        return "no limit"
    if what == _CAP_LABELS["github_runners"] and isinstance(was, int):
        return "Off" if was == 0 else "no limit" if was >= CAP_MAX else str(was)
    return str(was)[:10] if what == "End date" else str(was)


def _overrides(session: Session, org: Org) -> list[tuple[str, object, Callable[[], None]]]:
    """Every override the org holds, as `(label, value, clear)` — the value as `_record_cleared` records it, and what
    clears it. The one list both the move's preview (`cleared_overrides`) and the move (`clear_overrides`) read."""
    def unset(attr: str) -> Callable[[], None]:
        return lambda: setattr(org, attr, None)

    def clear_end() -> None:
        org.plan_end_overridden, org.plan_end_override = False, None

    def clear_then_plan() -> None:
        org.then_plan_overridden, org.then_plan_key_override = False, None

    out: list[tuple[str, object, Callable[[], None]]] = [
        (_CAP_LABELS[kind], getattr(org, f"limit_{kind}"), unset(f"limit_{kind}")) for kind in COLUMN_CAP_KINDS
        if getattr(org, f"limit_{kind}") is not None
    ]
    if org.plan_end_overridden:
        out.append(("End date", iso_or_none(org.plan_end_override), clear_end))
    out.extend((label, getattr(org, attr), unset(attr)) for label, attr in _LIMIT_OVERRIDES if getattr(org, attr) is not None)
    if org.then_plan_overridden:
        out.append((THEN_PLAN, plan_name(session, org.then_plan_key_override) or "none", clear_then_plan))
    out.extend((spec.label, getattr(org, f"{spec.name}_override"), unset(f"{spec.name}_override")) for spec in SWITCHES
               if getattr(org, f"{spec.name}_override") is not None)
    if org.providers_override is not None:
        out.append(("Providers", _cell_labels(provider_auth_policy.decoded(org.providers_override)),
                    lambda: setattr(org, "providers_override", None)))
    if org.products_overridden:
        out.append(("Products", [describe(line, session) for line in org_lines(session, org.id)],
                    lambda: clear_products(session, org)))
    return out


def _cell_labels(cells: frozenset[Cell]) -> list[str]:
    """A provider grid as the org's history records it ("<provider> · OAuth"), in catalogue order."""
    return [provider_auth_policy.cell_label((row, auth)) for row, auth in provider_auth_policy.encoded(cells)]


def cleared_overrides(session: Session, org: Org) -> list[str]:
    """The overrides a plan move would clear (DA-4), each with its value ("Agents (On)") — `DryRunOut.overrides_cleared`,
    which the move's confirm lists beside its Keep overrides choice."""
    return [f"{what} ({was_text(what, was)})" for what, was, _ in _overrides(session, org)]


def _record_cleared(session: Session, org: Org, actor: Optional[AdminUser], what: str, was: object) -> None:
    record_write(
        session, actor, OVERRIDE_CLEARED,
        f"Cleared org {org.name}'s {what} override (was {was_text(what, was)}) with its plan change",
        data={"org_id": str(org.id), "override": what, "from": was},
        org_id=org.id,
    )


def clear_overrides(session: Session, org: Org, actor: Optional[AdminUser]) -> None:
    """Clear every override the org holds (caps, end, agent range, static slots, then plan, every switch, providers, products —
    DA-4), one history row per value."""
    for what, was, clear in _overrides(session, org):
        clear()
        _record_cleared(session, org, actor, what, was)


def assign(
    session: Session, org: Org, plan_key: str, actor: Optional[AdminUser], *, kind: str, keep_overrides: bool = False,
) -> bool:
    """Put `org` on `plan_key`: a no-op (False) when it is already on it. Otherwise stamps `plan_started_at`
    (the plan starts afresh — DA-6), clears `pending_plan_key` and every override (DA-4) — none with `keep_overrides`,
    the operator's choice on the move's confirm — records `kind` (the move) plus one row per cleared value, and
    wakes the bundle reconciler after the commit. The caller validates `plan_key` and commits. The agents bought
    and the billing interval clear too."""
    if org.plan_key == plan_key:
        return False
    before = org.plan_key
    # The cleared overrides first, so the move is the newest row of the org's history (one transaction stamps them
    # all alike; the history breaks the tie by id).
    if not keep_overrides:
        clear_overrides(session, org, actor)
    kept = ", overrides kept" if keep_overrides and _overrides(session, org) else ""
    record_write(
        session, actor, kind,
        f"Moved org {org.name} from {plan_name(session, before)} to {plan_name(session, plan_key)}{kept}",
        data={"org_id": str(org.id), "from": before, "to": plan_key, **({"kept_overrides": True} if kept else {})},
        org_id=org.id,
    )
    org.plan_key = plan_key
    org.plan_started_at = _now()
    org.pending_plan_key = None
    # A staging org's terms are its parent's (`terms_org`); its own plan columns follow, so a reader of the bare
    # column (quotas, the operator's lists) sees the same plan.
    session.execute(
        update(Org).where(Org.parent_org_id == org.id).values(plan_key=plan_key, plan_started_at=org.plan_started_at)
    )
    # What the old plan's subscription bought is not the new plan's (`org_plan_admin.on_entitlement` writes it anew).
    org.agents_bought = org.billing_interval = None
    if actor is not None:
        stamp_actor(org, actor)
    wake_bundles(session)
    return True


def wake_bundles(session: Session) -> None:
    """Wake the bundle reconciler (`app/plan_bundles.py::run`) once this transaction commits. Loss-tolerant: the
    cron's interval is the backstop."""
    from app import cron_runtime

    cron_runtime.wake_after_commit(session, BUNDLES_ACTIVITY)


# ── Bundled products ─────────────────────────────────────────────────────────────────────────────────


def receives(terms: PlanTerms, org: Org) -> bool:
    """Whether `org` receives its plan's products now — its nodes and its Jaah AI credit (§4.1, §4.4): not once
    its plan has ended, while it is not active, on the operator plane (NC-8), while a priced choice awaits
    checkout, or as a staging org (its parent receives them)."""
    return not (
        terms.ended or org.lifecycle != ORG_ACTIVE or not is_tenant_org(org.id) or org.pending_plan_key
        or org.parent_org_id is not None
    )


def desired_nodes(terms: PlanTerms, org: Org) -> list[tuple[str, int]]:
    """The node instances `org` should hold, as `(fingerprint, ordinal)` (§4.1); none unless it `receives`."""
    if not receives(terms, org):
        return []
    wanted: Counter[str] = Counter()
    for line in terms.products.value:
        fingerprint = line.fingerprint()
        if fingerprint is not None:
            wanted[fingerprint] += line.quantity
    return [(fp, ordinal) for fp, count in wanted.items() for ordinal in range(1, count + 1)]


def bundle_diff(session: Session, org: Org, terms: PlanTerms) -> BundleDiff:
    """`desired_nodes` against the org's held `org_bundled_products` instances, counted per fingerprint."""
    wanted = set(desired_nodes(terms, org))
    held = session.execute(
        select(OrgBundledProduct.fingerprint, OrgBundledProduct.ordinal, OrgBundledProduct.state).where(
            OrgBundledProduct.org_id == org.id, OrgBundledProduct.state.in_(_HELD_STATES),
            OrgBundledProduct.comp_id.is_(None),  # a comp's copies are not the plan's to provision or release
        )
    ).all()
    have = {(fp, ordinal) for fp, ordinal, _state in held}
    diff = BundleDiff()
    for fp, ordinal in sorted(wanted):
        bucket = diff.carry if (fp, ordinal) in have else diff.provision
        bucket[fp] = bucket.get(fp, 0) + 1
    for fp, ordinal, state in held:
        if (fp, ordinal) not in wanted and state in _ACTIVE_STATES:
            diff.release[fp] = diff.release.get(fp, 0) + 1
    return diff


def _price_names(session: Session) -> dict[tuple[str, str], str]:
    """Each catalogue row's name as a product line shows it: a Shared tier carries its disk ("Tier S · 20 GB")."""
    return {
        (r.kind, r.key): f"{r.name} · {r.disk_gb} GB" if r.kind == "shared" and r.disk_gb else r.name
        for r in session.scalars(select(HostingPrice)).all()
    }


def _dollars(cents: int) -> str:
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def describe(line: ProductLine, session: Session, *, names: Optional[dict[tuple[str, str], str]] = None) -> str:
    """THE product label: "2 × Shared node · Tier S · 20 GB" (a tier named "Shared …" says Shared once: "Shared
    node (8 vCPU, 32 GB) · 20 GB"), "Dedicated node · t3.large · 100 GB", "Jaah AI · $5/month",
    "Live Cast · 3 projects · Backfill 100" (its quantity is the projects, so it takes no "N ×").
    `names` is a prefetched `_price_names` for a caller labelling many lines."""
    if line.kind == "live_cast":
        projects = "1 project" if line.quantity == 1 else f"{line.quantity} projects"
        return f"Live Cast · {projects} · Backfill {line.backfill_tasks}"
    names = _price_names(session) if names is None else names
    if line.kind == "shared_node":
        tier = names.get(("shared", line.tier_key or ""), line.tier_key) or ""
        # A tier the operator named for its kind ("Shared (8 vCPU, 32 GB) · 20 GB") already says Shared: its word
        # becomes the kind's ("Shared node (8 vCPU, 32 GB) · 20 GB"), never "Shared node · Shared (…)".
        # A tier already named "Shared node …" keeps one "node" ("Shared node S", never "Shared node node S"); no
        # name at all (nor key) is the bare kind, never "Shared node · " with a dangling separator.
        named = tier[:6].lower() == "shared" and not tier[6:7].isalnum()
        rest = tier[6:]
        if named and rest[:5].lower() == " node" and not rest[5:6].isalnum():
            rest = rest[5:]
        label = f"Shared node{rest}" if named else f"Shared node · {tier}" if tier else "Shared node"
    elif line.kind == "dedicated_node":
        size = names.get(("instance", line.instance_type or ""), line.instance_type)
        label = f"Dedicated node · {size} · {line.storage_gb} GB"
    else:
        label = f"Jaah AI · {_dollars(line.credit_cents or 0)}/month"
    return label if line.quantity == 1 else f"{line.quantity} × {label}"


def describe_fingerprint(fingerprint: str, session: Session, names: dict[tuple[str, str], str]) -> str:
    """A fingerprint's label without a count (the dry-run's rows carry the count apart)."""
    kind, _, rest = fingerprint.partition(":")
    if kind == "shared_node":
        return describe(ProductLine(None, "shared_node", 1, tier_key=rest), session, names=names)
    instance_type, _, gb = rest.rpartition(":")
    return describe(
        ProductLine(None, "dedicated_node", 1, instance_type=instance_type, storage_gb=int(gb) if gb.isdigit() else None),
        session, names=names,
    )


class ProductError(ValueError):
    """A product line the catalogue cannot sell: the sentence says which and why."""


LIVE_CAST_NOT_OFFERED: Final = "Live Cast isn't offered. Set its price in Products › Live Cast."


def validate_lines(session: Session, lines: Sequence[ProductLine]) -> list[ProductLine]:
    """`lines` normalized to their kind's fields, each checked against the ACTIVE hosting catalogue (NC-2)."""
    active = {(r.kind, r.key) for r in session.scalars(select(HostingPrice).where(HostingPrice.active)).all()}
    out: list[ProductLine] = []
    for line in lines:
        if line.quantity < 1:
            raise ProductError("A product's quantity is at least 1.")
        if line.kind == "shared_node":
            if not line.tier_key or ("shared", line.tier_key) not in active:
                raise ProductError(f"No Shared tier {line.tier_key!r} is offered. Pick one from the catalogue.")
            out.append(ProductLine(None, "shared_node", line.quantity, tier_key=line.tier_key))
        elif line.kind == "dedicated_node":
            if not line.instance_type or ("instance", line.instance_type) not in active:
                raise ProductError(f"No machine size {line.instance_type!r} is offered. Pick one from the catalogue.")
            if line.storage_gb is None or ("storage", str(line.storage_gb)) not in active:
                raise ProductError(f"No {line.storage_gb} GB storage is offered. Pick one from the catalogue.")
            out.append(ProductLine(
                None, "dedicated_node", line.quantity, instance_type=line.instance_type, storage_gb=line.storage_gb,
            ))
        elif line.kind == "jaah_ai_credit":
            if line.credit_cents is None or line.credit_cents < 0:
                raise ProductError("Jaah AI credit needs an amount of 0 or more.")
            out.append(ProductLine(None, "jaah_ai_credit", line.quantity, credit_cents=line.credit_cents))
        elif line.kind == "live_cast":
            if LIVE_CAST_PRICE not in active:
                raise ProductError(LIVE_CAST_NOT_OFFERED)
            # A line sent without its N takes the catalogue's default, as the editors pre-fill it.
            backfill = line.backfill_tasks
            if backfill is None:
                backfill = settings_reads.get_int(LIVE_CAST_BACKFILL_KNOB)
            if not 1 <= backfill <= MAX_BACKFILL:
                raise ProductError(f"Live Cast's backfill is a whole number from 1 to {MAX_BACKFILL}.")
            out.append(ProductLine(None, "live_cast", line.quantity, backfill_tasks=backfill))
        else:
            raise ProductError(f"Unknown product {line.kind!r}.")
    return out


def _line_columns(line: ProductLine, sort_order: int, label: str) -> dict[str, object]:
    return {
        "name": label, "kind": line.kind, "quantity": line.quantity, "tier_key": line.tier_key,
        "instance_type": line.instance_type, "storage_gb": line.storage_gb, "credit_cents": line.credit_cents,
        "backfill_tasks": line.backfill_tasks, "sort_order": sort_order,
    }


def replace_plan_lines(session: Session, plan: BillingPlan, lines: Sequence[ProductLine], actor: AdminUser) -> None:
    """Replace `plan`'s whole product list with already-validated `lines`."""
    names = _price_names(session)
    session.execute(delete(BillingPlanProduct).where(BillingPlanProduct.plan_id == plan.id))
    for index, line in enumerate(lines):
        row = BillingPlanProduct(plan_id=plan.id, **_line_columns(line, index, describe(line, session, names=names)))
        stamp_actor(row, actor, created=True)
        session.add(row)
    session.flush()


def clear_products(session: Session, org: Org) -> None:
    """Drop the org's products override: it follows its plan's list again."""
    session.execute(delete(OrgPlanProduct).where(OrgPlanProduct.org_id == org.id))
    org.products_overridden = False


def replace_org_lines(session: Session, org: Org, lines: Sequence[ProductLine], actor: AdminUser) -> None:
    """Make already-validated `lines` the org's whole products override (`products_overridden`)."""
    names = _price_names(session)
    session.execute(delete(OrgPlanProduct).where(OrgPlanProduct.org_id == org.id))
    for index, line in enumerate(lines):
        row = OrgPlanProduct(org_id=org.id, **_line_columns(line, index, describe(line, session, names=names)))
        stamp_actor(row, actor, created=True)
        session.add(row)
    org.products_overridden = True
    session.flush()


def is_unsold(plan: BillingPlan, account) -> bool:
    """`plan` has no Price on the pinned Stripe `account` (None = billing not connected)."""
    from app.org_plans import priced_on

    return priced_on(plan, account) is None

