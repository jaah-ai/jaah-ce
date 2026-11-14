"""The ticket gate's judgement on the control plane (architecture §3.5, PRD D8): is a Jira issue or a ServiceNow change
request in one of the states the gate names?

- Jira through `app/jira_api.py` (its one hand on a Jira site): a fixed JQL passes when any match is in a chosen
  status; "ask when pressed" checks the key typed into the Deploy dialog (`releases.inputs.ticket`).
- ServiceNow through the Table API on `change_request` (an encoded query, or the change number typed), with the
  same Basic login the `servicenow` credential type holds (`integrations/vendors/servicenow.toml`). Its host is the
  customer's, so every call dials only a vetted public address (`http_bounded.vetted_addresses`).

The credential is a scope path on the stored gate, resolved by the broker's own rule (`task_secrets.granted_to`)
for the principal asking: the node's task launcher at run time, the viewer in Settings. No value ever leaves here:
reports name the ticket and its status, never the login.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, Optional, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import http_bounded, jira_api
from app.credential_crypto import has_sealed_secret, open_secret
from app.credential_keys import NodeVaultUnavailable
from app.models.credentials import Credential
from app.models.releases import PipelineGate

log = logging.getLogger("pipeline-tickets")

SYSTEM_LABELS: Mapping[str, str] = {"jira": "Jira", "servicenow": "ServiceNow"}
#: The credential type each system reads.
CREDENTIAL_TYPE: Mapping[str, str] = {"jira": "jira", "servicenow": "servicenow"}
_SN_TIMEOUT_S = 15.0
_SN_MAX_BYTES = 256 * 1024
_SN_MATCHES = 20
#: A ServiceNow change number as people type it (CHG0031042); also any record number of the table.
_SN_NUMBER_RE = re.compile(r"^[A-Za-z]{2,10}[0-9]{1,20}$")
_JIRA_GATEWAY = "https://api.atlassian.com/ex/jira/"
NO_TICKET = "No ticket was given when this step was pressed."


class TicketError(Exception):
    """A plain sentence for the person reading the gate's report or the Settings picker."""


@dataclass(frozen=True)
class Verdict:
    passed: bool
    report: str
    link: Optional[str] = None
    #: The ticket was read and is in another state: at the gate's deadline that is a failure, not a time-out.
    read: bool = False


class Ticket(NamedTuple):
    key: str
    status_id: str
    status_name: str
    url: Optional[str]


# ── Which steps ask for a ticket when pressed ────────────────────────────────────────────────────────


def ask_gates(session: Session, project_id: int) -> dict[Any, PipelineGate]:
    """`{stage_id: gate}`: the first enabled "ask when pressed" ticket gate on the step INTO each stage."""
    rows = session.scalars(select(PipelineGate).where(
        PipelineGate.project_id == project_id, PipelineGate.phase == "before", PipelineGate.kind == "ticket",
        PipelineGate.enabled.is_(True)).order_by(PipelineGate.position))
    out: dict[Any, PipelineGate] = {}
    for gate in rows:
        if (gate.config or {}).get("source") == "ask":
            out.setdefault(gate.stage_id, gate)
    return out


_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def clean_key(value: Optional[str]) -> Optional[str]:
    """A typed ticket key, trimmed, or None; ValueError for something no ticket system calls a key."""
    text = (value or "").strip()
    if not text:
        return None
    if not _KEY_RE.match(text):
        raise ValueError("Not a ticket key (like OAM-123 or CHG0031042).")
    return text


# ── Credentials ─────────────────────────────────────────────────────────────────────────────────────


def resolve(session: Session, principal: Any, project_id: int, path: Optional[str], system: str) -> Credential:
    """The one credential `path` names for `principal` on the project, of `system`'s type, or `TicketError`."""
    from app.routes.agent.task_secrets import granted_to  # noqa: PLC0415 — the broker's rule; routes import us

    label = SYSTEM_LABELS.get(system, system)
    if not path:
        raise TicketError(f"This gate has no {label} credential. Pick one in Settings › Pipeline.")
    if path.lower().startswith("me/"):
        raise TicketError("Pick a credential shared with this project, not a personal one.")
    matches = granted_to(session, principal, project_id, path) if principal is not None else []
    if len(matches) > 1:
        raise TicketError(f"Two credentials share the name {path}. Rename one, then pick it again.")
    if not matches:
        raise TicketError(f"The credential {path} can't be used here. Pick one you can.")
    credential = matches[0][1]
    if credential.type != CREDENTIAL_TYPE.get(system):
        raise TicketError(f"The credential {path} is not a {label} credential.")
    if credential.needs_reconnect_at is not None:
        raise TicketError(f"The credential {path} needs to be connected again.")
    return credential


def _jira_site(session: Session, credential: Credential) -> jira_api.JiraSite:
    """A Jira site from an API-token credential, or from a Connect one (bearer through Atlassian's gateway)."""
    config = credential.config if isinstance(credential.config, dict) else {}
    cloud_id = str(config.get("cloud_id") or "").strip()
    if cloud_id and not has_sealed_secret(config, "api_token"):
        from app import oauth2_tokens  # noqa: PLC0415 — only a Connect row mints

        token = oauth2_tokens.access_token_for(session, credential)
        if token is None or not re.fullmatch(r"[A-Za-z0-9-]+", cloud_id):
            raise TicketError("The Jira connection could not sign in. Connect it again.")
        base = str(config.get("base_url") or "").strip().rstrip("/") or None
        return jira_api.JiraSite(base=_JIRA_GATEWAY + cloud_id, email="", api_token=token.token, bearer=True,
                                 browse_base=base)
    try:
        return jira_api.site_from_credential(credential)
    except jira_api.JiraCredentialInvalid as exc:
        raise TicketError(str(exc)) from None


class _Instance(NamedTuple):
    base: str
    username: str
    password: str


def _servicenow(credential: Credential) -> _Instance:
    config = credential.config if isinstance(credential.config, dict) else {}
    base = str(config.get("instance_url") or "").strip().rstrip("/")
    username = str(config.get("username") or "").strip()
    if not base.startswith("https://") or not username or not has_sealed_secret(config, "password"):
        raise TicketError("The ServiceNow credential needs its instance URL, user name and password.")
    try:
        password = open_secret(credential, "password")
    except NodeVaultUnavailable:
        raise TicketError("NodeVault credential — not available to Jaah") from None
    except ValueError:
        raise TicketError("The ServiceNow password can't be opened; enter it again.") from None
    return _Instance(base, username, password)


# ── ServiceNow Table API ────────────────────────────────────────────────────────────────────────────


def _sn_rows(instance: _Instance, table: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """`GET /api/now/table/{table}` rows, through a client pinned to the vetted public addresses of the instance."""
    url = f"{instance.base}/api/now/table/{table}"
    refusal, addresses = http_bounded.vetted_addresses(url)
    if refusal is not None or not addresses:
        # One sentence whatever the reason: naming why would tell a credential's author which internal names exist.
        log.info("servicenow call refused: %s", refusal or "no address")
        raise TicketError("ServiceNow can't be reached at that address. Use its public https:// address.")
    host = httpx.URL(url).raw_host.decode("ascii")
    # A wall-clock bound on the whole read: the client's timeout bounds each read, not a drip.
    deadline = time.monotonic() + _SN_TIMEOUT_S
    try:
        with http_bounded.pinned_client(host, addresses, _SN_TIMEOUT_S) as client, client.stream(
                "GET", url, params=dict(params), auth=httpx.BasicAuth(instance.username, instance.password),
                headers={"Accept": "application/json"}) as response:
            raw, truncated = http_bounded.read_capped(response, _SN_MAX_BYTES,
                                                      stop=lambda: time.monotonic() > deadline)
            status = response.status_code
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, UnicodeError) as exc:
        raise TicketError(f"ServiceNow can't be reached ({type(exc).__name__}).") from None
    if status in (401, 403):
        raise TicketError(f"ServiceNow refused the login (HTTP {status}). Check the credential and its role.")
    if not 200 <= status < 300:
        raise TicketError(f"ServiceNow answered HTTP {status}.")
    parsed = None if truncated else http_bounded.json_or_none(raw)
    result = parsed.get("result") if isinstance(parsed, dict) else None
    if not isinstance(result, list):
        raise TicketError("ServiceNow answered with something that is not a table result.")
    return [row for row in result if isinstance(row, dict)]


def _sn_field(row: Mapping[str, Any], name: str) -> tuple[str, str]:
    """`(value, display_value)` of a field read with `sysparm_display_value=all`."""
    raw = row.get(name)
    if isinstance(raw, dict):
        return str(raw.get("value") or ""), str(raw.get("display_value") or raw.get("value") or "")
    return str(raw or ""), str(raw or "")


def _sn_ticket(instance: _Instance, row: Mapping[str, Any]) -> Ticket:
    number = _sn_field(row, "number")[1]
    sys_id = _sn_field(row, "sys_id")[0]
    state, state_name = _sn_field(row, "state")
    link = f"{instance.base}/change_request.do?sys_id={sys_id}" if re.fullmatch(r"[0-9a-f]{32}", sys_id) else None
    return Ticket(number, state, state_name, link)


def _sn_changes(instance: _Instance, query: str) -> list[Ticket]:
    rows = _sn_rows(instance, "change_request", {
        "sysparm_query": query, "sysparm_fields": "number,state,sys_id", "sysparm_display_value": "all",
        "sysparm_limit": _SN_MATCHES, "sysparm_exclude_reference_link": "true"})
    return [_sn_ticket(instance, row) for row in rows]


_SN_STATE_RE = re.compile(r"^-?[A-Za-z0-9_]{1,40}$")
_JIRA_STATUS_RE = re.compile(r"^[0-9]{1,20}$")
_JIRA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")


def _jira_url(site: jira_api.JiraSite, key: str) -> Optional[str]:
    """A ticket's browse link, or None: a Connect site with no address of its own has none a person can open."""
    if site.bearer and not site.browse_base:
        return None
    try:
        return jira_api.issue_browse_url(site, key)
    except jira_api.JiraApiError:
        return None


def _sn_number(key: str) -> str:
    text = (key or "").strip()
    if not _SN_NUMBER_RE.match(text):
        raise TicketError(f"{text or 'That'} is not a ServiceNow change number (like CHG0031042).")
    return text.upper()


# ── A signed-in system ──────────────────────────────────────────────────────────────────────────────

class Connection(NamedTuple):
    """A system ready to call: its secrets opened (and a Connect token minted) on the caller's session, so the
    calls themselves need no session and may run off the serving loop."""

    system: str
    jira: Optional[jira_api.JiraSite] = None
    servicenow: Optional[_Instance] = None


def connect(session: Session, credential: Credential, system: str) -> Connection:
    if system == "jira":
        return Connection(system, jira=_jira_site(session, credential))
    if system == "servicenow":
        return Connection(system, servicenow=_servicenow(credential))
    raise TicketError(f"Unknown ticket system {system!r}.")


# ── What Settings and the Deploy dialog read ────────────────────────────────────────────────────────


def states(conn: Connection, project_key: Optional[str] = None) -> list[dict[str, str]]:
    """The states a ticket can be in, from the system itself (AC7.4): `[{id, name}]`."""
    try:
        if conn.jira is not None:
            site = conn.jira
            return [{"id": s.id, "name": s.name}
                    for s in jira_api.statuses(site, project_key or site.project_key)]
        assert conn.servicenow is not None
        rows = _sn_rows(conn.servicenow, "sys_choice", {
            "sysparm_query": "name=change_request^element=state^inactive=false^ORDERBYsequence",
            "sysparm_fields": "value,label,language", "sysparm_limit": 200})
    except jira_api.JiraApiError as exc:
        raise TicketError(str(exc)) from None
    seen: dict[str, str] = {}
    for row in rows:
        value, label = str(row.get("value") or "").strip(), str(row.get("label") or "").strip()
        language = str(row.get("language") or "en")
        if value and label and (value not in seen or language == "en"):
            seen[value] = label
    return [{"id": value, "name": label} for value, label in seen.items()]


def lookup(conn: Connection, key: str) -> Optional[Ticket]:
    """The ticket `key` names, or None when the system knows none (the Deploy dialog's check)."""
    try:
        if conn.jira is not None:
            site = conn.jira
            try:
                found = jira_api.ticket(site, key.strip().upper())
            except jira_api.JiraApiError as exc:
                if exc.status in (400, 404):
                    return None
                raise
            return Ticket(found.key, found.status.id, found.status.name, _jira_url(site, found.key))
        assert conn.servicenow is not None
        instance = conn.servicenow
        try:
            number = _sn_number(key)
        except TicketError:
            return None
        matches = _sn_changes(instance, f"number={number}")
        return matches[0] if matches else None
    except jira_api.JiraApiError as exc:
        raise TicketError(str(exc)) from None


# ── The gate ────────────────────────────────────────────────────────────────────────────────────────


def _named(states_: Sequence[Mapping[str, Any]]) -> str:
    names = [str(s.get("name") or s.get("id")) for s in states_]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def judge(conn: Connection, config: Mapping[str, Any], asked_key: Optional[str]) -> Verdict:
    """The gate's answer: passed when the ticket (or any match of the fixed query) is in one of the chosen states.
    A failure names the ticket and its current status. A system that cannot answer is a failed check, with why."""
    system = conn.system
    chosen = [s for s in config.get("states") or [] if isinstance(s, dict)]
    wanted = {str(s.get("id")) for s in chosen}
    needs = f"needs {_named(chosen)}" if chosen else "no state is chosen"
    label = SYSTEM_LABELS.get(system, system)
    try:
        if config.get("source") == "ask":
            if not (asked_key or "").strip():
                return Verdict(False, NO_TICKET)
            found = lookup(conn, asked_key or "")
            if found is None:
                return Verdict(False, f"{label} has no ticket {asked_key.strip() if asked_key else ''}.")
            tickets = [found]
        elif conn.jira is not None:
            site, query = conn.jira, str(config.get("query") or "").strip()
            if _JIRA_KEY_RE.match(query):  # one ticket, typed as its key: never a query that could match others
                query = f'key = "{query.upper()}"'

            def jira(jql: str) -> list[Ticket]:
                return [Ticket(t.key, t.status.id, t.status.name, _jira_url(site, t.key))
                        for t in jira_api.tickets_matching(site, jql)]

            # Any match in a chosen status passes, however many matches there are: ask for those first. A status
            # Jira no longer has refuses that query; the plain one still answers.
            ids = sorted(i for i in wanted if _JIRA_STATUS_RE.match(i))
            try:
                tickets = jira(f"({query}) AND status in ({', '.join(ids)})") if ids else []
            except jira_api.JiraApiError as exc:
                if exc.status != 400:
                    raise
                tickets = []
            tickets = tickets or jira(query)
        else:
            assert conn.servicenow is not None
            query = str(config.get("query") or "").strip()
            if _SN_NUMBER_RE.match(query):  # one change, typed as its number: ServiceNow ignores a bad condition
                query = f"number={query.upper()}"
            values = sorted(v for v in wanted if _SN_STATE_RE.match(v))
            tickets = _sn_changes(conn.servicenow, f"{query}^stateIN{','.join(values)}") if values else []
            tickets = tickets or _sn_changes(conn.servicenow, query)
    except jira_api.JiraApiError as exc:
        return Verdict(False, f"{label} could not be checked: {exc}")
    except TicketError as exc:
        return Verdict(False, str(exc))
    if not tickets:
        return Verdict(False, f"No {label} ticket matches the query.")
    approved = next((t for t in tickets if t.status_id in wanted), None)
    if approved is not None:
        return Verdict(True, f"{approved.key} is {approved.status_name or 'approved'}.", approved.url)
    first = tickets[0]
    more = f" (and {len(tickets) - 1} more)" if len(tickets) > 1 else ""
    return Verdict(False, f"{first.key} is {first.status_name or 'in an unknown state'}{more}; {needs}.", first.url,
                   read=True)
