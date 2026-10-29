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
SOURCE_KINDS = (SOURCE_WEBHOOK, SOURCE_GITHUB, SOURCE_GMAIL, SOURCE_SLACK, SOURCE_USER_REGISTERED)
# The kinds a sender reaches over HTTP: each has an ingress token (its URL) and a signing secret.
HTTP_SOURCE_KINDS = (SOURCE_WEBHOOK, SOURCE_GITHUB)

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
DELIVERY_STATES = (
    RECEIVED, PREFLIGHTING, QUEUED, DISPATCHED, FILTERED, DEDUPED, RATE_CAPPED, SKIPPED, FAILED, UNMATCHED, FILING,
)
TRANSIENT_STATES = (RECEIVED, PREFLIGHTING, FILING)
# What the deliveries log may be filtered by. `queued` is listed: a queued row is waiting, and the owner
# asks "what is waiting?" as often as "what happened?".
OUTCOMES = (QUEUED, DISPATCHED, FILTERED, DEDUPED, RATE_CAPPED, SKIPPED, FAILED, UNMATCHED)

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
}
assert tuple(OUTCOME_LABELS) == DELIVERY_STATES, "every delivery state needs exactly one label"

# Health (§6), derived on read by `trigger_health.trigger_health`; never a column.
HEALTH_ACTIVE = "active"
HEALTH_DEGRADED = "degraded"
HEALTH_FAILED = "failed"
HEALTH_PAUSED = "paused"
HEALTH_STATES = (HEALTH_ACTIVE, HEALTH_DEGRADED, HEALTH_FAILED, HEALTH_PAUSED)

# Limits (PRD A4, A5, A8, A9, R11) — one home each.
MAX_BODY_BYTES = 1024 * 1024
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

# The webhook auth schemes (`source_config.auth`, §3.3). `api_key` is the only one a new webhook takes: a Jaah API
# key of the trigger's org, its owner the filer (`trigger_intake._key_verdict`). `hmac` and `bearer` are legacy: a
# trigger saved with one keeps it, and its secret, until its owner switches it to `api_key`.
AUTH_API_KEY = "api_key"
AUTH_HMAC = "hmac"
AUTH_BEARER = "bearer"
LEGACY_WEBHOOK_AUTHS = (AUTH_HMAC, AUTH_BEARER)
# A `github` trigger's scheme: never stored in `source_config`, implied by the kind.
AUTH_GITHUB = "github"

# The system activity that processes deliveries (`app/trigger_intake.py`).
INTAKE_ACTIVITY = "trigger-intake"
# The daily system activity that sends each owner's digest and applies the idle auto-pause (`app/trigger_notices.py`).
DIGEST_ACTIVITY = "trigger-digest"

# `trigger_notices.kind` (§3.1): what the bell derives its trigger kinds from — the two an owner gets (and their
# email template types), and the one every Owner and Admin gets for a failing or stalling automation
# (`app/automation_alerts.py`).
NOTICE_FAILED = "trigger_failed"
NOTICE_DIGEST = "trigger_digest"
NOTICE_AUTOMATION = "automation_alert"
NOTICE_KINDS = (NOTICE_FAILED, NOTICE_DIGEST, NOTICE_AUTOMATION)
# How far back a digest looks, and the notification-preference event its email (and the failure email) goes under.
DIGEST_WINDOW_HOURS = 24
NOTIFICATION_EVENT = "triggers"
