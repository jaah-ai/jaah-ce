"""The trigger vocabulary — stdlib-only, so `app/models/` renders its CHECKs
from these sets and every other module names them instead of re-listing them.

A trigger (`models.TaskTrigger`) turns an inbound event into one filed Task in its own project. Its source
kinds, preflight kinds, delivery states, and the human words each delivery outcome is shown with live here.
The behaviour lives in `trigger_intake` (the processor), `trigger_health` (counters and the derived badge),
`trigger_template` (the renderer) and `routes/triggers.py` (the console API).
"""
from __future__ import annotations

TASK_TRIGGERS_TABLE = "task_triggers"
TRIGGER_DELIVERIES_TABLE = "trigger_deliveries"
TRIGGER_DEDUP_TABLE = "trigger_dedup"
TRIGGER_NOTICES_TABLE = "trigger_notices"

SOURCE_WEBHOOK = "webhook"
SOURCE_GITHUB = "github"
SOURCE_GMAIL = "gmail"
SOURCE_SLACK = "slack"
# An in-platform event: a person registering — a self-service sign-up on Jaah (the operator's to
# watch, `USER_SCOPE_PLATFORM`) or a new member of the trigger's own org (`USER_SCOPE_ORG`). No sender, no
# ingress: the control plane raises it itself (`app/trigger_user_registered.py`).
SOURCE_USER_REGISTERED = "user_registered"
# Production QA's source kinds (`app/production_qa/`): four monitor adapters a sender reaches over HTTP, then three
# the control plane raises itself from an incident's transitions (`app/production_qa/events.py`, the `user_registered`
# precedent: no sender, no ingress). The CHECK admits them all (`SCHEMA_SOURCE_KINDS`); each joins `SOURCE_KINDS`
# when the code that serves it lands.
SOURCE_SENTRY = "sentry"
SOURCE_DATADOG = "datadog"
SOURCE_PAGERDUTY = "pagerduty"
SOURCE_CLOUDWATCH = "cloudwatch"
SOURCE_INCIDENT = "incident"
SOURCE_DEPLOY_FAILED = "deploy_failed"
SOURCE_HEALTH_BREACH = "health_breach"
# GitLab's deploy webhooks (`app/production_qa/gitlab_deploys.py`): a sender reaches it over HTTP, and it only records.
SOURCE_GITLAB = "gitlab"
# A forge's deploy facts for Production QA, recorded only (`app/production_qa/azure_devops.py`): Azure DevOps service
# hooks. GitHub's ride the `github` kind (`record_only: true`).
SOURCE_AZURE_DEVOPS = "azure_devops"
# A record-only Bitbucket Cloud repository webhook (`app/production_qa/bitbucket.py`): its `repo:push` and build-status
# deliveries become Production QA's deploy facts.
SOURCE_BITBUCKET = "bitbucket"
_FIRST_SOURCE_KINDS = (SOURCE_WEBHOOK, SOURCE_GITHUB, SOURCE_GMAIL, SOURCE_SLACK, SOURCE_USER_REGISTERED)
# The monitor adapters: every one only records (`app/production_qa/recorder.py::records_only`).
MONITOR_SOURCE_KINDS = (SOURCE_SENTRY, SOURCE_DATADOG, SOURCE_PAGERDUTY, SOURCE_CLOUDWATCH)
# The CP-raised incident family: an automation on one of these files a task for an incident of its own project.
INCIDENT_SOURCE_KINDS = (SOURCE_INCIDENT, SOURCE_DEPLOY_FAILED, SOURCE_HEALTH_BREACH)
# The forges' deploy recorders with a kind of their own (GitHub's ride the `github` kind, `record_only: true`): every
# one only records.
DEPLOY_SOURCE_KINDS = (SOURCE_GITLAB, SOURCE_AZURE_DEVOPS, SOURCE_BITBUCKET)
# The issue trackers (`app/trackers/`): a tracker connection's own trigger, one per connection. Its events reach the
# tracker ingress (`routes/trackers.py`), never `ingest_http`, so none is an HTTP kind here; a connection writes the
# trigger and its default automation (`trackers.connections.connect`).
SOURCE_LINEAR = "linear"
SOURCE_JIRA = "jira"
SOURCE_BACKLOG = "backlog"
TRACKER_SOURCE_KINDS = (SOURCE_LINEAR, SOURCE_JIRA, SOURCE_BACKLOG)
SOURCE_KINDS = (_FIRST_SOURCE_KINDS + MONITOR_SOURCE_KINDS + INCIDENT_SOURCE_KINDS + DEPLOY_SOURCE_KINDS
                + TRACKER_SOURCE_KINDS)
PRODUCTION_QA_SOURCE_KINDS = MONITOR_SOURCE_KINDS + INCIDENT_SOURCE_KINDS + DEPLOY_SOURCE_KINDS
# What `ck_task_triggers_source_kind` admits: the served kinds plus the ones reserved above.
SCHEMA_SOURCE_KINDS = _FIRST_SOURCE_KINDS + PRODUCTION_QA_SOURCE_KINDS + TRACKER_SOURCE_KINDS
# The kinds a sender reaches over HTTP: each has an ingress token (its URL) and a signing secret.
HTTP_SOURCE_KINDS = (SOURCE_WEBHOOK, SOURCE_GITHUB) + DEPLOY_SOURCE_KINDS + MONITOR_SOURCE_KINDS
# The served adapters: each proves its sender itself (`app/production_qa/adapters/__init__.py::adapter_for` → its
# `verify`) — with the trigger's sealed secret, except CloudWatch, which AWS SNS signs with its own certificate (no
# secret at all). GitLab's token and Bitbucket's signature are checked by intake itself (`AUTH_GITLAB`,
# `AUTH_BITBUCKET`).
ADAPTER_SOURCE_KINDS = MONITOR_SOURCE_KINDS + (SOURCE_AZURE_DEVOPS,)
# The adapters whose vendor mints the signing secret (Sentry's integration Client Secret, a PagerDuty subscription's
# secret): the owner pastes it over the one Jaah generated (`routes/automations.py`, `PUT …/secret`), and Jaah never
# rotates it. Datadog has no signing scheme, so it sends the Jaah-generated secret itself (`DATADOG_TOKEN_HEADER`).
VENDOR_SECRET_KINDS = (SOURCE_SENTRY, SOURCE_PAGERDUTY)

# `source_config.scope` of a `user_registered` trigger: whose registration it watches.
USER_SCOPE_PLATFORM = "platform"
USER_SCOPE_ORG = "org"
USER_SCOPES = (USER_SCOPE_ORG, USER_SCOPE_PLATFORM)
# `source_config.moment` (platform scope only): when a sign-up counts — the application submitted, or the org
# approved (by an operator, or automatically at sign-up). An org member's one moment is the join.
USER_MOMENT_APPLIED = "applied"
USER_MOMENT_APPROVED = "approved"
USER_MOMENTS = (USER_MOMENT_APPLIED, USER_MOMENT_APPROVED)

PREFLIGHT_NONE = "none"
PREFLIGHT_SCRIPT = "script"
PREFLIGHT_MODEL = "model"
PREFLIGHT_KINDS = (PREFLIGHT_NONE, PREFLIGHT_SCRIPT, PREFLIGHT_MODEL)

# `trigger_deliveries.state`. The first three are transient; the rest are the logged outcomes (R14 + A1).
RECEIVED = "received"
PREFLIGHTING = "preflighting"
QUEUED = "queued"
DISPATCHED = "dispatched"
FILTERED = "filtered"
DEDUPED = "deduped"
RATE_CAPPED = "rate_capped"
SKIPPED = "skipped"
FAILED = "failed"
UNMATCHED = "unmatched"
# A delivery the fire engine has claimed and is filing target by target. Admitted by
# the CHECK since revision 0417; written by the fire engine only.
FILING = "filing"
# A delivery waiting out a quiet-hours window until `trigger_deliveries.not_before` (`app/production_qa/`), then
# flipped back to `received`. Never picked by the processor (`trigger_intake._next_row`, `triggers_with_work`) and
# never counted by the hourly cap (`trigger_intake.filings_in_last_hour`).
HELD = "held"
DELIVERY_STATES = (
    RECEIVED, PREFLIGHTING, QUEUED, DISPATCHED, FILTERED, DEDUPED, RATE_CAPPED, SKIPPED, FAILED, UNMATCHED, FILING,
    HELD,
)
TRANSIENT_STATES = (RECEIVED, PREFLIGHTING, FILING)

# The words a state is shown with — never the code (architecture §7's quality bar).
OUTCOME_LABELS = {
    RECEIVED: "Received",
    PREFLIGHTING: "Preflight running",
    QUEUED: "Queued",
    DISPATCHED: "Dispatched",
    FILTERED: "Filtered",
    DEDUPED: "Duplicate",
    RATE_CAPPED: "Rate capped",
    SKIPPED: "Skipped",
    FAILED: "Failed",
    UNMATCHED: "Unmatched",
    FILING: "Filing",
    HELD: "Held",
}
assert tuple(OUTCOME_LABELS) == DELIVERY_STATES, "every delivery state needs exactly one label"

# A tracker stop that stopped its task (`app/trackers/intake.py::_stop`) logs `filtered` with this reason; it reads as a
# stop, not a refusal. Every other `filtered` row keeps "Filtered".
STOPPED_TASK_REASON = "stopped Task #{task_id}"
_STOPPED_TASK_PREFIX = STOPPED_TASK_REASON.split("{", 1)[0]


def outcome_label(state: str, reason: str | None) -> str:
    """The word a delivery's outcome is shown with: `OUTCOME_LABELS`, except a tracker stop that landed ("Stopped")."""
    if state == FILTERED and (reason or "").startswith(_STOPPED_TASK_PREFIX):
        return "Stopped"
    return OUTCOME_LABELS.get(state, state)


# Health (§6), derived on read by `trigger_health.trigger_health`; never a column.
HEALTH_ACTIVE = "active"
HEALTH_DEGRADED = "degraded"
HEALTH_FAILED = "failed"
HEALTH_PAUSED = "paused"

# Limits (PRD A4, A5, A8, A9, R11) — one home each.
MAX_BODY_BYTES = 1024 * 1024
# A record-only `github` trigger's signed delivery (Production QA's Deploys trigger): GitHub caps a webhook payload at
# 25 MB and sends nothing larger, so 25 MiB admits every push GitHub delivers — a promotion push of 500+ commits with
# file lists passes 1 MiB. Chosen per delivery by `trigger_intake.body_limit`, before the body is read.
MAX_RECORD_ONLY_GITHUB_BODY_BYTES = 25 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_PREVIEW_BYTES = 8 * 1024
MAX_PREFLIGHT_OUTPUT_BYTES = 64 * 1024
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 120
MIN_PREFLIGHT_TIMEOUT_SECONDS = 5
# Under the node's 170 s execute cap (`apps/jaahd/commands.py`), so a script preflight never outlives its call.
MAX_PREFLIGHT_TIMEOUT_SECONDS = 165
DEFAULT_CONCURRENCY_CAP = 1
DEFAULT_QUEUE_DEPTH = 10
DEFAULT_RATE_LIMIT_PER_HOUR = 10
DEFAULT_AUTO_PAUSE_FAILURES = 5
# Ceilings on the owner's limits: what one trigger may hold or file is bounded, so a valid sender cannot grow the
# shared database or the fleet's queue without limit.
MAX_CONCURRENCY_CAP = 20
MAX_QUEUE_DEPTH = 100
MAX_RATE_LIMIT_PER_HOUR = 1000
DELIVERY_RETENTION_DAYS = 90
EVENT_RETENTION_DAYS = 30
DEDUP_RETENTION_DAYS = 90
# The longest an item id / natural dedup key may be; a longer one is hashed (`trigger_intake.dedup_key_for`).
MAX_DEDUP_KEY_CHARS = 200

# The header a webhook sender signs with, and the optional one naming its own delivery id (the natural
# dedup key, A13). Lowercase: headers are compared case-insensitively.
SIGNATURE_HEADER = "x-jaah-signature-256"
DELIVERY_ID_HEADER = "x-jaah-delivery-id"
# GitHub's own three (architecture §4 "GitHub (C4)"): its body signature (same `sha256=<hex>` form as ours), the
# event name the filters match, and the delivery id its Redeliver resends (the natural dedup key).
GITHUB_SIGNATURE_HEADER = "x-hub-signature-256"
GITHUB_EVENT_HEADER = "x-github-event"
GITHUB_DELIVERY_HEADER = "x-github-delivery"
# Sentry's (`app/production_qa/adapters/sentry.py`): the body signature (bare hex, no prefix), the resource the body
# describes, and the request id a retry resends (the natural dedup key).
SENTRY_SIGNATURE_HEADER = "sentry-hook-signature"
SENTRY_RESOURCE_HEADER = "sentry-hook-resource"
SENTRY_REQUEST_ID_HEADER = "request-id"
# Datadog's (`adapters/datadog.py`) and Azure DevOps' (`production_qa/azure_devops.py`): no vendor signing, so the
# trigger's secret travels in a custom header (A5).
DATADOG_TOKEN_HEADER = "x-jaah-token"
# PagerDuty's (`adapters/pagerduty.py`): `v1=<hex>` signatures, comma-separated.
PAGERDUTY_SIGNATURE_HEADER = "x-pagerduty-signature"
# GitLab's (`production_qa/gitlab_deploys.py`): the Secret token in plain text (compared in constant time), the event
# name (`Push Hook`, …), and the id its retries resend (the natural dedup key).
GITLAB_TOKEN_HEADER = "x-gitlab-token"
GITLAB_EVENT_HEADER = "x-gitlab-event"
GITLAB_IDEMPOTENCY_HEADER = "idempotency-key"
# Bitbucket Cloud's (support.atlassian.com/bitbucket-cloud/docs/event-payloads/ and …/manage-webhooks/): the body
# signature (`sha256=<hex>`, WebSub form, present once the webhook has a secret), the event key, and the request id.
BITBUCKET_SIGNATURE_HEADER = "x-hub-signature"
BITBUCKET_EVENT_HEADER = "x-event-key"
BITBUCKET_REQUEST_HEADER = "x-request-uuid"

# The webhook auth schemes (`source_config.auth`, §3.3). `api_key` is the only one a new webhook takes: a Jaah API
# key of the trigger's org, its owner the filer (`trigger_intake._key_verdict`). `hmac` and `bearer` are legacy: a
# trigger saved with one keeps it, and its secret, until its owner switches it to `api_key`.
AUTH_API_KEY = "api_key"
AUTH_HMAC = "hmac"
AUTH_BEARER = "bearer"
LEGACY_WEBHOOK_AUTHS = (AUTH_HMAC, AUTH_BEARER)
# A `github` trigger's scheme: never stored in `source_config`, implied by the kind.
AUTH_GITHUB = "github"
# A `gitlab` trigger's: the `X-Gitlab-Token` header carries the secret itself.
AUTH_GITLAB = "gitlab"
# A `bitbucket` trigger's: Bitbucket's `X-Hub-Signature`, implied by the kind as GitHub's is.
AUTH_BITBUCKET = "bitbucket"

# The system activity that processes deliveries (`app/trigger_intake.py`).
INTAKE_ACTIVITY = "trigger-intake"

# `trigger_notices.kind` (§3.1): what the bell derives its trigger kinds from — the one an owner gets (and its email
# template type), the daily digest's (no longer written, PRD R9; rows written before still read), and the one every
# Owner and Admin gets for a failing or stalling automation (`app/automation_alerts.py`), and the one they get for a
# node failure or AI account login alert (`app/alert_record.py::_bell_org_admins`).
NOTICE_FAILED = "trigger_failed"
NOTICE_DIGEST = "trigger_digest"
NOTICE_AUTOMATION = "automation_alert"
NOTICE_SYSTEM = "system_alert"
NOTICE_KINDS = (NOTICE_FAILED, NOTICE_DIGEST, NOTICE_AUTOMATION, NOTICE_SYSTEM)
# The notification event the failure notice goes under (`app/notification_catalog.py`).
NOTIFICATION_EVENT = "triggers"
