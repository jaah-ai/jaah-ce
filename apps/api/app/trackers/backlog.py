"""The Backlog (Nulab) tracker adapter: a dedicated Backlog user "Jaah" connected with OAuth or an API key
(`integrations/vendors/backlog.toml`, the connection's credential), and one Backlog webhook per project that user
belongs to, posting to the connection's own URL.

Backlog webhooks carry no signature and no custom header, so the URL is the verifier: its path token finds the
connection (`intake.claimed_connection`, hashed), and its query names the project (`p`) with a check value for that
project (`s`), which `verify` checks along with the payload's own project. All are derived from the connection's
sealed webhook secret (`connections.open_webhook_secret`), never stored in the clear: the adapter can rebuild the URLs
to register, re-sync or remove its webhooks, and a reconnect (a new connection, a new secret) rotates them. A
project's admin, who can read its webhook URL in Backlog, cannot post events as another project. `on_connect`
replaces the random URL token `connections.connect` answered with the derived one — Backlog is told the URL here,
never by the admin, so the `ingress_url` the connect answers is not the one in use — and registers the webhooks.
Issue keys and project ids from a payload are checked against Backlog's formats before they reach an API path.

Webhook activity types read (`parse`): 1 issue created, 2 issue updated, 3 comment added; any other is ignored.
- assigned / unassigned — the issue's assignee becomes / stops being the Jaah user (type 1 `assignee`, type 2 the
  `assigner` change);
- labeled / unlabeled — the start label (`settings.start_label`, default `jaah`) is a Backlog *category*: added on
  create, or added / removed in a type 2 category change (field `component` on the wire). A trimmed "created" activity
  that omits the assignee and categories is completed by reading the issue;
- mentioned — a comment naming `@<Jaah's user id>` or `@<Jaah's name>`; comment — any other comment.
At most one start and one stop per activity. A comment the Jaah user wrote is ignored, so Jaah never answers its own
comments; its assignee and category changes count (Jaah makes none, so they are a person's: a connection signed in
with a person's own key hears that person). Who Jaah is (`jaah_user`) is learned at connect and refreshed hourly, keeping the last answer when
Backlog cannot be reached.

Projects (`list_containers`) are the Jaah user's Backlog projects, keyed by Backlog's numeric project id; listing
them only reads. Connecting and every settings save (`on_settings_saved`) register a webhook on any project that has
none yet (a project's link save too), so adding the Jaah user to a project and saving the connection's Mapping tab
is the whole setup for it. Done (`mark_done`) sets the issue's status to the project status
named by the done state; Backlog's built-in statuses have no "Done", so the default done state falls back to its
built-in "Closed". Tracker emails are not treated as verified (`Actor.email_verified`): a Backlog profile address
proves nothing, so a Backlog user starts tasks only while the connection allows non-members — except the connected
user itself (`own_account`), whose starts are the connecting member's.

The issue picker (`search_issues`, `get_issue`) reads Backlog's issue list
(https://developer.nulab.com/docs/backlog/api/2/get-issue-list/: `GET /api/v2/issues`, `keyword`, repeated
`projectId[]`, `count` 1..100) and one issue (https://developer.nulab.com/docs/backlog/api/2/get-issue/:
`GET /api/v2/issues/{issueIdOrKey}`); both answer `issueKey`, `summary`, `description`, `projectId`. The API gives
no browse URL, so it is `<space origin>/view/<KEY>`, as for intake events.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select

from app import credential_crypto, http_bounded, oauth2_tokens
from app.db import BridgedSession
from app.models.credentials import Credential
from app.models.trackers import TrackerConnection
from app.tenancy import seam
from app.trackers import connections, protocol
from app.trackers.protocol import Actor, IssueDetail, IssueEvent, IssueRef, IssueSummary
from app.trackers.vocabulary import (
    DEFAULT_DONE_STATE,
    KIND_ASSIGNED,
    KIND_COMMENT,
    KIND_LABELED,
    KIND_MENTIONED,
    KIND_UNASSIGNED,
    KIND_UNLABELED,
)

log = logging.getLogger("tracker-backlog")

TRACKER = "backlog"
# Nulab's three hosted domains (`backlog.toml` `origin_hosts`); a space on any other host is never dialled.
SPACE_HOSTS = ("backlog.com", "backlog.jp", "backlogtool.com")
# Per call: `mark_done` makes up to three (a token refresh, the statuses, the update) inside the write-back's 15 s.
TIMEOUT_SECONDS = 4.0
# Re-syncing webhooks stops starting calls after this long; the next settings save goes on.
SYNC_BUDGET_SECONDS = 8.0
ACTIVITY_ISSUE_CREATED, ACTIVITY_ISSUE_UPDATED, ACTIVITY_COMMENT_ADDED = 1, 2, 3
ACTIVITY_TYPES = (ACTIVITY_ISSUE_CREATED, ACTIVITY_ISSUE_UPDATED, ACTIVITY_COMMENT_ADDED)
WEBHOOK_NAME = "Jaah"
WEBHOOK_DESCRIPTION = "Starts and follows Jaah tasks. Managed by Jaah: removed when the connection is disconnected."
# Backlog's built-in closed status, used when the done state is the default and the project has no status by that name.
BUILTIN_CLOSED_STATUS = "Closed"
# How long the Jaah user's identity (who "Jaah" is in this space) is reused before it is fetched again.
IDENTITY_TTL_SECONDS = 3600.0
_URL_TOKEN = b"backlog-url-token|"
_URL_CHECK = b"backlog-url-check|"
_CHECK_PARAM = "s"
_PROJECT_PARAM = "p"
# What Backlog's issue keys and project ids look like; anything else from a payload is refused before it reaches a
# URL path (a forged key must not steer Jaah's own calls to another endpoint).
_ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*")
_PROJECT_ID = re.compile(r"[1-9][0-9]*")
# Backlog's issue list answers at most this many issues per call (`count`).
SEARCH_MAX = 100
# Backlog names a category change `component` on the wire (its label is "Category"); `category` is accepted too.
_CATEGORY_FIELDS = ("component", "category")


class BacklogError(protocol.TrackerError):
    """A Backlog call that did not do what was asked, or a connection that cannot act: one sentence, never a token."""


class BacklogNotFound(BacklogError):
    """Backlog answered 404: what the call named does not exist (or the Jaah user cannot see it)."""


@dataclass(frozen=True)
class JaahUser:
    """The Backlog user the connection acts as: numeric `id`, login `user_id`, display `name`."""
    id: str
    user_id: str
    name: str


@dataclass(frozen=True)
class _Auth:
    origin: str
    headers: dict[str, str]


# ── The connection's URL ──────────────────────────────────────────────────────────────────────────────


def _derived(conn: TrackerConnection, label: bytes) -> str:
    secret = connections.open_webhook_secret(conn)
    if not secret:
        raise BacklogError("the connection has no webhook secret — reconnect")
    digest = hmac.new(secret.encode(), label + str(conn.id).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def url_token(conn: TrackerConnection) -> str:
    """The path token of the connection's webhook URL (its SHA-256 is `connection_token_hash` after `on_connect`)."""
    return _derived(conn, _URL_TOKEN)


def check_value(conn: TrackerConnection, project_id: str) -> str:
    """The `s` value of one project's webhook URL: it vouches for that project only."""
    return _derived(conn, _URL_CHECK + project_id.encode() + b"|")


def hook_base(conn: TrackerConnection) -> str:
    """The connection's ingress URL (on the public hooks host when there is one), before the per-project query."""
    from app.routes.trackers import ingress_url

    return ingress_url(conn, url_token(conn))


def hook_url(conn: TrackerConnection, project_id: str) -> str:
    """The URL the Backlog webhook of project `project_id` posts to: a project's events cannot be sent as
    another's, since `verify` matches the payload's project to `p` and `s` to both."""
    query = urlencode({_PROJECT_PARAM: project_id, _CHECK_PARAM: check_value(conn, project_id)})
    return f"{hook_base(conn)}?{query}"


def issue_key(value: str) -> str:
    """`value` when it is a Backlog issue key (`ACME-7`); refused otherwise."""
    if not _ISSUE_KEY.fullmatch(value or ""):
        raise ValueError(f"not a Backlog issue key: {value[:40]!r}")
    return value


def project_id_of(value: Any) -> str:
    """A Backlog numeric project id as text; refused otherwise."""
    text = str(value if value is not None else "")
    if not _PROJECT_ID.fullmatch(text):
        raise ValueError(f"not a Backlog project id: {text[:40]!r}")
    return text


# ── Backlog's API ─────────────────────────────────────────────────────────────────────────────────────


def space_origin(base_url: Optional[str]) -> str:
    """`https://<space host>` for a space URL on one of Nulab's hosts; refused otherwise."""
    parts = urlsplit((base_url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host.endswith(f".{suffix}") for suffix in SPACE_HOSTS):
        raise BacklogError("the space URL must be https://<space>.backlog.com, .backlog.jp or .backlogtool.com")
    if parts.port not in (None, 443):
        raise BacklogError("the space URL must not name a port")
    return f"https://{host}"


def _auth(conn: TrackerConnection) -> _Auth:
    """The space origin and auth headers from the connection's credential: its API key, else its OAuth access token
    (refreshed when due, `oauth2_tokens`)."""
    if conn.credential_id is None:
        raise BacklogError("the connection has no secret; connect again with a Backlog secret")
    with seam.detached(), seam.bound_to(conn.org_id), BridgedSession() as session:
        credential = session.scalar(select(Credential).where(Credential.id == conn.credential_id))
        if credential is None:
            raise BacklogError("the connection's Backlog secret was deleted; connect again with another")
        config = dict(credential.config or {})
        origin = space_origin(config.get("base_url") or (f"https://{conn.site}" if conn.site else None))
        if credential_crypto.has_sealed_secret(config, "api_key"):
            return _Auth(origin, {"Backlog-API-Key": credential_crypto.open_secret(credential, "api_key")})
        token = oauth2_tokens.access_token_for(session, credential)
        if token is None:
            raise BacklogError("the Backlog secret has no live sign-in; reconnect it under Secrets")
        return _Auth(origin, {"Authorization": f"Bearer {token.token}"})


def _call(auth: _Auth, method: str, path: str, *, data: Optional[dict[str, Any]] = None,
          params: Optional[dict[str, Any]] = None) -> Any:
    """One API call, its JSON answer. A read timeout is raised as `TimeoutError` (`protocol`: it may have landed)."""
    try:
        with http_bounded.client(TIMEOUT_SECONDS, headers=auth.headers) as client:
            response = client.request(method, f"{auth.origin}/api/v2{path}", data=data, params=params)
    except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
        # The request went out: it may have landed (`protocol`), so the write-back must not repeat it.
        raise TimeoutError(f"Backlog {method} {path} timed out") from exc
    except httpx.HTTPError as exc:
        raise BacklogError(f"Backlog {method} {path} failed: {exc.__class__.__name__}") from exc
    if response.status_code == 404:
        raise BacklogNotFound(f"Backlog {method} {path} answered 404")
    if response.status_code >= 400:
        raise BacklogError(f"Backlog {method} {path} answered {response.status_code}")
    return http_bounded.json_or_none(response.content)


# ── Who Jaah is in the space ──────────────────────────────────────────────────────────────────────────

_IDENTITY: dict[UUID, tuple[float, JaahUser]] = {}


def fetch_myself(conn: TrackerConnection) -> JaahUser:
    me = _call(_auth(conn), "GET", "/users/myself") or {}
    if not isinstance(me, dict) or me.get("id") is None:
        raise BacklogError("Backlog did not say who the connected user is")
    return JaahUser(id=str(me["id"]), user_id=str(me.get("userId") or ""), name=str(me.get("name") or ""))


def jaah_user(conn: TrackerConnection) -> JaahUser:
    """The connected Backlog user, fetched at most once an hour per connection (warmed by `on_connect`). A failed
    refetch keeps the last identity: who Jaah is changes only when the credential does. Raises only when this
    process has never learned it and Backlog cannot say now."""
    held = _IDENTITY.get(conn.id)
    if held is not None and time.monotonic() - held[0] < IDENTITY_TTL_SECONDS:
        return held[1]
    try:
        user = fetch_myself(conn)
    except (BacklogError, TimeoutError):
        if held is None:
            raise
        log.warning("backlog connection %s: could not refresh the Jaah user; keeping the last one", conn.id)
        user = held[1]
    _IDENTITY[conn.id] = (time.monotonic(), user)
    return user


# ── Parsing a webhook ─────────────────────────────────────────────────────────────────────────────────


def _names(value: Any) -> set[str]:
    """The category names a change value lists (`"jaah, bug"`, one per line, or empty)."""
    return {part.strip().lower() for part in re.split(r"[,\n]", str(value or "")) if part.strip()}


def _mentions(text: str, me: JaahUser) -> bool:
    for handle in (me.user_id, me.name):
        if handle and re.search(rf"(?<![\w@])@{re.escape(handle)}(?![\w])", text, re.IGNORECASE):
            return True
    return False


def _issue_ref(conn: TrackerConnection, origin: str, project: Mapping[str, Any],
               content: Mapping[str, Any]) -> IssueRef:
    key = issue_key(f"{project.get('projectKey')}-{content.get('key_id')}")
    return IssueRef(tracker=TRACKER, site=conn.site or urlsplit(origin).hostname or "", key=key,
                    url=f"{origin}/view/{key}", container_id=project_id_of(project.get("id")))


def _origin_of(conn: TrackerConnection) -> str:
    return space_origin(f"https://{conn.site}") if conn.site else ""


def parse_activity(conn: TrackerConnection, me: JaahUser, payload: Mapping[str, Any],
                   fetch_issue: Optional[Callable[[str], dict[str, Any]]] = None) -> list[IssueEvent]:
    """The events one Backlog activity means for Jaah (none for an activity of another type, nor a comment by Jaah).
    `fetch_issue(key)` reads an issue when a "created" activity omits its assignee and categories."""
    kind_of = payload.get("type")
    if kind_of not in ACTIVITY_TYPES:
        return []
    user = payload.get("createdUser") or {}
    if payload.get("id") is None:
        return []
    # The Jaah user's own comments are Jaah's write-back, never handed on; its assignee and category changes are a
    # person's (Jaah makes none), as when the connection signs in with that person's own key.
    by_me = str(user.get("id")) == me.id
    project = payload.get("project") or {}
    content = payload.get("content") or {}
    issue = _issue_ref(conn, _origin_of(conn), project, content)
    actor = Actor(external_id=str(user.get("id") or ""), name=str(user.get("name") or user.get("userId") or ""),
                  email=user.get("mailAddress") or None, email_verified=False)
    label = str(connections.settings_of(conn)["start_label"]).strip().lower()
    activity_id = str(payload.get("id"))
    comment = content.get("comment") or {}
    comment_text = str(comment.get("content") or "").strip()
    comment_id = str(comment["id"]) if comment.get("id") is not None else None
    kinds: list[str] = []
    if kind_of == ACTIVITY_ISSUE_CREATED:
        if fetch_issue is not None and "assignee" not in content and "category" not in content:
            # A trimmed "created" activity names neither: the issue itself says who it went to and its categories.
            content = {**content, **{k: v for k, v in fetch_issue(issue.key).items() if k in ("assignee", "category")}}
        assignee = content.get("assignee") or {}
        if assignee and str(assignee.get("id")) == me.id:
            kinds.append(KIND_ASSIGNED)
        if label in {str(c.get("name") or "").strip().lower() for c in content.get("category") or []}:
            kinds.append(KIND_LABELED)
    elif kind_of == ACTIVITY_ISSUE_UPDATED:
        for change in content.get("changes") or []:
            field, new, old = change.get("field"), change.get("new_value"), change.get("old_value")
            if field == "assigner":
                # Backlog names the assignee by display name here; the issue's own assignee, when the payload
                # carries it, settles a name two users share.
                current = content.get("assignee")
                is_me = (str(current.get("id")) == me.id) if isinstance(current, dict) else None
                if me.name and str(new or "") == me.name and str(old or "") != me.name and is_me is not False:
                    kinds.append(KIND_ASSIGNED)
                elif me.name and str(old or "") == me.name and str(new or "") != me.name and is_me is not True:
                    kinds.append(KIND_UNASSIGNED)
            elif field in _CATEGORY_FIELDS:
                added, removed = _names(new) - _names(old), _names(old) - _names(new)
                if label in added:
                    kinds.append(KIND_LABELED)
                elif label in removed:
                    kinds.append(KIND_UNLABELED)
    if comment_text and not by_me:
        kinds.append(KIND_MENTIONED if _mentions(comment_text, me) else KIND_COMMENT)
    # One start and one stop per activity at most: an issue created assigned to Jaah AND with the start category is
    # one request for work, not two.
    starts = [k for k in kinds if k in (KIND_ASSIGNED, KIND_LABELED)][:1]
    stops = [k for k in kinds if k in (KIND_UNASSIGNED, KIND_UNLABELED)][:1]
    kinds = stops + starts + [k for k in kinds if k in (KIND_MENTIONED, KIND_COMMENT)]
    events = []
    for kind in kinds:
        is_comment = kind in (KIND_MENTIONED, KIND_COMMENT)
        events.append(IssueEvent(
            event_id=f"{activity_id}:{kind}", kind=kind, issue=issue, actor=actor,  # type: ignore[arg-type]
            title=str(content.get("summary") or ""), body=str(content.get("description") or ""),
            comment_id=comment_id if is_comment else None, text=comment_text if is_comment else None,
            session_id=None,
        ))
    return events


# ── Webhooks ──────────────────────────────────────────────────────────────────────────────────────────


def _projects(auth: _Auth, *, archived: bool = False) -> list[dict[str, Any]]:
    """The Jaah user's projects: active ones, or every one (`archived=True`, for removing webhooks)."""
    rows = _call(auth, "GET", "/projects" if archived else "/projects?archived=false") or []
    return [row for row in rows if isinstance(row, dict) and row.get("id") is not None]


def _ensure_url_token(conn: TrackerConnection) -> None:
    """Make the derived URL token the connection's (its hash on the row), when it is not yet: a connect whose
    `on_connect` failed before storing it is repaired by the next settings save, never left with webhooks that 404."""
    wanted = connections.token_hash(url_token(conn))
    if conn.connection_token_hash == wanted:
        return
    from app.trackers.intake import connection_session

    with connection_session(conn.id) as (session, _org):
        row = session.scalar(select(TrackerConnection).where(TrackerConnection.id == conn.id))
        if row is None:
            return
        row.connection_token_hash = wanted
        session.commit()
    conn.connection_token_hash = wanted


def sync_webhooks(conn: TrackerConnection, auth: _Auth, projects: list[dict[str, Any]]) -> list[str]:
    """Register the connection's webhook on every project in `projects` that lacks it, within
    `SYNC_BUDGET_SECONDS`; the ids it registered on."""
    _ensure_url_token(conn)
    deadline = time.monotonic() + SYNC_BUDGET_SECONDS
    added = []
    for project in projects:
        if time.monotonic() >= deadline:
            log.info("backlog connection %s: webhook sync stopped at its budget; the next settings save goes on", conn.id)
            break
        pid = project_id_of(project["id"])
        url = hook_url(conn, pid)
        hooks = _call(auth, "GET", f"/projects/{pid}/webhooks") or []
        if any(isinstance(h, dict) and h.get("hookUrl") == url for h in hooks):
            continue
        _call(auth, "POST", f"/projects/{pid}/webhooks", data={
            "name": WEBHOOK_NAME, "description": WEBHOOK_DESCRIPTION, "hookUrl": url, "allEvent": "false",
            "activityTypeIds[]": [str(t) for t in ACTIVITY_TYPES],
        })
        added.append(pid)
    return added


def check_webhooks(conn: TrackerConnection, containers: list[tuple[str, str]]) -> list[protocol.WebhookCheck]:
    """Per mapped project: `ok` when its webhook posts to `hook_url`, `stale` when a Jaah webhook there posts to
    another address (an older connection's, or another live one's), `missing` when none does; the address to paste
    rides along on every state but `ok`."""
    try:
        auth = _auth(conn)
    except BacklogError as exc:
        return [protocol.WebhookCheck(conn.site or "Backlog", "unknown", str(exc))]
    out = []
    deadline = time.monotonic() + SYNC_BUDGET_SECONDS
    for container_id, name in containers:
        try:
            pid = project_id_of(container_id)
        except ValueError:
            continue
        try:
            url = hook_url(conn, pid)
        except BacklogError as exc:  # no webhook secret: nothing can post here until a reconnect
            return [protocol.WebhookCheck(conn.site or "Backlog", "unknown", str(exc))]
        if time.monotonic() >= deadline:
            out.append(protocol.WebhookCheck(name, "unknown", "Not checked: Backlog was slow. Reload to check.", url))
            continue
        try:
            hooks = _call(auth, "GET", f"/projects/{pid}/webhooks") or []
        except BacklogError as exc:
            refused = str(exc).endswith(("answered 401", "answered 403"))
            out.append(protocol.WebhookCheck(name, "unknown", _HOOKS_UNREADABLE if refused else str(exc), url))
            continue
        except TimeoutError:
            out.append(protocol.WebhookCheck(name, "unknown", "Backlog didn't answer in time; reload.", url))
            continue
        urls = [str(hook.get("hookUrl") or "") for hook in hooks if isinstance(hook, dict)]
        if url in urls:
            # The address vouches for posting as this project: answered only where it has to be pasted.
            out.append(protocol.WebhookCheck(name, "ok", "Backlog posts here."))
        elif any(connections.ingress_token_of(conn, other) for other in urls):
            out.append(protocol.WebhookCheck(
                name, "stale", "Its Jaah webhook posts to another connection's address. If that connection is "
                               "gone, paste this address into it in Backlog › Project settings › Webhooks.", url))
        else:
            out.append(protocol.WebhookCheck(
                name, "missing", "No Jaah webhook. Add one with this address in Backlog › Project settings › "
                                 "Webhooks.", url))
    return out


_HOOKS_UNREADABLE = ("Jaah can't read this project's webhooks: its Backlog user isn't a project admin. Check Backlog › "
                     "Project settings › Webhooks has one with this address.")


def remove_webhooks(conn: TrackerConnection, auth: _Auth) -> list[str]:
    """Delete every webhook of this connection (any URL on its ingress token) from every project the Jaah user can
    see, archived ones included; the ids it removed from."""
    base = hook_base(conn)
    removed = []
    for project in _projects(auth, archived=True):
        pid = project_id_of(project["id"])
        for hook in _call(auth, "GET", f"/projects/{pid}/webhooks") or []:
            url = str(hook.get("hookUrl") or "") if isinstance(hook, dict) else ""
            if url == base or url.startswith(base + "?"):
                _call(auth, "DELETE", f"/projects/{pid}/webhooks/{int(hook['id'])}")
                removed.append(pid)
    return removed


def status_id(auth: _Auth, project_id: str, state_name: str) -> str:
    """The id of the project's status named `state_name` (case-insensitive); the default done state falls back to
    Backlog's built-in "Closed". Refused, naming the project's statuses, when neither exists."""
    statuses = [s for s in _call(auth, "GET", f"/projects/{project_id_of(project_id)}/statuses") or [] if isinstance(s, dict)]
    by_name = {str(s.get("name") or "").strip().lower(): str(s["id"]) for s in statuses if s.get("id") is not None}
    wanted = state_name.strip().lower()
    if wanted in by_name:
        return by_name[wanted]
    if wanted == DEFAULT_DONE_STATE.lower() and BUILTIN_CLOSED_STATUS.lower() in by_name:
        return by_name[BUILTIN_CLOSED_STATUS.lower()]
    names = ", ".join(str(s.get("name")) for s in statuses) or "none"
    raise BacklogError(f"project {project_id} has no status {state_name!r} (it has: {names})")


def _issue_detail(auth: _Auth, row: Any) -> IssueDetail:
    """An issue as Backlog answers it (list or single), for the picker; refused when it names no key or project."""
    row = protocol.as_dict(row)
    try:
        key = issue_key(str(row.get("issueKey") or ""))
        container_id = project_id_of(row.get("projectId"))
    except ValueError:
        raise BacklogError("Backlog answered an issue without a key or project") from None
    return IssueDetail(key=key, title=str(row.get("summary") or ""), url=f"{auth.origin}/view/{key}",
                       container_id=container_id, body_markdown=str(row.get("description") or ""))


def _summary(detail: IssueDetail) -> IssueSummary:
    return IssueSummary(key=detail.key, title=detail.title, url=detail.url, container_id=detail.container_id)


def search_issues(conn: TrackerConnection, query: str, container_ids: list[str], limit: int) -> list[IssueSummary]:
    """Issues of the projects `container_ids` matching `query`, the exact key first when `query` is one."""
    if not container_ids:
        return []
    auth = _auth(conn)
    count = max(1, min(int(limit), SEARCH_MAX))
    found: list[IssueSummary] = []
    if protocol.key_shaped(query):
        try:
            found.append(_summary(_issue_detail(auth, _call(auth, "GET", f"/issues/{issue_key(query.strip().upper())}"))))
        except (BacklogError, ValueError):
            pass  # no such issue, or one this sign-in may not read: the keyword search still answers
    rows = _call(auth, "GET", "/issues", params={
        "keyword": query, "projectId[]": [project_id_of(cid) for cid in container_ids], "count": count,
    }) or []
    if not isinstance(rows, list):
        raise BacklogError("Backlog answered the issue search without a list")
    found.extend(_summary(_issue_detail(auth, row)) for row in rows)
    return protocol.keep_containers(found, container_ids)[:count]


# ── The adapter ───────────────────────────────────────────────────────────────────────────────────────


class BacklogAdapter:
    name = TRACKER
    ack_sync = False
    # Jaah registers each project's webhook from the secret (`sync_webhooks`): nothing for an admin to paste.
    admin_adds_webhook = False
    names_connection_in_payload = False
    forge_app = False

    def own_account(self, conn: Any) -> Optional[str]:
        """The Backlog user the connection signs in as (`jaah_user`), whose starts are the connecting member's
        (`intake.starter_for`)."""
        return jaah_user(conn).id

    def verify(self, conn: Any, headers: Mapping[str, str], raw: bytes, query: Mapping[str, str]) -> bool:
        """The URL's token found the connection; its `s` must vouch for its `p` (constant time), and the payload must
        be that project's: one project's webhook URL cannot post another project's events."""
        given, project = query.get(_CHECK_PARAM) or "", query.get(_PROJECT_PARAM) or ""
        if not given or not _PROJECT_ID.fullmatch(project):
            return False
        if not hmac.compare_digest(given.encode(), check_value(conn, project).encode()):
            return False
        payload = json.loads(raw)
        return isinstance(payload, dict) and str((payload.get("project") or {}).get("id")) == project

    def parse(self, conn: Any, headers: Mapping[str, str], raw: bytes) -> list[IssueEvent]:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("a Backlog webhook body is one JSON object")
        if payload.get("type") not in ACTIVITY_TYPES:
            return []
        def fetch_issue(key: str) -> dict[str, Any]:
            answer = _call(_auth(conn), "GET", f"/issues/{issue_key(key)}")
            return answer if isinstance(answer, dict) else {}

        return parse_activity(conn, jaah_user(conn), payload, fetch_issue)

    async def list_containers(self, conn: Any) -> list[tuple[str, str]]:
        def work() -> list[tuple[str, str]]:
            return [(str(p["id"]), f"{p.get('name') or p.get('projectKey')} ({p.get('projectKey')})")
                    for p in _projects(_auth(conn))]

        return await asyncio.to_thread(work)

    async def search_issues(self, conn: Any, query: str, container_ids: Sequence[str],
                            limit: int) -> list[IssueSummary]:
        ids = [str(cid) for cid in container_ids]
        if not ids:
            return []
        return await asyncio.to_thread(search_issues, conn, query, ids, limit)

    async def get_issue(self, conn: Any, key: str) -> IssueDetail:
        def work() -> IssueDetail:
            try:
                path = f"/issues/{issue_key(key.strip().upper())}"
            except ValueError:
                raise BacklogError(f"{key[:40]!r} is not a Backlog issue key") from None
            auth = _auth(conn)
            return _issue_detail(auth, _call(auth, "GET", path))

        return await asyncio.to_thread(work)

    async def ack(self, conn: Any, issue: IssueRef, task_url: str, session_id: Optional[str]) -> None:
        await self.post_comment(conn, issue, f"Jaah started a task for this issue: {task_url}", session_id)

    async def post_comment(self, conn: Any, issue: IssueRef, markdown: str, session_id: Optional[str]) -> None:
        path = f"/issues/{issue_key(issue.key)}/comments"
        await asyncio.to_thread(lambda: _call(_auth(conn), "POST", path, data={"content": markdown}))

    async def mark_done(self, conn: Any, issue: IssueRef, state_name: str, session_id: Optional[str]) -> None:
        path = f"/issues/{issue_key(issue.key)}"

        def work() -> None:
            auth = _auth(conn)
            _call(auth, "PATCH", path,
                  data={"statusId": status_id(auth, issue.container_id, state_name)})

        await asyncio.to_thread(work)

    async def on_connect(self, conn: Any) -> None:
        """Make the derived URL the connection's (first: it needs no network), learn the space and who Jaah is in
        it, register the webhooks. What fails here is repaired by the next settings save (`on_settings_saved`)."""
        def work() -> None:
            _ensure_url_token(conn)
            try:
                auth = _auth(conn)
                if not conn.site:
                    conn.site = urlsplit(auth.origin).hostname
                _IDENTITY[conn.id] = (time.monotonic(), fetch_myself(conn))
            except BacklogError as exc:
                # A secret that can never work (none, no sign-in, another host, refused by Backlog): the person
                # connecting fixes it. Backlog unreachable is a timeout, raised as is (the route answers 502).
                raise protocol.ConnectRefused(str(exc)) from None
            sync_webhooks(conn, auth, _projects(auth))

        await asyncio.to_thread(work)

    async def on_settings_saved(self, conn: Any) -> None:
        """Repair the URL token and register the webhook on any project that lacks one (a project the Jaah user was
        added to since): the write path a container listing, a GET, never takes."""
        def work() -> None:
            auth = _auth(conn)
            sync_webhooks(conn, auth, _projects(auth))

        await asyncio.to_thread(work)

    async def check_webhooks(self, conn: Any, containers: Sequence[tuple[str, str]]) -> list[protocol.WebhookCheck]:
        """Each mapped project's webhooks against the address Jaah registers there (`hook_url`), which is answered
        to paste by hand when Jaah cannot register it (`protocol`)."""
        return await asyncio.to_thread(check_webhooks, conn, list(containers))

    async def on_disconnect(self, conn: Any) -> None:
        def work() -> None:
            _IDENTITY.pop(conn.id, None)
            remove_webhooks(conn, _auth(conn))

        await asyncio.to_thread(work)


ADAPTER = protocol.register(BacklogAdapter())
