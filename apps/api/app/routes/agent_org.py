"""The agent-facing org doors: read-only views of the launcher's org (projects, nodes, worktrees, members) and a
notify door that reaches any member of it, or a Slack channel the org connected.

Who acts: the human the task acts for (`routes/agent/auth.py::launcher_principal`), never the runner; what each
door may reach is that human's own console authority, asked through the console's own keys and visibility clauses
(`scoped_project_select`, the node roster `routes/nodes/reads.py::_roster`, `visible_worktree_clause`), always
narrowed to the CALLER TASK's org (never the launcher's home org: an operator launcher's is the operator plane). The caller is `live_agent_caller`: declared in `X-Agent-Task-Id`, held by this
node, still live. No persona licence (operator decision 1 in the task's brief: any persona may use any tool).

The notify door's cap (`task_notify.AGENT_MESSAGES_PER_HOUR`, counted off the caller's `message` alert rows, the
catalog event `message`) is the one the task-scoped `notify` checks too: one call writes one such row, whatever it
reached. Each named member is told on their own channels by `user_notify.deliver` (their notification channels decide
the legs); a Slack channel is
posted with the org's own workspace token (`slack_workspaces.bot_token_for_org`) through the plan-gated
`slack_egress.slack_api_post` seam. Naming members asks `users:view` (the member list's own key); a channel post
reaches the whole org, so it asks an org admin of the caller's org (or an operator) and a human-started chain
(`require_human_started`). A refused address is a generic 422 that names none, so notify is no membership oracle.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import (
    agent_runs, alert_relay, alert_record, edition_features, notify_targets, org_names, secret_redact,
    task_notify, user_notify,
)
from app.authz.catalog import PROJECTS_VIEW_KEY, Tenancy
from app.authz.declarations import infra
from app.authz.visibility import scoped_project_select, visible_worktree_clause
from app.db_async import on_session, on_thread
from app.entity_shape import StrictModel, from_row
from app.identities import label_expr
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.notifications import Alert
from app.models.orgs import in_live_org
from app.models.placement_groups import PlacementGroup, PlacementGroupNode
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.user_activity import record_task_note

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`).
from app.routes.agent.auth import AgentDoor, caller_and_launcher, require_human_started
from app.routes.nodes.reads import _node_outs, _roster

log = logging.getLogger("agent-org")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator
# (see `routes/agent_project_settings.py`).
router = APIRouter(tags=["agent-org"])

#: The bound on a read's rows: a model reads the answer, so a page the size of the console's default.
_LIST_LIMIT_MAX = 500
#: The most people one notify call may name: a broadcast to a whole org is a Slack channel's job.
_RECIPIENTS_MAX = 50
#: `#name` (Slack's channel-name alphabet) or a channel id (`C…`/`G…`), the two `chat.postMessage` takes.
_SLACK_CHANNEL_RE = re.compile(r"#[a-z0-9][a-z0-9._-]{0,79}|[CG][A-Z0-9]{8,}")


# ── The wire shapes ───────────────────────────────────────────────────────────────────────────────────


class OrgProjectOut(BaseModel):
    """One project, the facts a runner needs to name it — never a gate command, env or credential."""

    id: int
    name: str
    repo_url: Optional[str]
    forge: Optional[str]
    integration_branch: str
    branch_prefix: str
    push_enabled: bool
    default_persona: Optional[str]
    default_model: Optional[str]


class OrgNodeCountsOut(BaseModel):
    worktrees: int
    task_slots: int
    runners: int


class OrgNodeOut(BaseModel):
    """One node, as the console roster reads it (`NodeOut`), cut to status and capacity."""

    id: str
    name: str
    # `online` | `offline` | `unknown` (`jaah_discovery.status_and_last_seen`).
    status: str
    last_seen: Optional[str]
    enabled: bool
    draining: bool
    # `aws` | `self_hosted` | `co_located` | `multi_node_host`, or None when the reader may not see the host.
    hosting: Optional[str]
    os: str
    microvm: bool
    counts: OrgNodeCountsOut
    # The node groups this node is in, by name (`app/placement_groups.py`); every node is also in All.
    groups: list[str] = []


class OrgWorktreeOut(BaseModel):
    id: UUID
    node_id: UUID
    node: str
    project_id: int
    project: str
    number: int
    branch: Optional[str]
    # `free` | `task`: the slot's only occupancy fact (`ck_worktrees_state_ownership`).
    state: str
    task_id: Optional[int]
    last_active: Optional[datetime]


class OrgMemberOut(BaseModel):
    id: UUID
    name: str
    email: str


class OrgNotifyIn(StrictModel):
    """`users` (ids or emails) and/or `slack_channel` (`#name` or a channel id); at least one."""

    message: str = Field(min_length=1, max_length=task_notify.AGENT_MESSAGE_MAX_CHARS)
    users: list[str] = Field(default_factory=list, max_length=_RECIPIENTS_MAX)
    slack_channel: Optional[str] = Field(default=None, max_length=80)


class OrgNotifyRecipientOut(BaseModel):
    user_id: UUID
    email: str
    # Each channel's `user_notify.Delivery` outcome (slack, email, telegram): `sent`, `failed`, or `skipped: <reason>`.
    channels: dict[str, str]


class OrgNotifySlackOut(BaseModel):
    channel: str
    # `sent`, or `failed: <Slack's error>`.
    outcome: str


class OrgNotifyOut(BaseModel):
    recipients: list[OrgNotifyRecipientOut]
    slack_channel: Optional[OrgNotifySlackOut]


# ── Who acts ──────────────────────────────────────────────────────────────────────────────────────────


def _require_key(principal: Any, caller: Task, key: str) -> None:
    """403 unless the launcher holds `key` somewhere — the console route's own `@feature` floor."""
    if not principal.has(key):
        log.info("agent org: refused task=%s (launcher lacks %s)", caller.id, key)
        raise HTTPException(
            status_code=403,
            detail=(
                f"the person task {caller.id} acts for does not hold {key}, so they could not see this in the "
                "console either. Name it in your report."
            ),
        )


# ── The reads ─────────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/agent/tasks/org/projects", response_model=list[OrgProjectOut])
@infra(Tenancy.ORG_SCOPED)
@on_session
def list_org_projects(
    session: Session,
    door: AgentDoor,
    limit: int = Query(default=200, ge=1, le=_LIST_LIMIT_MAX),
) -> list[OrgProjectOut]:
    """The projects of the launcher's org they may view (`GET /api/projects`' own scope, by name)."""
    caller, _user, principal = caller_and_launcher(session, *door)
    rows = session.scalars(
        scoped_project_select(principal, key=PROJECTS_VIEW_KEY)
        .where(Project.org_id == caller.org_id, in_live_org(Project.org_id))
        .order_by(Project.name)
        .limit(limit)
    ).all()
    return [OrgProjectOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/api/agent/tasks/org/nodes", response_model=list[OrgNodeOut])
@infra(Tenancy.ORG_SCOPED)
@on_session(side_reads=True)
def list_org_nodes(
    session: Session,
    door: AgentDoor,
) -> list[OrgNodeOut]:
    """The launcher's org's nodes they may view, with presence and slot counts: the console roster
    (`GET /api/nodes`, tenant-narrowed) without its presence-only hosts, which belong to no org."""
    caller, user, principal = caller_and_launcher(session, *door)
    _require_key(principal, caller, "nodes:view")
    rows, presence, _ids = _roster(session, principal)
    rows = {key: row for key, row in rows.items() if row.org_id == caller.org_id}
    outs = _node_outs(session, user, principal, sorted(rows, key=lambda key: (rows[key].name, key)), rows, presence)
    groups = _groups_by_node(session, [out.id for out in outs if out.id])
    return [
        from_row(OrgNodeOut, out, id=out.id or "", counts=from_row(OrgNodeCountsOut, out.counts),
                 groups=groups.get(out.id or "", []))
        for out in outs
    ]


def _groups_by_node(session: Session, node_ids: list[str]) -> dict[str, list[str]]:
    """`{node id: its node group names, sorted}` in one read."""
    out: dict[str, list[str]] = {}
    if not node_ids:
        return out
    for node_id, name in session.execute(
        select(PlacementGroupNode.node_id, PlacementGroup.name)
        .join(PlacementGroup, PlacementGroup.id == PlacementGroupNode.group_id)
        .where(PlacementGroupNode.node_id.in_([UUID(node_id) for node_id in node_ids]))
        .order_by(PlacementGroup.name)
    ).all():
        out.setdefault(str(node_id), []).append(name)
    return out


@router.get("/api/agent/tasks/org/worktrees", response_model=list[OrgWorktreeOut])
@infra(Tenancy.ORG_SCOPED)
@on_session
def list_org_worktrees(
    session: Session,
    door: AgentDoor,
    project: Optional[int] = Query(default=None, description="a project id from task_org_projects"),
    node: Optional[UUID] = Query(default=None, description="a node id from task_org_nodes"),
    limit: int = Query(default=200, ge=1, le=_LIST_LIMIT_MAX),
) -> list[OrgWorktreeOut]:
    """The launcher's org's pool slots they may view (`GET /api/worktrees`' own clause), with who holds each."""
    caller, _user, principal = caller_and_launcher(session, *door)
    _require_key(principal, caller, "worktrees:view")
    stmt = (
        select(Worktree, Project.name, Node.name)
        .join(Project, Project.id == Worktree.project_id)
        .join(Node, Node.id == Worktree.node_id)
        .where(visible_worktree_clause(principal), Worktree.org_id == caller.org_id)
        .order_by(Project.name, Worktree.number, Node.name)
        .limit(limit)
    )
    if project is not None:
        stmt = stmt.where(Worktree.project_id == project)
    if node is not None:
        stmt = stmt.where(Worktree.node_id == node)
    return [
        from_row(OrgWorktreeOut, wt, node=node_name, project=project_name)
        for wt, project_name, node_name in session.execute(stmt).all()
    ]


@router.get("/api/agent/tasks/org/members", response_model=list[OrgMemberOut])
@infra(Tenancy.ORG_SCOPED)
@on_session
def list_org_members(
    session: Session,
    door: AgentDoor,
    limit: int = Query(default=200, ge=1, le=_LIST_LIMIT_MAX),
) -> list[OrgMemberOut]:
    """The launcher's org's enabled members, at the People tab's key (`users:view`): who notify may name."""
    caller, _user, principal = caller_and_launcher(session, *door)
    _require_key(principal, caller, "users:view")
    rows = session.execute(
        select(AdminUser.id, label_expr(), AdminUser.email)
        .where(
            AdminUser.org_id == caller.org_id, AdminUser.is_system.is_(False), AdminUser.disabled_at.is_(None),
        )
        .order_by(AdminUser.email)
        .limit(limit)
    ).all()
    return [OrgMemberOut(id=member_id, name=name, email=email) for member_id, name, email in rows]


# ── Notify ────────────────────────────────────────────────────────────────────────────────────────────


def _recipients(session: Session, org_id: UUID, named: list[str]) -> list[AdminUser]:
    """Each named enabled member of `org_id` (by id, else by email) — `list_org_members`' row set — deduplicated
    in order; one generic 422 before anything is sent when any entry is not, naming none (no membership oracle)."""
    found: dict[UUID, AdminUser] = {}
    in_org = (AdminUser.org_id == org_id, AdminUser.is_system.is_(False), AdminUser.disabled_at.is_(None))
    for entry in (raw.strip() for raw in named):
        try:
            where = AdminUser.id == UUID(entry)
        except ValueError:
            where = func.lower(AdminUser.email) == entry.lower()
        member = session.scalars(select(AdminUser).where(where, *in_org)).first() if entry else None
        if member is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "a named user is not an enabled member of this org (task_org_members lists who is); nothing "
                    "was sent"
                ),
            )
        found.setdefault(member.id, member)
    return list(found.values())


def _slack_token(org_id: UUID, channel: str) -> str:
    """The org's own workspace token for a channel post, or a 409 saying why there is none."""
    if not _SLACK_CHANNEL_RE.fullmatch(channel):
        raise HTTPException(status_code=422, detail=f"slack_channel must be '#name' or a channel id, got {channel!r}")
    if not edition_features.enabled("slack"):
        raise HTTPException(status_code=409, detail="this edition has no Slack; nothing was sent")
    # The `slack` feature's modules: imported past its guard, so an edition without it never loads them.
    from app import slack_egress, slack_workspaces

    if not slack_egress.plan_allows(org_id):
        raise HTTPException(status_code=409, detail="this org's plan does not include Slack; nothing was sent")
    token = slack_workspaces.bot_token_for_org(org_id)
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="this org has no connected Slack workspace (an org admin connects one in Settings); nothing was sent",
        )
    return token


def _event(session: Session, caller: Task, message: str, agent_name: str, recipient: AdminUser) -> dict[str, Any]:
    """The `alert_relay.message` event one member is told. Names the caller task (its title, its link) only to a
    member who may read it (`notify_targets.may_read_task`), as `task_notify._tell_creator` asks."""
    payload: dict[str, Any] = {
        "actor": agent_name, "question_body": alert_relay.inert_mrkdwn(message),
        "org": org_names.org_name(session, caller.org_id),
    }
    if not notify_targets.may_read_task(session, recipient, caller.id):
        return alert_relay.message(task_notify.ORG_NOTIFY_EVENT, message, payload=payload)
    built = task_notify.build_task_notify_event(
        caller, session.get(Project, caller.project_id), None, task_notify.ORG_NOTIFY_EVENT, reason_body=message,
        org=payload["org"],
    )
    return alert_relay.message(
        task_notify.ORG_NOTIFY_EVENT, built.get("reason"), repo=built.get("repo"), test=bool(built.get("test")),
        task_id=caller.id, payload={**built["payload"], **payload},
    )


def _post_to_channel(org_id: UUID, token: str, channel: str, text: str) -> str:
    if not edition_features.enabled("slack"):
        return "failed: this edition has no Slack"
    from app import slack_egress

    try:
        parsed = slack_egress.slack_api_post(
            "chat.postMessage", token, {"channel": channel, "text": text}, org_id=org_id
        )
    except slack_egress.SlackApiError as exc:
        log.warning("agent org: Slack post to %s failed (%s)", channel, exc)
        return "failed: transport"
    return "sent" if parsed.get("ok") is True else f"failed: {parsed.get('error') or 'unknown'}"


@router.post("/api/agent/tasks/org/notify", response_model=OrgNotifyOut)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def notify_org(
    session: Session,
    body: OrgNotifyIn,
    door: AgentDoor,
) -> OrgNotifyOut:
    """Tell members of the launcher's org (each on their own channels) and/or post into a Slack channel the org
    connected. 429 past the task's hourly cap; answers each recipient's per-channel outcome."""
    # A worker thread: the Slack post is blocking outbound I/O made while the caller's row lock is held.
    caller, user, principal = caller_and_launcher(session, *door)
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message is empty")
    if not body.users and not body.slack_channel:
        raise HTTPException(status_code=422, detail="name at least one of users or slack_channel")
    org_id = caller.org_id
    if body.users:
        _require_key(principal, caller, "users:view")
    if body.slack_channel:
        require_human_started(session, caller, what="posting to a Slack channel")
        # An org admin of THIS org, or an operator: `is_org_admin` alone holds for an admin of any org.
        if not (principal.is_operator or (principal.is_org_admin and user.org_id == org_id)):
            log.info("agent org: refused Slack post to task=%s (launcher is no admin of its org)", caller.id)
            raise HTTPException(
                status_code=403,
                detail=(
                    f"posting to a Slack channel reaches the whole org, so it is an org admin's; the person task "
                    f"{caller.id} acts for is not one. Name the people instead, or name it in your report."
                ),
            )
    recipients = _recipients(session, org_id, body.users)
    channel = body.slack_channel.strip() if body.slack_channel else None
    token = _slack_token(org_id, channel) if channel else None
    # Lock-then-count: held to the commit, so a concurrent call counts this one's alert row.
    session.execute(select(Task.id).where(Task.id == caller.id).with_for_update())
    posted = task_notify.agent_messages_last_hour(session, caller.id)
    if posted >= task_notify.AGENT_MESSAGES_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=(
                f"task {caller.id} has posted {posted} messages in the last hour, the most allowed "
                f"({task_notify.AGENT_MESSAGES_PER_HOUR}); try again later"
            ),
        )
    message = secret_redact.redact(body.message)[0]
    agent_name = agent_runs.run_agent_name(caller) or "Agent"
    to = {"users": [str(r.id) for r in recipients], "slack_channel": channel}
    # The console's record of the post and what spends the cap; a fresh `issue` so `uq_alerts_dedup` never folds two.
    session.add(Alert(
        org_id=caller.org_id, kind=task_notify.ORG_NOTIFY_EVENT, task_id=caller.id,
        issue=f"{task_notify.ORG_NOTIFY_EVENT}-{uuid.uuid4().hex[:12]}",
        window_start=alert_record.current_window_start(), reason=message, payload=to,
    ))
    record_task_note(session, caller.id, "agent_message", message, {"agent": agent_name, **to})
    # Counted before anything leaves: a commit that fails after delivery would send messages the cap never saw.
    session.commit()
    delivered = [
        (recipient, user_notify.deliver(
            session, recipient, _event(session, caller, message, agent_name, recipient),
            event_name=task_notify.ORG_NOTIFY_EVENT,
        ))
        for recipient in recipients
    ]
    slack_out = None
    if channel is not None and token is not None:
        text = f"{agent_name} (task {caller.id}): {alert_relay.inert_mrkdwn(message)}"
        slack_out = OrgNotifySlackOut(channel=channel, outcome=_post_to_channel(org_id, token, channel, text))
    # What `user_notify.deliver` recorded (its activity rows).
    session.commit()
    log.info(
        "agent org: task=%s notified users=%s slack=%s (as launcher=%s)",
        caller.id, len(recipients), slack_out.outcome if slack_out else None, user.id,
    )
    return OrgNotifyOut(
        recipients=[
            OrgNotifyRecipientOut(user_id=r.id, email=r.email, channels=d.as_data()) for r, d in delivered
        ],
        slack_channel=slack_out,
    )
