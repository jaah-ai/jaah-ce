"""The "Dashboard", "Retention", "Compliance" and "General" Settings groups of the Settings catalogue, in render order."""
from __future__ import annotations

from app.operator_settings.knobs import Knob, _Section, _sectioned

KNOBS: tuple[Knob, ...] = _sectioned(
    _Section("Dashboard", "AI review"),
    # ── "Dashboard" > "AI review": the operator dashboard's weekly AI quality review ──
    # Read on every `outcome-review` firing (`app/operator_dashboard/review.py`); the sample size is also shown on the
    # Quality panel, and a `dashboard_*` value joins the dashboard's cache key (`frame.knob_values`).
    Knob(
        key="dashboard_ai_model",
        tier="operator",
        label="Model",
        help=(
            "The model that scores delivered tasks and writes the weekly digest, on the support agent's provider "
            "and key."
        ),
        # Free text, as `summary_model`: an API model id. Another provider's tier model maps to the support
        # provider's model of the same tier (`llm_client.provider_model`).
        kind="str", env=None, default="claude-opus-5-5", minimum=1, maximum=200,
        feature="operator_dashboard",
    ),
    Knob(
        key="dashboard_review_sample",
        tier="operator",
        label="Reviews per week",
        help="How many tasks delivered in the last 7 days each weekly run scores.",
        kind="int", env=None, default=15, minimum=1, maximum=200,
        feature="operator_dashboard",
    ),
    _Section("Dashboard", "Tenant health"),
    # ── "Dashboard" > "Tenant health": the dashboard's tenant-health rules ──
    # Read per request by `app/operator_dashboard/derived.py::tenants`. Their values join the dashboard's cache key
    # (`frame.knob_values`, every `dashboard_*` key), so an edit shows on the next load, not after the TTL.
    Knob(
        key="dashboard_churn_drop_pct",
        tier="operator",
        label="Churn drop",
        help="A tenant is a churn risk when its tasks created fall this much versus the period before.",
        kind="int", env=None, default=40, minimum=1, maximum=100, unit="%",
        feature="operator_dashboard",
    ),
    Knob(
        key="dashboard_upsell_limit_days",
        tier="operator",
        label="Upsell days",
        help="A tenant is an upsell lead after this many days in the last 30 at 95% quota or at its wallet floor.",
        kind="int", env=None, default=3, minimum=1, maximum=30, unit="days",
        feature="operator_dashboard",
    ),
    Knob(
        key="dashboard_failure_amber_pct",
        tier="operator",
        label="Failure rate amber",
        help="Tenant health turns amber at this failure rate.",
        kind="int", env=None, default=15, minimum=0, maximum=100, unit="%",
        feature="operator_dashboard",
    ),
    Knob(
        key="dashboard_failure_red_pct",
        tier="operator",
        label="Failure rate red",
        help="Tenant health turns red at this failure rate.",
        kind="int", env=None, default=30, minimum=0, maximum=100, unit="%",
        feature="operator_dashboard",
    ),
    Knob(
        key="dashboard_quota_amber_pct",
        tier="operator",
        label="Quota amber",
        help="Tenant health turns amber when an account's 7-day quota peaks at this.",
        kind="int", env=None, default=80, minimum=0, maximum=100, unit="%",
        feature="operator_dashboard",
    ),
    Knob(
        key="dashboard_quota_red_pct",
        tier="operator",
        label="Quota red",
        help="Tenant health turns red when an account's 7-day quota peaks at this.",
        kind="int", env=None, default=95, minimum=0, maximum=100, unit="%",
        feature="operator_dashboard",
    ),
    _Section("Retention", "Crons"),
    # ── "Retention" > "Crons" and "Activity": how much history the nightly retention sweep keeps ──
    # `app/cron_retention.py` reads these on every `retention_sweep` firing (three knobs, one sweep). DBOS-side bounds are the cron row's own `activity_params`. Keys are mirrored as literals
    # (importing that module would pull the ORM in); a test asserts the pairing.
    Knob(
        key="cron_runs_retention_days",
        tier="operator",
        label="History",
        help="How many days of cron runs the nightly sweep keeps.",
        # Floor one day, so this morning's failure is still in the ledger. Ceiling: the operational policy
        # bound, mirrored from `app/cron_retention.py::POLICY_MAX_RETENTION_DAYS`.
        kind="int", env=None, default=90, minimum=1, maximum=365, unit="days",
    ),
    Knob(
        key="cron_runs_per_cron_cap",
        tier="operator",
        label="Rows each",
        help="The most run rows any one cron keeps, whatever their age; what bounds a ten-second cadence.",
        # Per-cron: 100 firings is a readable window; the ceiling is a typo guard.
        kind="int", env=None, default=10000, minimum=100, maximum=1000000, unit="runs",
    ),
    _Section("Retention", "Activity"),
    Knob(
        key="activity_events_retention_days",
        tier="operator",
        label="History",
        help="How many days of the activity log the nightly sweep keeps. Older rows, the exclusion log "
             "included, are deleted for good; task pipeline history stays.",
        # In "Crons" though it bounds `activity_events` and `tenancy_exclusions`: both are bounds of the one
        # `retention_sweep`, so one panel holds the whole policy. The help names the exclusion log because
        # shortening this deletes the tenancy seam's standing evidence, unrecoverably. Why the two share a
        # window: `app/cron_retention.py`, above `_DELETE_TENANCY_EXCLUSIONS_BY_AGE_SQL`. Floor a week (the
        # trail must answer "who changed this last week"); the default and the ceiling (the security-event
        # policy bound) are mirrored from that module.
        kind="int", env=None, default=365, minimum=7, maximum=400, unit="days",
    ),
    _Section("Retention", "Organizations"),
    # ── "Retention" > "Organizations": the range an org admin may pick for their own org's windows ──
    # `app/org_retention.py` reads both per request and per sweep; keys mirrored as literals there. An org can
    # only shorten: each class's ceiling is also capped by the deployment's own window.
    Knob(
        key="org_retention_min_days",
        tier="operator",
        label="Shortest",
        help="The fewest days an org may keep its transcripts, activity and task artifacts.",
        # Floor one day: a zero window would delete what a runner is still writing.
        kind="int", env=None, default=7, minimum=1, maximum=365, unit="days",
    ),
    Knob(
        key="org_retention_max_days",
        tier="operator",
        label="Longest",
        help="The most days an org may keep them. Never longer than the platform keeps them.",
        kind="int", env=None, default=365, minimum=1, maximum=400, unit="days",
    ),
    _Section("Retention", "Sessions"),
    # ── "Retention" > "Sessions": the one bound on the operator's stale-session delete ──
    # Resolved per call by `app/sessions_cleanup.py::stale_after` (count and delete both
    # re-read). Its own section: it bounds a manual action, not the nightly sweep. Key and default are
    # mirrored as literals (that module pulls the ORM in); `tests/test_sessions_cleanup.py` pins them.
    Knob(
        key="stale_session_after_hours",
        tier="operator",
        label="Stale after",
        help="How long a session with no transcript may sit before Audit offers to delete it.",
        # Floor one hour, a safety bound: the session retry ladder works in minutes. A week is the ceiling.
        kind="int", env=None, default=6, minimum=1, maximum=168, unit="hours",
    ),
    _Section("Compliance", "Evidence"),
    # ── "Compliance" > "Evidence": the SOC 2 evidence collectors (`app/compliance_evidence/`) ──
    Knob(
        key="compliance_github_credential_id",
        tier="operator",
        label="GitHub evidence token",
        help="A token used for nothing else, with repository administration and Dependabot alerts read: the "
             "daily evidence checks read the repository's rulesets, settings and alerts with it. Empty, both fail "
             "and raise an alarm.",
        # On `git_app_credential_id`'s terms (0/36, no env). Read on the operator plane only
        # (`app/compliance_evidence/github.py::token`); set on prod by owner action I-15.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("github",),
        feature="compliance",
    ),
    _Section("Compliance", "Alarms"),
    # ── "Compliance" > "Alarms": where every control-plane alarm files (`app/cron_alarms.py::alarm_project_id`) ──
    # An id, not a name: a hard-coded project name outlived a rename and every alarm 404ed silently.
    # The evidence collectors' drift alarms are SOC 2 CC7.2/CC7.3 evidence; set on prod by owner action I-16.
    Knob(
        key="alarm_project_id",
        tier="operator",
        label="Alarm project",
        help="The project cron, node and evidence alarms are filed into, as hidden tasks its org's members can "
             "read. Unset, no alarm is filed.",
        # Any org's project, not org zero alone: the operator's own projects live in its company org on dev and
        # prod. The help names the cost — a fleet alarm body (other tenants' nodes) is readable in that org.
        # 0..12: a `projects.id` as text, blank = unset. No env: the id differs per control plane.
        kind="str", env=None, default="", minimum=0, maximum=12,
        project=True,
    ),
    _Section("General"),
    # Read once per identity payload (`routes/auth.py::_admin_out_bound`); a user's own stored `theme` wins, and
    # a user who never picked one follows this (`app/preferences.py::UserPreferences.theme`).
    Knob(
        key="default_theme",
        tier="operator",
        label="Default theme",
        help="The console theme for every user who has not picked their own.",
        # Dark by operator decision.
        kind="str", env=None, default="dark",
        choices=("light", "dark"), choice_labels=("Light", "Dark"),
    ),
    Knob(
        key="cron_timezone",
        tier="operator",
        label="Timezone",
        help="The one zone the whole control plane keeps local time in: cron times and the task list's Today "
             "grouping. Interval schedules stay on UTC.",
        # A closed set: the string goes to `ZoneInfo` and DBOS's schedule table, where a bad name is
        # unroutable. Adding a zone is a one-line catalogue edit. Default `America/New_York`, the operator's
        # clock. The length bound backs the set against a value written straight into `system_state`, which
        # `cron_timezone()` would log on every evaluation.
        kind="str", env="CRON_TIMEZONE", default="America/New_York", minimum=1, maximum=64,
        choices=(
            "UTC",
            "America/New_York",
            "America/Chicago",
            "America/Denver",
            "America/Los_Angeles",
            "Europe/London",
            "Europe/Berlin",
            "Asia/Jerusalem",
            "Asia/Kolkata",
            "Asia/Singapore",
            "Australia/Sydney",
        ),
        # "General": deployment-wide, acted on by the control plane with no form in front of it.
    ),
    # The bound on an unanswered question: behaviour knobs (no form reads them), re-read every 15s pass by
    # `task_pipeline.expire_timed_out_awaiting_response_tasks`, so an edit moves a parked row's clock. Two
    # stages: first the runner is stopped (the question stays answerable; a clean worktree is freed), then
    # a tree holding work in progress is wiped and the task failed.
    Knob(
        key="unanswered_question_timeout_hours",
        tier="operator",
        label="Question timeout",
        help="How long a runner waits on an unanswered question before it is stopped; answering starts a fresh "
             "runner.",
        # Two hours (operator's number). The one-hour floor is a safety bound (below it runners mid-answer
        # get stopped); the week ceiling doubles as "off".
        kind="int", env=None, default=2, minimum=1, maximum=168, unit="hours",
    ),
    Knob(
        key="unanswered_question_release_days",
        tier="operator",
        label="Question release",
        help="How long a task waits on an unanswered question before it fails and its worktree is wiped, "
             "unsaved work included.",
        # Fourteen days (operator's number). Set below the stop above and the row fails straight from a
        # live park, a coherent choice needing no guard.
        kind="int", env=None, default=14, minimum=1, maximum=365, unit="days",
    ),
    # Re-read once a minute by `app/cpu_profile.py`'s loop, on every api task; that module is the home of
    # what it logs and how to query it.
    Knob(
        key="cpu_profiling",
        tier="operator",
        label="CPU profiling",
        help="Samples the control plane's CPU one minute in every five and logs its hottest stacks to "
             "CloudWatch. Costs about 1% CPU and up to ~0.1 GB of logs a day per task.",
        kind="bool", env=None, default=False,
    ),
)
