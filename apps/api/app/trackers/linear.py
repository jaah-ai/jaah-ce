"""Linear: Jaah's own agent app (`apps/api/app/integrations/vendors/linear.toml`, `actor=app`), so people delegate
issues to Jaah and @mention it, and each workspace's events reach the shared ingress
(`POST /api/public/trackers/linear/events`) naming the workspace (`organizationId`, `connection_key`).

    Linear-Signature (HMAC-SHA256 of the raw body, the app's webhook signing secret) → the webhook → IssueEvents

What each webhook is (`parse`):

    AgentSessionEvent created, with a comment      mentioned   text = the comment; session_id = the session
    AgentSessionEvent created, no comment          assigned    (the issue was delegated to Jaah)
    AgentSessionEvent prompted, signal "stop"      unassigned  (someone stopped Jaah in the session)
    AgentSessionEvent prompted, any other          comment     text = the message, in the same session
    Issue create/update, start label added         labeled
    Issue update, start label removed              unlabeled
    Issue update, delegate taken off               unassigned  (un-delegated)
    Comment create by a person                     comment

An event's id is the comment it carries when it carries one (`comment:<id>`): Linear sends a mention both as a
session and as a Comment, and a session reply both as a prompt and as a Comment, so the second lands on the first's
dedup key and does nothing twice. A comment Jaah itself wrote (a bot actor, an actor that is not a person, or the
app user the connection's token signs in as — Linear names Jaah's own comments by that user, `APP_USER_SETTING`) is
never handed on (`protocol`): else every write-back would come back as a reply Jaah is not allowed to make.

The webhook signing secret is the APP's, one per deployment, on the operator-plane `oauth_app` secret the knob
`oauth_app.linear` names (field `webhook_secret`, `credentials/registry.py`) — never on a connection row. A delivery
older than `REPLAY_WINDOW_MS` (Linear's `webhookTimestamp`) is refused like a forged one.

Talking back (GraphQL, the connection's `linear` credential: the app install's OAuth token, renewed by
`oauth2_tokens`): in an agent session every write is an agent activity (`agentActivityCreate`) — the
acknowledgement a `thought` plus the task's link on the session (`externalUrls`), a question an `elicitation`, the
outcome a `response` or `error`; with no session (a label start) a plain comment. `ack_sync`: Linear marks a session
unresponsive unless it hears from the agent within ~10 s, so the ingress files the task and acknowledges it inside
the request. Done is `issueUpdate` to the team's state named by the connection's done state, else its first
`completed` state.

Who: Linear signs people in only by an email link, Google or SAML, so a person's email in a Linear payload is one
Linear (or the workspace's own identity provider) has proved (`Actor.email_verified`). Accepted limits: a workspace
whose SAML/SCIM provider asserts a Jaah member's email is trusted to; any workspace user who may edit the issue can
stop its task (a stop only halts work); taking the delegate off an issue stops its live task whoever that delegate
was, while moving it to another agent stops nothing (Linear's payload names delegates only by id); a reply that
Linear sends both as a prompt without `sourceCommentId` and as a Comment reaches the task twice; and Linear expects
a reply to a session that was not a start (a stop, a mention on a busy issue) which the intake has no seam to send —
the session's next write answers it.

The issue picker (`search_issues`, `get_issue`): `searchIssues(term, filter, first)` narrowed to the mapped teams
(`filter: {team: {id: {in: …}}}`; the deprecated `issueSearch` is never used), and `issue(id:)`, which takes an
identifier (`ENG-123`) and answers a miss as a GraphQL error. A container is a team id, as `list_containers` lists.

Facts: https://linear.app/developers/agents, /agent-interaction, /agent-signals, /webhooks (confirmed 2026-11-10);
https://raw.githubusercontent.com/linear/linear/master/packages/sdk/src/schema.graphql and
https://linear.app/developers/filtering (confirmed 2026-11-12).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Mapping, Optional, Sequence, cast

import httpx

from app.trackers import protocol
from app.trackers.protocol import (
    Actor,
    EventKind,
    IssueDetail,
    IssueEvent,
    IssueRef,
    IssueSummary,
    as_dict as _dict,
    header as _header,
)
from app.trackers.vocabulary import (
    DEFAULT_START_LABEL,
    KIND_ASSIGNED,
    KIND_COMMENT,
    KIND_LABELED,
    KIND_MENTIONED,
    KIND_UNASSIGNED,
    KIND_UNLABELED,
    NOT_ALLOWED_REPLY,
)

log = logging.getLogger("tracker-linear")

TRACKER = "linear"
GRAPHQL_URL = "https://api.linear.app/graphql"
SIGNATURE_HEADER = "linear-signature"
# Linear's own advice: refuse a delivery whose `webhookTimestamp` is more than a minute off the clock.
REPLAY_WINDOW_MS = 60_000
HTTP_TIMEOUT_SECONDS = 10.0
# The app's signing secret is opened once per this long per replica, not on every webhook (each open is an audited
# read): a rotated secret takes up to this long to reach every replica. A missing one is never cached.
SECRET_TTL_SECONDS = 60.0
APP_KNOB = "oauth_app.linear"
APP_CREDENTIAL_TYPE = "oauth_app"
_CONSUMER = "tracker_ingress"
# How many earlier comments of the issue a start's body carries.
MAX_PREVIOUS_COMMENTS = 20
SESSION_LINK_TIMEOUT_SECONDS = 3.0
# The task's link on the session (`agentSessionUpdate.externalUrls`).
TASK_LINK_LABEL = "Jaah task"

_secret_lock = threading.Lock()
_secret_cache: tuple[float, Optional[str]] = (0.0, None)


class LinearError(protocol.TrackerError):
    """Linear refused a call, or answered without the data asked for; the message is Linear's own, never a token."""


# ── Proving the request ───────────────────────────────────────────────────────────────────────────────


def app_webhook_secret() -> Optional[str]:
    """The Jaah Linear app's webhook signing secret, or None while the knob names no app with one."""
    global _secret_cache
    with _secret_lock:
        at, cached = _secret_cache
        if time.monotonic() - at < SECRET_TTL_SECONDS and at:
            return cached
        secret = _open_app_secret()
        _secret_cache = (time.monotonic(), secret) if secret else (0.0, None)
        return secret


def forget_app_secret() -> None:
    """Drop the cached secret (a test, or an operator who just rotated it and cannot wait `SECRET_TTL_SECONDS`)."""
    global _secret_cache
    with _secret_lock:
        _secret_cache = (0.0, None)


def _open_app_secret() -> Optional[str]:
    from app import operator_credentials
    from app.operator_settings import reads as settings_reads

    try:
        credential_id = settings_reads.get_text(APP_KNOB).strip()
    except KeyError:
        return None
    if not credential_id:
        return None
    opened = operator_credentials.opened(credential_id, APP_CREDENTIAL_TYPE, consumer=_CONSUMER) or {}
    secret = str(opened.get("webhook_secret") or "").strip()
    return secret or None


def signature(secret: str, raw: bytes) -> str:
    """Linear's `Linear-Signature`: hex HMAC-SHA256 of the raw body."""
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _payload(raw: bytes) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("a Linear webhook is a JSON object")
    return payload


# ── Reading a webhook ─────────────────────────────────────────────────────────────────────────────────


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _person(raw: Any, user_id: Any = None) -> Optional[Actor]:
    """A Linear user as the intake's `Actor`; None for anything that is not a person (an app, an integration). A
    payload naming the user only by id (`user_id`) is still that user, unnamed and matching no member."""
    user = _dict(raw) or ({"id": user_id} if _text(user_id) else {})
    if not user.get("id"):
        return None
    kind = _text(user.get("type")).lower()
    if kind and kind != "user":
        return None
    email = _text(user.get("email")) or None
    return Actor(external_id=str(user["id"]), name=_text(user.get("name")), email=email,
                 email_verified=email is not None)


def _issue_ref(issue: Mapping[str, Any], url: Any = None) -> Optional[IssueRef]:
    key = _text(issue.get("identifier"))
    team = _text(issue.get("teamId")) or _text(_dict(issue.get("team")).get("id"))
    if not key or not team:
        return None
    return IssueRef(tracker=TRACKER, site="", key=key, url=_text(issue.get("url")) or _text(url), container_id=team)


def _issue_body(issue: Mapping[str, Any], previous: Any = None) -> str:
    """The issue's description and, when Linear sent them, its earlier comments (newest last)."""
    body = _text(issue.get("description"))
    lines: list[str] = []
    for comment in (previous if isinstance(previous, list) else [])[-MAX_PREVIOUS_COMMENTS:]:
        comment = _dict(comment)
        text = _text(comment.get("body"))
        if not text:
            continue
        who = _text(_dict(comment.get("user")).get("name")) or "Someone"
        lines.append(f"**{who}:** {text}")
    if lines:
        # Anyone in the workspace may have written them: quoted context for the task, never its instruction.
        quoted = "\n\n".join("\n".join(f"> {row}" for row in line.splitlines()) for line in lines)
        body = (body + "\n\n" if body else "") + "## Earlier comments (quoted from Linear)\n\n" + quoted
    return body


def _event_of(event_id: str, kind: str, *fields: Any) -> IssueEvent:
    """An `IssueEvent` of one of the vocabulary's kinds (`trackers.vocabulary`, plain strings there)."""
    return IssueEvent(event_id, cast(EventKind, kind), *fields)


def _session_event(payload: Mapping[str, Any]) -> list[IssueEvent]:
    session = _dict(payload.get("agentSession"))
    issue = _dict(session.get("issue"))
    ref = _issue_ref(issue, session.get("url"))
    session_id = _text(session.get("id"))
    if ref is None or not session_id:
        return []
    title, body = _text(issue.get("title")), _issue_body(issue, payload.get("previousComments"))
    action = _text(payload.get("action"))
    if action == "created":
        actor = _person(session.get("creator"), session.get("creatorId"))
        if actor is None:
            return []
        comment = _dict(session.get("comment"))
        comment_id = _text(comment.get("id")) or _text(session.get("commentId")) or None
        text = _text(comment.get("body"))
        if comment_id and text:
            return [_event_of(f"comment:{comment_id}", KIND_MENTIONED, ref, actor, title, body, comment_id, text,
                               session_id)]
        return [_event_of(f"session:{session_id}", KIND_ASSIGNED, ref, actor, title, body, None, None, session_id)]
    if action == "prompted":
        activity = _dict(payload.get("agentActivity"))
        actor = _person(activity.get("user"), activity.get("userId"))
        if actor is None:
            return []
        if _text(activity.get("signal")).lower() == "stop":
            return [_event_of(f"stop:{_text(activity.get('id')) or session_id}", KIND_UNASSIGNED, ref, actor, title,
                               body, None, None, session_id)]
        content = _dict(activity.get("content"))
        text = _text(content.get("body")) or _text(activity.get("body"))
        comment_id = _text(activity.get("sourceCommentId")) or None
        event_id = f"comment:{comment_id}" if comment_id else f"activity:{_text(activity.get('id'))}"
        if not text or event_id.endswith(":"):
            return []
        return [_event_of(event_id, KIND_COMMENT, ref, actor, title, body, comment_id, text, session_id)]
    return []


def _label_names(labels: Any) -> dict[str, str]:
    """`{id: name}` of an issue's labels as Linear sent them."""
    out: dict[str, str] = {}
    for label in labels if isinstance(labels, list) else []:
        label = _dict(label)
        if label.get("id"):
            out[str(label["id"])] = _text(label.get("name"))
    return out


def _issue_events(conn: Any, payload: Mapping[str, Any]) -> list[IssueEvent]:
    action = _text(payload.get("action"))
    if action not in ("create", "update"):
        return []
    issue = _dict(payload.get("data"))
    ref = _issue_ref(issue, payload.get("url"))
    actor = _person(payload.get("actor"))
    if ref is None or actor is None:
        return []
    from app.trackers.connections import settings_of

    start_label = (_text(settings_of(conn).get("start_label")) or DEFAULT_START_LABEL).lower()
    before = _dict(payload.get("updatedFrom"))
    stamp = _text(issue.get("updatedAt")) or _text(payload.get("createdAt")) or _text(payload.get("webhookId"))
    title, body = _text(issue.get("title")), _issue_body(issue)
    events: list[IssueEvent] = []

    def event(kind: str) -> IssueEvent:
        return _event_of(f"issue:{issue.get('id')}:{stamp}:{kind}", kind, ref, actor, title, body, None, None, None)

    current = _label_names(issue.get("labels"))
    has_label = any(name.lower() == start_label for name in current.values())
    if action == "create":
        return [event(KIND_LABELED)] if has_label else []
    if "labelIds" in before:
        previous_ids = {str(i) for i in before.get("labelIds") or []}
        added = [cid for cid, name in current.items() if cid not in previous_ids and name.lower() == start_label]
        removed = previous_ids - set(current) - {str(i) for i in issue.get("labelIds") or []}
        if added:
            events.append(event(KIND_LABELED))
        elif removed and not has_label and _has_live_task(conn, ref.key) \
                and start_label in _removed_label_names(conn, sorted(removed)):
            events.append(event(KIND_UNLABELED))
    if "delegateId" in before:
        # Taken off, not moved: a move to another delegate may be TO Jaah (whose own session starts the task), and
        # the payload names delegates only by id.
        if before.get("delegateId") and not issue.get("delegateId"):
            events.append(event(KIND_UNASSIGNED))
    return events


def _has_live_task(conn: Any, issue_key: str) -> bool:
    """Whether the issue has a task at work: only then can a label's removal stop anything, so only then is Linear
    asked what the removed labels were (one read; no call for the many label edits on other issues)."""
    from sqlalchemy.orm import object_session

    from app.trackers.intake import live_link

    session = object_session(conn)
    return session is not None and live_link(session, conn.id, issue_key) is not None


def _removed_label_names(conn: Any, label_ids: list[str]) -> set[str]:
    """The lower-cased names of labels the issue just lost (Linear's update names only their ids): asked of Linear
    with the connection's token. Empty when Linear cannot be asked — a removal not proven to be the start label never
    stops a task."""
    try:
        data = _call_sync(conn, "query Labels($ids: [ID!]) { issueLabels(filter: {id: {in: $ids}}) "
                                "{ nodes { id name } } }", {"ids": label_ids})
    except Exception:  # noqa: BLE001 — a lookup that fails proves nothing
        log.warning("linear connection %s: could not name removed labels", getattr(conn, "id", "?"), exc_info=True)
        return set()
    return {_text(node.get("name")).lower() for node in _nodes(data, "issueLabels")}


def _comment_events(payload: Mapping[str, Any], app_user_id: Optional[str]) -> list[IssueEvent]:
    if _text(payload.get("action")) != "create":
        return []
    comment = _dict(payload.get("data"))
    if comment.get("botActor"):
        return []  # an app's comment, Jaah's own among them
    actor = _person(comment.get("user"))
    payload_actor = _dict(payload.get("actor"))
    if actor is None or (payload_actor and _person(payload_actor) is None):
        return []
    if app_user_id and actor.external_id == app_user_id:
        return []  # Jaah's own write-back, posted as its app user
    issue = _dict(comment.get("issue"))
    ref = _issue_ref(issue, payload.get("url"))
    text, comment_id = _text(comment.get("body")), _text(comment.get("id"))
    if ref is None or not text or not comment_id or text == NOT_ALLOWED_REPLY:
        return []
    return [_event_of(f"comment:{comment_id}", KIND_COMMENT, ref, actor, _text(issue.get("title")),
                       _issue_body(issue), comment_id, text, None)]


def _app_user_id(conn: Any) -> Optional[str]:
    """The app user the connection's token signs in as: kept on its settings, else asked of Linear and kept (the
    intake commits it with the event). Only an app viewer counts: a member's key in the secret silences no one. None
    when Linear cannot be asked (tried again after `APP_USER_RETRY_SECONDS`): the person checks still hold."""
    from sqlalchemy.orm import object_session

    if conn is None or object_session(conn) is None:
        return None
    kept = (conn.settings or {}).get(APP_USER_SETTING)
    if isinstance(kept, str) and kept:
        return kept
    key = (str(conn.id), str(conn.credential_id))
    known = _APP_USERS.get(key)
    if known is None or (known[0] is None and time.monotonic() - known[1] >= APP_USER_RETRY_SECONDS):
        try:
            viewer = _dict(_call_sync(conn, _VIEWER, {}).get("viewer"))
            found = _text(viewer.get("id")) if viewer.get("app") is True else ""
        except Exception:  # noqa: BLE001 — not knowing Jaah's own user only loses this one filter
            log.warning("linear connection %s: could not learn its app user", conn.id, exc_info=True)
            found = ""
        known = (found or None, time.monotonic())
        _APP_USERS[key] = known
    if known[0] is not None:
        conn.settings = {**(conn.settings or {}), APP_USER_SETTING: known[0]}
    return known[0]


# ── Talking to Linear ─────────────────────────────────────────────────────────────────────────────────


def _post_graphql(authorization: str, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
    """One GraphQL call; its `data`. Raises `TimeoutError` on a read timeout (it may have landed), `LinearError` on
    any refusal. The single seam to Linear's API (tests replace it)."""
    from app import http_bounded

    try:
        with http_bounded.client(HTTP_TIMEOUT_SECONDS, {"Authorization": authorization}) as client:
            reply = client.post(GRAPHQL_URL, json={"query": query, "variables": dict(variables)})
    except httpx.TimeoutException as exc:
        raise TimeoutError("Linear did not answer in time") from exc
    try:
        body = reply.json()
    except ValueError:
        body = {}
    errors = body.get("errors") if isinstance(body, dict) else None
    if reply.status_code >= 400 or errors:
        message = _text(_dict(errors[0]).get("message")) if isinstance(errors, list) and errors else ""
        raise LinearError(f"Linear answered {reply.status_code}" + (f": {message[:200]}" if message else ""))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise LinearError("Linear answered with no data")
    return data


def _authorization(session: Any, credential_id: Any) -> str:
    """The `Authorization` header the connection's `linear` credential acts with: its OAuth access token (renewed
    when due) as a Bearer. A personal API key never claims a workspace (`on_connect`), so it is never asked for."""
    from sqlalchemy import select

    from app import oauth2_tokens
    from app.credential_crypto import has_sealed_secret, open_secret
    from app.models.credentials import Credential

    row = session.scalar(select(Credential).where(Credential.id == credential_id)) if credential_id else None
    if row is None:
        raise LinearError("the connection's Linear secret is gone — reconnect Linear")
    config = row.config or {}
    if has_sealed_secret(config, "access_token"):
        minted = oauth2_tokens.access_token_for(session, row)
        return f"Bearer {minted.token if minted is not None else open_secret(row, 'access_token')}"
    raise LinearError("the connection's Linear secret holds no sign-in — select Connect Linear on it")


def _call_sync(conn: Any, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
    """A call as the connection, from a thread already inside the connection's session (`parse`)."""
    from sqlalchemy.orm import object_session

    session = object_session(conn)
    if session is None:
        raise LinearError("no session to read the connection's secret in")
    return _post_graphql(_authorization(session, conn.credential_id), query, variables)


def _call_detached(conn: Any, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
    from app.trackers.intake import connection_session

    with connection_session(conn.id) as (session, _org):
        authorization = _authorization(session, conn.credential_id)
    return _post_graphql(authorization, query, variables)


async def _call(conn: Any, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_call_detached, conn, query, variables)


def _nodes(data: Mapping[str, Any], *path: str) -> list[dict[str, Any]]:
    node: Any = data
    for key in path:
        node = _dict(node).get(key)
    return [item for item in _dict(node).get("nodes") or [] if isinstance(item, dict)]


def _ok(data: Mapping[str, Any], mutation: str) -> None:
    if not _dict(data.get(mutation)).get("success"):
        raise LinearError(f"Linear did not accept {mutation}")


def activity_type(markdown: str) -> str:
    """The agent activity a write-back's comment is (`trackers.writeback`'s own sentences): a question asks
    (`elicitation`), the outcome ends the session (`response` or `error`), anything else is a `thought`."""
    text = markdown.lstrip()
    if text.startswith("**Question"):
        return "elicitation"
    if text.startswith("Done."):
        return "response"
    if text.startswith("Failed."):
        return "error"
    return "thought"


_ACTIVITY = ("mutation Activity($input: AgentActivityCreateInput!) "
             "{ agentActivityCreate(input: $input) { success } }")
_COMMENT = "mutation Comment($input: CommentCreateInput!) { commentCreate(input: $input) { success } }"
_SESSION_LINK = ("mutation Link($id: String!, $input: AgentSessionUpdateInput!) "
                 "{ agentSessionUpdate(id: $id, input: $input) { success } }")
_ISSUE_ID = "query Issue($id: String!) { issue(id: $id) { id } }"
_ISSUE_STATES = ("query Issue($id: String!) { issue(id: $id) { id team { states { nodes { id name type position } } } "
                 "} }")
_ISSUE_UPDATE = ("mutation Done($id: String!, $input: IssueUpdateInput!) "
                 "{ issueUpdate(id: $id, input: $input) { success } }")
_TEAMS = "query Teams($after: String) { teams(first: 100, after: $after) { nodes { id key name } " \
         "pageInfo { hasNextPage endCursor } } }"
_ORGANIZATION = "query Workspace { viewer { id app } organization { id urlKey name } }"
_VIEWER = "query Viewer { viewer { id app } }"
# Where the connection keeps its app user's id (`settings`), learned from its first comment event.
APP_USER_SETTING = "linear_app_user_id"
# (connection id, credential id) -> (app user id or None when Linear could not say, when asked)
_APP_USERS: dict[tuple[str, str], tuple[Optional[str], float]] = {}
APP_USER_RETRY_SECONDS = 60

_SEARCH = ("query Search($term: String!, $filter: IssueFilter, $first: Int) "
           "{ searchIssues(term: $term, filter: $filter, first: $first) { nodes { id identifier title url team { id } } } }")
_ISSUE_DETAIL = "query IssueDetail($id: String!) { issue(id: $id) { identifier title url description team { id } } }"
# `searchIssues(first:)` accepts 1..250.
SEARCH_MAX = 250


def _summary(node: Mapping[str, Any]) -> IssueSummary:
    return IssueSummary(key=_text(node.get("identifier")), title=_text(node.get("title")), url=_text(node.get("url")),
                        container_id=_text(_dict(node.get("team")).get("id")))


async def _comment(conn: Any, issue: IssueRef, body: str) -> None:
    """A plain comment on the issue (no agent session: a label start), found by its identifier."""
    found = _dict((await _call(conn, _ISSUE_ID, {"id": issue.key})).get("issue"))
    if not found.get("id"):
        raise LinearError(f"Linear has no issue {issue.key}")
    _ok(await _call(conn, _COMMENT, {"input": {"issueId": str(found["id"]), "body": body}}), "commentCreate")


def done_state_id(states: list[dict[str, Any]], state_name: str) -> Optional[str]:
    """The workflow state a merged task moves its issue to: the one named `state_name` (any case), else the team's
    first `completed` state."""
    wanted = state_name.strip().lower()
    for state in states:
        if _text(state.get("name")).lower() == wanted:
            return str(state["id"])
    completed = sorted((s for s in states if _text(s.get("type")) == "completed"),
                       key=lambda s: float(s.get("position") or 0))
    return str(completed[0]["id"]) if completed else None


class LinearAdapter:
    name = TRACKER
    ack_sync = True
    admin_adds_webhook = False
    names_connection_in_payload = True
    forge_app = False

    def connection_key(self, headers: Mapping[str, str], raw: bytes) -> Optional[str]:
        return _text(_payload(raw).get("organizationId")) or None

    def verify(self, conn: Any, headers: Mapping[str, str], raw: bytes, query: Mapping[str, str]) -> bool:
        secret = app_webhook_secret()
        sent = (_header(headers, SIGNATURE_HEADER) or "").strip().lower()
        if not secret or not sent or not hmac.compare_digest(signature(secret, raw), sent):
            return False
        stamp = _payload(raw).get("webhookTimestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) \
                or abs(time.time() * 1000 - stamp) > REPLAY_WINDOW_MS:
            log.warning("linear connection %s: a signed delivery outside the replay window", conn.id)
            return False
        return True

    def parse(self, conn: Any, headers: Mapping[str, str], raw: bytes) -> list[IssueEvent]:
        payload = _payload(raw)
        kind = _text(payload.get("type"))
        if kind == "AgentSessionEvent":
            return _session_event(payload)
        if kind == "Issue":
            return _issue_events(conn, payload)
        if kind == "Comment":
            return _comment_events(payload, _app_user_id(conn))
        return []

    async def list_containers(self, conn: Any) -> list[tuple[str, str]]:
        teams: list[tuple[str, str]] = []
        after: Optional[str] = None
        for _page in range(20):
            data = await _call(conn, _TEAMS, {"after": after})
            for team in _nodes(data, "teams"):
                key, name = _text(team.get("key")), _text(team.get("name"))
                teams.append((str(team["id"]), f"{name} ({key})" if key else name))
            info = _dict(_dict(data.get("teams")).get("pageInfo"))
            if not info.get("hasNextPage"):
                break
            after = _text(info.get("endCursor")) or None
        return teams

    async def search_issues(self, conn: Any, query: str, container_ids: Sequence[str],
                            limit: int) -> list[IssueSummary]:
        teams = [str(cid) for cid in container_ids]
        if not teams or limit <= 0:
            return []
        found: list[IssueSummary] = []
        if protocol.key_shaped(query):
            try:
                found.append(_summary(_dict((await _call(conn, _ISSUE_DETAIL, {"id": query.strip()})).get("issue"))))
            except LinearError as exc:
                if "not found" not in str(exc).lower():  # Linear answers an unknown identifier as an error
                    raise
        data = await _call(conn, _SEARCH, {"term": query, "filter": {"team": {"id": {"in": teams}}},
                                           "first": max(1, min(limit, SEARCH_MAX))})
        found += [_summary(node) for node in _nodes(data, "searchIssues")]
        return protocol.keep_containers(found, teams)[:limit]

    async def get_issue(self, conn: Any, key: str) -> IssueDetail:
        issue = _dict((await _call(conn, _ISSUE_DETAIL, {"id": key.strip()})).get("issue"))
        if not issue.get("identifier"):
            raise LinearError(f"Linear has no issue {key}")
        summary = _summary(issue)
        return IssueDetail(key=summary.key, title=summary.title, url=summary.url, container_id=summary.container_id,
                           body_markdown=_text(issue.get("description")))

    async def ack(self, conn: Any, issue: IssueRef, task_url: str, session_id: Optional[str]) -> None:
        line = f"Started work: [{TASK_LINK_LABEL}]({task_url})"
        if not session_id:
            await _comment(conn, issue, line)
            return
        _ok(await _call(conn, _ACTIVITY, {"input": {"agentSessionId": session_id,
                                                    "content": {"type": "thought", "body": line}}}),
            "agentActivityCreate")
        try:
            # Bounded: Linear is waiting on the ingress's answer, and the activity has already acknowledged it.
            _ok(await asyncio.wait_for(_call(conn, _SESSION_LINK, {"id": session_id, "input": {
                "externalUrls": [{"label": TASK_LINK_LABEL, "url": task_url}]}}), SESSION_LINK_TIMEOUT_SECONDS),
                "agentSessionUpdate")
        except Exception:  # noqa: BLE001 — the activity landed; the link on the session is a convenience
            log.warning("linear connection %s: could not link the task on session %s", conn.id, session_id,
                        exc_info=True)

    async def post_comment(self, conn: Any, issue: IssueRef, markdown: str, session_id: Optional[str]) -> None:
        if not session_id:
            await _comment(conn, issue, markdown)
            return
        _ok(await _call(conn, _ACTIVITY, {"input": {"agentSessionId": session_id, "content": {
            "type": activity_type(markdown), "body": markdown}}}), "agentActivityCreate")

    async def mark_done(self, conn: Any, issue: IssueRef, state_name: str, session_id: Optional[str]) -> None:
        data = await _call(conn, _ISSUE_STATES, {"id": issue.key})
        found = _dict(data.get("issue"))
        if not found.get("id"):
            raise LinearError(f"Linear has no issue {issue.key}")
        state_id = done_state_id(_nodes(found, "team", "states"), state_name)
        if state_id is None:
            raise LinearError(f"the team of {issue.key} has no state named {state_name!r} and no completed state")
        _ok(await _call(conn, _ISSUE_UPDATE, {"id": str(found["id"]), "input": {"stateId": state_id}}),
            "issueUpdate")

    async def on_connect(self, conn: Any) -> None:
        """Learn the workspace the connection's token reaches: its id is how the shared ingress finds the
        connection, its URL key the connection's site. Only the Jaah app's own install (a workspace admin's
        `actor=app` sign-in, whose viewer is the app user) may claim a workspace: a member's personal API key would
        let any member route the whole workspace's events to their own Jaah org."""
        try:
            data = await _call(conn, _ORGANIZATION, {})
        except LinearError as exc:  # Linear's own sentence (a revoked token's 401) is the one to fix the secret by
            raise protocol.ConnectRefused(str(exc)) from exc
        if _dict(data.get("viewer")).get("app") is not True:
            raise protocol.ConnectRefused("connect Linear with Connect Linear (Jaah's app, approved by a workspace "
                                          "admin), not an API key")
        workspace = _dict(data.get("organization"))
        if not workspace.get("id"):
            raise protocol.ConnectRefused("Linear named no workspace for this secret")
        conn.external_org_id = str(workspace["id"])
        conn.site = conn.site or _text(workspace.get("urlKey")) or None

    async def on_disconnect(self, conn: Any) -> None:
        """Nothing to undo at Linear: the app stays installed until a workspace admin removes it, and with no
        connection its events are refused at the ingress."""
        return None


protocol.register(LinearAdapter())
