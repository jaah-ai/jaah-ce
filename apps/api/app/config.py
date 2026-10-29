"""Env-driven settings. All values come from the process environment
(compose injects them from the root ``.env``; see ``.env.example``), with
safe local defaults so the app boots out of the box."""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache

from app.jaah_env import env_flag_enabled

#: `Settings.live_public_base_url` when the environment sets none.
LIVE_PUBLIC_BASE_URL = "https://live.jaah.ai"


@dataclass(frozen=True)
class Settings:
    app_title: str
    database_url: str
    frontend_url: str
    # Whether anyone on the internet can reach this console (`CONSOLE_PUBLIC`; terraform sets it from the public
    # UI's ingress: main yes, dev no — admins only). Not public, a self-hosted Ubuntu one-liner joins the tailnet
    # and fetches from the address nodes dial (`routes/nodes/register.py::_joins_tailnet_first`). Unset = public.
    console_public: bool
    public_rate_limit: int
    tz: str
    # No GitHub token here: git access is a `credentials` row resolved per task
    # (`app/git_access.py`). Deliberately no env fallback — one would reinstate a shared fleet identity.
    # ── Session transcripts ──
    # Pinned spellings from the ECS task definition.
    # Empty = unconfigured (never guessed); the app degrades and `session_transcript_config()` warns
    # once. No `AWS_DEFAULT_REGION` fallback, so a stray shell variable cannot configure it.
    aws_region: str
    session_transcript_bucket: str
    # ── Session-efficiency analytics (Athena over the transcript bucket) ──
    # Pinned spellings from the ECS task definition; no credential of its own. All four or none:
    # `session_analytics_config()` refuses a partial set.
    session_analytics_workgroup: str
    session_analytics_database: str
    session_analytics_table: str
    session_analytics_output: str
    # ── Node journals (SOC2 G-18) ──
    # The CloudWatch Logs group nodes' journals ship into, one stream per node; terraform sets
    # `/jaah/<env>/nodes`. Empty = unconfigured; `node_log_group()` is the reader.
    node_log_group: str
    # ── Security-event archive (SOC2 G-16) ──
    # The Object Lock bucket the nightly export writes into (`app/security_archive.py`); terraform sets it
    # from the security-archive module. Empty = unconfigured: the export cron is a `noop`
    # naming this variable.
    security_archive_bucket: str
    # ── Evidence store (SOC 2 Type II recurring evidence) ──
    # The bucket `app/compliance_evidence/store.py` files control evidence into; terraform sets it on every
    # environment (Object Lock in the boundary only). Empty =
    # unconfigured (a local stack): nothing is written.
    evidence_bucket: str
    # The AWS evidence collectors' targets (`app/compliance_evidence/aws.py`), comma-separated: the paging SNS topic
    # ARNs whose subscriptions are checked, and the deployed image URIs whose ECR scans are. Terraform sets both
    # on the ECS task definition. Empty = none configured, which those collectors report as drift.
    evidence_paging_topic_arns: str
    evidence_images: str
    # ── CloudWatch metrics (SOC2 G-15) ──
    # The namespace `app/metrics_emf.py` writes its Embedded Metric Format lines under; terraform sets it
    # inside the SOC 2 boundary only. Empty = unconfigured: the
    # emitter cron is a `noop` naming this variable.
    metrics_namespace: str
    # ── Alarm relay (SOC2 CC7.2) ──
    # The SQS queue both alarm topics feed (provisioned in Terraform); `app/alarm_relay.py`
    # drains it to Slack. Set inside the SOC 2 boundary only. Empty = unconfigured: the cron is a `noop`.
    alarm_relay_queue_url: str
    # ── Task attachments ──
    # Self-hosted S3-compatible endpoint (local MinIO); empty in deployed environments. Setting it makes
    # `presign.presigned_url(allow_insecure=True)` legal. Consulted only by `app/attachment_storage.py`,
    # so transcripts and the apt channel are never redirected by it.
    s3_endpoint_url: str
    # ── Operator-alert channel (app/alert_relay.py) ──
    # Env config, not `~/.claude/.omc-config.json`: the api container has no such file. Empty token →
    # nothing sent, warned once. `alert_relay_enabled` is a kill switch, not the credential gate.
    alert_relay_enabled: bool
    # Socket Mode consumer kill switch. The test suite runs the app lifespan, so conftest forces this
    # to `0` to keep `./dc test` from opening a live WebSocket to Slack.
    slack_socket_mode_enabled: bool
    # Generic JSON webhook fallback when no per-user Slack recipient resolves. The URL is the
    # credential, so `_http_url` validates it (urllib would raise with the URL in the message).
    alert_webhook_url: str
    # ── Trigger webhooks (app/routes/triggers.py) ──
    # The public hooks edge's base address (`https://hooks.example.com`), set by terraform from
    # `var.hooks_hostname`. A deployment address, not a feature switch: empty (a local or CI stack) means no
    # webhook/GitHub trigger can be created here, because nobody outside could reach its URL.
    hooks_public_base_url: str
    # ── Live Cast (app/routes/live_cast.py) ──
    # The public viewer's base address a published project's URL is built on; terraform sets it per environment
    # (`https://live.dev.example.com` on dev). The default lives here: prod's address.
    live_public_base_url: str
    # ── macOS Full Disk Access profile (app/macos_pppc.py) ──
    # The Apple Developer Team ID that signs `jaah-anchor`, so the PPPC profile can pin its code requirement;
    # terraform sets it from `var.apple_team_id`. Empty (or not 10 of `[A-Z0-9]`, warned) = unconfigured: the
    # profile route answers 404 naming `JAAH_APPLE_TEAM_ID`.
    apple_team_id: str


_APPLE_TEAM_ID = re.compile(r"[A-Z0-9]{10}")


def _apple_team_id(raw: str) -> str:
    """RAW when it is a Team ID, else "" — a malformed one is unconfigured (warned), never put in a profile."""
    if raw and _APPLE_TEAM_ID.fullmatch(raw) is None:
        log.warning("JAAH_APPLE_TEAM_ID is not a 10-character [A-Z0-9] Apple Team ID; the macOS profile is off")
        return ""
    return raw


def _http_url(name: str, raw: str) -> str:
    """Fail closed at Settings construction on an alert URL urllib cannot POST to.

    The URL is a credential; a malformed one would make urllib raise with it in a logged traceback.
    Empty is allowed (unconfigured). The error names the variable and never echoes the value."""
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"{name} must be an http(s) URL with a host (got scheme {parsed.scheme!r}, "
            f"host {parsed.netloc!r}) — the value itself is withheld from this message because it "
            "carries a credential. Fix it in the root .env."
        )
    return raw


@dataclass(frozen=True)
class DbPoolSizes:
    pool_size: int
    max_overflow: int
    dbos_pool_size: int | None  # None = DBOS's own default
    identity_pool_size: int
    background_pool_size: int


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """An integer >= `minimum`, or `default` when unset or empty; anything else refuses the boot."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def db_pool_sizes() -> DbPoolSizes:
    """The per-task DB connection ceiling: `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` for the request engine
    (`app/db.py::_pool_kwargs`), `DB_IDENTITY_POOL_SIZE` and `DB_BACKGROUND_POOL_SIZE` for its two side engines
    (`app/db.py`'s identity and background lanes), and `DBOS_POOL_SIZE` for DBOS's system-database engine
    (`app/durable/runtime.py`). Set per environment by the deployment because the
    control plane autoscales and each environment's RDS class bounds tasks x pool. Unset — the local stack —
    keeps 20 + 20, 5, 10 and DBOS's default. A pool size of 0 means UNBOUNDED to SQLAlchemy, hence minimum 1.
    Not cached: read once per engine at import, and tests set the environment per case."""
    dbos = _env_int("DBOS_POOL_SIZE", 0)
    return DbPoolSizes(
        pool_size=_env_int("DB_POOL_SIZE", 20, minimum=1),
        max_overflow=_env_int("DB_MAX_OVERFLOW", 20),
        dbos_pool_size=dbos or None,
        identity_pool_size=_env_int("DB_IDENTITY_POOL_SIZE", 5, minimum=1),
        background_pool_size=_env_int("DB_BACKGROUND_POOL_SIZE", 10, minimum=1),
    )


def request_thread_limit() -> int:
    """The request threadpool's size (`app/pool_watch.py::apply_thread_limit`): `API_THREAD_LIMIT`, or the app
    pool's capacity (`pool_size + max_overflow`), so no more sync handlers run at once than there are
    connections for them. Not cached: read once at lifespan start, and tests set the environment per case."""
    sizes = db_pool_sizes()
    return _env_int("API_THREAD_LIMIT", sizes.pool_size + sizes.max_overflow, minimum=1)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else env_flag_enabled(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Imported here, not at module level: `db_credentials` pulls in sqlalchemy, and DB-free modules
    # (e.g. `app/attachment_storage.py`'s check stage) import `app.config`.
    from app import db_credentials

    return Settings(
        app_title=os.environ.get("APP_TITLE", "Jaah"),
        # Single home for the database URL choice.
        database_url=db_credentials.database_url(),
        frontend_url=os.environ.get("FRONTEND_URL", "http://localhost:3020"),
        console_public=_env_bool("CONSOLE_PUBLIC", True),
        public_rate_limit=int(os.environ.get("PUBLIC_RATE_LIMIT", "600")),
        tz=os.environ.get("TZ", "America/New_York"),
        aws_region=os.environ.get("AWS_REGION", "").strip(),
        session_transcript_bucket=os.environ.get("SESSION_TRANSCRIPT_BUCKET", "").strip(),
        session_analytics_workgroup=os.environ.get("SESSION_ANALYTICS_WORKGROUP", "").strip(),
        session_analytics_database=os.environ.get("SESSION_ANALYTICS_DATABASE", "").strip(),
        session_analytics_table=os.environ.get("SESSION_ANALYTICS_TABLE", "").strip(),
        session_analytics_output=os.environ.get("SESSION_ANALYTICS_OUTPUT", "").strip(),
        node_log_group=os.environ.get("NODE_LOG_GROUP", "").strip(),
        security_archive_bucket=os.environ.get("SECURITY_ARCHIVE_BUCKET", "").strip(),
        evidence_bucket=os.environ.get("EVIDENCE_BUCKET", "").strip(),
        evidence_paging_topic_arns=os.environ.get("EVIDENCE_PAGING_TOPIC_ARNS", "").strip(),
        evidence_images=os.environ.get("EVIDENCE_IMAGES", "").strip(),
        metrics_namespace=os.environ.get("METRICS_NAMESPACE", "").strip(),
        alarm_relay_queue_url=os.environ.get("ALARM_RELAY_QUEUE_URL", "").strip(),
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL", "").strip().rstrip("/"),
        alert_relay_enabled=_env_bool("ALERT_RELAY_ENABLED", True),
        slack_socket_mode_enabled=_env_bool("SLACK_SOCKET_MODE_ENABLED", True),
        alert_webhook_url=_http_url(
            "ALERT_WEBHOOK_URL", os.environ.get("ALERT_WEBHOOK_URL", "").strip()
        ),
        hooks_public_base_url=_http_url(
            "HOOKS_PUBLIC_BASE_URL", os.environ.get("HOOKS_PUBLIC_BASE_URL", "").strip()
        ).rstrip("/"),
        live_public_base_url=_http_url(
            "LIVE_PUBLIC_BASE_URL", os.environ.get("LIVE_PUBLIC_BASE_URL", "").strip() or LIVE_PUBLIC_BASE_URL
        ).rstrip("/"),
        apple_team_id=_apple_team_id(os.environ.get("JAAH_APPLE_TEAM_ID", "").strip()),
    )


# ── Session transcripts: the off-until-configured read ──
# One predicate so callers never re-derive "both variables set" or re-emit the warning. First miss
# warns, the rest log at debug (consulted per session end). Non-None means configured, not capable:
# credentials come from `app/aws/container_credentials.py`, signing from `app/aws/presign.py` (query
# string signing; `app/credentials/sigv4.py` is the header signer, for credential rows).
log = logging.getLogger("app.config")

_transcript_warned = False


def reset_session_transcript_warning() -> None:
    """Test seam only — the warn-once latch is process-local and is never reset in production."""
    global _transcript_warned
    _transcript_warned = False


def session_transcript_config() -> tuple[str, str] | None:
    """Return ``(aws_region, bucket)`` when transcript storage is configured, else ``None``.

    ``None`` is a legitimate, shipped state — the feature is OFF and the control plane keeps running.
    It is announced once at WARNING, naming both environment variables and which of them is empty. An edition
    without `aws` has no S3: always None, its sessions on the local volume (`session_transcript_archive.on_volume`);
    its boot refuses the bucket setting (`app/ce_bootstrap.py::refuse_removed_settings`)."""
    from app import edition_features

    if not edition_features.enabled("aws"):
        return None
    settings = get_settings()
    region = settings.aws_region
    bucket = settings.session_transcript_bucket
    if region and bucket:
        return region, bucket

    global _transcript_warned
    message = (
        "session transcript storage is UNCONFIGURED — transcripts will not be stored or served "
        "(AWS_REGION=%s, SESSION_TRANSCRIPT_BUCKET=%s). Both are required. They are set by the "
        "deployment's task definition; for a local stack put them in the root .env. Clearing "
        "SESSION_TRANSCRIPT_BUCKET is also the deliberate kill switch for this feature."
    )
    args = ("set" if region else "empty", "set" if bucket else "empty")
    if _transcript_warned:
        log.debug(message, *args)
    else:
        _transcript_warned = True
        log.warning(message, *args)
    return None


# ── Session-efficiency analytics: the second off-until-configured read ──
# Separate from `session_transcript_config()` so an operator can tell which feature is off. Promises
# "named", never "reachable"; `app/session_efficiency.py` folds credential and reach failures into one
# `status:"unavailable"`. The region is shared with transcripts rather than a fifth variable.
@dataclass(frozen=True)
class SessionAnalyticsConfig:
    """The five names one Athena query needs (a dataclass to avoid five positional strings)."""

    region: str
    workgroup: str
    database: str
    table: str
    output_location: str


_analytics_warned = False


def session_analytics_settings() -> tuple[tuple[str, bool], ...]:
    """Each of the five variables by name, with whether it is set — never its value (Audit > Control plane
    shows this list, so an operator reads which name the unconfigured line is missing)."""
    settings = get_settings()
    return (
        ("AWS_REGION", bool(settings.aws_region)),
        ("SESSION_ANALYTICS_WORKGROUP", bool(settings.session_analytics_workgroup)),
        ("SESSION_ANALYTICS_DATABASE", bool(settings.session_analytics_database)),
        ("SESSION_ANALYTICS_TABLE", bool(settings.session_analytics_table)),
        ("SESSION_ANALYTICS_OUTPUT", bool(settings.session_analytics_output)),
    )


# Each variable's shape, never a value: Audit > Control plane shows it beside an empty setting, so the operator knows
# what kind of string to set. The spellings terraform pins are in the ECS task definition.
SESSION_ANALYTICS_FORMATS = {
    "AWS_REGION": "AWS region code (us-east-1)",
    "SESSION_ANALYTICS_WORKGROUP": "Athena workgroup name",
    "SESSION_ANALYTICS_DATABASE": "Glue database name",
    "SESSION_ANALYTICS_TABLE": "Glue table name",
    "SESSION_ANALYTICS_OUTPUT": "s3://<bucket>/",
}


def reset_session_analytics_warning() -> None:
    """Test seam only — the warn-once latch is process-local and is never reset in production."""
    global _analytics_warned
    _analytics_warned = False


def session_analytics_config() -> SessionAnalyticsConfig | None:
    """Return the analytics coordinates when the read layer is configured, else ``None``.

    ``None`` is a shipped state (normal on a local ``./dc`` stack). All five names or nothing: a
    partial set is treated as unconfigured. Warns once, then logs at debug.
    """
    settings = get_settings()
    config = SessionAnalyticsConfig(
        region=settings.aws_region,
        workgroup=settings.session_analytics_workgroup,
        database=settings.session_analytics_database,
        table=settings.session_analytics_table,
        output_location=settings.session_analytics_output,
    )
    if all(is_set for _, is_set in session_analytics_settings()):
        return config

    global _analytics_warned
    message = (
        "session-efficiency analytics is UNCONFIGURED — Audit > Sessions > Efficiency will "
        "report itself unavailable (AWS_REGION=%s, SESSION_ANALYTICS_WORKGROUP=%s, "
        "SESSION_ANALYTICS_DATABASE=%s, SESSION_ANALYTICS_TABLE=%s, SESSION_ANALYTICS_OUTPUT=%s). All "
        "five are required. They are set by the deployment's task definition "
        "from its session-analytics resources; clearing them is also the deliberate kill switch for "
        "this feature."
    )
    args = tuple("set" if is_set else "empty" for _, is_set in session_analytics_settings())
    if _analytics_warned:
        log.debug(message, *args)
    else:
        _analytics_warned = True
        log.warning(message, *args)
    return None


# ── Node journals: the third off-until-configured read ──
# Consulted per node shipment, so it warns once and then logs at debug, as the two above.
_node_log_group_warned = False


def reset_node_log_group_warning() -> None:
    """Test seam only — the warn-once latch is process-local and is never reset in production."""
    global _node_log_group_warned
    _node_log_group_warned = False


def node_log_group() -> str | None:
    """The CloudWatch Logs group node journals ship into, or ``None`` when shipping is unconfigured.

    A value that is not a CloudWatch log group name is unconfigured too (the feature degrades; the
    control plane keeps running), and the warning says which. An edition without `node_log_archive` has no
    group: always None."""
    from app import edition_features

    if not edition_features.enabled("node_log_archive"):
        return None
    # Imported here: the pattern is single-homed with the client, which pulls in httpx.
    from app.aws.cloudwatch_logs import GROUP_PATTERN

    value = get_settings().node_log_group
    if value and GROUP_PATTERN.fullmatch(value):
        return value

    global _node_log_group_warned
    message = (
        "node log shipping is UNCONFIGURED — node journals will be refused with 503 (NODE_LOG_GROUP=%s). "
        "It is set by the deployment's task definition to "
        "/jaah/<env>/nodes; clearing it is also the deliberate kill switch for this feature."
    )
    state = "not a log group name" if value else "empty"
    if _node_log_group_warned:
        log.debug(message, state)
    else:
        _node_log_group_warned = True
        log.warning(message, state)
    return None
