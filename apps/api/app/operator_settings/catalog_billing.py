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
        help="Billing charges through this Stripe account; empty turns billing off.",
        more="Takes effect at the next billing read, with no restart. While empty, every billing page reports not "
        "connected and every billing change is refused.",
        docs="administration/plan-and-billing/billing",
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
        help="Stripe Tax adds tax to invoices where you're registered.",
        more="Set Stripe Tax up in the Stripe Dashboard first. Read on every charge, so it applies without a restart.",
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
        help="Cancelled or lapsed paid plans move orgs to this plan.",
        more="Blank uses the default plan row, following its own then-plan rule. A plan retired after it was picked "
        "counts as blank, and so does the plan that lapsed.",
        kind="str", env=None, default="", minimum=0, maximum=64, plan=True,
        feature="billing",
    ),
    # ── "Billing" > "Plans": annual billing (`app/org_plans.py::yearly_on`). Off, tenants see monthly prices only and
    # no purchase or switch goes yearly; a yearly subscription keeps its interval. Read per request: no restart.
    Knob(
        key="billing_annual",
        tier="operator",
        label="Annual billing",
        help="Off: orgs see monthly prices and buy monthly only.",
        more="Turning it off leaves existing yearly subscriptions on their yearly interval. Read per request, so no "
        "restart.",
        docs="administration/plan-and-billing/subscription",
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
        help="Bedrock calls sign with a stored key or assumed role.",
        more="In the control plane's own AWS account, Terraform sets Control plane role plus the region, invoke role "
        "and log group, so nothing is typed.",
        kind="str", env="PLATFORM_BEDROCK_SOURCE", default="credential",
        choices=("credential", "task_role"), choice_labels=("Stored credential", "Control plane role"),
        choice_help=("Uses the stored AWS credential picked below.", "Uses the control plane's own role and account."),
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_credential_id",
        tier="operator",
        label="Bedrock credential",
        help="Bedrock calls sign with this credential; empty hides Jaah AI.",
        more="Used only when Bedrock access is Stored credential. Pick an operator AWS credential marked System. While "
        "empty, no organization is offered the Jaah AI account.",
        # A handle (see `cloud_node_tailscale_credential_id`); `env=None`, 0..36 a shape check, no floor.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("aws",),
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_region",
        tier="operator",
        label="Bedrock region",
        help="Bedrock calls and inference profiles use this AWS region.",
        more="For example us-east-1: the region the platform's inference profiles live in.",
        kind="str", env="PLATFORM_BEDROCK_REGION", default="us-east-1", minimum=0, maximum=32,
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_invoke_role_arn",
        tier="operator",
        label="Invoke role ARN",
        help="Runners assume this role to call Bedrock.",
        more="The Terraform output invoke_role_arn.",
        kind="str", env="PLATFORM_BEDROCK_INVOKE_ROLE_ARN", default="", minimum=0, maximum=2048,
        feature="jaah_ai_credit",
    ),
    Knob(
        key="platform_bedrock_log_group",
        tier="operator",
        label="Invocation log group",
        help="Jaah AI usage is metered from Bedrock logs here.",
        more="Bedrock must write its model-invocation logs to this CloudWatch log group.",
        kind="str", env="PLATFORM_BEDROCK_LOG_GROUP", default="/jaah/bedrock/invocations", minimum=0, maximum=512,
        feature="jaah_ai_credit",
    ),
    _Section("Billing", "Live Cast"),
    # ── "Billing" > "Live Cast": the Live Cast product's catalogue default. Its price is the Live Cast
    # product's (`app/products/kinds_impl/live_cast.py`), shown beside this on Products.
    Knob(
        key="live_cast_default_backfill",
        tier="operator",
        label="Default backfill",
        help="Published Live Cast lines first evaluate this many recent tasks.",
        more="Applies to new lines only; a saved line keeps its own number.",
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
        help="Each new organization gets this Jaah AI credit once.",
        more="Granted when the organization is created. 0 gives none.",
        docs="administration/plan-and-billing/jaah-ai",
        kind="float", env=None, default=0.0, minimum=0, maximum=10_000, unit="USD",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_min_topup_usd",
        tier="operator",
        label="Minimum top-up",
        help="Card top-ups below this amount are refused.",
        docs="administration/plan-and-billing/jaah-ai",
        kind="float", env=None, default=10.0, minimum=1, maximum=10_000, unit="USD",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_default_floor_usd",
        tier="operator",
        label="Default pause point",
        help="Jaah AI work pauses when a balance falls to this.",
        more="May be negative, letting a balance run below zero before work pauses. An organization's own pause point, "
        "set on its Jaah AI page under Orgs, overrides it.",
        docs="administration/plan-and-billing/jaah-ai",
        kind="float", env=None, default=0.0, minimum=-1_000_000, maximum=1_000_000, unit="USD",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_default_markup_pct",
        tier="operator",
        label="Default markup",
        help="Added to Bedrock's price on every Jaah AI call.",
        more="0 passes the cost through unchanged. An organization's own markup, set on its Jaah AI page under Orgs, "
        "overrides it.",
        kind="float", env=None, default=0.0, minimum=0, maximum=1000, unit="%",
        feature="jaah_ai_credit",
    ),
    Knob(
        key="jaah_ai_reconcile_tolerance_pct",
        tier="operator",
        label="Usage tolerance",
        help="Metered cost differing from AWS's by more gets flagged.",
        more="Compared per organization per day: Jaah's metered token cost against what AWS billed for it.",
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
        help="Off: organizations can't rent a dedicated host monthly.",
        more="Hides the offer and refuses new orders.",
        docs="administration/plan-and-billing/hosting",
        kind="bool", env=None, default=True,
        feature="hostings",
    ),
    Knob(
        key="rent_shared_enabled",
        tier="org",
        label="Shared node hosting",
        help="Off: organizations can't rent a node on a shared host.",
        more="Hides the offer and refuses new orders.",
        docs="administration/plan-and-billing/hosting",
        kind="bool", env=None, default=True,
        feature="hostings",
    ),
    # Operator-tier: how long the deployment keeps an unpaid or stopped hosting's machine (Architecture §3.4).
    Knob(
        key="rental_grace_days",
        tier="operator",
        label="Unpaid grace",
        help="Hostings with a failed payment stop after this many days.",
        more="Counted from when the payment failed. A node a plan includes also stops this long after the plan stops "
        "including it.",
        docs="administration/plan-and-billing/hosting",
        kind="int", env=None, default=7, minimum=0, maximum=60, unit="days",
        feature="hostings",
    ),
    Knob(
        key="rental_retention_days",
        tier="operator",
        label="Stopped retention",
        help="Stopped hostings' machines and data are deleted after this.",
        more="Deletion can't be undone. Resuming a hosting before then keeps its data.",
        docs="administration/plan-and-billing/hosting",
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
        help="Jaah-hosted machines launch through this cloud connection.",
        more="Its credential must be the operator's own.",
        # A handle (see `cloud_node_tailscale_credential_id`); `env=None`, 0..36 a shape check.
        kind="str", env=None, default="", minimum=0, maximum=36,
        cloud_connection=True,
        feature="hostings",
    ),
    _Section("Billing", "Easy Mode"),
    # ── "Billing" > "Easy Mode": what Easy Mode's app hosting provisions with (`app/hosted_apps/config.py`). Infra
    # facts (bucket, distribution, subnets, ...) are not knobs: they are read at run time from the apps account's
    # `/jaah/ea/<env>/config` after assuming the role below. Create refuses while any of these is blank.
    Knob(
        key="easy_mode_aws_provisioner_role_arn",
        tier="operator",
        label="Provisioner role",
        help="Easy Mode apps are provisioned through this apps-account role.",
        more="ea-dev-provisioner on dev, ea-main-provisioner on live; it also decides whether this console makes "
        "dev or live apps. Creating an app is refused while any Easy Mode setting is blank.",
        kind="str", env=None, default="", minimum=0, maximum=2048,
        feature="easy_mode_hosting",
    ),
    Knob(
        key="easy_mode_github_credential_id",
        tier="operator",
        label="GitHub admin credential",
        help="Creates app repos from templates and sets their deploy settings.",
        more="Must be an operator GitHub credential. It never leaves the control plane.",
        # A handle (see `cloud_node_tailscale_credential_id`); `env=None`, 0..36 a shape check.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("github",),
        feature="easy_mode_hosting",
    ),
    Knob(
        key="easy_mode_github_org",
        tier="operator",
        label="GitHub organization",
        help="New Easy Mode app repos are created in this organization.",
        kind="str", env=None, default="jaah-apps", minimum=0, maximum=39,
        feature="easy_mode_hosting",
    ),
    Knob(
        key="easy_mode_templates",
        tier="operator",
        label="Template repos",
        help="New apps' repos are copied from these template repos.",
        more=('JSON naming an app template\'s repo, e.g. {"web": "owner/repo"}; a template it leaves out uses its '
              'built-in repo.'),
        kind="str", env=None,
        default='{"node": "jaah-apps/easy-template-node", "python": "jaah-apps/easy-template-python"}',
        minimum=0, maximum=512,
        feature="easy_mode_hosting",
    ),
)
