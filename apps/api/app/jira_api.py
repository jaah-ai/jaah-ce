"""Jira Cloud REST client: the control plane's one hand that reaches a Jira site.

Backs the console's task/issue link: typeahead lookup, reading an issue into `tasks.body` markdown,
and posting a back-reference comment. Sync `httpx`, like `app/github_api.py`.

`base_url` is operator-supplied, so every call is an SSRF surface and carries `app/http_bounded.py`'s
bounds: no redirects, identity encoding, a streamed body capped at 64 KiB, and an origin pin.

The credential is plaintext in this process (`models.Credential`): the api_token never reaches a log,
event payload or exception message, and no third-party response body is echoed into one.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app import http_bounded
from app.models.credentials import Credential
from app.credential_crypto import has_sealed_secret, open_secret
from app.credential_keys import NodeVaultUnavailable
from app.credentials.registry import CREDENTIAL_TYPES

log = logging.getLogger("jira-api")

# One budget per outbound call, `github_api`'s rather than the tester's 10.0s: a route here does at
# most two calls and a slow-but-working site must not read as broken.
_TIMEOUT_SECONDS = 15.0

# The tester's read cap: a bounded read is all that stands between an operator-named host and this
# single-worker process's memory.
_MAX_RESPONSE_BYTES = 64 * 1024

# Jira's own limit on a comment, measured over the ENTIRE serialized ADF (node names, attrs and
# brackets all count), not the visible text.
MAX_COMMENT_ADF_CHARS = 32_767

# Jira 429s do not always carry `Retry-After`, so we carry our own.
DEFAULT_RETRY_AFTER_SECONDS = 30
#: The most a `Retry-After` is believed; past it a rate limit reads as a refusal to wait for.
MAX_RETRY_AFTER_SECONDS = 3600

# Bound on what the typeahead puts on the wire and asks back.
_MAX_QUERY_CHARS = 200
_MAX_RESULTS = 50

# JQL has no prepared statements: every interpolated value is validated against a strict pattern and
# refused (never escaped) before being double-quoted. Project and issue keys are validated whatever
# their provenance.
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")

# Depth guard for the ADF walker: third-party JSON of arbitrary shape, so a pathologically nested
# document must flatten out rather than exhaust the stack.
_MAX_ADF_DEPTH = 40


class JiraCredentialInvalid(RuntimeError):
    """The credential row cannot be used at all — a missing/blank required field, an unusable base
    URL, a malformed project key. Distinct from an API failure so a caller reports a config error the
    operator can fix. NEVER echoes a stored value."""


class JiraApiError(RuntimeError):
    """The call reached Jira and Jira refused, or the transport failed.

    `status` is Jira's HTTP status when there was one (None for a transport failure), so a route maps
    it instead of re-deriving it from prose. `retry_after` is set only for a 429. The message is
    operator-actionable and carries neither the token nor a third-party response body."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class JiraSite(NamedTuple):
    """One Jira site plus the credential that reaches it. `base` is normalised (no trailing slash, no
    query/fragment) so a caller appends a path unconditionally; `project_key` is None for a plain
    site credential, which searches site-wide."""

    base: str
    email: str
    api_token: str
    project_key: Optional[str] = None

    @property
    def host(self) -> str:
        """The host alone, for a message: `base` may carry `user:pass@` userinfo by design."""
        return urlsplit(self.base).hostname or "the configured Jira site"

    @property
    def display_base(self) -> str:
        """`base` with userinfo stripped: the only form that may leave the server.

        A link we hand out reaches any `projects:view` holder, and userinfo in a browse URL authenticates
        nothing on Jira Cloud, so it would be pure leak. The port is kept for self-hosted sites.
        """
        parts = urlsplit(self.base)
        host = parts.hostname or ""
        netloc = f"{host}:{parts.port}" if parts.port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")


class JiraIssueRef(NamedTuple):
    """One issue as the typeahead knows it. `status` is None when the decoration call did not answer
    — the picker itself returns no status field."""

    key: str
    summary: str
    status: Optional[str] = None


# ── Credential → site ───────────────────────────────────────────────────────────────────────────────

def site_from_credential(credential: Credential) -> JiraSite:
    """The site a `jira` credential row names, or `JiraCredentialInvalid`.

    Fails CLOSED on anything unusable: a blank required field reaching the wire would be an
    unauthenticated call to an operator-named host. Field LABELS come from the registry, the single
    home for what a type's fields are called, so a message names the box the operator sees."""
    if getattr(credential, "type", None) != "jira":
        raise JiraCredentialInvalid(
            f"Credential {getattr(credential, 'id', '?')} is type "
            f"{getattr(credential, 'type', None)!r}, not \"jira\"."
        )
    config = getattr(credential, "config", None)
    if not isinstance(config, dict):
        raise JiraCredentialInvalid("Jira credential holds no configuration.")

    base_raw = _required(config, "base_url")
    base = _normalised_base(base_raw)
    if base is None or http_bounded.origin(base) is None:
        raise JiraCredentialInvalid(f"{_label('base_url')} is not a usable http(s) URL.")
    return JiraSite(
        base=base,
        email=_required(config, "email"),
        api_token=_required_secret(credential, config, "api_token"),
        project_key=_optional_project_key(config),
    )


def _label(key: str) -> str:
    spec = CREDENTIAL_TYPES["jira"]
    return next((field.label for field in spec.fields if field.key == key), key)


def _required(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise JiraCredentialInvalid(f"{_label(key)} is required on the Jira credential.")
    return value.strip()


def _required_secret(credential: Any, config: dict[str, Any], key: str) -> str:
    """A sealed field opened through the one seam (`credential_crypto.open_secret`) because it is about
    to go on the wire; absent, blank, or unopenable (a moved, re-pointed or tampered envelope) all fail
    closed with the field's label and never its value."""
    if not has_sealed_secret(config, key):
        raise JiraCredentialInvalid(f"{_label(key)} is required on the Jira credential.")
    try:
        value = open_secret(credential, key)
    except NodeVaultUnavailable:
        # D1: the control plane holds no NodeVault key; say so rather than "re-enter it".
        raise JiraCredentialInvalid("NodeVault credential — not available to Jaah") from None
    except ValueError:
        raise JiraCredentialInvalid(
            f"{_label(key)} on the Jira credential cannot be opened; re-enter it."
        ) from None
    if not value.strip():
        raise JiraCredentialInvalid(f"{_label(key)} is required on the Jira credential.")
    return value.strip()


def _optional_project_key(config: dict[str, Any]) -> Optional[str]:
    value = config.get("project_key")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not _PROJECT_KEY_RE.match(value.strip()):
        # The pattern, not the value: a rejected key is interpolated into no message either, since
        # the same string was about to be interpolated into JQL.
        raise JiraCredentialInvalid(
            f"{_label('project_key')} must be letters, digits and underscores starting with a letter."
        )
    return value.strip()


def _normalised_base(raw: str) -> Optional[str]:
    """`raw` with query, fragment and trailing slash dropped, or None when it is not http(s).

    A stored `https://host/?x=1` would otherwise build `…/?x=1/rest/api/3/…`: a garbage path answered
    with a 404 that reads as the operator's fault. `netloc` is kept verbatim (userinfo and port
    included) so the request goes exactly where the operator pointed it."""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")).rstrip("/")


# ── The bounded client ──────────────────────────────────────────────────────────────────────────────

def _client() -> httpx.Client:
    """The one client every call here uses — `http_bounded`'s policy (it says why), this timeout."""
    return http_bounded.client(_TIMEOUT_SECONDS, {"Accept": "application/json"})


class _Reply(NamedTuple):
    status: int
    parsed: Any
    retry_after: int


def _request(
    site: JiraSite,
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
) -> _Reply:
    """One bounded, origin-pinned call to `site`, or `JiraApiError` for a transport failure.

    The origin pin is a refusal: one path splice (`@evil.example/x`) would send the credential elsewhere.
    A body that filled the cap (`http_bounded.read_capped`) is unreadable, never parsed from a prefix.
    """
    url = f"{site.base}{path}"
    pinned = http_bounded.origin(site.base)
    if pinned is None or http_bounded.origin(url) != pinned:
        log.error("jira request refused: url origin does not match the credential's base URL "
                  "(url_origin=%s pinned=%s)", http_bounded.origin(url), pinned)
        raise JiraApiError(
            f"Refused to call {site.host}: the request URL leaves the credential's base URL.")
    auth = httpx.BasicAuth(site.email, site.api_token)
    try:
        with _client() as client, client.stream(
            method, url, params=params, json=body, auth=auth
        ) as response:
            raw, truncated = http_bounded.read_capped(response, _MAX_RESPONSE_BYTES)
            return _Reply(
                status=response.status_code,
                parsed=None if truncated else http_bounded.json_or_none(raw),
                # The one `Retry-After` parser (`http_bounded`); Jira's default when absent, capped so a hostile
                # value cannot park the caller. Only a 429 reads it (`_raise_for_status`).
                retry_after=int(http_bounded.retry_after_seconds(
                    response.headers, absent=DEFAULT_RETRY_AFTER_SECONDS, floor=0, cap=MAX_RETRY_AFTER_SECONDS)),
            )
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, UnicodeError) as exc:
        # The exception CLASS, never `str(exc)`: httpx renders the URL into its message, and an
        # operator's base URL may legitimately carry `user:pass@` userinfo.
        raise JiraApiError(
            f"Could not reach {site.host} ({type(exc).__name__}) — check the Jira Base URL and this "
            "server's network access."
        ) from exc


def _raise_for_status(reply: _Reply, subject: str) -> None:
    """Map a non-2xx to `JiraApiError`. No response body is quoted: Jira's error prose echoes the
    request back, and this module's requests carry the operator's own values."""
    if 200 <= reply.status < 300:
        return
    if reply.status == 429:
        raise JiraApiError(
            f"Jira rate-limited {subject}; retry in {reply.retry_after}s.", status=429, retry_after=reply.retry_after)
    if reply.status in (401, 403):
        raise JiraApiError(
            f"Jira refused {subject} (HTTP {reply.status}) — check that the credential's account "
            "email and API token are current and that the account may perform it.",
            status=reply.status)
    if reply.status == 404:
        # Jira DELIBERATELY conflates "no such issue" with "no permission", so the message must not
        # claim the issue does not exist.
        raise JiraApiError(
            f"Jira returned 404 for {subject} — either it does not exist or the credential's account "
            "cannot see it; Jira answers the same for both.", status=404)
    if reply.status == 400:
        raise JiraApiError(
            f"Jira rejected {subject} as invalid (HTTP 400) — check the Project key on the "
            "credential and the issue key.", status=400)
    raise JiraApiError(f"Jira returned HTTP {reply.status} for {subject}.", status=reply.status)


# ── Typeahead ───────────────────────────────────────────────────────────────────────────────────────

def search_issues(site: JiraSite, query: str, *, limit: int = 15) -> list[JiraIssueRef]:
    """Issues matching `query`, via `GET /rest/api/3/issue/picker`.

    The picker, not JQL, because JQL cannot prefix-match an issue key. `POST /rest/api/3/search` is gone
    (410). Sections are flattened and deduped by key in first-seen order; `summaryText` is plain where
    `summary` may carry highlight markup.
    """
    text = (query or "").strip()
    if not text or limit <= 0:
        return []
    params: dict[str, Any] = {"query": text[:_MAX_QUERY_CHARS], "showSubTasks": "true"}
    if site.project_key:
        params["currentJQL"] = f"project = {_jql_quoted(site.project_key)}"
    reply = _request(site, "GET", "/rest/api/3/issue/picker", params=params)
    _raise_for_status(reply, "the issue search")
    return _decorated_with_status(site, _picker_refs(reply.parsed, min(limit, _MAX_RESULTS)))


def _picker_refs(parsed: Any, limit: int) -> list[JiraIssueRef]:
    refs: list[JiraIssueRef] = []
    seen: set[str] = set()
    sections = parsed.get("sections") if isinstance(parsed, dict) else None
    for section in sections if isinstance(sections, list) else []:
        issues = section.get("issues") if isinstance(section, dict) else None
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, dict):
                continue
            key = issue.get("key")
            if not isinstance(key, str) or not key.strip() or key in seen:
                continue
            summary = issue.get("summaryText")
            if not isinstance(summary, str):
                summary = issue.get("summary")
            seen.add(key)
            refs.append(JiraIssueRef(key=key, summary=summary if isinstance(summary, str) else ""))
            if len(refs) >= limit:
                return refs
    return refs


def _decorated_with_status(site: JiraSite, refs: list[JiraIssueRef]) -> list[JiraIssueRef]:
    """`refs` with `status` filled from one `POST /rest/api/3/search/jql`, best-effort.

    Any failure returns the refs with `status=None` rather than failing the lookup. `fields` must be
    requested (the endpoint defaults to `id`) and the JQL must be bounded, so no bare `order by`.
    """
    keys = [ref.key for ref in refs if _ISSUE_KEY_RE.match(ref.key)]
    if not keys:
        return refs
    jql = "key in (" + ", ".join(_jql_quoted(key) for key in keys) + ")"
    try:
        reply = _request(site, "POST", "/rest/api/3/search/jql",
                         body={"jql": jql, "fields": ["status"], "maxResults": len(keys)})
        if not 200 <= reply.status < 300:
            log.warning("jira status decoration returned HTTP %s — issues keep status=None",
                        reply.status)
            return refs
        names = _status_names(reply.parsed)
    except Exception as exc:  # noqa: BLE001 - see the docstring: decoration must never fail a lookup
        log.warning("jira status decoration failed (%s) — issues keep status=None",
                    type(exc).__name__)
        return refs
    return [ref._replace(status=names.get(ref.key)) for ref in refs]


def _status_names(parsed: Any) -> dict[str, str]:
    """`{key: status name}` from a `search/jql` body. That envelope has no `total` and no `startAt` —
    it pages by `nextPageToken`/`isLast` — and nothing here pages: one bounded `key in (...)` asks
    for every key at once."""
    issues = parsed.get("issues") if isinstance(parsed, dict) else None
    names: dict[str, str] = {}
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
            continue
        fields = issue.get("fields")
        status = fields.get("status") if isinstance(fields, dict) else None
        name = status.get("name") if isinstance(status, dict) else None
        if isinstance(name, str) and name.strip():
            names[issue["key"]] = name
    return names


def _jql_quoted(value: str) -> str:
    """A validated identifier wrapped in double quotes, for the one JQL string this module builds.

    Validation, not escaping: the value must already be a project or issue key, so anything carrying
    a quote, a backslash or a JQL operator is a bug or an attack and is refused rather than
    sanitised into something that runs."""
    if not (_PROJECT_KEY_RE.match(value) or _ISSUE_KEY_RE.match(value)):
        raise JiraApiError("Refused to build a JQL query from a value that is not a Jira key.",
                          status=400)
    return f'"{value}"'


def _validated_issue_key(key: Any) -> str:
    """`key` as an issue key, or `JiraApiError` — the guard on everything this module puts in a URL
    path or in JQL. `status=400` so a route answers the caller that supplied it, rather than
    reporting a Jira outage."""
    if isinstance(key, str) and _ISSUE_KEY_RE.match(key.strip()):
        return key.strip()
    raise JiraApiError("Not a Jira issue key (expected e.g. \"OAM-123\").", status=400)


# ── Read one issue ──────────────────────────────────────────────────────────────────────────────────

def get_issue(site: JiraSite, key: str) -> tuple[str, str]:
    """`(summary, description as markdown)` for one issue.

    `fields=summary,description` only — the full issue is a large document and nothing here reads the
    rest. On v3 `description` is ADF JSON (or null), so it goes through `adf_to_markdown`; an absent
    or null description is `""`, never a placeholder."""
    issue_key = _validated_issue_key(key)
    reply = _request(site, "GET", f"/rest/api/3/issue/{issue_key}",
                     params={"fields": "summary,description"})
    _raise_for_status(reply, f"issue {issue_key}")
    fields = reply.parsed.get("fields") if isinstance(reply.parsed, dict) else None
    fields = fields if isinstance(fields, dict) else {}
    summary = fields.get("summary")
    return (summary if isinstance(summary, str) else ""), adf_to_markdown(fields.get("description"))


def issue_browse_url(site: JiraSite, key: str) -> str:
    """The human URL for an issue — what a comment or a console link points at.

    `display_base`, never `base`: this string is HANDED OUT, and `base` may carry `user:pass@`."""
    return f"{site.display_base}/browse/{_validated_issue_key(key)}"


# ── Post a comment ──────────────────────────────────────────────────────────────────────────────────

def post_comment(site: JiraSite, key: str, *, text: str, link_label: str, link_url: str) -> None:
    """Post one paragraph: `text`, a space, then `link_label` hyperlinked to `link_url`.

    v3 requires ADF; success is 201. Jira's 32,767-character cap covers the serialized ADF and is checked
    before the request, so an oversized body is an actionable refusal here.
    """
    issue_key = _validated_issue_key(key)
    doc = _comment_doc(text, link_label, link_url)
    serialized = json.dumps(doc, ensure_ascii=False)
    if len(serialized) > MAX_COMMENT_ADF_CHARS:
        # The one pre-flight `JiraApiError`, `status=400` because it is what Jira would answer and
        # what a route should answer: the caller's text is too long, and shortening it is the fix.
        raise JiraApiError(
            f"Comment is too long for Jira: {len(serialized)} characters of formatted content "
            f"exceeds the {MAX_COMMENT_ADF_CHARS} limit. Shorten the text.", status=400)
    reply = _request(site, "POST", f"/rest/api/3/issue/{issue_key}/comment", body={"body": doc})
    if reply.status != 201:
        _raise_for_status(reply, f"the comment on {issue_key}")
        raise JiraApiError(
            f"Jira accepted the comment on {issue_key} with HTTP {reply.status}, not 201 — treating "
            "it as unposted.", status=reply.status)


def _comment_doc(text: str, link_label: str, link_url: str) -> dict[str, Any]:
    """The ADF document `post_comment` sends. A link is a text node carrying a `link` mark, not
    markdown and not HTML. The href is checked to be http(s) here: a `javascript:` URL in a comment
    Jira renders is a stored-XSS gift, and no caller of ours has a reason to send one."""
    if urlsplit(link_url).scheme not in ("http", "https"):
        raise JiraApiError("A comment link must be an http(s) URL.", status=400)
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    if link_label:
        if content:
            content.append({"type": "text", "text": " "})
        content.append({
            "type": "text",
            "text": link_label,
            "marks": [{"type": "link", "attrs": {"href": link_url}}],
        })
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": content}]}


# ── ADF → markdown ──────────────────────────────────────────────────────────────────────────────────
# Markdown, because `tasks.body` stores markdown. Input is third-party JSON of arbitrary shape: no key
# is assumed, `content` may not be a list, and an unknown node type recurses so its text survives.

_INLINE_TYPES = {"text", "hardBreak", "mention", "emoji", "date", "inlineCard", "status"}
_LIST_TYPES = {"bulletList", "orderedList"}

# Applied innermost-first, so `**`code`**` nests the way markdown reads it.
_MARK_WRAPPERS = (("code", "`"), ("strike", "~~"), ("em", "_"), ("strong", "**"))


def adf_to_markdown(node: Any) -> str:
    """An Atlassian Document Format document rendered as markdown. `None`, a non-document, or a
    document whose content is unreadable all yield `""` — never a partial-looking placeholder."""
    if node is None:
        return ""
    if isinstance(node, list):
        return _blocks(node, 1).strip()
    return _block(node, 0).strip()


def _content(node: Any) -> list[Any]:
    items = node.get("content") if isinstance(node, dict) else None
    return items if isinstance(items, list) else []


def _attrs(node: Any) -> dict[str, Any]:
    attrs = node.get("attrs") if isinstance(node, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def _blocks(nodes: list[Any], depth: int) -> str:
    rendered = [_block(child, depth) for child in nodes]
    return "\n\n".join(part for part in rendered if part.strip())


def _block(node: Any, depth: int) -> str:
    """One block-level node as markdown. Inline types are delegated to `_inline`, which is what lets
    an unknown wrapper recurse blindly: whatever its children are, they render."""
    if depth > _MAX_ADF_DEPTH or not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind in _INLINE_TYPES:
        return _inline(node, depth)
    if kind == "doc":
        return _blocks(_content(node), depth + 1)
    if kind == "paragraph":
        return _inlines(node, depth)
    if kind == "heading":
        level = _attrs(node).get("level")
        level = level if isinstance(level, int) and 1 <= level <= 6 else 1
        return f"{'#' * level} {_inlines(node, depth)}".rstrip()
    if kind in _LIST_TYPES:
        return _list(node, depth, ordered=kind == "orderedList")
    if kind == "listItem":
        return _item_body(node, depth + 1)
    if kind == "codeBlock":
        language = _attrs(node).get("language")
        fence = language if isinstance(language, str) and language.strip() else ""
        return f"```{fence}\n{_plain(node, depth)}\n```"
    if kind == "blockquote":
        inner = _blocks(_content(node), depth + 1)
        return "\n".join(f"> {line}".rstrip() for line in inner.split("\n"))
    if kind == "rule":
        return "---"
    if kind == "table":
        return _table(node, depth)
    if kind in ("tableRow", "tableCell", "tableHeader"):
        return _blocks(_content(node), depth + 1)
    children = _content(node)
    if children and all(isinstance(c, dict) and c.get("type") in _INLINE_TYPES for c in children):
        return _inlines(node, depth)
    return _blocks(children, depth + 1)


def _inlines(node: Any, depth: int) -> str:
    return "".join(_inline(child, depth + 1) for child in _content(node))


def _inline(node: Any, depth: int) -> str:
    """One inline node as markdown."""
    if depth > _MAX_ADF_DEPTH or not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = _attrs(node)
    if kind == "text":
        text = node.get("text")
        return _marked(text, node.get("marks")) if isinstance(text, str) else ""
    if kind == "hardBreak":
        return "  \n"
    if kind == "mention":
        name = attrs.get("text") or attrs.get("displayName")
        name = name.strip() if isinstance(name, str) else ""
        return "" if not name else (name if name.startswith("@") else f"@{name}")
    if kind == "emoji":
        for candidate in (attrs.get("shortName"), attrs.get("text")):
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return ""
    if kind == "date":
        return _date(attrs.get("timestamp"))
    if kind == "inlineCard":
        url = attrs.get("url")
        return url if isinstance(url, str) else ""
    inner = "".join(_inline(child, depth + 1) for child in _content(node))
    if inner:
        return inner
    # An unknown inline node with no children still usually carries its own label (a `status`
    # lozenge, for one), and emitting it beats dropping it.
    label = attrs.get("text")
    return label if isinstance(label, str) else ""


def _marked(text: str, marks: Any) -> str:
    if not text:
        return ""
    names = {m.get("type") for m in marks if isinstance(m, dict)} if isinstance(marks, list) else set()
    for name, wrapper in _MARK_WRAPPERS:
        if name in names:
            text = f"{wrapper}{text}{wrapper}"
    href = next(
        (_attrs(m).get("href") for m in marks if isinstance(m, dict) and m.get("type") == "link"),
        None,
    ) if isinstance(marks, list) else None
    return f"[{text}]({href})" if isinstance(href, str) and href else text


def _date(timestamp: Any) -> str:
    """An ADF date as `YYYY-MM-DD`. `attrs.timestamp` is epoch MILLISECONDS as a string; anything
    unparseable falls back to whatever was there, so no information is invented or lost."""
    raw = str(timestamp) if timestamp is not None else ""
    try:
        return datetime.fromtimestamp(int(raw) / 1000, timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError, OSError):
        return raw


def _plain(node: Any, depth: int) -> str:
    """Descendant text with no markdown applied — for a code block, where a `**` from a mark would
    become part of the code."""
    if depth > _MAX_ADF_DEPTH or not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    return "".join(_plain(child, depth + 1) for child in _content(node))


def _list(node: Any, depth: int, *, ordered: bool) -> str:
    lines = []
    for index, item in enumerate(_content(node), start=1):
        marker = f"{index}. " if ordered else "- "
        body = _item_body(item, depth + 1)
        pad = " " * len(marker)
        first, *rest = (body or "").split("\n")
        chunk = marker + first
        for line in rest:
            chunk += "\n" + (pad + line if line.strip() else "")
        lines.append(chunk.rstrip())
    return "\n".join(line for line in lines if line.strip())


def _item_body(item: Any, depth: int) -> str:
    """One list item's children. A NESTED LIST hugs its parent item with a single newline while other
    blocks keep the blank line: a blank line before a nested list makes it a loose list, which
    renders as extra paragraphs."""
    out = ""
    for child in _content(item):
        rendered = _block(child, depth + 1)
        if not rendered.strip():
            continue
        separator = "\n" if isinstance(child, dict) and child.get("type") in _LIST_TYPES else "\n\n"
        out = rendered if not out else f"{out}{separator}{rendered}"
    return out


def _table(node: Any, depth: int) -> str:
    """A GFM pipe table. The first row is the header whatever its cells are typed as — GFM has no
    headerless table, and inventing an empty header row would print a blank band."""
    rows = [r for r in _content(node) if isinstance(r, dict) and r.get("type") == "tableRow"]
    grid = [[_cell(c, depth + 1) for c in _content(row) if isinstance(c, dict)] for row in rows]
    grid = [row for row in grid if row]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    lines = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in grid]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def _cell(cell: Any, depth: int) -> str:
    text = _blocks(_content(cell), depth + 1)
    return text.replace("|", "\\|").replace("\n", " ").strip()
