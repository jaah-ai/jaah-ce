"""Connect, configure and disconnect a tracker — the org admin's side (`routes/trackers.py`).

A connection owns one trigger of its tracker's source kind and that trigger's parent automation — the default
automation, created on connect so a tracker start works with no other setup: Target = the mapped Jaah projects,
Action = a `coder` Task, Full Auto (no org-level involvement setting exists; the automation's own switch is the one to
change). Saving mappings keeps that Target equal to the primary-linked projects (`apply_settings`; the links
themselves are `project_links`').

What the tracker presents to Jaah is answered once and stored only hashed or sealed: a URL token for a tracker that
posts to its connection's own address (Backlog, a Jira bot user), with a signing secret; a claim code for a Jira Forge
app, which redeems it; nothing for Linear, whose app is Jaah's own and names its workspace in each event.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import automation_service, credential_consumers, credential_crypto
from app.actors import stamp_actor
from app.automations import ACTION_TASK, MODE_SPECIFIC, TARGET_PROJECTS
from app.clock import utcnow
from app.entity_shape import add_with_org_slug, new_uuid7
from app.models.automations import Automation, TaskTrigger
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.models.projects import Project
from app.models.trackers import TrackerConnection
from app.trackers import protocol
from app.trackers.vocabulary import (
    AUTH_FORGE,
    AUTH_KINDS,
    CLAIM_CODE_TTL_HOURS,
    DEFAULT_SETTINGS,
    TRACKER_LABELS,
    TRACKERS,
)
from app.triggers import MAX_CONCURRENCY_CAP, MAX_QUEUE_DEPTH

# The default automation's Task: the intake's event carries the composed prompt and title (`intake.tracker_event`).
BODY_TEMPLATE = "{{tracker.prompt}}"
TITLE_TEMPLATE = "{{tracker.issue_key}}: {{tracker.title}}"
DEFAULT_PERSONA = "coder"
# One task per issue, many issues at once: the trigger's per-project cap, queue and hourly rate are its maxima's
# order, not a webhook's defaults (1 running, 10 an hour), which would hold a team's second issue back.
RATE_LIMIT_PER_HOUR = 200

_WEBHOOK_SECRET = credential_crypto.RowEnvelope(b"tracker-webhook-secret|", "tracker_connections")
_FORGE_TOKEN = credential_crypto.RowEnvelope(b"tracker-forge-token|", "tracker_connections")


_WORKSPACE_HELD = "this workspace is already connected"


class ConnectionRefused(ValueError):
    """A connect or settings save the caller must fix: `field` and the readable reason."""

    def __init__(self, field: str, reason: str, status: int = 422):
        super().__init__(f"{field}: {reason}")
        self.field, self.reason, self.status = field, reason, status


@dataclass
class Connected:
    connection: TrackerConnection
    webhook_secret: Optional[str] = None
    claim_code: Optional[str] = None
    connection_token: Optional[str] = None
    # Set when `new_webhook_secret` passed a system-owned trigger to its caller.
    trigger_owner_taken: bool = False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def resolves_from_payload(tracker: str, auth_kind: str) -> bool:
    """True when the tracker's events name their connection in the payload (the shared ingress, no URL token):
    Linear (Jaah's own OAuth app) and a Jira Forge app."""
    return auth_kind == AUTH_FORGE or bool(getattr(protocol.adapter_for(tracker), "names_connection_in_payload", False))


def has_own_webhook(conn: TrackerConnection) -> bool:
    """True when a site admin adds the connection's webhook by hand (the adapter's `admin_adds_webhook`), so its URL
    and secret may be replaced (`new_webhook_secret`)."""
    adapter = protocol.adapter_for(conn.tracker)
    return (not resolves_from_payload(conn.tracker, conn.auth_kind)
            and bool(getattr(adapter, "admin_adds_webhook", False)))


def ingress_token_of(conn: TrackerConnection, url: str) -> Optional[str]:
    """The connection token `url` posts to this environment's ingress of `conn`'s tracker with
    (`…/api/public/trackers/<tracker>/events/<token>`), or None for an address elsewhere (another environment's
    included)."""
    from app.routes.trackers import ingress_url

    mine, theirs = urlsplit(ingress_url(conn)), urlsplit(url)
    prefix = mine.path.rsplit("/", 1)[0] + "/"
    if (theirs.scheme, theirs.netloc) != (mine.scheme, mine.netloc) or not theirs.path.startswith(prefix):
        return None
    return theirs.path[len(prefix):].strip("/") or None


def open_webhook_secret(conn: TrackerConnection) -> Optional[str]:
    """The plaintext signing secret an adapter's `verify` compares against, or None when the connection has none."""
    if not conn.webhook_secret_sealed:
        return None
    return _WEBHOOK_SECRET.open(conn.webhook_secret_sealed, conn.id)["secret"]


def seal_forge_token(conn: TrackerConnection, token: str) -> None:
    conn.forge_token_sealed = _FORGE_TOKEN.seal(conn.id, {"token": token})


def open_forge_token(conn: TrackerConnection) -> Optional[str]:
    if not conn.forge_token_sealed:
        return None
    return _FORGE_TOKEN.open(conn.forge_token_sealed, conn.id)["token"]


def settings_of(conn: TrackerConnection) -> dict[str, Any]:
    """The connection's settings, every key present (`DEFAULT_SETTINGS` for one never saved)."""
    return {**DEFAULT_SETTINGS, **(conn.settings or {})}


def mapped_project_ids(session: Session, conn: TrackerConnection) -> list[int]:
    """The projects the connection's containers start tasks in (their primary links, `trackers.project_links`),
    oldest link first: the default automation's Target."""
    from app.trackers import project_links

    return list(dict.fromkeys(row.project_id for row in project_links.primary_rows(session, conn.id)))


def connect(session: Session, actor: AdminUser, *, tracker: str, auth_kind: str, credential_id: Optional[UUID],
            site: Optional[str]) -> Connected:
    """Create the connection, its trigger and default automation, and the secrets to answer once. Flushes; the caller
    commits, then runs the adapter's `on_connect`. The connection's org is the actor's own; an actor who is no member
    of it (an operator acting as the org) is refused (409), since the automation would run as them. An existing one
    is fixed by taking ownership (`automation_service.take_ownership`, offered on the connection page)."""
    if tracker not in TRACKERS:
        raise ConnectionRefused("tracker", f"must be one of {', '.join(TRACKERS)}")
    if auth_kind not in AUTH_KINDS:
        raise ConnectionRefused("auth_kind", f"must be one of {', '.join(AUTH_KINDS)}")
    if auth_kind == AUTH_FORGE and not getattr(protocol.adapter_for(tracker), "forge_app", False):
        raise ConnectionRefused("auth_kind", "forge is Jira's app only")
    if protocol.adapter_for(tracker) is None:
        raise ConnectionRefused("tracker", f"{TRACKER_LABELS[tracker]} can't be connected yet", status=409)
    org_id = actor.org_id
    if automation_service.actor_outside_org(actor, org_id):
        # The connection's automation runs as its creator, and an owner outside the org is refused at every
        # start (`trigger_intake.REASON_TRIGGER_OWNER_OTHER_ORG`): never mint one that cannot start a task.
        raise ConnectionRefused(
            "org", automation_service.switch_org_detail(session, org_id, "connect a tracker"), status=409)
    if auth_kind == AUTH_FORGE:
        if credential_id is not None:
            raise ConnectionRefused("credential_id", "a Forge connection holds its own token; send null")
    else:
        if credential_id is None:
            raise ConnectionRefused("credential_id", f"an {auth_kind} connection needs the credential to act with")
        credential = session.scalar(select(Credential).where(Credential.id == credential_id))
        if credential is None or credential.org_id != org_id:
            raise ConnectionRefused("credential_id", "no such credential in this organization", status=404)
        credential_consumers.refuse_if_nodevault(credential, credential_consumers.TRACKER_CONNECTION)
    site = (site or "").strip() or None
    label = TRACKER_LABELS[tracker] + (f" · {site}" if site else "")
    conn = TrackerConnection(
        id=new_uuid7(), org_id=org_id, name=label, tracker=tracker, auth_kind=auth_kind, site=site,
        credential_id=credential_id, settings=dict(DEFAULT_SETTINGS),
    )
    stamp_actor(conn, actor, created=True)
    out = Connected(connection=conn)
    if auth_kind == AUTH_FORGE:
        out.claim_code = secrets.token_urlsafe(32)
        conn.claim_code_hash = token_hash(out.claim_code)
        conn.claim_expires_at = utcnow() + timedelta(hours=CLAIM_CODE_TTL_HOURS)
    elif not resolves_from_payload(tracker, auth_kind):
        out.connection_token, out.webhook_secret = _mint_url_secrets(conn)
    session.add(conn)
    session.flush()
    trigger = _create_trigger(session, conn, actor)
    conn.trigger_id = trigger.id
    session.flush()
    return out


def _mint_url_secrets(conn: TrackerConnection) -> tuple[str, str]:
    """A fresh URL token (stored hashed) and signing secret (stored sealed) for a connection the tracker posts to by its
    own URL; answers both, the only time they are readable."""
    token, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.connection_token_hash = token_hash(token)
    conn.webhook_secret_sealed = _WEBHOOK_SECRET.seal(conn.id, {"secret": secret})
    return token, secret


def new_webhook_secret(session: Session, conn: TrackerConnection, actor: AdminUser) -> Connected:
    """Replace the connection's URL token and signing secret (the old URL stops working at once) and answer the new
    ones once. A connection whose trigger the system owns (one migrated from a per-project Jira secret) passes to
    `actor`,
    an admin of its org, so the starts the new webhook brings can file tasks. `ConnectionRefused` (409) for a
    connection whose webhook an admin does not add by hand (`has_own_webhook`), and for a caller outside the org
    while the system owns the trigger (the starts could never file). Flushes; the caller commits."""
    from app.entity_shape import SYSTEM_ACTOR_ID

    if not has_own_webhook(conn):
        raise ConnectionRefused("tracker", "this connection has no webhook URL to replace", status=409)
    trigger = trigger_of(session, conn)
    system_owned = trigger is not None and trigger.created_by_id == SYSTEM_ACTOR_ID
    if system_owned and automation_service.actor_outside_org(actor, conn.org_id):
        raise ConnectionRefused(
            "org", automation_service.switch_org_detail(session, conn.org_id, "make a new webhook URL"), status=409)
    out = Connected(connection=conn)
    out.connection_token, out.webhook_secret = _mint_url_secrets(conn)
    stamp_actor(conn, actor)
    if system_owned and trigger is not None:
        out.trigger_owner_taken = True
        automation = session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
        for row in (trigger, automation):
            if row is not None:
                row.created_by_id = actor.id
                stamp_actor(row, actor)
    session.flush()
    return out


def _create_trigger(session: Session, conn: TrackerConnection, actor: AdminUser) -> TaskTrigger:
    """The connection's trigger and its default automation (`automation_service.sync_trigger` with the spec)."""
    from app import node_targets

    trigger = TaskTrigger(
        id=new_uuid7(), org_id=conn.org_id, name=conn.name,
        description=f"Starts a task when an issue is assigned to Jaah, mentions Jaah or gets the start label in "
                    f"{TRACKER_LABELS[conn.tracker]}.",
        # The child's one project: no mapping yet, so the org's Nodes project carries it until one is saved.
        project_id=node_targets.ensure_nodes_project(session, conn.org_id).id,
        source_kind=conn.tracker, source_config={"connection_id": str(conn.id)},
        persona=DEFAULT_PERSONA, prompt_template=BODY_TEMPLATE, title_template=TITLE_TEMPLATE, full_auto=True,
        concurrency_cap=MAX_CONCURRENCY_CAP, queue_depth=MAX_QUEUE_DEPTH, rate_limit_per_hour=RATE_LIMIT_PER_HOUR,
    )
    stamp_actor(trigger, actor, created=True)
    add_with_org_slug(session, trigger, trigger.name, default="trigger")
    automation_service.sync_trigger(session, trigger, _spec(conn, []))
    return trigger


def _spec(conn: TrackerConnection, project_ids: list[int],
          params: Optional[dict[str, Any]] = None) -> automation_service.ParentSpec:
    return automation_service.ParentSpec(
        target_kind=TARGET_PROJECTS, target_mode=MODE_SPECIFIC, target_ids=list(project_ids),
        action_type=ACTION_TASK,
        action_params=params if params is not None else {
            "persona": DEFAULT_PERSONA, "body": BODY_TEMPLATE, "title_template": TITLE_TEMPLATE, "autonomous": True,
        },
    )


def apply_settings(session: Session, conn: TrackerConnection, actor: AdminUser, raw: dict[str, Any]) -> None:
    """Save the settings (every key validated; an absent key keeps its value) and bring the default automation's
    Target to the mapped projects. `mappings` writes the connection's links (`project_links.apply_admin_mappings`,
    the rows the project page edits too); a project must be the org's own. Flushes."""
    from app.trackers import project_links

    # The connection row's lock first, as every link write takes it: a concurrent project-page write cannot slip
    # between this save's read of the settings and its write.
    project_links._lock(session, conn.id, conn.org_id)
    changes: dict[str, Any] = {}
    mappings: Optional[list[tuple[str, int]]] = None
    if "mappings" in raw:
        mappings = []
        seen: set[str] = set()
        for entry in raw["mappings"] or []:
            container = str(entry.get("container_id") or "").strip()
            if not container:
                raise ConnectionRefused("mappings", "every mapping needs a container_id")
            if container in seen:
                raise ConnectionRefused("mappings", f"{container!r} is mapped twice")
            seen.add(container)
            try:
                project_id = int(entry.get("project_id"))
            except (TypeError, ValueError):
                raise ConnectionRefused("mappings", f"{container!r} needs a project_id") from None
            project = session.scalar(select(Project).where(Project.id == project_id))
            if project is None or project.org_id != conn.org_id:
                raise ConnectionRefused("mappings", _foreign_project(session, conn, container, project), status=404)
            mappings.append((container, project_id))
    if "start_label" in raw:
        label = str(raw["start_label"] or "").strip()
        if not label:
            raise ConnectionRefused("start_label", "must not be blank")
        changes["start_label"] = label
    if "done_state" in raw:
        changes["done_state"] = (str(raw["done_state"]).strip() or None) if raw["done_state"] is not None else None
    if "allow_unmapped_users" in raw:
        changes["allow_unmapped_users"] = bool(raw["allow_unmapped_users"])
    if mappings is not None:
        project_links.apply_admin_mappings(session, actor, conn, mappings)  # retargets
    conn.settings = {**settings_of(conn), **changes}
    stamp_actor(conn, actor)
    _retarget(session, conn)
    session.flush()


def _foreign_project(session: Session, conn: TrackerConnection, container: str, project: Optional[Project]) -> str:
    """The refusal of a mapping to a project outside the connection's org, worded for a person: never the numeric id.
    A project the caller can read (an operator's) is named with its org; one it cannot (another tenant's, or gone) is
    named by the caller's own container, so the refusal reveals nothing of another org."""
    home = session.get(Org, conn.org_id)
    home_name = home.name if home is not None else "this org"
    if project is None:
        return f"The project mapped to {container} isn't one of {home_name}'s projects. Pick one of theirs."
    other = session.get(Org, project.org_id)
    owner = f"belongs to {other.name}" if other is not None else "is in another org"
    return f"“{project.name}” {owner}, not {home_name}. Pick one of {home_name}'s projects."


def _retarget(session: Session, conn: TrackerConnection) -> None:
    """The default automation's Target = the mapped projects (its action params kept as the admin left them); the
    trigger child carries the first one."""
    trigger = trigger_of(session, conn)
    if trigger is None:
        return
    automation = session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
    project_ids = mapped_project_ids(session, conn)
    if project_ids:
        trigger.project_id = project_ids[0]
    session.flush()
    automation_service.sync_trigger(
        session, trigger, _spec(conn, project_ids, dict(automation.action_params) if automation is not None else None),
    )


def keep_adapter_fields(session: Session, connection_id: UUID, learned: TrackerConnection) -> None:
    """Save what an adapter's `on_connect` set on its (detached) copy — the tracker's workspace id the shared ingress resolves by,
    a confirmed site, a Forge token. Refused (`ConnectionRefused`, 409) when another connection already holds that
    workspace. Flushes."""
    conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
    if conn is None:
        return
    if learned.external_org_id is not None:
        held = session.scalar(select(TrackerConnection.id).where(
            TrackerConnection.tracker == conn.tracker, TrackerConnection.external_org_id == learned.external_org_id,
            TrackerConnection.id != conn.id,
        ))
        if held is not None:
            raise ConnectionRefused("tracker", _WORKSPACE_HELD, status=409)
    conn.external_org_id, conn.site = learned.external_org_id, learned.site
    conn.forge_token_sealed, conn.forge_token_expires_at = learned.forge_token_sealed, learned.forge_token_expires_at
    try:
        session.flush()
    except IntegrityError:  # a concurrent connect claimed the workspace between the check and the flush
        raise ConnectionRefused("tracker", _WORKSPACE_HELD, status=409) from None


def trigger_of(session: Session, conn: TrackerConnection) -> Optional[TaskTrigger]:
    if conn.trigger_id is None:
        return None
    return session.scalar(select(TaskTrigger).where(TaskTrigger.id == conn.trigger_id))


def disconnect(session: Session, conn: TrackerConnection, actor: AdminUser) -> None:
    """Delete the connection, then its trigger through the trigger's own writer (audited; the parent automation goes
    with its last child). The issue links go with the connection (CASCADE); the tasks it started stay. Commits."""
    from app.routes.triggers import write_trigger_delete

    trigger = trigger_of(session, conn)
    session.delete(conn)
    session.flush()
    if trigger is not None:
        write_trigger_delete(session, trigger, actor)
    else:
        session.commit()
