"""The Alerts panel's API: a membership's notification channels and the catalog it sets them against.

Mounted twice over one set of handlers: `/api/me/notification-channels` (the signed-in membership, Profile > Alerts)
and `/api/users/{user_id}/notification-channels` (`/users/:id` > Notifications). The rules are
`app/notification_channels.py`'s and the vocabulary `app/notification_catalog.py`'s.

* `GET` — `{channels, catalog, add_options}`.
* `PATCH /{channel_id}` — `{settings}` (the whole document) or `{group, level}` / `{event, level}`; 422 on a choice the
  catalog refuses (an unknown name or level, or an Owner/Admin-only alert set for a member).
* `DELETE /{channel_id}` — remove it; the account-email channel may be removed too, and adding its
  address as an Email channel brings it back.
* `GET /api/me/notification-channels/slack/channels` — the org workspace's channels an Owner/Admin may pick
  (`[{id, name, is_private, is_member}]`); 409 `{message, reconnect: true}` while the workspace never granted the
  channel scopes (`app/slack_conversations.py`).
* `POST /api/me/notification-channels` `{type: 'slack', kind: 'channel', conversation_id}` — add that channel (201);
  Owners/Admins and the operator only. The name is read from Slack, never taken from the request.
* `POST /api/me/notification-channels/slack-dm/code` — a Connect code; its redeem adds a Slack DM channel (a second
  Slack member's Connect is a second DM). The code's status is `GET /api/auth/me/slack-link-code`.
* `POST /api/me/notification-channels/telegram/code` — `{code, expires_at, private_link, group_link}`: the bot's deep
  links carrying one code; the chat or group that redeems it becomes a Telegram channel (each further chat a further
  channel). The code's status is `GET /api/auth/me/telegram-link`. Removing a Telegram channel deletes its chat's
  link once no other membership of the person has a channel there.
* `POST /api/me/notification-channels` `{type: "email", address}` — 201 one Channel, unverified, its confirm email sent; `POST /{channel_id}/verify`
  `{code}` — the Channel, proven; `POST /{channel_id}/resend` — 202, a fresh link and code (the account-email
  channel's Verify). The rules and the plain refusals are `app/notification_emails.py`'s.

Authorization: whoever may edit a user on `/users/:id` (an Org Owner/Admin in their own org, the operator fleet-wide —
`PATCH /api/users/{id}`'s bar, its Owner and superadmin rules included) may change another membership's settings and remove its channels; reading needs
`users:view`, as `GET /api/users/{id}` does. Adding a destination is the person's own act (the Connect and
deep-link flows, the Slack channel picker), so only the `/api/me` mount adds one.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated, Any, Iterator, Literal, Optional, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import access, ce_caps, db_async, edition_features, notification_catalog, notification_channels, notification_emails, org_gate
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.principal import AsyncCaller, Principal, current_caller, current_principal
from app.authz.declarations import always, feature, org_admin, session_only
from app.db_async import DbDep, on_session
from app.entity_shape import get_or_404, or_404
from app.models.identity import AdminUser
from app.tenancy import seam

router = APIRouter(tags=["notifications"])


class ChannelPatch(BaseModel):
    """One PATCH: a whole settings document, or one group's or one event's level."""

    model_config = ConfigDict(extra="forbid")

    settings: Optional[dict[str, Any]] = None
    group: Optional[str] = None
    event: Optional[str] = None
    level: Optional[Literal["off", "brief", "full"]] = None


class ChannelAdd(BaseModel):
    """`POST /api/me/notification-channels`: the one addable shape here, a Slack channel by its conversation id."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["slack"]
    kind: Literal["channel"]
    conversation_id: str


class EmailChannelIn(BaseModel):
    """`POST …/notification-channels`: a typed address to add as an Email channel."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["email"]
    address: str = Field(max_length=320)


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=16)


def _target_user(session: Session, user_id: UUID) -> AdminUser:
    user = get_or_404(session, AdminUser, user_id, detail="unknown user")
    if user.is_system or org_gate.org_is_deleted(session, user.org_id):
        raise HTTPException(status_code=404, detail="unknown user")
    return user


def _editable_target(session: Session, user_id: UUID, actor: AdminUser, principal: Optional[Principal]) -> AdminUser:
    """The member whose channels an admin changes, under `PATCH /api/users/{id}`'s own bar: only a superadmin
    touches a superadmin's account, and only an Owner (or the operator) an Owner's (`access.may_edit_user`)."""
    user = _target_user(session, user_id)
    if user.is_superadmin and not actor.is_superadmin:
        raise HTTPException(status_code=403, detail="Only a superadmin can change a superadmin's account.")
    if not access.may_edit_user(session, principal, user_id):
        raise HTTPException(status_code=403, detail=access.OWNER_SUBJECT_DETAIL)
    return user


@contextmanager
def _own(session: Session, admin: AdminUser) -> Iterator[AdminUser]:
    """The signed-in membership, with its org entered for the block: a `BOOTSTRAP` route binds nothing, and a
    channel is its owner's org's row."""
    with seam.identity_scope():
        user = session.scalars(select(AdminUser).where(AdminUser.id == admin.id)).first()
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    with seam.bound_to(user.org_id):
        yield user


def _channel(session: Session, user: AdminUser, channel_id: UUID) -> Any:
    return or_404(notification_channels.find(session, user, channel_id), detail="unknown channel")


def _patch(session: Session, user: AdminUser, actor: AdminUser, channel_id: UUID, body: ChannelPatch) -> dict:
    row = _channel(session, user, channel_id)
    try:
        notification_channels.change_settings(
            session, row, user, actor, settings=body.settings, group=body.group, event=body.event, level=body.level,
        )
    except notification_catalog.ChoiceRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return notification_channels.channels_document(session, user, actor)


def _delete(session: Session, user: AdminUser, actor: AdminUser, channel_id: UUID) -> dict:
    notification_channels.remove(session, _channel(session, user, channel_id), user)
    session.commit()
    return notification_channels.channels_document(session, user, actor)


def _one(session: Session, user: AdminUser, row: Any) -> dict:
    return notification_channels.channel_document(session, row, user, notification_channels.viewer_is_admin(session, user))


def _added(session: Session, user: AdminUser, actor: AdminUser, body: EmailChannelIn) -> tuple[dict, Any]:
    """The new Email channel's document and its confirm mail (None when the address was already proven), unsent."""
    row, mail = notification_emails.add(session, user, actor, body.address)
    return _one(session, user, row), mail


def _add(session: Session, user: AdminUser, actor: AdminUser, body: EmailChannelIn) -> dict:
    channel, mail = _added(session, user, actor, body)
    if mail is not None:
        notification_emails.send(mail)
    return channel


def _verify(session: Session, user: AdminUser, actor: AdminUser, channel_id: UUID, body: CodeIn) -> dict:
    row = _channel(session, user, channel_id)
    notification_emails.verify_code(session, user, actor, row, body.code)
    # The person's login email may have been stamped by the redeem's own UPDATE: read it fresh.
    session.expire_all()
    return _one(session, user, _channel(session, user, channel_id))


def _resend(session: Session, user: AdminUser, actor: AdminUser, channel_id: UUID) -> dict:
    notification_emails.send(notification_emails.resend(session, user, actor, _channel(session, user, channel_id)))
    return {"sent": True}


# ── The signed-in membership ──────────────────────────────────────────────────────────────────────────────────


@router.get("/api/me/notification-channels")
@always(Tenancy.BOOTSTRAP)
@session_only
@on_session
def my_channels(session: Session, admin: AdminUser = Depends(require_user)) -> dict:
    with _own(session, admin) as user:
        return notification_channels.channels_document(session, user, user)


@router.patch("/api/me/notification-channels/{channel_id}")
@always(Tenancy.BOOTSTRAP)
@session_only
@on_session
def patch_my_channel(
    session: Session, channel_id: UUID, body: ChannelPatch, admin: AdminUser = Depends(require_user),
) -> dict:
    with _own(session, admin) as user:
        return _patch(session, user, user, channel_id, body)


@router.delete("/api/me/notification-channels/{channel_id}")
@always(Tenancy.BOOTSTRAP)
@session_only
@on_session
def delete_my_channel(session: Session, channel_id: UUID, admin: AdminUser = Depends(require_user)) -> dict:
    with _own(session, admin) as user:
        return _delete(session, user, user, channel_id)


# ── Slack: the org workspace's channels, and a further DM ────────────────────────────────────────────────────


def _may_add_channel(caller: AsyncCaller) -> bool:
    """An Owner or Admin of the caller's org, or the operator: `notification_channels.viewer_is_admin`'s bar, read
    off the request's principal so a member is refused before Slack is asked."""
    principal = caller.principal
    return principal is not None and (principal.is_org_admin or principal.is_operator)


def _reconnect() -> JSONResponse:
    # Inline, before the import: an edition without Slack ships no `slack_*` module (the community tree's check).
    if not edition_features.enabled("slack"):
        raise ce_caps.not_in_edition()
    from app import slack_conversations

    return JSONResponse(
        status_code=409, content={"detail": {"message": slack_conversations.RECONNECT, "reconnect": True}},
    )


@router.get("/api/me/notification-channels/slack/channels")
@always(Tenancy.BOOTSTRAP)
@session_only
async def my_slack_channels(db: DbDep, caller: AsyncCaller = Depends(current_caller)) -> Any:
    """The channels of the caller's org's workspace, to pick an alert channel from. Slack is asked off the event
    loop with the route's connection handed back first."""
    # Inline, before the import: an edition without Slack ships no `slack_*` module (the community tree's check).
    if not edition_features.enabled("slack"):
        raise ce_caps.not_in_edition()
    from app import slack_conversations

    if not _may_add_channel(caller):
        raise HTTPException(status_code=403, detail=notification_channels.ADMIN_ONLY)
    org_id = caller.actor.org_id
    await db_async.release(db)

    def listed() -> list[dict[str, Any]]:
        workspace = slack_conversations.workspace_of(org_id)
        return [found.document() for found in slack_conversations.channels(workspace)]

    try:
        return {"channels": await run_in_threadpool(listed)}
    except slack_conversations.NeedsReconnect:
        return _reconnect()
    except slack_conversations.SlackRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


@router.post("/api/me/notification-channels", status_code=201)
@always(Tenancy.BOOTSTRAP)
@session_only
async def add_my_channel(
    db: DbDep, body: Annotated[Union[ChannelAdd, EmailChannelIn], Field(discriminator="type")],
    caller: AsyncCaller = Depends(current_caller),
) -> Any:
    """Add a channel. A typed email address: one Channel, unverified, its confirm email sent (`_add`). A Slack channel
    of the org's workspace: checked with Slack (it exists, a private one has the bot in it) and named from Slack's
    answer; the row stores its conversation id, and the answer is the whole channels document."""
    if isinstance(body, EmailChannelIn):
        email = body

        def add_email(session: Session) -> tuple[dict, Any]:
            with _own(session, caller.actor) as user:
                return _added(session, user, user, email)

        channel, mail = await db.run_sync(add_email)
        await db_async.release(db)
        if mail is not None:
            # The mail goes out off the event loop, the route's connection handed back first.
            await run_in_threadpool(notification_emails.send, mail)
        return channel
    # Inline, before the import: an edition without Slack ships no `slack_*` module (the community tree's check).
    if not edition_features.enabled("slack"):
        raise ce_caps.not_in_edition()
    from app import slack_conversations

    if not _may_add_channel(caller):
        raise HTTPException(status_code=403, detail=notification_channels.ADMIN_ONLY)
    if not slack_conversations.CONVERSATION_ID_RE.match(body.conversation_id):
        raise HTTPException(status_code=422, detail="conversation_id is not a Slack channel id")
    org_id = caller.actor.org_id
    await db_async.release(db)

    def found() -> tuple[str, Any]:
        workspace = slack_conversations.workspace_of(org_id)
        return workspace.team_id, slack_conversations.conversation(workspace, body.conversation_id)

    try:
        team_id, conversation = await run_in_threadpool(found)
    except slack_conversations.NeedsReconnect:
        return _reconnect()
    except slack_conversations.SlackRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None

    def write(session: Session) -> dict:
        with _own(session, caller.actor) as user:
            try:
                notification_channels.add_slack_channel(
                    session, user, team_id=team_id, conversation_id=conversation.id, name=conversation.name,
                )
            except notification_channels.ChannelRefused as exc:
                session.rollback()
                raise HTTPException(status_code=exc.status, detail=exc.detail) from None
            session.commit()
            return notification_channels.channels_document(session, user, user)

    return await db.run_sync(write)


@router.post("/api/me/notification-channels/slack-dm/code")
@always(Tenancy.BOOTSTRAP)
@session_only
async def issue_my_slack_dm_code(db: DbDep, admin: AdminUser = Depends(require_user)) -> Any:
    """A Connect code for a Slack DM channel: the Slack member who sends it to the bot becomes a DM of the caller's
    (a second member, a second DM). The same code and status as `POST/GET /api/auth/me/slack-link-code`."""
    from app.routes import auth as auth_routes

    return await auth_routes.issue_slack_link_code(db=db, admin=admin)


@router.post("/api/me/notification-channels/telegram/code")
@always(Tenancy.BOOTSTRAP)
@session_only
async def issue_my_telegram_code(db: DbDep, admin: AdminUser = Depends(require_user)) -> Any:
    """A code for a further Telegram channel, in a private-chat link and an add-to-group link. The same code and
    status as `POST /api/auth/me/telegram-link-code` and `GET /api/auth/me/telegram-link`."""
    if not edition_features.enabled("telegram"):
        raise ce_caps.not_in_edition()
    from app.routes import auth as auth_routes

    issued = await auth_routes.issue_telegram_link_code(db=db, admin=admin)
    return {"code": issued.code, "expires_at": issued.expires_at, "private_link": issued.url, "group_link": issued.group_url}


@router.post("/api/me/notification-channels/{channel_id}/verify")
@always(Tenancy.BOOTSTRAP)
@session_only
@on_session
def verify_my_email(
    session: Session, channel_id: UUID, body: CodeIn, admin: AdminUser = Depends(require_user),
) -> dict:
    with _own(session, admin) as user:
        return _verify(session, user, user, channel_id, body)


@router.post("/api/me/notification-channels/{channel_id}/resend", status_code=202)
@always(Tenancy.BOOTSTRAP)
@session_only
@on_session
def resend_my_email(session: Session, channel_id: UUID, admin: AdminUser = Depends(require_user)) -> dict:
    with _own(session, admin) as user:
        return _resend(session, user, user, channel_id)


# ── Another membership (`/users/:id`) ─────────────────────────────────────────────────────────────────────────


@router.get("/api/users/{user_id}/notification-channels")
@feature("users", "view", tenancy=Tenancy.CALLER_PLANE)
@on_session
def user_channels(session: Session, user_id: UUID, actor: AdminUser = Depends(require_user)) -> dict:
    return notification_channels.channels_document(session, _target_user(session, user_id), actor)


@router.patch("/api/users/{user_id}/notification-channels/{channel_id}")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def patch_user_channel(
    session: Session, user_id: UUID, channel_id: UUID, body: ChannelPatch,
    actor: AdminUser = Depends(require_user), principal: Optional[Principal] = Depends(current_principal),
) -> dict:
    return _patch(session, _editable_target(session, user_id, actor, principal), actor, channel_id, body)


@router.delete("/api/users/{user_id}/notification-channels/{channel_id}")
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def delete_user_channel(
    session: Session, user_id: UUID, channel_id: UUID,
    actor: AdminUser = Depends(require_user), principal: Optional[Principal] = Depends(current_principal),
) -> dict:
    return _delete(session, _editable_target(session, user_id, actor, principal), actor, channel_id)


# The email writes on `/users/:id` pass the admin bar AND `notification_emails.may_prove`: the operator, or the person
# reaching their own row — an Owner or Admin typing an address for someone else is refused (403). Session-only, as the
# `/api/me` twins: an API key resolves to its owner, so a key holder could otherwise route the owner's alerts away.


@router.post("/api/users/{user_id}/notification-channels", status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
@on_session
def add_user_email(
    session: Session, user_id: UUID, body: EmailChannelIn, actor: AdminUser = Depends(require_user),
) -> dict:
    return _add(session, _target_user(session, user_id), actor, body)


@router.post("/api/users/{user_id}/notification-channels/{channel_id}/verify")
@org_admin(Tenancy.CALLER_PLANE)
@session_only
@on_session
def verify_user_email(
    session: Session, user_id: UUID, channel_id: UUID, body: CodeIn, actor: AdminUser = Depends(require_user),
) -> dict:
    return _verify(session, _target_user(session, user_id), actor, channel_id, body)


@router.post("/api/users/{user_id}/notification-channels/{channel_id}/resend", status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
@on_session
def resend_user_email(
    session: Session, user_id: UUID, channel_id: UUID, actor: AdminUser = Depends(require_user),
) -> dict:
    return _resend(session, _target_user(session, user_id), actor, channel_id)
