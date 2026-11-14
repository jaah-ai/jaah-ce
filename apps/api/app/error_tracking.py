"""Error tracking with Sentry: OFF until the operator sets a DSN.

The `error_tracking_dsn` operator setting ("Integrations" > "Sentry") is read once, at process start
(`app/main.py::_lifespan`): blank (the default) initialises nothing, so no Sentry client exists and nothing
leaves the process; a DSN starts the SDK for this process's life, so a change applies on the next API
task restart. The console's own DSN (`error_tracking_dsn_web`) is served by `GET /api/public/settings`
and initialised in the browser (`apps/web/src/lib/errorTracking.ts`).

NOTHING PERSONAL, SECRET OR SOURCE IS SENT. Only unhandled exceptions are reported: the default and
auto-enabled integrations are off (no logging, SQL, HTTP-client or subprocess capture), breadcrumbs are off,
and `data_collection` collects no user info, cookies, headers, bodies, query parameters, frame variables or
source lines. `scrub_event` (the `before_send`) then removes what an event still holds: the request body,
cookies, query string and every header but `user-agent`, the user except an opaque id, frame source, and any
secret the shared redaction pass (`app/secret_redact.py`) or this module's additions find in a message, an
exception value or a context. Its browser twin is `apps/web/src/lib/errorTrackingScrub.ts`.

Enabling it makes Sentry a subprocessor: the operator adds its row to the public subprocessor list in the same
change that sets a DSN on prod (the vendor register's Sentry row says so). Where to set both DSNs: the operator
docs' Sentry integration page.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger("app.error_tracking")

REDACTED = "[redacted]"

# What the shared pass (`app/secret_redact.py`) does not cover, each replaced whole: emails; URL user info,
# query strings and fragments; prefixed tokens shorter than its own floors; chat-bot tokens (`<id>:<secret>`).
_EXTRA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), REDACTED),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE), rf"\1{REDACTED}@"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?#\"'<>]*)[?#][^\s\"'<>]*", re.IGNORECASE), rf"\1?{REDACTED}"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-|xox[abposr]-|glpat-)[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(?<![0-9])\d{6,}:[A-Za-z0-9_-]{30,}"), REDACTED),
    # `password=…`, `access_token: …`, `"client_secret": "…"`: the value goes, the name stays.
    (
        re.compile(r"""([A-Za-z0-9_]*(?:pass(?:word)?|passwd|secret|token|api[_-]?key))(["']?\s*[:=]\s*["']?)[^\s"'&,;]+""", re.IGNORECASE),
        rf"\1\2{REDACTED}",
    ),
)
# Key names whose value is dropped whatever it looks like.
# Matched whole word by word (`x_auth_token`, `api-key`), so `author` and `oauth_provider` stay.
_SECRET_KEYS = re.compile(
    r"(^|[_-])(pass(word)?|passwd|secrets?|tokens?|auth|cookies?|session|api[_-]?key|credentials?|dsn)($|[_-])", re.IGNORECASE
)
_KEPT_HEADERS = frozenset({"user-agent"})


def scrub_text(text: str) -> str:
    """`text` with every secret and email in it replaced. Raises `secret_redact.RedactionUnavailable` when the
    shared pass cannot load, so text is never passed on half-redacted."""
    from app import secret_redact

    text = secret_redact.redacted(text)
    for pattern, replacement in _EXTRA_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub_value(value: Any) -> Any:
    """Strings scrubbed, containers walked; a secret-named key's value dropped whole."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEYS.search(k) else _scrub_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    return value


def _scrub_request(request: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    if isinstance(request.get("url"), str):
        # The path only: a query string can carry a token or an email.
        kept["url"] = scrub_text(request["url"].split("?", 1)[0])
    if isinstance(request.get("method"), str):
        kept["method"] = request["method"]
    headers = request.get("headers")
    if isinstance(headers, dict):
        kept["headers"] = {k: scrub_text(str(v)) for k, v in headers.items() if str(k).lower() in _KEPT_HEADERS}
    # `data`, `cookies`, `query_string` and `env` are never kept.
    return kept


def scrub_event(event: dict[str, Any], hint: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """`event` with nothing personal, secret or source left in it (module docstring)."""
    if isinstance(event.get("request"), dict):
        event["request"] = _scrub_request(event["request"])
    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {"id": str(user["id"])} if user.get("id") is not None else {}
    for frame_holder in (event.get("exception") or {}).get("values") or []:
        if isinstance(frame_holder.get("value"), str):
            frame_holder["value"] = scrub_text(frame_holder["value"])
        for frame in (frame_holder.get("stacktrace") or {}).get("frames") or []:
            # No variables and no source: a frame is its file, function and line.
            for key in ("vars", "context_line", "pre_context", "post_context"):
                frame.pop(key, None)
    event.pop("breadcrumbs", None)
    for key in ("message", "logentry", "extra", "contexts", "tags", "transaction"):
        if key in event:
            event[key] = _scrub_value(event[key])
    return event


def before_send(event: dict[str, Any], hint: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The SDK's hook: the scrubbed event, or None (nothing sent) when the shared redaction pass is unavailable."""
    from app import secret_redact

    try:
        return scrub_event(event, hint)
    except secret_redact.RedactionUnavailable:
        log.warning("error tracking: event dropped; the redaction pass is unavailable")
        return None


def _environment() -> str:
    """The control plane's name in Sentry: the host of `FRONTEND_URL`, so each environment reports apart."""
    from app.login_oauth import frontend_origin

    return urlsplit(frontend_origin()).hostname or "unknown"


def init(dsn: str) -> bool:
    """Start the Sentry SDK for this process with `dsn`, or do nothing when it is blank. True when started."""
    dsn = dsn.strip()
    if not dsn:
        return False
    import sentry_sdk

    from app.version import TOOL_VERSION

    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        release=TOOL_VERSION,
        environment=_environment(),
        # Unhandled exceptions from requests only: no logging, SQL, HTTP-client, subprocess or thread capture.
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        max_breadcrumbs=0,
        # Collect nothing personal and no source (the SDK's successor to `send_default_pii`); with it set, the
        # SDK's own event scrubber is off, so `before_send` is the whole guarantee.
        data_collection={
            "user_info": False,
            "cookies": {"mode": "allowlist", "terms": []},
            "http_headers": {"request": {"mode": "allowlist", "terms": ["user-agent"]}},
            "http_bodies": [],
            "url_query_params": {"mode": "allowlist", "terms": []},
            "database_query_data": False,
            "queues": False,
            "stack_frame_variables": False,
            "frame_context_lines": 0,
        },
        before_send=before_send,
        # Errors only: no tracing option at all (any rate, even 0.0, starts a transaction per request).
    )
    return True


def start() -> None:
    """Read `error_tracking_dsn` and start the SDK when set. Never refuses the boot: a failure is logged."""
    from app.operator_settings import reads as settings_reads

    try:
        dsn = settings_reads.get_text("error_tracking_dsn")
    except Exception:  # noqa: BLE001 — error tracking must never stop the API serving
        log.warning("error tracking: not started; the setting could not be read", exc_info=True)
        return
    try:
        started = init(dsn)
    except Exception:  # noqa: BLE001 — likewise; the DSN itself is never logged
        log.warning("error tracking: not started; the SDK refused to start", exc_info=False)
        return
    if started:
        log.info("error tracking: on (Sentry)")
