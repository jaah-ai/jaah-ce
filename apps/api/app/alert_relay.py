"""The operator-alert relay: the only path that delivers an alert to a human.

Config comes from the process env (`app.config.Settings`); the one env channel is `ALERT_WEBHOOK_URL`,
and the per-user Slack recipient is resolved from the DB (`notify_targets`) and passed in. A missing
channel logs one warning; a delivery failure raises `AlertRelayError` so "could not deliver" never
reads as "nothing to deliver" (`routes/agent/fleet_observe.py:create_alert`).

`reason`/`url` are untrusted publisher text; `AlertIn.node_id` is a reporting hint only.
The dedup key (`uq_alerts_dedup`) excludes `node_id`: `(repo, issue)` is one work identity.
Test isolation: `test`-flagged payloads are dropped here; suite-wide, `tests/conftest.py` pins
`ALERT_RELAY_ENABLED=0` and `tests/alert_channel_guard.py` replaces `_urlopen`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import UUID

from app import edition_features, http_bounded, notify_text
from app.config import get_settings
# Imported so the shared request fact cannot drift (`app/slack_egress.py` sends it too). The egress function
# itself is not shared (see that module).
from app.notify_text import NO_UNFURL

# The community edition has no Slack: `relay_alert` returns before a Slack recipient's leg, the only reader of
# these names. Both are stdlib-only at import, keeping this module DB-free (the plan gate touches the DB only
# for a tenant org).
if edition_features.enabled("slack"):
    from app import slack_blocks, slack_egress

log = logging.getLogger("app.alert_relay")

# The single home of the kind contract, `assigned` included. Validated at `AlertIn`
# (pydantic's `kind: str` accepts `""`); an unknown kind is a 422. Order is the Profile > Notifications
# panel's order (`preferences.NOTIFICATION_EVENTS`). `assigned` is produced in-process only; over HTTP a
# node can never name a task or an assigner (`AlertIn._strip_task_id`). `login-expiring` (a Claude login
# about to pass its hard refresh-token lifetime) is its own kind, not a `self-heal`: nothing heals it
# — only an operator's re-login does — and an operator mutes it apart. Producer:
# `fleet_observe.emit_login_expiry_alerts`. `node-disk-paused` (a node's disk went critical, so the disk gate
# stopped its runner and CI dispatch until usage drops under warn) is its own kind, so an operator can mute it
# apart; producer `node_disk_events.after_commit`. `node-tmp-inodes` (a node's /tmp inode use crossed 90%, so
# `mktemp` is about to fail there) likewise; producer `node_tmp_inode_alarm.after_commit`. `alarm` is a page
# (`PAGE_KINDS`). `agent-message` is an SDK agent's informational post to its task's channel
# (`task_notify.announce_agent_message`).
ALERT_KINDS = (
    "done", "blocked", "error", "failed", "self-heal", "login-expiring", "node-disk-paused", "node-tmp-inodes",
    "alarm", "agent-message", "assigned",
)

# The pages: a CloudWatch alarm changing state, relayed by `app/alarm_relay.py` to the env channel and every
# superadmin's Slack DM. In-process only (`AlertIn` refuses them, so no node can page the operators) and not a
# subscribable event (`preferences.NOTIFICATION_EVENTS` leaves them out), so no recipient can mute one.
PAGE_KINDS = ("alarm",)

# The node-level kinds: no task, so no recipient chain. Their in-process producers also tell the node org's Owners
# and Admins (`alert_record.record_alert(tell_org_admins=True)`), and only those may subscribe to them
# (`preferences.ORG_ADMIN_EVENTS`).
NODE_KINDS = ("login-expiring", "node-disk-paused", "node-tmp-inodes")

# Kinds only the control plane raises: the pages, `node-disk-paused` and `node-tmp-inodes` (CP decisions,
# so a node posting one is either forging it or squatting its dedup identity), and `agent-message` (its rate
# cap and its author are the CP's `notify` route's). `AlertIn` refuses them.
CP_ONLY_KINDS = PAGE_KINDS + ("node-disk-paused", "node-tmp-inodes", "agent-message")


class AlertRelayError(RuntimeError):
    """A channel was configured and delivery to it failed; distinct from every `False` ("not sent")
    outcome of `relay_alert`."""


# ── Untrusted-content hardening ──────────────────────────────────────────────────────────────────────
# Untrusted publisher text; channels auto-link bare domains, `www.` hosts and non-http deep links
# too, so all three forms are caught. Named groups let `_sanitize_reason` tell alternatives apart.
_URL_LIKE_RE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.\-]*://\S+)"                            # any scheme://... (http, tg, ...)
    r"|(?P<www>www\.\S+)"                                              # www.-prefixed host
    r"|(?P<bare>\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}\b(?:/\S*)?)",  # bare domain + TLD
    re.IGNORECASE,
)
# Accepted residual: ASCII-only, so an IDN/homoglyph domain rides through.
# Open defect: the `scheme` and `bare` alternatives backtrack quadratically on poster-chosen input
# (`AlertIn.issue`/`reason` have no `max_length`). Do not bound the pattern: every bound is a redaction
# bypass, since `re.sub` emits the skipped prefix verbatim. The only lever is an input length ceiling,
# an owner's call.

# The bare alternative also matches filenames (`reconcile.py`); a `bare` match ending in one of these
# is kept. `scheme://`/`www.` matches are always redacted.
_NON_TLD_EXTENSIONS = frozenset({
    "py", "js", "ts", "tsx", "jsx", "go", "rb", "sh", "md", "json", "yaml", "yml",
    "toml", "log", "txt", "csv", "ini", "cfg", "conf", "lock", "sql", "html", "css",
})


def _sanitize_reason(reason: str) -> str:
    def _redact(match: re.Match) -> str:
        bare = match.group("bare")
        if bare is not None:
            tld = bare.rsplit(".", 1)[-1].split("/", 1)[0].lower()
            if tld in _NON_TLD_EXTENSIONS:
                return bare
        return "[link removed]"

    return _URL_LIKE_RE.sub(_redact, reason)


def inert_mrkdwn(text: str, *, limit: int | None = None) -> str:
    """Untrusted text made safe for a Slack mrkdwn field: URL-neutralized, flat, bounded, escaped.

    Single home of the recipe; the order is load-bearing:
      * `_sanitize_reason` first (escaping `<>` still leaves a bare `evil.example` auto-linking);
      * flatten to one line and clip to `limit` via `notify_text._clip` (a newline lets forged text read
        as the bot's next paragraph; an over-long field is `ok:false`);
      * escape `&`, `<`, `>` last.

    `limit` is keyword-only and optional so a self-clipping renderer can take this as a plain callable;
    `None` means `_SLACK_TEXT_MAX_CHARS`. Not here: the NFKC/Cc-Cf fold (lives in a DB-importing module)
    and `"` -> `'` (a caller concern).
    """
    folded = _sanitize_reason(text)
    clipped = notify_text._clip(
        " ".join(folded.split()), _SLACK_TEXT_MAX_CHARS if limit is None else limit
    )
    return escape_markup(clipped)


def escape_markup(text: str) -> str:
    """`&`, `<`, `>` as Slack entities, `&` first so an escape is never escaped again. The one escape every
    mrkdwn field ends with (`inert_mrkdwn`, `_escape_mrkdwn`, `site_enquiry._escape_for_slack`)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _validate_notify_url(url: str) -> str | None:
    """Only a genuine `https://github.com/...` link is relayed as a clickable URL; anything else is
    dropped. Whitespace rejects first: `urlsplit` stops `netloc` at a space, so a trailing
    `" evil.example/phish"` would smuggle a second link. Returns the `urlunsplit` form, never the raw input."""
    if any(ch.isspace() for ch in url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme == "https" and parsed.netloc.lower() == "github.com":
        return urllib.parse.urlunsplit(parsed)
    return None


def _console_link(url: str) -> str | None:
    """A link to this deployment's own console, admitted by matching its configured origin, or `None`.

    Not `_validate_notify_url`: that is a github allowlist for untrusted `event["url"]`, while a card url
    is composed from our `FRONTEND_URL` (`task_external_comment._console_task_url`); the allowlist would
    reject every one and leave brief mode with no link.

    Still validated: `FRONTEND_URL` is operator-set, so only a url matching its origin (scheme plus host,
    case-insensitive) is returned. Fails closed on a blank or scheme-less setting, a mismatched host, a
    non-http scheme. Whitespace rejects first and `urlunsplit` is returned, as in `_validate_notify_url`.
    """
    if not url or any(ch.isspace() for ch in url):
        return None
    configured = (get_settings().frontend_url or "").strip()
    if not configured:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        origin = urllib.parse.urlsplit(configured)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if (parsed.scheme, parsed.netloc.lower()) != (origin.scheme, origin.netloc.lower()):
        return None
    return urllib.parse.urlunsplit(parsed)


def target_label(event: dict[str, Any]) -> str:
    """Which repo/issue this alert is about (`owner/repo#12`, whichever half exists, or `""`), shared by
    `format_message` and the card footer so one message never names two things.

    Not sanitized here. `repo` is CP-composed (`task_notify._notify_repo`); `issue` is not
    (`AlertIn.issue`, off the wire, no `max_length`). Sanitizing the composed label would rewrite the
    repo's `word.tld`, so `_brief_text` inerts the `issue` field instead. Deferred follow-up: inert `issue`
    (and bound it at `AlertIn`) on every leg that renders this label. Cards render it as `plain_text`."""
    repo = event.get("repo") or ""
    issue = event.get("issue")
    return f"{repo}#{issue}" if repo and issue else (repo or (f"#{issue}" if issue else ""))


# ── The headline: what a message says happened ──
#     {project}|{task id} {task title}: {status}      ->  jaah-sell|3647 Market research: Shipped
# The words are the reader's, not the pipeline's: explicit and total over `ALERT_KINDS` (the suite pins
# it); an unmapped kind falls back to its titlecased name (`_status_word`).
_STATUS_WORDS = {
    "done": "Shipped",
    "blocked": "Needs your answer",
    "error": "Error",
    "failed": "Failed",
    "self-heal": "Self-heal",
    "login-expiring": "Re-login soon",
    "node-disk-paused": "Disk full, paused",
    "node-tmp-inodes": "/tmp inodes high",
    "assigned": "Assigned to you",
    # Follows the agent's name (`payload["actor"]`): "Acme Bot posted".
    "agent-message": "Posted",
    # Not alert kinds: `comment_notify` / `comment_reactions` events, which never cross `AlertIn`.
    "mention": "Mentioned you",
    "reaction": "Reacted",
    # Not an alert kind either: `durable/support_notify_queue.py`'s, which never crosses `AlertIn`.
    "support": "Support",
    # Nor this: a Jaah AI wallet notice's DM (`app/token_billing/alerts.py`), which never crosses `AlertIn`.
    "billing": "Billing",
    # Nor this: a Jaah API key's expiry reminder (`app/api_keys.py::sweep`), which never crosses `AlertIn`.
    "api_key_expiry": "API key expiring",
    # Nor this: an automation failing or stalling, to its org's Owners and Admins (`app/automation_alerts.py`).
    "automation_failures": "Automation failing",
    # Only reached by an `alarm` event with no alarm in its payload; a real one renders `_alarm_lines`.
    "alarm": "Alarm",
}


# Caps only the degraded fallback arm, so a hand-built kind cannot put an unbounded string on the line.
_STATUS_WORD_MAX = 40


def _status_word(kind: Any) -> str:
    """The reader-facing word one alert kind ends its line with. Total over `ALERT_KINDS`; anything else
    (only reachable from a test double) degrades to its own name titlecased rather than a blank tail."""
    name = str(kind or "").strip()
    if not name:
        return "?"
    mapped = _STATUS_WORDS.get(name.lower())
    if mapped is not None:
        return mapped
    # Bounded like every other field; unreachable in production, reachable from a test double.
    return notify_text._clip(name.replace("-", " ").replace("_", " ").capitalize(), _STATUS_WORD_MAX)


def _redact_explicit_urls(text: str) -> str:
    """`_sanitize_reason` minus its bare-domain rule: `scheme://…` and `www.…` are redacted, a plain
    `word.tld` is left alone.

    For names, not reasons: `jaah.io` is a legitimate project name and the bare rule would turn the
    line into `[link removed]|3647 …`. Explicit URLs are still redacted (the form Slack reliably links on
    the flat leg).

    Residual: a node cannot write a name (the key is stripped at the HTTP boundary), but any `projects:edit`
    grantee can rename a project to a bare `evil.to/phish`. Bounded: 40 characters, no markup, and
    nothing on either card (`verbatim`). The pattern is ASCII-only, as with `_sanitize_reason`."""
    def _redact(match: re.Match) -> str:
        return match.group(0) if match.group("bare") is not None else "[link removed]"

    return _URL_LIKE_RE.sub(_redact, text)


def _escape_mrkdwn(text: str) -> str:
    """The `inert_name` a Slack surface injects into `build_task_line`: explicit URLs redacted, then the
    markup openers escaped.

    The order is load-bearing: redaction first, then `&`, then `<` and `>` (which would close our
    `<url|label>` token early). Applied to the field, never the composed line.

    The `|` drop is not here: stripping after redaction can re-form a URL (`https:|//|evil.test`), so
    `build_task_line` drops `|` from the raw value first. Accepted residuals, as for titles: `*bold*`,
    `_italics_`, `:shortcodes:` render, and a bare `word.tld` auto-links without `verbatim`."""
    return escape_markup(_redact_explicit_urls(text))


def task_parts(event: dict[str, Any]) -> dict[str, Any] | None:
    """The Task an alert is about, as `notify_text.build_task_line`'s arguments, or `None` when the
    event is not about a Task (a self-heal page, a node's `failed` over HTTP).

    The security property: the parts ride in `event["payload"]`, the in-process-only channel
    `AlertIn._strip_task_id` empties at the HTTP boundary (`_TRUSTED_ALERT_PAYLOAD_KEYS`), so no node can
    choose what a trusted DM says or which scope it names. Only `task_notify` fills them.

    The id is `isinstance`-guarded because it sits inside `<url|label>`, where a composable value could
    forge a second link; `bool` is excluded (`True` would render as task `1`)."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    # The payload's id first, then the event's (`record_alert`'s dedup-scoping parameter, which the
    # backstop uses to stay unanswerable). Any producer may set the latter, but it only becomes a headline
    # beside the trusted title/project keys.
    task_id = payload.get("task_id", event.get("task_id"))
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        return None
    # A task id alone is not a headline: a payload that merely scopes (e.g. `comment_id`) keeps its
    # `reason`. `not in`, not falsiness: `build_task_notify_event` spells all keys and `project` may be
    # `None`; absence means the producer never authored a headline.
    if "task_title" not in payload and "project" not in payload:
        return None
    return {
        "task_id": task_id,
        # Raw: `notify_text._fit` folds and inerts each field; folding here too would be a second home.
        "project": payload.get("project"),
        "task_title": payload.get("task_title"),
        "status": _status_word(event.get("kind")),
        # The org the Task is in (`app/org_names.py`), so a person in several orgs reads whose it is.
        "org": payload.get("org"),
        # The SDK agent whose run this is (`task_notify.build_task_notify_event`); absent for a built-in.
        **({"agent": agent} if (agent := _payload_str(payload, "agent")) else {}),
    }


def _actor(event: dict[str, Any]) -> str | None:
    """Who performed the act a task line reports, off the trusted payload, or `None`.

    `payload["actor"]` is in-process-only (stripped at the HTTP boundary), so a node cannot put a
    colleague's name before "mentioned you". Raw: `build_task_line` inerts it."""
    payload = event.get("payload")
    actor = payload.get("actor") if isinstance(payload, dict) else None
    return actor if isinstance(actor, str) else None


def task_headline(event: dict[str, Any], *, link: str | None = None) -> str | None:
    """The plain-text one-line headline for a task-scoped alert, or `None` when there is no Task.

    Plain means "emits no mrkdwn" (the output also goes to the generic webhook), not "neutralizes
    nothing": a Slack incoming webhook renders `text` as mrkdwn, so an unsanitized title like
    `evil.example/phish` would auto-link. So the title takes `_sanitize_reason` and the project name
    `_redact_explicit_urls` (see `build_task_line`). The escape is omitted: `&amp;` would print literally
    on a plain endpoint; `task_headline_mrkdwn` adds it."""
    return _task_line(event, link, inert=_sanitize_reason, inert_name=_redact_explicit_urls)


def task_headline_mrkdwn(event: dict[str, Any], *, link: str | None = None) -> str | None:
    """`task_headline` for a Slack mrkdwn surface: untrusted fields escaped, and the identifier wearing
    `link` when the caller has a validated console url.

    `link` is not checked here and must already have passed both guards (`_console_card_link`);
    `build_task_line` re-checks the grammar half, so a refusal degrades to the bare id."""
    return _task_line(event, link, inert=inert_mrkdwn, inert_name=_escape_mrkdwn)


def _task_line(event: dict[str, Any], link: str | None, *, inert: Any, inert_name: Any) -> str | None:
    parts = task_parts(event)
    if parts is None:
        return None
    return notify_text.build_task_line(**parts, url=link, inert=inert, inert_name=inert_name, actor=_actor(event))


def format_message(event: dict[str, Any]) -> str:
    """Human-readable one-liner for the operator channel. Never includes a token, a config value, or the
    self-reported `node_id`. `reason`/`url` are untrusted: `url` is dropped unless it is a genuine
    `https://github.com/...` link, and every URL-shaped substring in `reason` is neutralized.

    Two headline shapes: `{project}|{id} {title}: {status}` for a task alert, `{owner/repo}: {status}`
    otherwise. No `[jaah]` prefix (Slack stamps the app name). Plain text throughout, since this also
    goes to the generic webhook; Slack legs re-render with `task_headline_mrkdwn`.

    The second line of a task alert is `question_body` alone (the title is in the headline); a task-less
    alert still renders `reason` whole."""
    alarm = _alarm_lines(event)
    if alarm is not None:
        # Escaped as well as URL-neutralized, unlike the rest of this function: CloudWatch text reaches a Slack
        # `text` here (the DM fallback, a Slack-shaped webhook), where `<!channel>` would ping. The url is ours.
        head, detail, url = alarm
        lines = [inert_mrkdwn(line) for line in (head, *detail.split("\n"))]
        return "\n".join([*lines, *([url] if url else [])])
    headline = task_headline(event)
    if headline is None:
        target = target_label(event)
        status = _status_word(event.get("kind"))
        head = f"{target}: {status}" if target else status
        detail = str(event["reason"]) if event.get("reason") else ""
    else:
        head = headline
        payload = event.get("payload")
        # Present-but-empty means "no question" (the headline is the whole message); a missing key means a
        # producer that never sends one, so its `reason` is kept rather than lost.
        detail = ""
        if isinstance(payload, dict):
            detail = (
                str(payload.get("question_body") or "")
                if "question_body" in payload
                else str(event.get("reason") or "")
            )
    parts = [head]
    if detail:
        parts.append(_sanitize_reason(detail))
    if event.get("url"):
        safe_url = _validate_notify_url(str(event["url"]))
        if safe_url:
            parts.append(safe_url)
    return "\n".join(parts)


# ── The counters ─────────────────────────────────────────────────────────────────────────────────────
# Every layer above is best-effort, so these counters are the raw material for a detector that does
# not exist yet (`GET /api/health/components` exposes them). Process-local, and zero is a quiet fleet's
# healthy state. The durable positive signal is `alerts_relay_failed_total > 0`: a failed relay
# withdraws its `alerts` row, so this and one warning are the only evidence.
_counter_lock = threading.Lock()
_counters = {"alerts_relayed_total": 0, "alerts_relay_failed_total": 0}
# One warning per process; the rest are `debug`.
_warned: set[str] = set()


def counters() -> dict[str, int]:
    with _counter_lock:
        return dict(_counters)


def _bump(name: str) -> None:
    with _counter_lock:
        _counters[name] = _counters.get(name, 0) + 1


def reset_counters() -> None:
    """Test seam only; the counters are process-local and never reset in production."""
    with _counter_lock:
        for key in _counters:
            _counters[key] = 0
    _warned.clear()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        log.debug(message, *args)
        return
    _warned.add(key)
    log.warning(message, *args)


def _urlopen(req: urllib.request.Request, *, timeout: float):
    """The single network-egress seam of the whole alert path: nothing else here opens a socket.

    A named attribute because the suite default-denies it (`tests/alert_channel_guard.py` replaces it
    autouse-wide). Resolves `urllib.request.urlopen` at call time, since `test_alert_relay.py` stubs the
    stdlib function; do not rebind at import time. Deliberately NOT shared with `slack_egress._urlopen`:
    `test_no_live_alert_channel_guard.py` requires each seam module to own its one egress.
    """
    return urllib.request.urlopen(req, timeout=timeout)


def _http_post(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> None:
    """JSON POST via stdlib urllib. Raises `AlertRelayError` on a transport error or a non-2xx (swallowing
    `HTTPError` made an outage look like a delivery).

    No exception leaving here may carry `url`: for a webhook the URL is the credential. So `Request` is
    built inside the `try` (it raises `ValueError` quoting the URL), the catch-all interpolates nothing and
    re-raises `from None`, and `URLError`/`OSError` use `reason` only, never `str(exc)`."""
    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with _urlopen(req, timeout=timeout) as resp:
            resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
            if not (200 <= int(status) < 300):
                raise AlertRelayError(f"alert channel returned HTTP {status}")
    except AlertRelayError:
        raise
    except urllib.error.HTTPError as exc:
        raise AlertRelayError(f"alert channel returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise AlertRelayError(f"alert channel unreachable: {reason}") from exc
    except Exception:
        raise AlertRelayError(
            "alert channel request could not be built or sent — the channel URL is probably malformed "
            "(check the scheme on ALERT_WEBHOOK_URL). The underlying error is "
            "withheld deliberately: it quotes the URL, and the URL carries the credential."
        ) from None


# ── The Slack arm's transport ────────────────────────────────────────────────────────────────────────
# Here and through `_urlopen`, structurally: `tests/test_no_live_alert_channel_guard.py` asserts every
# `urlopen` sits inside `_urlopen` and no second HTTP client is imported. A sibling of `_http_post`
# because this arm needs the body (`ok:false` on a 200) and `Retry-After`.
_SLACK_CHAT_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
# Per request, not per call: with the one 429 retry the worst case is 10 + 5 + 10 = 25s (asserted).
_SLACK_TIMEOUT_SECONDS = 10.0
# Tier-limited (~1/sec per channel); `Retry-After` is honoured up to this cap.
_SLACK_RETRY_AFTER_CAP_SECONDS = 5.0

# ── The suffix's caps, and the absolute one under them ───────────────────────────────────────────────
# An over-long `text` suppresses the alert on every channel (`msg_too_long` is `200 {"ok": false}`,
# no webhook fall-through, dedup row withdrawn), and `options` comes verbatim from
# `Task.awaiting_response_body`. These caps sit far below Slack's 40_000: the suffix annotates the alert.
_SLACK_OPTION_MAX_CHARS = 200
_SLACK_OPTIONS_TOTAL_MAX_CHARS = 1_000
_SLACK_OPTIONS_MAX_COUNT = 10
# A divergence would make the card and flat fallback offer different choices. `raise`, not `assert`
# (`python -O` strips asserts).
# The community edition has no card to disagree with.
if edition_features.enabled("slack"):
    if slack_blocks._MAX_BUTTONS != _SLACK_OPTIONS_MAX_COUNT:
        raise RuntimeError(
            f"slack_blocks._MAX_BUTTONS ({slack_blocks._MAX_BUTTONS}) != _SLACK_OPTIONS_MAX_COUNT "
            f"({_SLACK_OPTIONS_MAX_COUNT}) — the card and the flat-text fallback would disagree"
        )
# Slack's `text` limit, enforced in `_slack_chat_post` on the finished `message + suffix`.
_SLACK_TEXT_MAX_CHARS = 40_000
# Visible, so a truncated message is not answered as if whole.
_TRUNCATED_MARKER = "…(truncated — see the console)"


class SlackPlanDropped(Exception):
    """The recipient's org is a tenant whose plan leaves Slack out: the DM was dropped, never sent, and
    counted on the org (`slack_egress.record_plan_drop`). Not an `AlertRelayError`: a deliberate non-delivery, so
    `relay_alert` answers `False` rather than a failure."""


def _slack_post(
    url: str, token: str, payload: dict[str, Any], *, org_id: UUID | None,
    timeout: float = _SLACK_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, str], bytes]:
    """The body- and header-returning sibling of `_http_post`: `(status, lower-cased headers, raw body)`.

    `org_id` is the recipient's org, gated as `slack_egress.slack_api_post` gates it (`plan_allows`); a drop
    raises `SlackPlanDropped`. A non-2xx is returned, not raised, so the 429 retry can read `Retry-After`.
    No headers in errors: the bot token rides in `Authorization`; the catch-all interpolates nothing and
    re-raises `from None`."""
    try:
        allowed = slack_egress.plan_allows(org_id)
    except Exception:  # noqa: BLE001 - an unreadable plan is a failed relay (callers withdraw and fall back)
        raise AlertRelayError("could not read the recipient's plan") from None
    if not allowed:
        slack_egress.record_plan_drop(org_id)
        raise SlackPlanDropped(f"org {org_id}'s plan leaves Slack out")
    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with _urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = int(getattr(resp, "status", None) or resp.getcode())
            slack_egress.record_delivery(slack_egress.SENT)
            return status, _header_map(getattr(resp, "headers", None)), body
    except urllib.error.HTTPError as exc:
        # The 4xx/5xx path, returned rather than raised.
        try:
            body = exc.read()
        except Exception:  # a body that cannot be read is a body that says nothing — not a new failure
            body = b""
        return int(exc.code), _header_map(getattr(exc, "headers", None)), body
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise AlertRelayError(f"slack api unreachable: {reason}") from exc
    except Exception:
        raise AlertRelayError(
            "slack api request could not be built or sent. The underlying error is withheld "
            "deliberately: it quotes the request, and the request carries the bot token in its "
            "Authorization header."
        ) from None


def _header_map(headers: Any) -> dict[str, str]:
    """Response headers as a plain lower-cased dict. Case-insensitive lookup is required: Slack has
    changed `Retry-After`'s casing before."""
    if headers is None:
        return {}
    return {str(name).lower(): str(value) for name, value in headers.items()}


def _slack_chat_post(
    token: str, channel: str, text: str, blocks: list[dict[str, Any]] | None = None, *, org_id: UUID | None
) -> tuple[str, str]:
    """POST one Slack DM and return the `(channel, ts)` read from the response.

    We post to the `U…` member id but replies arrive on the `D…` conversation id, so the correlation row
    must use the response's channel.

    Every non-success raises `AlertRelayError` (non-2xx, non-object body, `ok is not True` since Slack
    rejects with `200 {"ok": false}`, or no usable `channel`/`ts`), so the caller never falls through
    to the webhook. One 429 retry. Never logs the token, member id or preferences.

    Every post carries `NO_UNFURL` (a request property, so all shapes inherit it). `blocks` is additive;
    with blocks, `text` is the notification fallback. Both layers enforce their own caps; `text` is capped
    here, after concatenation, truncating rather than raising."""
    if len(text) > _SLACK_TEXT_MAX_CHARS:
        # Never log the text (untrusted content), only its size.
        log.warning(
            "slack alert text was %s characters and Slack accepts at most %s — truncated before posting "
            "rather than letting the whole alert be rejected; the operator's message carries the marker",
            len(text), _SLACK_TEXT_MAX_CHARS,
        )
        # Re-sanitize after cutting: a cut can turn a spared filename (`evil.toml`) into a TLD (`evil.to`).
        room = _SLACK_TEXT_MAX_CHARS - len(_TRUNCATED_MARKER)
        # Sanitize, then hard-cut again: sanitizing can grow text. A sliced `[link removed]` is inert.
        text = _sanitize_reason(text[:room])[:room] + _TRUNCATED_MARKER
    # `NO_UNFURL` first, the same order as `slack_egress.slack_api_post`.
    body_payload: dict[str, Any] = {**NO_UNFURL, "channel": channel, "text": text}
    if blocks:
        # `text` stays with `blocks`: Slack uses it for push notifications and previews.
        body_payload["blocks"] = blocks
    status, headers, body = _slack_post(_SLACK_CHAT_POST_MESSAGE_URL, token, body_payload, org_id=org_id)
    if status == 429:
        # Exactly once, not looped: "retried once" is the 25s wall-clock contract.
        # `min(Retry-After, 5s)`, never negative; an absent or HTTP-date value takes the cap, not zero.
        time.sleep(http_bounded.retry_after_seconds(
            headers, absent=_SLACK_RETRY_AFTER_CAP_SECONDS, floor=0.0, cap=_SLACK_RETRY_AFTER_CAP_SECONDS
        ))
        status, headers, body = _slack_post(_SLACK_CHAT_POST_MESSAGE_URL, token, body_payload, org_id=org_id)

    if not (200 <= status < 300):
        raise AlertRelayError(f"slack chat.postMessage returned HTTP {status}")
    try:
        parsed = json.loads(body.decode("utf-8", "replace")) if body else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise AlertRelayError("slack chat.postMessage returned a 2xx whose body is not a JSON object")
    if parsed.get("ok") is not True:
        error = parsed.get("error") or "unspecified"
        if blocks and error == "invalid_blocks":
            # Degrade to flat text rather than lose the alert (a Slack failure never reaches the webhook).
            # One level deep: the retry passes no blocks.
            log.warning(
                "slack alert: Slack rejected the question card (invalid_blocks) — reposting as flat "
                "text so the alert is not lost on every channel"
            )
            return _slack_chat_post(token, channel, text, org_id=org_id)
        raise AlertRelayError(f"slack chat.postMessage rejected the call (error={error})")

    posted_channel, ts = parsed.get("channel"), parsed.get("ts")
    if not (isinstance(posted_channel, str) and posted_channel and isinstance(ts, str) and ts):
        # A receipt from a half-answer would claim a correlation row that can never match.
        raise AlertRelayError("slack chat.postMessage answered ok:true without a usable channel/ts")
    return posted_channel, ts


# What one inerted `issue` may spend on the brief branch. Generous for any real identifier, and it
# keeps the appended link inside `_SLACK_TEXT_MAX_CHARS` (`AlertIn.issue` has no `max_length`, and
# truncation would cut the link off).
_BRIEF_ISSUE_MAX_CHARS = 200

# The brief link's label when the headline already names the subject (see `_brief_text`). One word,
# per the UI copy standard.
_OPEN_LABEL = "Open"

# How to answer, a constant so the inbound copy cannot drift. Flat text only (`_slack_suffix`).
_SLACK_REPLY_HINT = "Reply in this thread to answer."
# One hint per card shape; each shape has a control. The footer fits one line
# (`slack_blocks._CONTEXT_TEXT_MAX`, 75 chars, clips silently); "above" is footer-relative.
_SLACK_CARD_PICK_HINT = "Tap an option, or type an answer in the box above."
# A tick answers nothing until Submit; "Submit answer" is the button's own label.
_SLACK_CARD_TICK_HINT = "Tick what applies, or type an answer, then Submit answer."
# Optionless: the box is the answer (label per `slack_blocks._act_buttons`).
_SLACK_CARD_TYPE_HINT = "Type your answer in the box above, then Submit answer."


def _capped_options(options: Any) -> tuple[list[str], bool]:
    """The options as they will ship, plus whether anything was dropped or shortened: at most
    `_SLACK_OPTIONS_MAX_COUNT`, each at most `_SLACK_OPTION_MAX_CHARS`, `_SLACK_OPTIONS_TOTAL_MAX_CHARS`
    across all (each bound covers a different blow-up).

    Capping runs before `_sanitize_reason` so the sanitizer sees what crosses the wire (a later cut could
    turn `x.yaml` into `x.ya`). Sanitizing can grow text, hence `_SLACK_TEXT_MAX_CHARS` one layer down."""
    kept: list[str] = []
    used = 0
    truncated = False
    for index, option in enumerate(options):
        if index >= _SLACK_OPTIONS_MAX_COUNT or used >= _SLACK_OPTIONS_TOTAL_MAX_CHARS:
            truncated = True
            break
        text = str(option)
        room = min(_SLACK_OPTION_MAX_CHARS, _SLACK_OPTIONS_TOTAL_MAX_CHARS - used)
        if len(text) > room:
            text = text[:room]
            truncated = True
        used += len(text)
        kept.append(text)
    return kept, truncated


def _slack_suffix(options: Any, *, answerable: bool) -> str:
    """The Slack-only tail appended after `format_message`'s output: the choices and how to answer.

    Every string goes through `_sanitize_reason`, options individually before joining (no token can
    splice across options), after capping. Plain text, no `mrkdwn`: markup is a second interpreter over
    untrusted text.

    `answerable` is the caller's decision: a thread reply answers a Task only when `alert_record` wrote
    a `slack_threads` row, and a hint that often fails stops being read. This module stays DB-free."""
    lines: list[str] = []
    capped, truncated = _capped_options(options)
    if capped:
        # Unnumbered: nothing inbound accepts "2" as an answer.
        line = "Options: " + " | ".join(_sanitize_reason(option) for option in capped)
        if truncated:
            line = f"{line} {_TRUNCATED_MARKER}"
        lines.append(line)
    if answerable:
        lines.append(_sanitize_reason(_SLACK_REPLY_HINT))
    # No suffix at all, so `format_message`'s output is the whole message.
    return "\n" + "\n".join(lines) if lines else ""


def _question_card(event: dict[str, Any], target: Any) -> list[dict[str, Any]] | None:
    """The Block Kit card for a Task question, or `None` for every other alert.

    Only a `blocked` alert about a Task carries `target.question`, so everything else posts flat text.
    `getattr` because the target is duck-typed and never imported (`SlackTarget` would drag `models`/`db`
    into this DB-free module; its suite runs `--noconftest`).

    Options are capped here so buttons and the flat `Options:` line match. The URL goes through
    `_validate_notify_url` before becoming a button `url:`; otherwise the card has no button.
    """
    question = getattr(target, "question", None)
    if question is None:
        return None
    capped, truncated = _capped_options(getattr(target, "options", ()) or ())
    # The hint moves with multi-select: a tap-to-answer hint on a multiple-choice card would be false.
    multi_select = bool(getattr(question, "multi_select", False))
    # Display-only: a click is resolved against the round's own stored options. `option_details` is
    # index-aligned with `capped` because `_capped_options` keeps a prefix; its budget is the renderer's
    # (`slack_blocks._OPTION_DETAIL_TOTAL_MAX`).
    option_details = getattr(question, "option_details", ()) or ()
    round_header = getattr(question, "header", None)
    url = event.get("url")
    # The shape comes from the renderer's own `rendered_options`: `_capped_options` keeps blanks it
    # drops, so an all-blank round is the free-text card.
    shown = slack_blocks.rendered_options(capped)
    return slack_blocks.build_question_blocks(
        task_id=question.task_id,
        task_title=question.title,
        question=question.body,
        question_id=getattr(question, "question_id", None),
        options=capped,
        options_truncated=truncated,
        target_label=_sanitize_reason(target_label(event)) or "task",
        url=_validate_notify_url(str(url)) if url else None,
        reply_hint=(
            (_SLACK_CARD_TICK_HINT if multi_select else _SLACK_CARD_PICK_HINT)
            if shown
            else _SLACK_CARD_TYPE_HINT
        ),
        sanitize=_sanitize_reason,
        multi_select=multi_select,
        option_details=option_details,
        round_header=round_header,
        org=_card_org(event),
        agent=_card_agent(event),
    )


def _brief_text(event: dict[str, Any], target: Any) -> str:
    """The whole message for a recipient who asked for `detail="brief"`: a headline and a link, and
    nothing they declined.

    Brief declines the content, not the subject: the headline names the Task's project and title, but
    the question body, options and reply hint are dropped (the Notifications panel promises "no question
    text, no answer buttons", `apps/web/src/components/NotificationsPanel.tsx`). Re-rendered through the
    same functions with the content removed, so the content cannot leak via a second format.

    The headline must identify the task (a task alert has no `issue`, so `target_label` names only the
    repo); the task id and console url come off the target's card. The url goes through `_console_link`,
    not the github-only `_validate_notify_url`. One line: the identifier wears the link, which also avoids
    a bare url Slack would unfurl. No suffix or card (decided by the caller); a thread reply is still
    honoured, just not advertised.
    """
    brief = dict(event)
    brief.pop("reason", None)
    # The question also rides in `payload["question_body"]`; drop it on the copy's own payload copy
    # (`_done_card`/`_question_card` read the original). Belt and braces, so a brief message cannot carry
    # content even if the composition below is rearranged.
    if isinstance(brief.get("payload"), dict):
        brief["payload"] = {
            key: value for key, value in brief["payload"].items() if key != "question_body"
        }
    # Whichever card this target carries (at most one); the assignment card carries `task_id`/`url` too.
    card = (
        getattr(target, "question", None)
        or getattr(target, "done", None)
        or getattr(target, "assigned", None)
    )
    task_id = getattr(card, "task_id", None) if card is not None else None
    # Only when the headline has no identifier: a producer `issue` already gives `owner/repo#123`.
    # `isinstance` because the card is duck-typed: a hand-built `task_id` string could forge a second link
    # from the label position.
    label = f"Task {task_id}" if isinstance(task_id, int) and not event.get("issue") else None
    # Both guards, single-homed in `_console_card_link`. A refusal degrades to no link, never raw text.
    safe_url = _console_card_link(target, event)
    if safe_url is not None:
        # One line, one destination: the console link supersedes the event's github `url`. Only when we have
        # one, or a cardless alert would lose its only link.
        brief.pop("url", None)
        # Inert the `issue` field (unvalidated by `AlertIn`): on one line an unterminated
        # `x<https://evil.test|` would swallow our console link. Only that field: inerting the whole line would
        # rewrite a `repo` like `owner/jaah.io` to `[link removed]`. On the copy, since cards render
        # `target_label` raw in `plain_text`.
        if brief.get("issue"):
            brief["issue"] = inert_mrkdwn(str(brief["issue"]), limit=_BRIEF_ISSUE_MAX_CHARS)
    # The identifier wears the link inside the headline (`task_headline_mrkdwn`, shared with the full
    # leg and cards). The `label` construction below serves events with no task headline (self-heal, a
    # node's `failed` over HTTP).
    headline = task_headline_mrkdwn(brief, link=safe_url)
    if headline is not None:
        if safe_url is not None:
            return headline
        # No console link, so the github url is kept, on its own line through the same allowlist as
        # `format_message`; the pop above only ran when `safe_url` existed.
        safe_event_url = _validate_notify_url(str(brief["url"])) if brief.get("url") else None
        return f"{headline}\n{safe_event_url}" if safe_event_url else headline
    text = format_message(brief)
    if safe_url is None:
        # A refused or absent url degrades to the headline alone, never raw text.
        return f"{text} — {label}" if label else text
    # One line with the identifier wearing the link, which also avoids an unfurlable bare url.
    # `<url|label>` is mrkdwn, right here but not in `format_message`, whose output also goes to the
    # generic webhook. Both halves are guarded: the url above, and the label (`Task <pk>` via `isinstance`,
    # or `_OPEN_LABEL`; a producer `issue` never reaches it). The headline's one producer field is inerted.
    return f"{text} — <{safe_url}|{label or _OPEN_LABEL}>"


def _card_project(event: dict[str, Any]) -> str | None:
    """The project name a card leads with, off the event's payload, never off the card.

    Through `task_parts` (the trusted in-process source, and one guard for "is this a task"), so
    `notify_targets` need not load a Project on the notify path. `None` is a real answer:
    `build_task_line` then starts at the identifier."""
    parts = task_parts(event)
    return parts["project"] if parts is not None else None


def _payload_str(payload: Any, key: str) -> str | None:
    """`payload[key]` when it is a string, else `None` — a trusted key read without trusting its type."""
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _card_agent(event: dict[str, Any]) -> str | None:
    """The SDK agent a card names, off the event's trusted payload, else `None` (a built-in runner's)."""
    return _payload_str(event.get("payload"), "agent")


def _card_org(event: dict[str, Any]) -> str | None:
    """The org name a card names, off the event's trusted payload (`task_parts`, as `_card_project`), else
    `None`. The question card reads it whether or not the payload authored a task headline."""
    parts = task_parts(event)
    if parts is not None:
        return parts["org"]
    payload = event.get("payload")
    org = payload.get("org") if isinstance(payload, dict) else None
    return org if isinstance(org, str) else None


def _console_card_link(target: Any, event: dict[str, Any] | None = None) -> str | None:
    """The console url off whichever card this target carries, through both guards, or `None`.

    The single home for the two-guard check used by three legs. Neither subsumes the other:
    `_console_link` answers "is this our url" (origin match against `frontend_url`), and
    `notify_text.link_url` answers "is it safe inside `<url|label>`", where `>` closes the token early
    and `|` hands the writer the label. `urlsplit` encodes nothing, so `<`, `>`, `|` and `Cf` bidi
    overrides survive the origin check inside a path.

    `getattr` throughout: the target is duck-typed (see `_question_card`); at most one card is present.
    A refusal by either guard degrades to no link, never raw text."""
    card = (
        getattr(target, "question", None)
        or getattr(target, "done", None)
        or getattr(target, "assigned", None)
    )
    url = getattr(card, "url", None) if card is not None else None
    if not url and event is not None:
        # No card, so the producer's own console URL (a mention, the needs-attention backstop). In-process
        # only (`_TRUSTED_ALERT_PAYLOAD_KEYS`), and it still takes both guards below.
        payload = event.get("payload")
        url = payload.get("console_url") if isinstance(payload, dict) else None
    return notify_text.link_url(_console_link(str(url))) if url else None


def _slack_flat_text(event: dict[str, Any], target: Any) -> str | None:
    """The whole cardless Slack message for a full-detail recipient, or `None` when the event is not about
    a Task and the caller should send `format_message`'s output unchanged.

    It differs from `format_message` in one field only (the identifier wears the console link, since this
    goes only to Slack), composed from the same functions so the recipients differ in what they omit,
    never in how they name the subject. The leg for a child `done`, a `failed`, and a cardless `blocked`;
    `_slack_suffix` still appends options and the reply hint."""
    # Accepted residual: the task id comes from the payload and the url from the card; both are built
    # off the same Task row by trusted in-process code. A second producer of either would need a cross-check.
    headline = task_headline_mrkdwn(event, link=_console_card_link(target, event))
    if headline is None:
        return _support_line(event, target)
    parts = [headline]
    payload = event.get("payload")
    body = str(payload.get("question_body") or "") if isinstance(payload, dict) else ""
    if body:
        parts.append(_sanitize_reason(body))
    # And the event's own URL, via the same allowlist, so "identical but for the link" holds for every
    # field (unreachable today; no task-bearing producer sends a `url`).
    if event.get("url"):
        safe_url = _validate_notify_url(str(event["url"]))
        if safe_url:
            parts.append(safe_url)
    return "\n".join(parts)


def _support_line(event: dict[str, Any], target: Any) -> str | None:
    """A support ticket DM (`durable/support_notify_queue.py`) or a billing notice's (`app/token_billing/
    alerts.py`): its one line with the page linked after it, or `None` for any other task-less event. The link
    takes `_console_card_link`'s two guards; a refused one degrades to the plain line (`format_message`), never
    raw text."""
    kind = event.get("kind")
    if kind not in ("support", "billing"):
        return None
    link = _console_card_link(target, event)
    if link is None:
        return None
    return f"{_status_word(kind)}: {_sanitize_reason(str(event.get('reason') or ''))} — <{link}|{_OPEN_LABEL}>"


def _done_card(event: dict[str, Any], target: Any) -> list[dict[str, Any]] | None:
    """The one-line Block Kit card for a shipped top-level Task, or a Task just assigned to the
    recipient; `None` for every other alert.

    `target.done` is populated only for a parentless Task's `done`, and `target.assigned` only by
    `resolve_slack_target(assigned=True)`; they are mutually exclusive by `alert_record`'s `kind`
    derivation, and `done` is asked first.

    `getattr` because the target is duck-typed (see `_question_card`). `inert_mrkdwn` is injected:
    `app/slack_blocks.py` imports nothing, and the title and assigner name are untrusted free text.
    `event` is kept for signature symmetry with `_question_card`.
    """
    done = getattr(target, "done", None)
    if done is not None:
        return slack_blocks.build_done_blocks(
            task_id=done.task_id,
            task_title=done.title,
            project=_card_project(event),
            status=_status_word(event.get("kind")),
            url=done.url,
            inert=inert_mrkdwn,
            inert_name=_escape_mrkdwn,
            org=_card_org(event),
            agent=_card_agent(event),
        )
    assigned = getattr(target, "assigned", None)
    if assigned is None:
        return None
    return slack_blocks.build_assigned_blocks(
        task_id=assigned.task_id,
        task_title=assigned.title,
        project=_card_project(event),
        status=_status_word(event.get("kind")),
        url=assigned.url,
        assigned_by=assigned.assigned_by,
        inert=inert_mrkdwn,
        inert_name=_escape_mrkdwn,
        org=_card_org(event),
    )


# ── The page: a CloudWatch alarm's state change (`app/alarm_relay.py`) ────────────────────────────────
# Read as a page, never as JSON: the state and the alarm's own description first, then which alarm and what
# it was, then CloudWatch's reason, then the console link. An `OK` is the resolution of an earlier page.
_ALARM_LINK_LABEL = "Open in CloudWatch"
_ALARM_NAME_MAX = 255
_ALARM_REASON_MAX = 1_000


def _alarm_fields(event: dict[str, Any]) -> dict[str, str] | None:
    """The alarm an `alarm` event carries, as strings, or `None` for any other event. The payload is
    in-process only (`AlertIn` refuses `PAGE_KINDS`); its text still came from CloudWatch, so every field is
    rendered inert."""
    if event.get("kind") not in PAGE_KINDS:
        return None
    payload = event.get("payload")
    alarm = payload.get("alarm") if isinstance(payload, dict) else None
    if not isinstance(alarm, dict):
        return None
    fields = {key: " ".join(str(alarm.get(key) or "").split()) for key in (
        "name", "description", "state", "previous_state", "reason", "console_url",
    )}
    return fields if fields["name"] and fields["state"] else None


def _alarm_lines(event: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """`(headline, detail, console url)` for an `alarm` event, plain text, or `None`.

    Headline: `<STATE> — <description>` (the name when the alarm has no description). Detail: the name and
    what it moved from (`resolved` for an `OK`), then CloudWatch's reason."""
    alarm = _alarm_fields(event)
    if alarm is None:
        return None
    state = alarm["state"].upper()
    name = notify_text._clip(alarm["name"], _ALARM_NAME_MAX)
    headline = f"{state} — {alarm['description'] or name}"
    previous = alarm["previous_state"].upper()
    if state == "OK":
        moved = f"resolved (was {previous})" if previous else "resolved"
    else:
        moved = f"was {previous}" if previous else ""
    detail = " · ".join(part for part in (name, moved) if part)
    if alarm["reason"]:
        detail = f"{detail}\n{notify_text._clip(alarm['reason'], _ALARM_REASON_MAX)}"
    return headline, detail, notify_text.link_url(alarm["console_url"]) if alarm["console_url"] else None


def _alarm_card(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The Block Kit page for an `alarm` event, or `None` for every other alert: a plain-text header
    (`<STATE> — <description>`), then one `mrkdwn` section with the detail and the console link."""
    lines = _alarm_lines(event)
    if lines is None:
        return None
    headline, detail, url = lines
    body = "\n".join(inert_mrkdwn(line) for line in detail.split("\n"))
    if url:
        body = f"{body}\n<{url}|{_ALARM_LINK_LABEL}>"
    return [
        {"type": "header", "text": slack_blocks._plain(slack_blocks._folded(headline), slack_blocks._HEADER_MAX)},
        {
            "type": "section",
            # `verbatim`: no auto-linking or mention parsing beyond our one console link.
            "text": {"type": "mrkdwn", "text": slack_blocks._clip(body, slack_blocks._SECTION_TEXT_MAX), "verbatim": True},
        },
    ]


def message(kind: str, reason: str | None = None, *, repo: str | None = None, issue: str | None = None,
            url: str | None = None, test: bool = False, payload: dict[str, Any] | None = None,
            **extra: Any) -> dict[str, Any]:
    """The event `relay_alert` and `email_notify` read: every key present, absent ones None. `extra` (e.g.
    `task_id`) sits before `payload`. Pure, so a durable step building it replays identically."""
    return {"kind": kind, "repo": repo, "issue": issue, "reason": reason, "url": url, "test": test,
            **extra, "payload": {} if payload is None else payload}


def is_test_flagged(event: dict[str, Any]) -> bool:
    """A synthetic alert must never reach the live operator channel. Checked on both shapes: a top-level
    `test` (`task_notify.py`, `AlertIn.test`) and one nested in `payload`."""
    if event.get("test"):
        return True
    payload = event.get("payload")
    return bool(isinstance(payload, dict) and payload.get("test"))


#: The `payload` key marking a question round asked while an earlier round of the same Task is still open: a
#: later question of the same `AskUserQuestion` batch (`task_notify._is_batch_follow_on`). In-process only
#: (`routes/agent/fleet_observe.py::_TRUSTED_ALERT_PAYLOAD_KEYS`).
BATCH_FOLLOW_ON_KEY = "batch_follow_on"


def brief_skips(event: dict[str, Any], detail: str) -> bool:
    """Does a recipient at `detail` hear nothing of this alert? A brief message carries no question text, so
    every round of a batch would read the same: a brief recipient is told once per batch, by its first round.
    Every channel asks this (`relay_alert`, `notify_targets.deliver_alert`)."""
    payload = event.get("payload")
    return detail == "brief" and isinstance(payload, dict) and payload.get(BATCH_FOLLOW_ON_KEY) is True


def relay_alert(event: dict[str, Any], *, target=None, receipt: dict | None = None) -> bool:
    """Deliver one alert to the operator-configured channel.

    True when a channel accepted it; False only for a deliberate, logged non-delivery (relay disabled,
    `test`-flagged, no channel). Raises `AlertRelayError` when a configured channel failed, which the
    caller must tell apart from "nothing to deliver" (`routes/agent/fleet_observe.py:create_alert`).

    `target`: a resolved Slack recipient or `None` (its `org_id` is plan-gated: off answers `False`),
    duck-typed and never imported (this module is DB-free,
    suite runs `--noconftest`). `None` falls through to the env-var channel; a failed Slack post does not
    (that would deliver twice).

    `receipt`: an out-parameter so the return stays exactly `bool` (`AlertOut.relayed`). Filled with both
    keys or neither, only after a 2xx `ok:true`. Blocking by design (sync route, threadpool)."""
    settings = get_settings()

    if not settings.alert_relay_enabled:
        # An opt-out is legitimate but must not be a silent `return`: say it once.
        _warn_once(
            "disabled",
            "alert relay disabled by ALERT_RELAY_ENABLED — alert not delivered to any operator channel "
            "(kind=%s repo=%s issue=%s)",
            event.get("kind"), event.get("repo"), event.get("issue"),
        )
        return False

    if is_test_flagged(event):
        log.warning(
            "alert relay: dropping test-flagged alert (kind=%s repo=%s issue=%s)",
            event.get("kind"), event.get("repo"), event.get("issue"),
        )
        return False

    message = format_message(event)

    # Step 1 — the pinned Slack recipient, ahead of both env-var channels.
    if target is not None:
        if not edition_features.enabled("slack"):
            # No Slack in this edition: a deliberate non-delivery, as a plan without Slack is (`SlackPlanDropped`).
            return False
        # The recipient's detail choice first: `"brief"` means no card, no options suffix, no reply hint
        # (see `_brief_text`). `getattr` so a hand-built target gets the full message.
        if brief_skips(event, getattr(target, "detail", "full")):
            # A deliberate non-delivery: the batch's first round already told this recipient.
            log.info(
                "alert relay: a later question of a batch — the brief recipient was told by its first "
                "(kind=%s repo=%s)", event.get("kind"), event.get("repo"),
            )
            return False
        if getattr(target, "detail", "full") == "brief":
            card, text = None, _brief_text(event, target)
        else:
            # Three mutually exclusive card shapes; `None` from every arm falls through to flat text.
            card = _question_card(event, target)
            if card is None:
                card = _done_card(event, target)
            if card is None:
                card = _alarm_card(event)
            # The Slack leg links the identifier; the webhook's `message` cannot. `_console_link` answers "is
            # this our url", `notify_text.link_url` "is it safe inside `<url|…>`"; a refusal degrades to the
            # unlinked headline.
            slack_text = _slack_flat_text(event, target)
            text = (slack_text if slack_text is not None else message) + _slack_suffix(
                target.options, answerable=target.answerable
            )
        try:
            # Two call shapes so a non-card call keeps its positional arity for test doubles.
            # `org_id`: the recipient's org, whose plan decides whether Slack is theirs (`_slack_post`).
            channel, ts = (
                _slack_chat_post(target.bot_token, target.member_id, text, card, org_id=target.org_id)
                if card is not None
                else _slack_chat_post(target.bot_token, target.member_id, text, org_id=target.org_id)
            )
        except SlackPlanDropped:
            # A deliberate non-delivery (the docstring's `False`), never a fall-through: the webhook is the
            # operator's channel, not this recipient's.
            log.info(
                "alert relay: Slack is not on the recipient's plan — alert not sent (kind=%s repo=%s issue=%s)",
                event.get("kind"), event.get("repo"), event.get("issue"),
            )
            return False
        except AlertRelayError:
            # A delivery failure is not a fall-through; the webhook arm runs only with no Slack recipient.
            _bump("alerts_relay_failed_total")
            raise
        if receipt is not None:
            receipt["channel"], receipt["ts"] = channel, ts
        _bump("alerts_relayed_total")
        return True  # a plain bool, deliberately — see the docstring on `receipt`

    if settings.alert_webhook_url:
        try:
            # `NO_UNFURL` here too: this channel is often Slack-shaped, and other receivers ignore the fields.
            _http_post(
                settings.alert_webhook_url,
                # `NO_UNFURL` first, as in `_slack_chat_post` (no keys overlap today).
                {**NO_UNFURL, "text": message, "event": event},
            )
        except AlertRelayError:
            _bump("alerts_relay_failed_total")
            raise
        _bump("alerts_relayed_total")
        return True

    # The drop that must be loud. Name the setting; never log the value, which may be the credential.
    _warn_once(
        "unconfigured",
        "alert relay has NO configured channel — alert NOT delivered (kind=%s repo=%s issue=%s). Either "
        "no user has a Slack notification method with a designated credential (Settings) or set "
        "ALERT_WEBHOOK_URL in the root .env; see infra/local/docker-compose.yml's api environment block.",
        event.get("kind"), event.get("repo"), event.get("issue"),
    )
    return False
