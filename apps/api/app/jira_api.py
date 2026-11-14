"""Jira Cloud REST client: the control plane's one hand that reaches a Jira site.

Backs the console's task/issue link (typeahead lookup, reading an issue into `tasks.body` markdown,
posting a back-reference comment) and the Jira tracker adapter (`app/trackers/jira.py`: who the
credential's account is, the site's projects, markdown comments, moving an issue to a status). Sync
`httpx`, like `app/github_api.py`.

`base_url` is operator-supplied, so every call is an SSRF surface and carries `app/http_bounded.py`'s
bounds: no redirects, identity encoding, a streamed body capped at 64 KiB, an origin pin, and a dial only at the
base URL's public addresses (`http_bounded.vetted_addresses`, then a client pinned to them), so an `http://` site or
one that resolves to a private or reserved address is refused before any request is sent.

The credential is plaintext in this process (`models.Credential`): the api_token never reaches a log,
event payload or exception message, and no third-party response body is echoed into one.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, NamedTuple, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from app import http_bounded
from app.clock import epoch_ms_or_none
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
MAX_ADF_DEPTH = 40


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
    # A Jira Forge app's site: `base` is Atlassian's API gateway for the site (`api.atlassian.com/ex/jira/<cloud id>`),
    # `api_token` the app's system token sent as a bearer token, and `browse_base` the site's own address links use.
    bearer: bool = False
    browse_base: Optional[str] = None

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
        parts = urlsplit(self.browse_base or self.base)
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


def project_key_of(config: Any) -> Optional[str]:
    """A Jira credential's project key (OAM in OAM-123) when it names a valid one, else None."""
    try:
        return _optional_project_key(config) if isinstance(config, dict) else None
    except JiraCredentialInvalid:
        return None


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
    """`raw` with query, fragment and trailing slash dropped, or None when it is not http(s) (an http:// site
    parses, and `_request` then refuses it: only a public https:// site is dialled).

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

def _client(host: str, addresses: tuple[str, ...]) -> httpx.Client:
    """The one client every call here uses — `http_bounded`'s policy (it says why), this timeout — dialing only
    `addresses` (what `http_bounded.vetted_addresses` passed) for `host`, so DNS rebinding cannot reach past the vet."""
    return http_bounded.pin(http_bounded.client(_TIMEOUT_SECONDS, {"Accept": "application/json"}), host, addresses)


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
    So is the address vet: a base URL that is not https, or whose host resolves to any private or reserved
    address, is refused with one sentence that never says which (no DNS oracle).
    A body that filled the cap (`http_bounded.read_capped`) is unreadable, never parsed from a prefix.
    """
    url = f"{site.base}{path}"
    pinned = http_bounded.origin(site.base)
    if pinned is None or http_bounded.origin(url) != pinned:
        log.error("jira request refused: url origin does not match the credential's base URL "
                  "(url_origin=%s pinned=%s)", http_bounded.origin(url), pinned)
        raise JiraApiError(
            f"Refused to call {site.host}: the request URL leaves the credential's base URL.")
    refusal, addresses = http_bounded.vetted_addresses(url)
    if refusal is not None or not addresses:
        log.info("jira call refused: %s", refusal or "no address")
        raise JiraApiError("Jira can't be reached at that address. Use the site's public https:// address.")
    auth = None if site.bearer else httpx.BasicAuth(site.email, site.api_token)
    headers = {"Authorization": f"Bearer {site.api_token}"} if site.bearer else None
    try:
        with _client(httpx.URL(url).raw_host.decode("ascii"), addresses) as client, client.stream(
            method, url, params=params, json=body, auth=auth, headers=headers
        ) as response:
            # A wall-clock bound on the whole read: the client's timeout bounds each read, not a drip.
            deadline = time.monotonic() + _TIMEOUT_SECONDS
            raw, truncated = http_bounded.read_capped(response, _MAX_RESPONSE_BYTES,
                                                      stop=lambda: time.monotonic() > deadline)
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

def search_issues(site: JiraSite, query: str, *, project_keys: Optional[Sequence[str]] = None,
                  limit: int = 15) -> list[JiraIssueRef]:
    """Issues matching `query`, via `GET /rest/api/3/issue/picker`.

    The picker, not JQL, because JQL cannot prefix-match an issue key. `POST /rest/api/3/search` is gone
    (410). Sections are flattened and deduped by key in first-seen order; `summaryText` is plain where
    `summary` may carry highlight markup. `project_keys` scopes it (`currentJQL = project in (…)`); None searches
    site-wide, an empty list finds nothing without a call. The picker's history section is not narrowed by
    `currentJQL`, so a caller that must stay inside its projects filters the answer too.
    """
    text = (query or "").strip()
    if not text or limit <= 0 or (project_keys is not None and not project_keys):
        return []
    params: dict[str, Any] = {"query": text[:_MAX_QUERY_CHARS], "showSubTasks": "true"}
    if project_keys:
        params["currentJQL"] = "project in (" + ", ".join(_jql_quoted(key) for key in project_keys) + ")"
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

class JiraIssue(NamedTuple):
    """One issue as `read_issue` reads it; `project_key` is "" when Jira names no project."""

    key: str
    summary: str
    description: str
    project_key: str


def get_issue(site: JiraSite, key: str) -> tuple[str, str]:
    """`(summary, description as markdown)` for one issue (`read_issue`)."""
    issue = read_issue(site, key)
    return issue.summary, issue.description


def read_issue(site: JiraSite, key: str) -> JiraIssue:
    """One issue's key, summary, description as markdown, and project key.

    `fields=summary,description,project` only — the full issue is a large document and nothing here reads the
    rest. On v3 `description` is ADF JSON (or null), so it goes through `adf_to_markdown`; an absent
    or null description is `""`, never a placeholder."""
    issue_key = _validated_issue_key(key)
    reply = _request(site, "GET", f"/rest/api/3/issue/{issue_key}",
                     params={"fields": "summary,description,project"})
    _raise_for_status(reply, f"issue {issue_key}")
    parsed = reply.parsed if isinstance(reply.parsed, dict) else {}
    fields = parsed.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    summary = fields.get("summary")
    project = fields.get("project")
    project_key = project.get("key") if isinstance(project, dict) else None
    answered_key = parsed.get("key")
    return JiraIssue(
        key=answered_key if isinstance(answered_key, str) and _ISSUE_KEY_RE.match(answered_key) else issue_key,
        summary=summary if isinstance(summary, str) else "",
        description=adf_to_markdown(fields.get("description")),
        project_key=project_key if isinstance(project_key, str) else "",
    )


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
    post_comment_doc(site, key, _comment_doc(text, link_label, link_url))


def post_comment_doc(site: JiraSite, key: str, doc: dict[str, Any]) -> None:
    """Post one ADF comment document (`post_comment`'s paragraph, or `markdown_to_adf`'s)."""
    issue_key = _validated_issue_key(key)
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


# ── The tracker adapter's calls ─────────────────────────────────────────────────────────────────────

class JiraAccount(NamedTuple):
    account_id: str
    display_name: str


def myself(site: JiraSite) -> JiraAccount:
    """The account the credential signs in as (`GET /rest/api/3/myself`): the Jaah user issues are
    assigned to and mention."""
    reply = _request(site, "GET", "/rest/api/3/myself")
    _raise_for_status(reply, "the credential's own account")
    parsed = reply.parsed if isinstance(reply.parsed, dict) else {}
    account_id = parsed.get("accountId")
    if not isinstance(account_id, str) or not account_id.strip():
        raise JiraApiError("Jira answered the account lookup without an account id.", status=reply.status)
    name = parsed.get("displayName")
    return JiraAccount(account_id.strip(), name if isinstance(name, str) else "")


def server_base(site: JiraSite) -> Optional[str]:
    """The site's own address (`GET /rest/api/3/serverInfo`'s `baseUrl`, `https://acme.atlassian.net`): where a
    Forge site's browse links point, its gateway `base` being no address a person opens. None when Jira names none."""
    reply = _request(site, "GET", "/rest/api/3/serverInfo")
    _raise_for_status(reply, "the site's address")
    parsed = reply.parsed if isinstance(reply.parsed, dict) else {}
    value = parsed.get("baseUrl")
    return _normalised_base(value) if isinstance(value, str) else None


# `project/search` pages; each project carries its avatar URLs, so a page this size stays inside the read cap.
_PROJECT_PAGE = 50
_MAX_PROJECT_PAGES = 10


def list_projects(site: JiraSite) -> list[tuple[str, str]]:
    """`(key, name)` of every project the account can browse, by name, at most
    `_PROJECT_PAGE * _MAX_PROJECT_PAGES` (`GET /rest/api/3/project/search`)."""
    out: list[tuple[str, str]] = []
    for page in range(_MAX_PROJECT_PAGES):
        reply = _request(site, "GET", "/rest/api/3/project/search", params={
            "startAt": page * _PROJECT_PAGE, "maxResults": _PROJECT_PAGE, "orderBy": "name",
        })
        _raise_for_status(reply, "the project list")
        parsed = reply.parsed if isinstance(reply.parsed, dict) else {}
        values = parsed.get("values")
        values = values if isinstance(values, list) else []
        for project in values:
            key = project.get("key") if isinstance(project, dict) else None
            if isinstance(key, str) and _PROJECT_KEY_RE.match(key):
                name = project.get("name")
                out.append((key, name if isinstance(name, str) and name.strip() else key))
        if parsed.get("isLast") is not False or not values:
            break
    return out


class JiraWebhook(NamedTuple):
    """One site webhook (Settings › System › WebHooks): its name, the URL it posts to, and whether it is on."""
    name: str
    url: str
    enabled: bool


def list_webhooks(site: JiraSite) -> list[JiraWebhook]:
    """The site's admin webhooks (`GET /rest/webhooks/1.0/webhook`; Jira answers 403 unless the account administers
    Jira)."""
    reply = _request(site, "GET", "/rest/webhooks/1.0/webhook")
    _raise_for_status(reply, "the webhook list")
    rows = reply.parsed if isinstance(reply.parsed, list) else []
    return [JiraWebhook(name=str(row.get("name") or ""), url=str(row.get("url") or ""),
                        enabled=row.get("enabled") is not False)
            for row in rows if isinstance(row, dict)]


def transition_to(site: JiraSite, key: str, state_name: str) -> bool:
    """Move the issue to the status named `state_name` (case-insensitive) through the workflow transition
    that leads there, matched by its target status first and its own name second. False when the issue is
    already in that status (nothing to do); `JiraApiError` (409) when no transition from its current status
    leads there."""
    issue_key = _validated_issue_key(key)
    wanted = (state_name or "").strip().casefold()
    if not wanted:
        raise JiraApiError("No status to move the issue to.", status=400)
    reply = _request(site, "GET", f"/rest/api/3/issue/{issue_key}", params={"fields": "status"})
    _raise_for_status(reply, f"issue {issue_key}")
    fields = reply.parsed.get("fields") if isinstance(reply.parsed, dict) else None
    status = fields.get("status") if isinstance(fields, dict) else None
    current = status.get("name") if isinstance(status, dict) else None
    if isinstance(current, str) and current.strip().casefold() == wanted:
        return False
    reply = _request(site, "GET", f"/rest/api/3/issue/{issue_key}/transitions")
    _raise_for_status(reply, f"the transitions of {issue_key}")
    transitions = reply.parsed.get("transitions") if isinstance(reply.parsed, dict) else None
    transition_id = _transition_for(transitions if isinstance(transitions, list) else [], wanted)
    if transition_id is None:
        raise JiraApiError(
            f"No transition moves {issue_key} from {current or 'its status'} to {state_name.strip()} — check the "
            "connection's done state against the project's workflow.", status=409)
    reply = _request(site, "POST", f"/rest/api/3/issue/{issue_key}/transitions",
                     body={"transition": {"id": transition_id}})
    if reply.status != 204:
        _raise_for_status(reply, f"the transition of {issue_key}")
        raise JiraApiError(
            f"Jira answered the transition of {issue_key} with HTTP {reply.status}, not 204 — treating it as not "
            "moved.", status=reply.status)
    return True


def _transition_for(transitions: list[Any], wanted: str) -> Optional[str]:
    """The id of the transition whose target status is `wanted`, else the one named `wanted`."""
    def named(value: Any) -> bool:
        return isinstance(value, str) and value.strip().casefold() == wanted

    for match in (lambda t: named((t.get("to") or {}).get("name")), lambda t: named(t.get("name"))):
        for transition in transitions:
            if isinstance(transition, dict) and isinstance(transition.get("id"), str) and match(transition):
                return transition["id"]
    return None


# ── markdown → ADF ──────────────────────────────────────────────────────────────────────────────────
# The little markdown Jaah's own write-backs carry (`trackers.writeback`): paragraphs, numbered and bullet
# lists, `**bold**`, `` `code` ``, `[label](url)` and bare URLs. Anything else is kept as its text.

_MD_INLINE = re.compile(
    r"\*\*(?P<strong>[^*\n]+)\*\*"
    r"|`(?P<code>[^`\n]+)`"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<href>https?://[^)\s]+)\)"
    r"|(?P<url>https?://[^\s<>()]+[^\s<>().,;:!?'\"])"
)
_MD_ORDERED = re.compile(r"^\d+\.\s+")
_MD_BULLET = re.compile(r"^[-*]\s+")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")


def markdown_to_adf(markdown: str) -> dict[str, Any]:
    """An ADF document for `markdown` (the subset above, plus `#` headings and ``` fences). Links are http(s) only
    by construction."""
    content: list[dict[str, Any]] = []
    # Fenced code first: a blank line inside a fence is the code's, never a paragraph break.
    for index, chunk in enumerate(re.split(r"^```[^\n]*$", (markdown or "").strip(), flags=re.M)):
        if index % 2:
            code = chunk.strip("\n")
            if code:
                content.append({"type": "codeBlock", "content": [{"type": "text", "text": code}]})
            continue
        for block in re.split(r"\n\s*\n", chunk.strip()):
            content.extend(_md_block([line.rstrip() for line in block.split("\n") if line.strip()]))
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


def _md_block(lines: list[str]) -> list[dict[str, Any]]:
    if not lines:
        return []
    heading = _MD_HEADING.match(lines[0])
    if heading:
        node = {"type": "heading", "attrs": {"level": len(heading.group(1))},
                "content": _md_inline(heading.group(2))}
        return [node] + _md_block(lines[1:])
    for pattern, kind in ((_MD_ORDERED, "orderedList"), (_MD_BULLET, "bulletList")):
        if all(pattern.match(line) for line in lines):
            return [{"type": kind, "content": [
                {"type": "listItem", "content": [_md_paragraph([pattern.sub("", line, count=1)])]} for line in lines
            ]}]
    return [_md_paragraph(lines)]


def _md_paragraph(lines: list[str]) -> dict[str, Any]:
    inline: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if index:
            inline.append({"type": "hardBreak"})
        inline.extend(_md_inline(line))
    return {"type": "paragraph", "content": inline}


def _md_inline(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    at = 0
    for match in _MD_INLINE.finditer(text):
        if match.start() > at:
            out.append({"type": "text", "text": text[at:match.start()]})
        if match.group("strong"):
            out.append({"type": "text", "text": match.group("strong"), "marks": [{"type": "strong"}]})
        elif match.group("code"):
            out.append({"type": "text", "text": match.group("code"), "marks": [{"type": "code"}]})
        else:
            href = match.group("href") or match.group("url")
            out.append({"type": "text", "text": match.group("label") or href,
                        "marks": [{"type": "link", "attrs": {"href": href}}]})
        at = match.end()
    if at < len(text):
        out.append({"type": "text", "text": text[at:]})
    return out


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
    if depth > MAX_ADF_DEPTH or not isinstance(node, dict):
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
    if depth > MAX_ADF_DEPTH or not isinstance(node, dict):
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
    stamp = epoch_ms_or_none(raw)
    return stamp.strftime("%Y-%m-%d") if stamp else raw


def _plain(node: Any, depth: int) -> str:
    """Descendant text with no markdown applied — for a code block, where a `**` from a mark would
    become part of the code."""
    if depth > MAX_ADF_DEPTH or not isinstance(node, dict):
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


# ── Ticket gate reads (`app/pipeline_gates/tickets.py`) ─────────────────────────────────────────────

class JiraStatus(NamedTuple):
    id: str
    name: str


class JiraTicket(NamedTuple):
    """One issue with its status, as a pipeline ticket gate judges it."""

    key: str
    summary: str
    status: JiraStatus


#: Most matches a fixed ticket query reads: a gate passes on the first in a chosen status.
_TICKET_MATCHES = 20
#: `statuses/search` pages; a page this size of bare statuses stays well inside the read cap.
_STATUS_PAGE = 100
_MAX_STATUS_PAGES = 5


def _status_of(fields: Any) -> JiraStatus:
    status = fields.get("status") if isinstance(fields, dict) else None
    status = status if isinstance(status, dict) else {}
    sid, name = status.get("id"), status.get("name")
    return JiraStatus(str(sid) if isinstance(sid, (str, int)) else "", name if isinstance(name, str) else "")


def _ticket(issue: Any) -> Optional[JiraTicket]:
    if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
        return None
    raw_fields = issue.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    summary = fields.get("summary")
    return JiraTicket(issue["key"], summary if isinstance(summary, str) else "", _status_of(fields))


def tickets_matching(site: JiraSite, jql: str) -> list[JiraTicket]:
    """The first issues `jql` matches, each with its status (`POST /rest/api/3/search/jql`). The JQL is the gate's
    own, written by a project admin; Jira parses it, a bad one is Jira's 400."""
    reply = _request(site, "POST", "/rest/api/3/search/jql",
                     body={"jql": jql, "fields": ["status", "summary"], "maxResults": _TICKET_MATCHES})
    _raise_for_status(reply, "the ticket query")
    issues = reply.parsed.get("issues") if isinstance(reply.parsed, dict) else None
    return [t for t in (_ticket(i) for i in (issues if isinstance(issues, list) else [])) if t is not None]


def ticket(site: JiraSite, key: str) -> JiraTicket:
    """One issue with its status (`GET /rest/api/3/issue/{key}`); `JiraApiError` 404 when Jira knows none."""
    issue_key = _validated_issue_key(key)
    reply = _request(site, "GET", f"/rest/api/3/issue/{issue_key}", params={"fields": "status,summary"})
    _raise_for_status(reply, f"issue {issue_key}")
    found = _ticket(reply.parsed)
    return found if found is not None else JiraTicket(issue_key, "", JiraStatus("", ""))


def statuses(site: JiraSite, project_key: Optional[str] = None) -> list[JiraStatus]:
    """The statuses a ticket can be in, de-duplicated by id, in Jira's order: one project's
    (`GET /rest/api/3/project/{key}/statuses`, every issue type's) when `project_key` is given, else the site's
    (`GET /rest/api/3/statuses/search`, paged)."""
    seen: dict[str, JiraStatus] = {}

    def keep(raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        sid, name = raw.get("id"), raw.get("name")
        if isinstance(sid, (str, int)) and isinstance(name, str) and name.strip() and str(sid) not in seen:
            seen[str(sid)] = JiraStatus(str(sid), name.strip())

    if project_key:
        if not _PROJECT_KEY_RE.match(project_key):
            raise JiraApiError("Not a Jira project key (expected e.g. \"OAM\").", status=400)
        reply = _request(site, "GET", f"/rest/api/3/project/{project_key}/statuses")
        _raise_for_status(reply, f"project {project_key}'s statuses")
        for issue_type in reply.parsed if isinstance(reply.parsed, list) else []:
            for status in (issue_type.get("statuses") if isinstance(issue_type, dict) else None) or []:
                keep(status)
        return list(seen.values())
    for page in range(_MAX_STATUS_PAGES):
        reply = _request(site, "GET", "/rest/api/3/statuses/search",
                         params={"startAt": page * _STATUS_PAGE, "maxResults": _STATUS_PAGE})
        _raise_for_status(reply, "the site's statuses")
        parsed = reply.parsed if isinstance(reply.parsed, dict) else {}
        raw_values = parsed.get("values")
        values: list[Any] = raw_values if isinstance(raw_values, list) else []
        for status in values:
            keep(status)
        if parsed.get("isLast") is not False or not values:
            break
    return list(seen.values())
