"""The "Accounts", "Organizations" and "Legal" Settings groups of the Settings catalogue, in render order."""
from __future__ import annotations

# Pure and import-safe (its suite pins that), so the quota defaults are single-homed there.
from app import jaah_scheduler
from app.operator_settings.knobs import Knob, _Section, _sectioned

KNOBS: tuple[Knob, ...] = _sectioned(
    _Section("Accounts", "Quotas"),
    Knob(
        key="quota_high_pct",
        tier="org",
        label="5-hour gate",
        help="Rolling 5h usage at or above which an account sits out of dispatch.",
        kind="int", env=None, default=jaah_scheduler.DEFAULT_HIGH_PCT, minimum=1, maximum=100,
        unit="%",
    ),
    Knob(
        key="quota_weekly_high_pct",
        tier="org",
        label="7-day gate",
        help="Rolling 7d plan usage at or above which an account sits out of dispatch.",
        kind="int", env=None, default=jaah_scheduler.DEFAULT_WEEKLY_HIGH_PCT, minimum=1,
        maximum=100, unit="%",
    ),
    Knob(
        key="max_runners_per_account",
        # Operator, not org: an account is a shared CP resource, and per-org "2" from two tenants would put
        # four runners on one account. The quota ceilings beside it are org-tier (a tenant's own throttle).
        tier="operator",
        label="Concurrent runners",
        help="Runners one account may carry at once, allocating ones included; a full account yields to the "
             "next best.",
        kind="int", env=None, default=jaah_scheduler.DEFAULT_MAX_RUNNERS_PER_ACCOUNT,
        minimum=1, maximum=64,
    ),
    _Section("Organizations", "Sign-up"),
    # ── "Organizations": self-service sign-up ──
    # Every knob here is operator-tier and must stay so: a tenant opening its own sign-up door is the escape the
    # tier exists to stop. Reader: the sign-up door (`signup_open`).
    Knob(
        key="signup_open",
        tier="operator",
        label="Open sign-up",
        help="Anyone may apply for an organization; each waits for your approval unless auto-approved below. Off, "
             "new visitors are sent to "
             "the waitlist.",
        kind="bool", env=None, default=True,
        feature="signup",
    ),
    # The sign-up flow's links. Read per request by `GET /api/public/settings` and per send by the
    # onboarding emails, so an edit applies at once. https only: they reach signed-out visitors and inboxes.
    Knob(
        key="signup_tutorials_url",
        tier="operator",
        label="Tutorials URL",
        help="Where the thank-you page, the under-review screen and every onboarding email send people to watch "
             "tutorials.",
        kind="str", env=None, default="https://jaah.ai/tutorials", minimum=1, maximum=500,
        https_url=True,
    ),
    Knob(
        key="signup_home_url",
        tier="operator",
        label="Home URL",
        help="Where the thank-you page and the under-review screen's Jaah Home button sends people.",
        kind="str", env=None, default="https://jaah.ai", minimum=1, maximum=500,
        https_url=True,
    ),
    Knob(
        key="signup_docs_url",
        tier="operator",
        label="Documentation URL",
        help="Where the thank-you page and the under-review screen's Documentation button sends people.",
        kind="str", env=None, default="https://docs.jaah.ai", minimum=1, maximum=500,
        https_url=True,
    ),
    Knob(
        key="signup_terms_url",
        tier="operator",
        label="Terms of Service URL",
        help="The Terms of Service an applicant accepts when applying. Empty links to this console's own "
             "/legal/terms page once published (Settings > Legal), and to jaah.ai/terms until then.",
        kind="str", env=None, default="", minimum=0, maximum=500,
        https_url=True, blank_ok=True,
    ),
    Knob(
        key="signup_privacy_url",
        tier="operator",
        label="Privacy Policy URL",
        help="The Privacy Policy an applicant accepts when applying. Empty links to this console's own "
             "/legal/privacy page once published (Settings > Legal), and to jaah.ai/privacy until then.",
        kind="str", env=None, default="", minimum=0, maximum=500,
        https_url=True, blank_ok=True,
    ),
    # Stored with every acceptance (`legal_acceptances.legal_version`), so it proves which text was agreed to.
    Knob(
        key="legal_version",
        tier="operator",
        label="Legal version",
        help="Recorded with each acceptance of the Terms and Privacy Policy. Change when the Terms or Privacy "
             "text changes. Shown as Last updated on the /legal pages.",
        kind="str", env=None, default="2026-10-05", minimum=1, maximum=40,
    ),
    Knob(
        key="signup_alert_recipients",
        tier="operator",
        label="New application alerts",
        help="Email addresses told about each new application, separated by commas. Empty sends to every "
             "enabled superadmin.",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        email_list=True,
        feature="signup",
    ),
    # Off pauses the domain join at every door (`app/signup.py`): registered `org_domains` rows are kept, so
    # turning it back on restores the join exactly as it was.
    Knob(
        key="signup_domain_join",
        tier="operator",
        label="Join by email domain",
        help="Someone whose email is on a domain an organization registered joins it automatically. Off, they "
             "apply for their own organization instead; registered domains are kept.",
        kind="bool", env=None, default=False,
        feature="signup",
    ),
    # A listed domain admits its verified applicants at sign-up (`app/org_approval.py::approve_at_signup`).
    Knob(
        key="signup_auto_approve_domains",
        tier="operator",
        label="Auto-approve domains",
        help="Applications from a verified email on these domains are approved at once, on the plan they "
             "chose. Separate with commas. Matters when Auto-approve all is off or past its cap.",
        kind="str", env=None, default="", minimum=0, maximum=2000,
        domain_list=True,
        feature="signup",
    ),
    # A rolling 24h bound on `signup_auto_approve_domains` per listed domain (`app/org_approval.py::
    # _over_daily_cap`): past it, an application waits for an operator as if its domain were not listed.
    Knob(
        key="signup_auto_approve_daily_cap",
        tier="operator",
        label="Auto-approvals per domain per day",
        help="Past this many in 24 hours, a domain's applications wait for your review.",
        kind="int", env=None, default=3, minimum=1, maximum=1000, unit="orgs",
        feature="signup",
    ),
    # On (the default), every submitted application — the sign-up's and the account menu's Create organization — is
    # approved at once; off leaves `signup_auto_approve_domains` the only automatic door (`app/org_approval.py::
    # approve_at_signup`), bounded by `signup_auto_approve_all_daily_cap`.
    Knob(
        key="signup_auto_approve_all",
        tier="operator",
        label="Auto-approve all",
        help="Approve every new organization at once, on the plan it chose. Off: only the domains above skip "
             "your review.",
        kind="bool", env=None, default=True,
        feature="signup",
    ),
    # A rolling 24h bound on `signup_auto_approve_all` across every domain (`app/org_approval.py::_over_all_cap`):
    # past it, an application waits for an operator unless its domain is listed and under its own cap.
    Knob(
        key="signup_auto_approve_all_daily_cap",
        tier="operator",
        label="Auto-approvals per day",
        help="Past this many automatic approvals in 24 hours, new organizations wait for your review.",
        kind="int", env=None, default=50, minimum=1, maximum=10000, unit="orgs",
        feature="signup",
    ),
    _Section("Organizations", "Cron minimum period"),
    # The minimum cron period's operator floor; the tenant default is a plan term (`billing_plans`, `_default`'s
    # by default), the per-org override and the org administrator's own value are `orgs` columns. Key mirrored as
    # a literal in `app/cron_min_period.py` (it owns the hierarchy); a test pins the pairing. Read per save and per
    # firing, so an edit takes effect at once.
    Knob(
        key="cron_min_period_operator_seconds",
        tier="operator",
        label="Operator minimum",
        help="The fastest any cron may fire, the control plane's own included: above 10 seconds it slows "
             "dispatch. No organization may be set below it.",
        # 10s: the fastest system cron (lease-drain) runs at it. Ceiling a day, so a daily cron stays legal.
        kind="int", env=None, default=10, minimum=1, maximum=86400, unit="seconds",
    ),
    _Section("Organizations", "Automation alerts"),
    # When an org's Owners and Admins are told a schedule is failing or stalling (`app/automation_alerts.py`, keys
    # mirrored there): how many wedged firings in a row (`crons.consecutive_skips`). Two counts, so a five-minute
    # schedule alerts after about an hour and a daily one after three days, not thirty. Read per firing.
    Knob(
        key="automation_alert_fast_runs",
        tier="operator",
        label="Faster than hourly",
        help="Owners and Admins (operators, for a system schedule) are alerted once a schedule firing more often "
             "than hourly fails or is skipped this many times in a row.",
        kind="int", env=None, default=10, minimum=1, maximum=1000, unit="runs",
    ),
    Knob(
        key="automation_alert_slow_runs",
        tier="operator",
        label="Hourly or slower",
        help="The same, for a schedule firing hourly or less often.",
        kind="int", env=None, default=3, minimum=1, maximum=1000, unit="runs",
    ),
    _Section("Organizations", "Two-factor"),
    # How long "Trust this device" skips the second factor on that browser (`app/mfa.py::trust_device`).
    # Read when a device is trusted, so an edit moves only devices trusted after it.
    Knob(
        key="mfa_remember_days",
        tier="operator",
        label="Trusted devices",
        help="How long a browser a user trusted at sign-in skips the two-factor code. Applies to devices "
             "trusted after the change.",
        kind="int", env=None, default=30, minimum=1, maximum=365, unit="days",
    ),
    _Section("Organizations", "HIPAA"),
    # Which vendors have signed a BAA with Jaah (`app/hipaa.py::warned_surfaces`): a HIPAA org is warned about
    # every integration whose vendor is not listed. Empty (the default) warns about all of them. The vendor ids are
    # `hipaa.SURFACES`' values.
    Knob(
        key="hipaa_vendor_baas",
        tier="operator",
        label="Vendors with a BAA",
        help="Vendor ids with a signed BAA, separated by commas. HIPAA organizations are warned about each "
             "integration whose vendor is not listed; empty warns about all.",
        kind="str", env=None, default="", minimum=0, maximum=500,
    ),
    _Section("Legal", "Public legal pages"),
    # THE PUBLIC LEGAL PAGES (`app/legal_pages.py`): the text is single-homed in the legal page sources, and these
    # are the deployment's own values it is filled with, so no legal entity or contact is ever committed.
    # Served at /legal/<page> only once published AND every value below is set.
    Knob(
        key="legal_pages_published",
        tier="operator",
        label="Publish legal pages",
        help="Serve every page under /legal to everyone. Off, only operators see them, marked Draft. Turn on "
             "after legal review.",
        kind="bool", env=None, default=False,
    ),
    Knob(
        key="legal_entity_name",
        tier="operator",
        label="Legal entity",
        help="The company that offers the service, as it appears in the terms and DPA.",
        kind="str", env=None, default="", minimum=0, maximum=200,
    ),
    Knob(
        key="legal_entity_address",
        tier="operator",
        label="Registered address",
        help="The legal entity's registered address, on one line.",
        kind="str", env=None, default="", minimum=0, maximum=300,
    ),
    Knob(
        key="legal_privacy_contact",
        tier="operator",
        label="Privacy contact",
        help="Where people send privacy and data-protection requests.",
        kind="str", env=None, default="", minimum=0, maximum=200,
        email_list=True,
    ),
    Knob(
        key="legal_security_contact",
        tier="operator",
        label="Security contact",
        help="Where people report a vulnerability.",
        kind="str", env=None, default="", minimum=0, maximum=200,
        email_list=True,
    ),
    Knob(
        key="legal_governing_law",
        tier="operator",
        label="Governing law",
        help="The jurisdiction whose law governs the terms and DPA, and whose courts hear disputes. Example: "
             "Delaware.",
        kind="str", env=None, default="", minimum=0, maximum=100,
    ),
)
