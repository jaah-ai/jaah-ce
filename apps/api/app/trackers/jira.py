"""The Jira Cloud tracker adapter, bot-user flavour: a dedicated Atlassian account ("Jaah") whose `jira` credential
the connection names (`auth_kind` `basic`). Jira posts to the connection's own URL
(`POST /api/public/trackers/jira/events/{connection_token}`) from a site webhook the Jira admin creates in
Settings › System › WebHooks with the connection's secret; every call back to Jira goes through `app/jira_api.py`.

    verify   `X-Hub-Signature: sha256=<hex>`, an HMAC-SHA256 of the raw body under the connection's secret
    parse    `jira:issue_created` / `jira:issue_updated`: the assignee becoming (or ceasing to be) Jaah's account is
             `assigned` (`unassigned`); the start label added (removed) is `labeled` (`unlabeled`).
             `comment_created`: a comment mentioning Jaah's account (an ADF mention, or wiki `[~accountid:…]`) is
             `mentioned`, its text without the mention the instruction; any other comment is `comment`. A comment
             Jaah's own account wrote is never handed on.
    write    a markdown comment (`jira_api.markdown_to_adf`); done = `jira_api.transition_to` the done state

Jaah's account id is the credential's own (`jira_api.myself`), learned at connect and kept on the connection's
settings (`ACCOUNT_SETTING`): reconnect after pointing the credential at another account. Comments that account makes
are never handed on (they are Jaah's own write-back); its assignee and label changes are, since Jaah never makes one —
a connection signed in with a person's own API token hears that person assign an issue to it. The container a
mapping names is the Jira project KEY (`list_containers`). A tracker user's email counts as verified only for an
Atlassian account (`accountType` `atlassian`, whose email Atlassian verifies); Jira hides it unless the user's profile
shows it, and then the user is unmapped — except the connection's own account (`own_account`), whose starts are
the connecting member's. The trust boundary this accepts: an Atlassian organization admin who owns a
verified domain can give a managed account any free address on it, and so read as that address's Jaah member.

This class is the Jira adapter's one home; the Jira Forge flavour (`auth_kind` `forge`) is the same class:

    reach    Jaah's Jira Forge app calls the shared ingress (`POST /api/public/trackers/jira/events`)
             with a Forge Invocation Token and `{"binding_code"?, "event"?}`; `event` is the webhook shape above, built
             by the app from Jira's product events, so `parse` reads both flavours alike.
    verify   the FIT (`forge_fit.verified_claims`); its site (`context.cloudId`) must be the connection's
             (`external_org_id`). An unbound connection accepts one call only: one carrying its unexpired claim code,
             from a site no other connection holds — that call binds the site and spends the code.
    write    with the app's system token, sealed on the row (`forge_token_sealed`) and replaced by every call, through
             Atlassian's gateway (`app.apiBaseUrl`, kept as `FORGE_API_SETTING`); the app calls every five minutes, so
             the token never lapses while it is installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import object_session

from app import jira_api
from app.clock import utcnow
from app.models.credentials import Credential
from app.trackers import connections, forge_fit, protocol
from app.trackers.protocol import Actor, IssueEvent, IssueRef, as_dict as _dict, header as _header
from app.trackers.vocabulary import (
    AUTH_BASIC,
    AUTH_FORGE,
    KIND_ASSIGNED,
    KIND_COMMENT,
    KIND_LABELED,
    KIND_MENTIONED,
    KIND_UNASSIGNED,
    KIND_UNLABELED,
)

T = TypeVar("T")

log = logging.getLogger("tracker-jira")

SIGNATURE_HEADER = "x-hub-signature"
_SIGNATURE_PREFIX = "sha256="
# The issue's latest comments that ride along in a start's task body.
_BODY_COMMENTS = 5
_WIKI_MENTION = re.compile(r"\[~accountid:([A-Za-z0-9:_\-]+)\]")

_ISSUE_EVENTS = ("jira:issue_created", "jira:issue_updated")
# Where the connection keeps Jaah's own account id (`settings`), learned from the credential at connect.
ACCOUNT_SETTING = "jira_account_id"
# Where a Forge connection reaches its site (`app.apiBaseUrl` of the app's last call), kept on its settings.
FORGE_API_SETTING = "forge_api_base"
_FORGE_GATEWAY = f"https://{forge_fit.API_GATEWAY_HOST}/ex/jira/"
_FORGE_UNCLAIMED = "Jaah's Jira app is not installed and claimed on this site yet."
_FORGE_LAPSED = "Jaah's Jira app has not called Jaah lately, so its token lapsed; check the app is still installed."
_CUT_NOTE = "\n\n… (shortened for Jira; the full text is on the task)"
_COMMENT_EVENT = "comment_created"


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def signature(secret: str, raw: bytes) -> str:
    """The `X-Hub-Signature` value Jira sends for `raw` under `secret`."""
    return _SIGNATURE_PREFIX + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class JiraAdapter:
    name = "jira"
    ack_sync = False
    admin_adds_webhook = True
    names_connection_in_payload = False
    forge_app = True
    account_setting = ACCOUNT_SETTING

    def __init__(self) -> None:
        # (connection id, credential id) → (account id, fetched at monotonic).
        # (connection id, credential id) → Jaah's account id, for a connection whose row has not kept it yet.
        self._accounts: dict[tuple[str, str], str] = {}

    # ── Inbound ───────────────────────────────────────────────────────────────────────────────────────

    def own_account(self, conn: Any) -> Optional[str]:
        """The account an API-token connection signs in as (kept at connect), whose starts are the connecting
        member's (`intake.starter_for`). None for a Forge connection: its account is the app's, never a person's."""
        if conn.auth_kind != AUTH_BASIC:
            return None
        kept = connections.settings_of(conn).get(ACCOUNT_SETTING)
        return kept if isinstance(kept, str) and kept else None

    def verify(self, conn: Any, headers: Mapping[str, str], raw: bytes, query: Mapping[str, str]) -> bool:
        if conn.auth_kind == AUTH_FORGE:
            return self._verify_forge(conn, headers, raw)
        if conn.auth_kind != AUTH_BASIC:
            return False
        secret = connections.open_webhook_secret(conn)
        sent = _header(headers, SIGNATURE_HEADER)
        if not secret or not sent:
            return False
        return hmac.compare_digest(sent.strip().encode(), signature(secret, raw).encode())

    def parse(self, conn: Any, headers: Mapping[str, str], raw: bytes) -> list[IssueEvent]:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("a Jira webhook body is a JSON object")
        if conn.auth_kind == AUTH_FORGE:
            _keep_forge_call(conn, headers)
            payload = payload.get("event")
            if not isinstance(payload, dict):
                return []  # the app's claim, or its token-renewing call: nothing happened on an issue
        kind = payload.get("webhookEvent")
        if kind not in _ISSUE_EVENTS and kind != _COMMENT_EVENT:
            return []
        issue = payload.get("issue")
        if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
            return []
        site = self._site(conn)
        bot = self._account_id(conn, site)
        if object_session(conn) is not None and connections.settings_of(conn).get(ACCOUNT_SETTING) != bot:
            # Kept on the row (the intake commits it with the event): no later event asks Jira who Jaah is.
            conn.settings = {**(conn.settings or {}), ACCOUNT_SETTING: bot}
        ref = _issue_ref(site, issue)
        if not ref.container_id:
            return []
        title, body = _title_and_body(issue)
        if kind == _COMMENT_EVENT:
            return _comment_events(payload, ref, title, body, bot)
        return _issue_events(payload, ref, title, body, bot, connections.settings_of(conn)["start_label"],
                             created=kind == "jira:issue_created")

    # ── Outbound ──────────────────────────────────────────────────────────────────────────────────────

    async def list_containers(self, conn: Any) -> list[tuple[str, str]]:
        projects = await self._read(conn, jira_api.list_projects)
        return [(key, f"{name} ({key})" if name != key else key) for key, name in projects]

    async def search_issues(self, conn: Any, query: str, container_ids: Sequence[str],
                            limit: int) -> list[protocol.IssueSummary]:
        """The picker's matches inside `container_ids` (project keys), a key-shaped query's own issue first. A
        picker issue's container is its key's prefix: the picker names no project, and its history section is
        not narrowed by `currentJQL`, so `keep_containers` drops what it let through."""
        if not container_ids:
            return []
        keys = list(container_ids)

        def run(site: jira_api.JiraSite) -> list[protocol.IssueSummary]:
            found: list[protocol.IssueSummary] = []
            if protocol.key_shaped(query):
                try:
                    found.append(_detail(site, jira_api.read_issue(site, query.strip())))
                except jira_api.JiraApiError as exc:
                    if exc.status != 404:  # no such issue, or one the account cannot see: the picker's answer
                        raise
            for ref in jira_api.search_issues(site, query, project_keys=keys, limit=limit):
                found.append(protocol.IssueSummary(key=ref.key, title=ref.summary,
                                                   url=jira_api.issue_browse_url(site, ref.key),
                                                   container_id=ref.key.rsplit("-", 1)[0]))
            return protocol.keep_containers(found, keys)[:limit]

        return await self._read(conn, run)

    async def get_issue(self, conn: Any, key: str) -> protocol.IssueDetail:
        return await self._read(conn, lambda site: _detail(site, jira_api.read_issue(site, key)))

    async def ack(self, conn: Any, issue: IssueRef, task_url: str, session_id: Optional[str]) -> None:
        await self.post_comment(conn, issue, f"Started work on this issue: [open the task in Jaah]({task_url})",
                                session_id)

    async def post_comment(self, conn: Any, issue: IssueRef, markdown: str, session_id: Optional[str]) -> None:
        doc = fitted_doc(markdown)
        await self._call(conn, lambda site: jira_api.post_comment_doc(site, issue.key, doc))

    async def mark_done(self, conn: Any, issue: IssueRef, state_name: str, session_id: Optional[str]) -> None:
        await self._call(conn, lambda site: jira_api.transition_to(site, issue.key, state_name))

    async def on_connect(self, conn: Any) -> None:
        """Prove the credential reaches its site (and learn Jaah's account); the site's host is the connection's. A
        credential that can never work (unusable, or refused by Jira) is `ConnectRefused`; Jira unreachable is raised
        as is (the route answers 502). A Forge connection has nothing to prove yet: its app claims it later."""
        if conn.auth_kind == AUTH_FORGE:
            return
        try:
            site = await asyncio.to_thread(self._site, conn)
            account_id = await asyncio.to_thread(self._account_id, conn, site, fresh=True)
        except jira_api.JiraCredentialInvalid as exc:
            raise protocol.ConnectRefused(str(exc)) from exc
        except jira_api.JiraApiError as exc:
            if exc.status in (401, 403, 404):
                raise protocol.ConnectRefused(str(exc)) from exc
            raise
        await asyncio.to_thread(_keep_account, conn.id, account_id)
        conn.site = urlsplit(site.display_base).hostname or conn.site

    async def on_disconnect(self, conn: Any) -> None:
        """Nothing on Jira's side to undo: the webhook is the Jira admin's to delete."""
        self._accounts = {key: value for key, value in self._accounts.items() if key[0] != str(conn.id)}

    # ── The Forge app's calls (the shared ingress) ────────────────────────────────────────────────────

    async def check_webhooks(self, conn: Any, containers: Sequence[tuple[str, str]]) -> list[protocol.WebhookCheck]:
        """An API-token connection's site webhooks against its current address (`protocol`); none for a Forge app,
        which needs no webhook."""
        if conn.auth_kind != AUTH_BASIC:
            return []
        where = conn.site or "Jira"
        try:
            hooks = await self._call(conn, jira_api.list_webhooks)
        except jira_api.JiraCredentialInvalid as exc:
            return [protocol.WebhookCheck(where, "unknown", str(exc))]
        except jira_api.JiraApiError as exc:
            if exc.status in (401, 403):
                return [protocol.WebhookCheck(where, "unknown", _WEBHOOKS_UNREADABLE)]
            return [protocol.WebhookCheck(where, "unknown", str(exc))]
        return [webhook_verdict(conn, where, hooks)]

    def connection_key(self, headers: Mapping[str, str], raw: bytes) -> Optional[str]:
        """The Jira site (cloud id) the call's FIT names — read unverified; `verify` checks it."""
        return forge_fit.cloud_id(forge_fit.unverified_claims(headers))

    def binding_key(self, headers: Mapping[str, str], raw: bytes) -> Optional[str]:
        """The hash of the claim code an unbound site's first call carries (`connections.token_hash`), or None."""
        code = _binding_code(raw)
        return connections.token_hash(code) if code else None

    def _verify_forge(self, conn: Any, headers: Mapping[str, str], raw: bytes) -> bool:
        try:
            claims = forge_fit.verified_claims(headers)
        except forge_fit.FitRefused as exc:
            log.warning("jira forge connection %s: %s", conn.id, exc)
            return False
        cloud = forge_fit.cloud_id(claims) or ""
        code = _binding_code(raw)
        if conn.external_org_id:
            if code is not None:
                # A claim code at a site already bound (a second connection's, or this one's spent code): refused, so
                # the app never reports a claim that bound nothing. Disconnect first to move the site.
                log.warning("jira forge connection %s: a claim code arrived for its bound site; refused", conn.id)
                return False
            return hmac.compare_digest(cloud.encode(), str(conn.external_org_id).encode())
        if not code or not conn.claim_code_hash or conn.claim_expires_at is None or conn.claim_expires_at <= utcnow():
            return False
        if not hmac.compare_digest(connections.token_hash(code).encode(), str(conn.claim_code_hash).encode()):
            return False
        from app.trackers.intake import Claim, resolve_connection

        # Every org's connections (the caller's session sees only this one's org).
        if resolve_connection(self.name, Claim("external_org_id", cloud)) is not None:
            log.warning("jira forge connection %s: site %s is another connection's; claim refused", conn.id, cloud)
            return False
        return True

    # ── Plumbing ──────────────────────────────────────────────────────────────────────────────────────

    async def _call(self, conn: Any, fn: Callable[[jira_api.JiraSite], T]) -> T:
        """`fn(site)` on a worker thread. A read timeout (the call may have landed) is raised as `TimeoutError`,
        which the write-back never retries (`protocol`)."""

        def run() -> T:
            return fn(self._site(conn))

        try:
            return await asyncio.to_thread(run)
        except jira_api.JiraApiError as exc:
            cause = exc.__cause__
            if isinstance(cause, httpx.TimeoutException) and not isinstance(cause, (httpx.ConnectTimeout,
                                                                                    httpx.PoolTimeout)):
                raise TimeoutError(str(exc)) from exc
            raise

    async def _read(self, conn: Any, fn: Callable[[jira_api.JiraSite], T]) -> T:
        """`_call` for a read the picker shows: a refusal is `TrackerError` (the module's messages are operator
        sentences that never carry a token); a read timeout stays `TimeoutError`."""
        try:
            return await self._call(conn, fn)
        except (jira_api.JiraApiError, jira_api.JiraCredentialInvalid) as exc:
            raise protocol.TrackerError(str(exc)) from exc

    def _site(self, conn: Any) -> jira_api.JiraSite:
        """The site the connection's credential reaches — read in the connection's own session when it has one
        (the ingress), else in a session bound to its org."""
        if conn.auth_kind == AUTH_FORGE:
            return _forge_site(conn)
        if conn.credential_id is None:
            raise jira_api.JiraCredentialInvalid("The Jira connection has no credential.")
        session = object_session(conn)
        if session is not None:
            return _site_of(session.scalar(select(Credential).where(Credential.id == conn.credential_id)))
        from app.trackers.intake import connection_session

        with connection_session(conn.id) as (bound, _org):
            return _site_of(bound.scalar(select(Credential).where(Credential.id == conn.credential_id)))

    def _account_id(self, conn: Any, site: jira_api.JiraSite, *, fresh: bool = False) -> str:
        """Jaah's account id: the one the connection keeps (learned at connect), else asked of Jira once."""
        kept = connections.settings_of(conn).get(ACCOUNT_SETTING)
        if not fresh and isinstance(kept, str) and kept:
            return kept
        key = (str(conn.id), str(conn.credential_id))
        if not fresh and key in self._accounts:
            return self._accounts[key]
        account_id = jira_api.myself(site).account_id
        self._accounts[key] = account_id
        return account_id


_WEBHOOKS_UNREADABLE = ("Jaah can't read this site's webhooks: its Jira account isn't a Jira admin. Check Jira › "
                        "System › WebHooks by hand.")


def webhook_verdict(conn: Any, where: str, hooks: Sequence[jira_api.JiraWebhook]) -> protocol.WebhookCheck:
    """Whether one of the site's webhooks posts to the connection's current address. The token is kept only hashed,
    so a webhook is the connection's when its URL's token hashes to `connection_token_hash`."""
    jaah = [(hook, token) for hook in hooks if (token := connections.ingress_token_of(conn, hook.url)) is not None]
    ours = [hook for hook, token in jaah
            if conn.connection_token_hash and connections.token_hash(token) == conn.connection_token_hash]
    def named(hook: jira_api.JiraWebhook) -> str:
        return f"“{hook.name}”" if hook.name.strip() else "A webhook"

    if any(hook.enabled for hook in ours):
        return protocol.WebhookCheck(where, "ok", f"{named(next(h for h in ours if h.enabled))} posts here.")
    if ours:
        return protocol.WebhookCheck(where, "off", f"{named(ours[0])} is switched off in Jira. Turn it on.")
    if jaah:
        return protocol.WebhookCheck(
            where, "stale", f"{named(jaah[0][0])} posts to another connection's address. If that connection is gone, "
                            "make a New webhook URL and paste it and its secret into that webhook.")
    return protocol.WebhookCheck(
        where, "missing", "No Jira webhook posts here. Make a New webhook URL and add it in Jira › System › WebHooks.")


def _keep_account(connection_id: Any, account_id: str) -> None:
    """Save Jaah's account id on the connection's settings (beside the admin's keys, which saving them keeps)."""
    from app.models.trackers import TrackerConnection
    from app.trackers.intake import connection_session

    with connection_session(connection_id) as (session, _org):
        row = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
        if row is not None:
            row.settings = {**(row.settings or {}), ACCOUNT_SETTING: account_id}
            session.commit()


def _binding_code(raw: bytes) -> Optional[str]:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    code = payload.get("binding_code") if isinstance(payload, dict) else None
    return code.strip() if isinstance(code, str) and 0 < len(code.strip()) <= 128 else None


def _keep_forge_call(conn: Any, headers: Mapping[str, str]) -> None:
    """What a verified Forge call tells the connection, staged on its row (the intake commits it): the site it binds
    (spending the claim code), the site's host and gateway address, and the app's fresh system token."""
    claims = forge_fit.unverified_claims(headers)  # `verify` checked this very token
    cloud = forge_fit.cloud_id(claims)
    if conn.external_org_id is None and cloud:
        conn.external_org_id, conn.claim_code_hash, conn.claim_expires_at = cloud, None, None
    conn.site = forge_fit.site_host(claims) or conn.site
    base = forge_fit.api_base(claims)
    if base and connections.settings_of(conn).get(FORGE_API_SETTING) != base:
        conn.settings = {**(conn.settings or {}), FORGE_API_SETTING: base}
    token = forge_fit.system_token(headers)
    if token is not None:
        connections.seal_forge_token(conn, token[0])
        conn.forge_token_expires_at = token[1]
    if not conn.site:
        # A trigger's token names no site address: ask the site itself, once, through the gateway.
        try:
            address = jira_api.server_base(_forge_site(conn))
        except (jira_api.JiraApiError, jira_api.JiraCredentialInvalid) as exc:
            log.warning("jira forge connection %s: the site's address could not be read: %s", conn.id, exc)
            address = None
        conn.site = urlsplit(address).hostname if address else None


def _forge_site(conn: Any) -> jira_api.JiraSite:
    """A Forge connection's site: the gateway address and system token its app's last call left."""
    token = connections.open_forge_token(conn) if conn.external_org_id else None
    if not token:
        raise jira_api.JiraCredentialInvalid(_FORGE_UNCLAIMED)
    if conn.forge_token_expires_at is not None and conn.forge_token_expires_at <= utcnow():
        raise jira_api.JiraCredentialInvalid(_FORGE_LAPSED)
    base = connections.settings_of(conn).get(FORGE_API_SETTING)
    if not isinstance(base, str) or not base.startswith(_FORGE_GATEWAY):
        base = _FORGE_GATEWAY + str(conn.external_org_id)
    return jira_api.JiraSite(base=base, email="", api_token=token, bearer=True,
                             browse_base=f"https://{conn.site}" if conn.site else None)


def fitted_doc(markdown: str) -> dict[str, Any]:
    """`markdown` as ADF inside Jira's comment cap: an over-long write-back keeps its head and says it was cut,
    rather than failing every retry."""
    doc = jira_api.markdown_to_adf(markdown)
    text = markdown
    while len(json.dumps(doc, ensure_ascii=False)) > jira_api.MAX_COMMENT_ADF_CHARS and len(text) > 200:
        text = text[: len(text) // 2]
        doc = jira_api.markdown_to_adf(text.rstrip() + _CUT_NOTE)
    return doc


def _detail(site: jira_api.JiraSite, issue: jira_api.JiraIssue) -> protocol.IssueDetail:
    """`issue` as the picker shows it; its container is the project Jira names (`fields=project`)."""
    return protocol.IssueDetail(key=issue.key, title=issue.summary, url=jira_api.issue_browse_url(site, issue.key),
                                container_id=issue.project_key, body_markdown=issue.description)


def _site_of(credential: Optional[Credential]) -> jira_api.JiraSite:
    if credential is None:
        raise jira_api.JiraCredentialInvalid("The Jira connection's credential was deleted.")
    return jira_api.site_from_credential(credential)


# ── Reading a webhook ─────────────────────────────────────────────────────────────────────────────────


def _issue_ref(site: jira_api.JiraSite, issue: dict[str, Any]) -> IssueRef:
    fields = _dict(issue.get("fields"))
    project = _dict(fields.get("project"))
    key = issue["key"]
    container = _str(project.get("key"))
    if not container and "-" in key:
        container = key.rsplit("-", 1)[0]
    return IssueRef(tracker="jira", site=urlsplit(site.display_base).hostname or site.host, key=key,
                    url=jira_api.issue_browse_url(site, key), container_id=container)


def _text(value: Any) -> str:
    """A Jira text field as markdown: ADF (REST v3) or wiki markup / plain text (what a webhook carries)."""
    if isinstance(value, dict):
        return jira_api.adf_to_markdown(value)
    if isinstance(value, str):
        return _WIKI_MENTION.sub("@user", value).strip()
    return ""


def _title_and_body(issue: dict[str, Any]) -> tuple[str, str]:
    """The issue's summary, and its description followed by its latest comments (when the webhook carries them)."""
    fields = _dict(issue.get("fields"))
    title = _str(fields.get("summary"))
    body = _text(fields.get("description"))
    holder = _dict(fields.get("comment"))
    comments = _list(holder.get("comments"))
    lines = []
    for comment in comments[-_BODY_COMMENTS:]:
        if not isinstance(comment, dict):
            continue
        text = _text(comment.get("body"))
        if text:
            lines.append(f"**{_actor(comment.get('author')).name or 'Someone'}:** {text}")
    if lines:
        body = (body + "\n\n" if body else "") + "## Comments\n\n" + "\n\n".join(lines)
    return title, body


def _actor(user: Any) -> Actor:
    user = _dict(user)
    email = _str(user.get("emailAddress"))
    return Actor(
        external_id=str(user.get("accountId") or ""), name=str(user.get("displayName") or ""), email=email or None,
        # Atlassian verifies an Atlassian account's email; a service-desk customer's may be anything typed.
        email_verified=bool(email) and user.get("accountType") == "atlassian",
    )


def _event(event_id: str, kind: str, ref: IssueRef, actor: Actor, title: str, body: str, *,
           comment_id: Optional[str] = None, text: Optional[str] = None) -> IssueEvent:
    return IssueEvent(event_id=event_id, kind=kind, issue=ref, actor=actor, title=title, body=body,  # type: ignore[arg-type]
                      comment_id=comment_id, text=text, session_id=None)


def _labels(value: Any) -> set[str]:
    return {label.casefold() for label in str(value or "").split()}


def _issue_events(payload: dict[str, Any], ref: IssueRef, title: str, body: str, bot: str, start_label: str, *,
                  created: bool) -> list[IssueEvent]:
    # A change the connection's own account made counts like anyone's: Jaah writes back only comments and the done
    # state, never an assignee or a label, so it is a person's (a connection signed in with that person's API token).
    actor = _actor(payload.get("user"))
    label = start_label.casefold()
    issue = payload["issue"]
    if created:
        # An issue born assigned to Jaah, or with the label: there is no changelog to read, only the fields.
        fields = _dict(issue.get("fields"))
        assignee = _dict(fields.get("assignee"))
        labels = _list(fields.get("labels"))
        out = []
        if assignee.get("accountId") == bot:
            out.append(_event(f"created:{issue.get('id')}:assigned", KIND_ASSIGNED, ref, actor, title, body))
        if label in {str(item).casefold() for item in labels}:
            out.append(_event(f"created:{issue.get('id')}:labeled", KIND_LABELED, ref, actor, title, body))
        return out
    changelog = _dict(payload.get("changelog"))
    items = _list(changelog.get("items"))
    # The changelog id is the change's own: a redelivery carries it again (the intake dedups on it). A change
    # without one could not be told from its replay, so it is dropped.
    stamp = changelog.get("id")
    if not stamp:
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("fieldId") or item.get("field")
        if field == "assignee":
            if item.get("to") == bot and item.get("from") != bot:
                out.append(_event(f"changelog:{stamp}:assigned", KIND_ASSIGNED, ref, actor, title, body))
            elif item.get("from") == bot and item.get("to") != bot:
                out.append(_event(f"changelog:{stamp}:unassigned", KIND_UNASSIGNED, ref, actor, title, body))
        elif field == "labels":
            before, after = _labels(item.get("fromString")), _labels(item.get("toString"))
            if label in after and label not in before:
                out.append(_event(f"changelog:{stamp}:labeled", KIND_LABELED, ref, actor, title, body))
            elif label in before and label not in after:
                out.append(_event(f"changelog:{stamp}:unlabeled", KIND_UNLABELED, ref, actor, title, body))
    return out


def _mentions(body: Any, bot: str) -> bool:
    if isinstance(body, str):
        return bot in _WIKI_MENTION.findall(body)
    stack, seen = [body], 0
    while stack and seen < 10_000:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            attrs = _dict(node.get("attrs"))
            if node.get("type") == "mention" and attrs.get("id") == bot:
                return True
            stack.extend(_list(node.get("content")))
    return False


def _without_mention(body: Any, bot: str, depth: int = 0) -> Any:
    """`body` with Jaah's own mention taken out (the instruction is what the comment says to Jaah)."""
    if depth > jira_api.MAX_ADF_DEPTH:
        return None
    if isinstance(body, str):
        return re.sub(rf"\[~accountid:{re.escape(bot)}\]\s*", "", body)
    if isinstance(body, dict):
        kept = {**body}
        if isinstance(body.get("content"), list):
            kept["content"] = [_without_mention(child, bot, depth + 1) for child in body["content"]
                               if not (isinstance(child, dict) and child.get("type") == "mention"
                                       and isinstance(child.get("attrs"), dict) and child["attrs"].get("id") == bot)]
        return kept
    return body


def _comment_events(payload: dict[str, Any], ref: IssueRef, title: str, body: str, bot: str) -> list[IssueEvent]:
    comment = payload.get("comment")
    if not isinstance(comment, dict) or comment.get("id") is None:
        return []
    author = _actor(comment.get("author"))
    if author.external_id == bot:
        # Jaah's own write-back: handing it on would have the task answer itself.
        return []
    comment_id = str(comment["id"])
    raw_body = comment.get("body")
    if _mentions(raw_body, bot):
        return [_event(f"comment:{comment_id}", KIND_MENTIONED, ref, author, title, body, comment_id=comment_id,
                       text=_text(_without_mention(raw_body, bot)))]
    return [_event(f"comment:{comment_id}", KIND_COMMENT, ref, author, title, body, comment_id=comment_id,
                   text=_text(raw_body))]


ADAPTER = protocol.register(JiraAdapter())
