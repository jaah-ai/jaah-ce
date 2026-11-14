"""The adapter contract every tracker (Linear, Jira, Backlog) implements, and the registry the intake looks it up in.

An adapter module is `app/trackers/<tracker>.py` and registers itself (`register(MyAdapter())`) at import;
`adapter_for` imports it the first time its tracker is asked for, so no list of adapters is kept anywhere. The intake never names a tracker's wire format: an adapter proves a request (`verify`), turns it into
`IssueEvent`s (`parse`) and talks back to the tracker (`ack`, `post_comment`, `mark_done`).

`conn` is the `models.trackers.TrackerConnection` row. `parse` emits `labeled`/`unlabeled` only for the
connection's start label (`conn.settings["start_label"]`), `mentioned` only for a comment that mentions Jaah, and
`comment` for any other comment on the issue — never one Jaah itself posted (`post_comment`, `ack`), which would
answer itself (`trackers.replies`). The intake trusts the kind it is handed.

The write-back (`trackers.writeback`) awaits `ack`, `post_comment` and `mark_done` from a worker thread
(`db_async.run_on_loop`), each bounded by a timeout; an adapter raises on any failure, and the write-back retries —
except a `TimeoutError`, which may have landed and is never retried: an adapter raises its HTTP client's read timeout
as `TimeoutError`.

An adapter whose events reach the shared ingress (`POST /api/public/trackers/{tracker}/events`, no connection token in the
URL: Linear, Jira Forge) also implements `connection_key(headers, raw) -> str | None`, the tracker's own
workspace or site id the request names; the intake finds the connection by it (`external_org_id`). It is optional on
the protocol because a token-URL adapter (Backlog, a Jira bot user) never needs it. That id is public, so the shared
route answers an unknown one exactly as it answers a failed `verify` (401).

The issue picker (`trackers.project_links`) reads through `search_issues` and `get_issue` (`IssueSummary`,
`IssueDetail`). `search_issues` answers `[]` for empty `container_ids` without a call, matches a key-shaped query to
that key exactly as well, narrows by `container_ids` in the tracker's own query AND drops any result whose container
is not one of them (a tracker may answer outside its filter). `get_issue` checks no container; the service does.
Both raise `TrackerError` (one operator sentence) on a refusal and `TimeoutError` on a read timeout.

`on_connect` raises `ConnectRefused` when the connection can never work (a secret that may not claim the
workspace); the connect route then removes the connection and answers the sentence. Any other failure removes it too.

An adapter may also have `async on_settings_saved(conn)`: a connection's settings save and a project's link save run
it after the response (a failure is logged), for tracker-side state the adapter keeps in step (Backlog's webhooks). `list_containers` only
reads: it is a GET's.

An adapter whose webhooks live in the tracker (a Jira API-token connection, Backlog) may also have
`async check_webhooks(conn, containers) -> list[WebhookCheck]`: it reads the tracker's webhooks and says, per webhook
place, whether one posts to this connection's current address (`ok`), points at another Jaah address (`stale`), is
switched off (`off`), is absent (`missing`), or could not be read (`unknown`). `containers` is the connection's mapped
`(container_id, name)` pairs. It only reads; the connection page shows the answer (`GET …/webhooks`).
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

# An issue key in every tracker: a project prefix, a dash, a number (`OAM-603`, `ENG-12`, `PRJ_X-4`).
ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")

EventKind = Literal["assigned", "unassigned", "mentioned", "labeled", "unlabeled", "comment"]


@dataclass(frozen=True)
class IssueRef:
    tracker: str
    site: str
    key: str
    url: str
    container_id: str


@dataclass(frozen=True)
class Actor:
    external_id: str
    name: str
    email: Optional[str]
    # True only when the tracker itself has verified `email` belongs to this user. The intake matches a Jaah member
    # by email only then: an address anyone can type into a tracker profile proves nothing.
    email_verified: bool = False


@dataclass(frozen=True)
class IssueEvent:
    event_id: str
    kind: EventKind
    issue: IssueRef
    actor: Actor
    title: str
    body: str
    comment_id: Optional[str]
    text: Optional[str]
    session_id: Optional[str]


@dataclass(frozen=True)
class IssueSummary:
    """One issue as the picker lists it. `key` is the format the intake stores (`ABC-12`); `container_id` the id
    `list_containers` returns and `tracker_mappings.container_id` stores; `url` the issue's browse URL."""
    key: str
    title: str
    url: str
    container_id: str


@dataclass(frozen=True)
class IssueDetail(IssueSummary):
    body_markdown: str = ""  # "" when the issue has no description


def key_shaped(query: str) -> bool:
    """True when `query` reads as an issue key (`ABC-12`): the adapters then also match that key exactly."""
    return ISSUE_KEY_RE.match(query.strip()) is not None


def keep_containers(results: "list[IssueSummary]", container_ids: "Sequence[str]") -> "list[IssueSummary]":
    """`results` without any whose container is not in `container_ids`, and without a repeated key."""
    allowed = {str(cid) for cid in container_ids}
    seen: set[str] = set()
    kept = []
    for item in results:
        if item.container_id in allowed and item.key not in seen:
            seen.add(item.key)
            kept.append(item)
    return kept


WebhookState = Literal["ok", "stale", "off", "missing", "unknown"]


@dataclass(frozen=True)
class WebhookCheck:
    """One webhook place (`where`: the site, or a container's name) and what posts from it. `detail` is the sentence
    a person acts on; `url` the address to paste there, when the adapter can rebuild it (never for a hashed token)."""
    where: str
    state: WebhookState
    detail: str
    url: Optional[str] = None


class TrackerError(RuntimeError):
    """An adapter's refusal: the tracker refused a call, answered without what was asked, or the connection cannot act
    (its secret is gone or holds no sign-in). The message is one sentence an admin can act on, never a token; a route
    shows it as the answer's detail."""


class ConnectRefused(RuntimeError):
    """`on_connect`'s refusal: the message is a sentence the person connecting can act on, never a token."""


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """The header `name` (given lowercase), whatever case the request spelled it in."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


class TrackerAdapter(Protocol):
    name: str
    # True when the tracker needs Jaah's acknowledgement inside the request (Linear's agent sessions): the ingress
    # files the task at once and calls `ack` before it answers.
    ack_sync: bool
    # True when a site admin adds Jaah's webhook in the tracker by hand, pasting the connection's URL and secret (a
    # Jira API-token connection), so New webhook URL may replace them; False when Jaah registers its own webhooks
    # from the secret (Backlog) or the tracker names the connection in the payload (Linear, a Jira Forge app).
    admin_adds_webhook: bool
    # True when every event names its connection's workspace in the payload, posted to the shared route (Linear's
    # app is Jaah's own); a connection of this tracker has no URL token.
    names_connection_in_payload: bool
    # True when the tracker has a Jaah app a connection may use instead of a credential (`auth_kind` forge: Jira).
    forge_app: bool
    # Optional, read with `getattr`: the `settings` key where a connection keeps the tracker account Jaah writes as
    # (Jira's `jira_account_id`), so another connection of the same site knows that account's comments are Jaah's
    # write-back (`intake.jaah_accounts`). An adapter that keeps none on the row leaves it out.

    def verify(self, conn: Any, headers: Mapping[str, str], raw: bytes, query: Mapping[str, str]) -> bool: ...

    def parse(self, conn: Any, headers: Mapping[str, str], raw: bytes) -> list[IssueEvent]: ...

    async def list_containers(self, conn: Any) -> list[tuple[str, str]]: ...

    async def search_issues(self, conn: Any, query: str, container_ids: Sequence[str],
                            limit: int) -> list[IssueSummary]: ...

    async def get_issue(self, conn: Any, key: str) -> IssueDetail: ...

    async def ack(self, conn: Any, issue: IssueRef, task_url: str, session_id: Optional[str]) -> None: ...

    async def post_comment(self, conn: Any, issue: IssueRef, markdown: str, session_id: Optional[str]) -> None: ...

    async def mark_done(self, conn: Any, issue: IssueRef, state_name: str, session_id: Optional[str]) -> None: ...

    async def on_connect(self, conn: Any) -> None: ...

    async def on_disconnect(self, conn: Any) -> None: ...

    # Optional: `own_account(conn) -> Optional[str]`, the tracker account id the connection signs in as (an event's
    # `Actor.external_id` form); a start from it is the connecting member's (`intake.starter_for`).


ADAPTERS: dict[str, TrackerAdapter] = {}
# Trackers whose module this tree does not ship (an adapter not built yet).
_NO_MODULE: set[str] = set()


def register(adapter: TrackerAdapter) -> TrackerAdapter:
    """Add `adapter` under its `name`, replacing any earlier one (a test's fake replaces nothing real)."""
    ADAPTERS[adapter.name] = adapter
    return adapter


def adapter_for(tracker: str) -> Optional[TrackerAdapter]:
    """The tracker's adapter, importing its module (`app.trackers.<tracker>`) on first ask; None while it has none."""
    if tracker not in ADAPTERS and tracker not in _NO_MODULE:
        from app.trackers.vocabulary import TRACKERS

        if tracker in TRACKERS:
            module = f"app.trackers.{tracker}"
            try:
                importlib.import_module(module)
            except ModuleNotFoundError as exc:
                if exc.name != module:
                    raise
                _NO_MODULE.add(tracker)  # asked once per process, not on every public request
    return ADAPTERS.get(tracker)
