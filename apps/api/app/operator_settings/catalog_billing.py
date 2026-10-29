"""The "Billing" Settings group of the Settings catalogue, in render order."""
from __future__ import annotations

from app.operator_settings.knobs import Knob, _Section, _sectioned

KNOBS: tuple[Knob, ...] = _sectioned(
    _Section("Billing", "Stripe"),
    # ── "Billing" > "Stripe": the one switch that turns billing on (`app/billing/`) ──
    # Operator-tier: the deployment has ONE Stripe account, and its readers (`app/billing/gateway.py`, the
    # org-less `billing-events`/`billing-reconcile` crons) resolve it with no org. Blank = not connected: every
    # billing read answers `connected: false` and every mutation 409s. There is no other gate and no flag —
    # selecting a credential here switches billing on at the next read, with no restart (AC-12.2).
    Knob(
        key="billing_stripe_credential_id",
        tier="operator",
        label="Stripe credential",
        help="The Stripe account billing charges through. Empty turns billing off.",
        # A handle (see `cloud_node_tailscale_credential_id`); `env=None`, 0..36 a shape check, no floor.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("stripe",),
        feature="billing",
    ),
    # ── "Billing" > "Stripe": Stripe Tax (`app/billing/tax.py`). Off until the owner has set Stripe Tax
    # up in the Stripe Dashboard; read per call, so it needs no restart.
    Knob(
        key="billing_tax_enabled",
        tier="operator",
        label="Collect tax",
        help="Stripe Tax adds tax to invoices where you are registered. Set it up in Stripe first.",
        kind="bool", env=None, default=False,
        feature="billing",
    ),
    _Section("Billing", "Plans"),
    # ── "Billing" > "Plans": where a cancelled or lapsed paid plan lands (`app/plan_terms.py::lapse_plan_key`).
    # Also picked on Products › Plans. Blank is the `(default)` row, with its own then-plan rule; a plan retired after
    # it was picked reads as blank.
    Knob(
        key="billing_lapse_plan_key",
        tier="operator",
        label="Plan after cancel",
        help="The plan an org moves to when its paid plan is cancelled or ends.",
        kind="str", env=None, default="", minimum=0, maximum=64, plan=True,
        feature="billing",
    ),
    # ── "Billing" > "Plans": annual billing (`app/org_plans.py::yearly_on`). Off, tenants see monthly prices only and
    # no purchase or switch goes yearly; a yearly subscription keeps its interval. Read per request: no restart.
    Knob(
        key="billing_annual",
        tier="operator",
        label="Annual billing",
        help="Sell plans with a yearly price annually. Off, orgs see monthly prices only.",
        kind="bool", env=None, default=False,
        feature="billing",
    ),
    _Section("Billing", "Bedrock"),
    # ── "Billing" > "Bedrock": the ONE platform Bedrock account behind Jaah token billing
    # (`app/bedrock/`). Operator-tier: exactly one connection per deployment (D12). Five scalar knobs rather
    # than one object, because the catalogue holds scalars; `app/bedrock/account.py::connection` reads
    # them together. In the control plane's own account Terraform sets the
    # source to `task_role` and the region, role and log group through the env vars below, so nothing is
    # typed. Under `credential`, a blank credential = P-unset (no Jaah account offered anywhere).
    Knob(
        key="platform_bedrock_source",
        tier="operator",
        label="Bedrock access",
        help="Where the control plane gets its Bedrock keys: its own AWS role (Bedrock in the account it runs "
        "in), or a stored AWS credential.",
        kind="str", env="PLATFORM_BEDROCK_SOURCE", default="credential",
        choices=("credential", "task_role"), choice_labels=("Stored credential", "Control plane role"),
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_credential_id",
        tier="operator",
        label="Bedrock credential",
        help="The operator AWS credential (marked System) of the platform Bedrock account, used when access is "
        "Stored credential. Empty turns the Jaah account off.",
        # A handle (see `cloud_node_tailscale_credential_id`); `env=None`, 0..36 a shape check, no floor.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("aws",),
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_region",
        tier="operator",
        label="Bedrock region",
        help="The AWS region the inference profiles live in, for example us-east-1.",
        kind="str", env="PLATFORM_BEDROCK_REGION", default="us-east-1", minimum=0, maximum=32,
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_invoke_role_arn",
        tier="operator",
        label="Invoke role ARN",
        help="The role runners assume to call Bedrock (the Terraform output invoke_role_arn).",
        kind="str", env="PLATFORM_BEDROCK_INVOKE_ROLE_ARN", default="", minimum=0, maximum=2048,
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_log_group",
        tier="operator",
        label="Invocation log group",
        help="The CloudWatch log group Bedrock writes model-invocation logs to.",
        kind="str", env="PLATFORM_BEDROCK_LOG_GROUP", default="/jaah/bedrock/invocations", minimum=0, maximum=512,
        feature="jaah_ai_credit",
    ),
    _Section("Billing", "Live Cast"),
    # ── "Billing" > "Live Cast": the Live Cast product's catalogue default. Its price is the
    # `rental_prices` row `live_cast:project` (`app/live_cast/billing.py`), shown beside this on Products.
    Knob(
        key="live_cast_default_backfill",
        tier="operator",
        label="Default backfill",
        help="How many of a project's latest tasks a new Live Cast line evaluates when published. Saved lines "
        "keep their own.",
        kind="int", env=None, default=100, minimum=1, maximum=1000, unit="tasks",
        feature="live_cast",
    ),
    _Section("Billing", "Jaah AI"),
    # ── "Billing" > "Jaah AI": the platform defaults of Jaah token billing (`app/token_billing/`).
    # Operator-tier, in the operator's units (dollars, percent); `app/token_billing/settings.py` converts them
    # and is their only reader. A tenant's own floor and markup are NOT knobs: the operator sets them on
    # Orgs > <tenant> > Jaah AI, and they live in `org_settings` under `jaah_ai.*` keys this catalogue never
    # names, so the generic Settings writer cannot reach them.
    Knob(
        key="jaah_ai_signup_credit_usd",
        tier="operator",
        label="Signup credit",
        help="Free Jaah AI credit each new organization gets once, when it is created. 0 gives none.",
        kind="float", env=None, default=0.0, minimum=0, maximum=10_000, unit="USD",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_min_topup_usd",
        tier="operator",
        label="Minimum top-up",
        help="The smallest card top-up a tenant may buy.",
        kind="float", env=None, default=10.0, minimum=1, maximum=10_000, unit="USD",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_default_floor_usd",
        tier="operator",
        label="Default pause point",
        help="Work on the Jaah account pauses when a balance falls to this. May be negative. An organization's "
        "own value overrides it.",
        kind="float", env=None, default=0.0, minimum=-1_000_000, maximum=1_000_000, unit="USD",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_default_markup_pct",
        tier="operator",
        label="Default markup",
        help="Added to Bedrock's price on every call. 0 passes the cost through. An organization's own value "
        "overrides it.",
        kind="float", env=None, default=0.0, minimum=0, maximum=1000, unit="%",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_reconcile_tolerance_pct",
        tier="operator",
        label="Usage tolerance",
        help="How far a node's token count may differ from AWS's before the call is flagged.",
        kind="float", env=None, default=2.0, minimum=0, maximum=100, unit="%",
        feature="jaah_ai_credit",
    ),
    _Section("Billing", "Hosting"),
    # ── "Billing" > "Hosting": which ways to host a node a tenant is offered ──
    # Product controls, not feature flags: default ON in code. The first two are org-tier, so the operator
    # turns one off for every tenant here or for one tenant on Orgs > <tenant> > Settings (D6, A-2). Read by
    # `app/hosting_catalog.py::offerings` on every request (the catalog, and the order path that charges).
    Knob(
        key="rent_dedicated_monthly_enabled",
        tier="org",
        label="Monthly hosting",
        help="A dedicated host, paid a month in advance.",
        kind="bool", env=None, default=True,
        feature="hostings",
    ),
    Knob(
        key="rent_shared_enabled",
        tier="org",
        label="Shared node hosting",
        help="A node on a host shared with other organizations.",
        kind="bool", env=None, default=True,
        feature="hostings",
    ),
    # Operator-tier: how long the deployment keeps an unpaid or stopped hosting's machine (Architecture §3.4).
    Knob(
        key="rental_grace_days",
        tier="operator",
        label="Unpaid grace",
        help="How long a hosting with a failed payment keeps running before it stops.",
        kind="int", env=None, default=7, minimum=0, maximum=60, unit="days",
        feature="hostings",
    ),
    Knob(
        key="rental_retention_days",
        tier="operator",
        label="Stopped retention",
        help="How long a stopped hosting's machine and data are kept before it is deleted.",
        kind="int", env=None, default=14, minimum=0, maximum=365, unit="days",
        feature="hostings",
    ),
    # Read by `app/hosting_provisioning.py` on every Dedicated launch and catalog read. Replaced the
    # credential pointer `rental_aws_credential_id`: a stale row under that key is ignored on
    # load (`_stored_candidates` reads the store by catalogue key) and dropped on the next whole-dict write.
    Knob(
        key="rental_connection_id",
        tier="operator",
        label="Hosting connection",
        help="The cloud connection Jaah-hosted machines launch through; its credential must be the operator's own.",
        # A handle (see `cloud_node_tailscale_credential_id`); `env=None`, 0..36 a shape check.
        kind="str", env=None, default="", minimum=0, maximum=36,
        cloud_connection=True,
        feature="hostings",
    ),
)
