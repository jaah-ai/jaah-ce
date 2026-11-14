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
        help="Accounts this far into 5-hour quota get no new runs.",
        more="Measured over the rolling five hours. The account takes runs again once its usage falls back "
             "below this percentage.",
        docs="using-jaah/model-accounts/how-model-accounts-work",
        kind="int", env=None, default=jaah_scheduler.DEFAULT_HIGH_PCT, minimum=1, maximum=100,
        unit="%",
    ),
    Knob(
        key="quota_weekly_high_pct",
        tier="org",
        label="7-day gate",
        help="Accounts this far into weekly quota get no new runs.",
        more="Measured over the rolling seven days of its plan. The account takes runs again once its usage "
             "falls back below this percentage.",
        docs="using-jaah/model-accounts/how-model-accounts-work",
        kind="int", env=None, default=jaah_scheduler.DEFAULT_WEEKLY_HIGH_PCT, minimum=1,
        maximum=100, unit="%",
    ),
    Knob(
        key="max_runners_per_account",
        # Org-tier and tenant-editable: an account belongs to one org, so its Org Admin sets the default for
        # the org's own subscription accounts (read per account, `routes/fleet.py::_max_runners_per_account`).
        # An account's own number overrides it; API-key accounts have no default cap (`jaah_scheduler`).
        tier="org",
        label="Concurrent runners",
        help="Each subscription account runs at most this many runners.",
        more="Counts runners still starting up as well as running ones. An account's own Max runners overrides"
             " this. API-key accounts have no limit unless one is set on the account.",
        docs="using-jaah/model-accounts/how-model-accounts-work",
        kind="int", env=None, default=jaah_scheduler.DEFAULT_MAX_RUNNERS_PER_ACCOUNT,
        minimum=1, maximum=64,
        tenant_editable=True,
    ),
    _Section("Organizations", "Sign-up"),
    # ── "Organizations": self-service sign-up ──
    # Every knob here is operator-tier and must stay so: a tenant opening its own sign-up door is the escape the
    # tier exists to stop. Reader: the sign-up door (`signup_open`).
    Knob(
        key="signup_open",
        tier="operator",
        label="Open sign-up",
        help="Off: new visitors are sent to the waitlist instead.",
        more="On, anyone may apply for an organization; each application waits for your approval unless an "
             "auto-approve setting below admits it.",
        kind="bool", env=None, default=True,
        feature="signup",
    ),
    # The sign-up flow's links. Read per request by `GET /api/public/settings` and per send by the
    # onboarding emails, so an edit applies at once. https only: they reach signed-out visitors and inboxes.
    Knob(
        key="signup_tutorials_url",
        tier="operator",
        label="Tutorials URL",
        help="Tutorials links on the thank-you page and emails open this.",
        kind="str", env=None, default="https://jaah.ai/tutorials", minimum=1, maximum=500,
        https_url=True,
    ),
    Knob(
        key="signup_home_url",
        tier="operator",
        label="Home URL",
        help="The Jaah Home button shown after applying opens this.",
        more="Shown on the thank-you page and on the under-review screen.",
        kind="str", env=None, default="https://jaah.ai", minimum=1, maximum=500,
        https_url=True,
    ),
    Knob(
        key="signup_docs_url",
        tier="operator",
        label="Documentation URL",
        help="The Documentation button shown after applying opens this.",
        more="Shown on the thank-you page and on the under-review screen.",
        kind="str", env=None, default="https://docs.jaah.ai", minimum=1, maximum=500,
        https_url=True,
    ),
    Knob(
        key="signup_terms_url",
        tier="operator",
        label="Terms of Service URL",
        help="Applicants accept the Terms of Service at this link.",
        more="Empty links to this console's own /legal/terms page once the legal pages are published (Settings"
             " > Legal), and to jaah.ai/terms until then.",
        kind="str", env=None, default="", minimum=0, maximum=500,
        https_url=True, blank_ok=True,
    ),
    Knob(
        key="signup_privacy_url",
        tier="operator",
        label="Privacy Policy URL",
        help="Applicants accept the Privacy Policy at this link.",
        more="Empty links to this console's own /legal/privacy page once the legal pages are published "
             "(Settings > Legal), and to jaah.ai/privacy until then.",
        kind="str", env=None, default="", minimum=0, maximum=500,
        https_url=True, blank_ok=True,
    ),
    # Stored with every acceptance (`legal_acceptances.legal_version`), so it proves which text was agreed to.
    Knob(
        key="legal_version",
        tier="operator",
        label="Legal version",
        help="Stored with each acceptance; shown as legal pages' Last updated.",
        more="Change when the Terms or Privacy text changes. Each recorded acceptance keeps the version in "
             "force when it was given, proving which text was agreed to.",
        docs="console-reference/help-and-profile/legal-pages",
        kind="str", env=None, default="2026-10-05", minimum=1, maximum=40,
    ),
    Knob(
        key="signup_alert_recipients",
        tier="operator",
        label="New application alerts",
        help="These addresses get an email about each new application.",
        more="Separate addresses with commas. Empty sends the alert to every enabled superadmin.",
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
        help="Matching email domains join that organization automatically.",
        more="Someone whose email is on a domain an organization registered joins it without applying. Off, "
             "they apply for their own organization instead; registered domains are kept, so turning it back "
             "on restores the join as it was.",
        kind="bool", env=None, default=False,
        feature="signup",
    ),
    # A listed domain admits its verified applicants at sign-up (`app/org_approval.py::approve_at_signup`).
    Knob(
        key="signup_auto_approve_domains",
        tier="operator",
        label="Auto-approve domains",
        help="Verified applicants from these domains are approved at once.",
        more="Separate domains with commas. The organization starts on the plan it chose. Matters only when "
             "Auto-approve all is off or past its daily cap; each domain is also bounded by its own daily cap.",
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
        help="Past this many in 24 hours, a domain's applications wait.",
        more="A rolling 24-hour count per listed domain. Past it, the domain's applications wait for your "
             "review as if it were not listed.",
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
        help="Every new organization is approved at once on its plan.",
        more="Covers both sign-up and Create organization from the account menu. Off: only the auto-approve "
             "domains above skip your review. Bounded by Auto-approvals per day.",
        kind="bool", env=None, default=True,
        feature="signup",
    ),
    # A rolling 24h bound on `signup_auto_approve_all` across every domain (`app/org_approval.py::_over_all_cap`):
    # past it, an application waits for an operator unless its domain is listed and under its own cap.
    Knob(
        key="signup_auto_approve_all_daily_cap",
        tier="operator",
        label="Auto-approvals per day",
        help="Past this many approvals in 24 hours, new organizations wait.",
        more="A rolling 24-hour count across all domains. Past it, an application waits for your review unless"
             " its domain is listed and under its own cap.",
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
        help="No cron fires more often than this, system crons included.",
        more="Above 10 seconds it slows dispatch, since the fastest system cron runs every 10 seconds. No "
             "organization may set a minimum below it. Applies from the next save or firing.",
        docs="administration/security-and-audit/settings",
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
        help="Failures in a row before a sub-hourly schedule alerts.",
        more="Counts failed or skipped firings in a row for a schedule that fires more often than hourly. "
             "Owners and Admins are alerted; for a system schedule, operators are. "
             "Alerted again about daily while the schedule stays stuck.",
        docs="using-jaah/automations/how-automations-work",
        kind="int", env=None, default=10, minimum=1, maximum=1000, unit="runs",
    ),
    Knob(
        key="automation_alert_slow_runs",
        tier="operator",
        label="Hourly or slower",
        help="Failures in a row before an hourly-or-slower schedule alerts.",
        more="Counts failed or skipped firings in a row for a schedule that fires hourly or less often. "
             "A separate, smaller count than the faster one, so a daily schedule alerts after days, not weeks. "
             "Alerted again about daily while it stays stuck, never sooner than this many more runs.",
        docs="using-jaah/automations/how-automations-work",
        kind="int", env=None, default=3, minimum=1, maximum=1000, unit="runs",
    ),
    _Section("Organizations", "Two-factor"),
    # How long "Trust this device" skips the second factor on that browser (`app/mfa.py::trust_device`).
    # Read when a device is trusted, so an edit moves only devices trusted after it.
    Knob(
        key="mfa_remember_days",
        tier="operator",
        label="Trusted devices",
        help="A trusted browser skips the two-factor code this long.",
        more="Applies only to devices trusted after the change; devices already trusted keep their original "
             "expiry.",
        docs="get-started/sign-in",
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
        help="HIPAA organizations are warned about integrations whose vendor is unlisted.",
        more="Vendor ids with a signed BAA, separated by commas. Empty warns about every integration.",
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
        help="Off: only operators see the legal pages, marked Draft.",
        more="On serves every page under /legal to everyone, but only once every legal value below is also "
             "set. Turn on after legal review.",
        docs="console-reference/help-and-profile/legal-pages",
        kind="bool", env=None, default=False,
    ),
    Knob(
        key="legal_entity_name",
        tier="operator",
        label="Legal entity",
        help="Named as the provider in the terms and DPA.",
        kind="str", env=None, default="", minimum=0, maximum=200,
    ),
    Knob(
        key="legal_entity_address",
        tier="operator",
        label="Registered address",
        help="Printed as the provider's registered address on legal pages.",
        more="One line.",
        kind="str", env=None, default="", minimum=0, maximum=300,
    ),
    Knob(
        key="legal_privacy_contact",
        tier="operator",
        label="Privacy contact",
        help="Legal pages send privacy and data-protection requests here.",
        kind="str", env=None, default="", minimum=0, maximum=200,
        email_list=True,
    ),
    Knob(
        key="legal_security_contact",
        tier="operator",
        label="Security contact",
        help="Legal pages and /.well-known/security.txt send vulnerability reports here.",
        kind="str", env=None, default="", minimum=0, maximum=200,
        email_list=True,
    ),
    Knob(
        key="legal_governing_law",
        tier="operator",
        label="Governing law",
        help="Named as the law and courts governing the terms.",
        more="Covers the terms and the DPA, including where disputes are heard. Example: Delaware.",
        kind="str", env=None, default="", minimum=0, maximum=100,
    ),
)
