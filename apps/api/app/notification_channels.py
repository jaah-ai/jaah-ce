"""Notification channels: every rule about a membership's `notification_channels` rows — which exist, what each is
called, whether it can deliver, what it may be offered, and how its settings change.

A channel is one destination (the login email, a proved typed address, a Slack DM or channel, a Telegram chat)
plus its own alert settings (`app/notification_catalog.py`). The delivery engine (`app/notify_targets.py`) reads
channels through `channels_of`; the panel's routes (`app/routes/notification_channels.py`) through
`channels_document`. Every membership carries one account-email channel from birth (`ensure_account_email`,
called by each path that makes a membership) and may remove it.

The actor columns stay the system actor's (their default): a channel is its owner's and dies with them (CASCADE), and
an actor column RESTRICTs, so stamping the owner — or an admin who changed a setting — would refuse deleting that
user.

Slack: a membership may hold several DMs (each a Connect from a Slack member, `set_slack_dm`) and, for an Owner or
Admin, channels of the org's workspace picked from a list (`add_slack_channel`; Slack's side is
`app/slack_conversations.py`). Telegram: one channel per chat the person linked — private chats and groups the bot
was added to (`set_telegram`, from `app/telegram_link.py`'s redeem); removing one releases its link (`remove`), and
deleting the membership releases every link only its channels held (`release_member_links`).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import edition_features, notification_catalog as catalog
from app.models.identity import AdminUser, NotificationEmail, TelegramLink
from app.models.notifications import NotificationChannel
from app.tenancy.ids import is_operator_user

log = logging.getLogger("app.notification_channels")

EMAIL, SLACK, TELEGRAM = catalog.CHANNEL_TYPES
SLACK_DM, SLACK_CHANNEL = "dm", "channel"

#: `Channel.status` on the wire.
ACTIVE = "active"
UNVERIFIED = "unverified"
UNLINKED = "unlinked"
NEEDS_RECONNECT = "needs_reconnect"
WORKSPACE_MISMATCH = "workspace_mismatch"

#: Type order in the panel: Email first (everyone has one), then Slack, then Telegram.
_TYPE_ORDER = {EMAIL: 0, SLACK: 1, TELEGRAM: 2}


def viewer_is_admin(session: Session, user: AdminUser) -> bool:
    """Whether `user` sees and receives the Owner/Admin alerts: an org Owner or Admin, or the operator."""
    from app import access

    return is_operator_user(user) or access.org_role_of(session, user.id) in (access.ORG_OWNER, access.ORG_ADMIN)


def channels_of(session: Session, user: AdminUser, channel_type: Optional[str] = None) -> list[NotificationChannel]:
    """`user`'s channels (of `channel_type`, when given), oldest first."""
    query = select(NotificationChannel).where(NotificationChannel.admin_user_id == user.id)
    if channel_type is not None:
        query = query.where(NotificationChannel.type == channel_type)
    rows = session.scalars(query.order_by(NotificationChannel.created_at, NotificationChannel.id)).all()
    return sorted(rows, key=lambda row: _TYPE_ORDER.get(row.type, 9))


def find(session: Session, user: AdminUser, channel_id: UUID) -> Optional[NotificationChannel]:
    """`user`'s channel `channel_id`, or None — never another membership's."""
    return session.scalars(
        select(NotificationChannel).where(NotificationChannel.id == channel_id, NotificationChannel.admin_user_id == user.id)
    ).first()


def ensure_account_email(session: Session, user: AdminUser) -> None:
    """Give a new membership its account-email channel (default settings). Idempotent; a system row gets none.
    Flushes; the caller commits."""
    if user.is_system or user.id is None:
        return
    session.execute(
        pg_insert(NotificationChannel)
        .values(
            org_id=user.org_id, admin_user_id=user.id, type=EMAIL, account_email=True, name=user.email or "",
        )
        .on_conflict_do_nothing(
            index_elements=["admin_user_id"], index_where=text("account_email")
        )
    )


def set_slack_dm(
    session: Session, user: AdminUser, *, member_id: str, team_id: str, display_name: str = ""
) -> NotificationChannel:
    """Give `user` a Slack DM channel to `member_id` in workspace `team_id` (a Connect's redeem): the DM already
    naming that member there is refreshed; else a DM proven in another workspace (unusable since the org reconnected)
    is moved (a tenant's only), keeping its settings; else a new one is added with default settings — a second Connect
    from another Slack member is a second DM. Staged, not committed."""
    from app.tenancy.ids import is_tenant_org

    dms = [c for c in channels_of(session, user, SLACK) if c.slack_kind == SLACK_DM]
    row = next((c for c in dms if c.slack_target_id == member_id and (c.slack_team_id or "") == team_id), None)
    if row is None and is_tenant_org(user.org_id):
        # Only a tenant's org changes workspace; the operator plane's DMs stay valid whatever team they name.
        row = next((c for c in dms if (c.slack_team_id or "") != team_id), None)
    if row is None and dms:
        # Audit only (ids are not logged): another Slack member now receives this membership's alerts too.
        log.warning("notification channels: admin_user_id=%s added a further Slack DM channel", user.id)
    if row is None:
        row = NotificationChannel(
            org_id=user.org_id, admin_user_id=user.id, type=SLACK, slack_kind=SLACK_DM, settings={},
        )
        session.add(row)
    row.slack_team_id = team_id
    row.slack_target_id = member_id
    row.name = display_name or member_id
    session.flush()
    return row


def slack_dm(session: Session, user: AdminUser) -> Optional[NotificationChannel]:
    """`user`'s oldest Slack DM channel, or None."""
    return next((c for c in channels_of(session, user, SLACK) if c.slack_kind == SLACK_DM), None)


@contextmanager
def org_session(org_id: Optional[UUID]) -> Iterator[Session]:
    """A short session of its own with `org_id` entered, for a caller that holds none (or holds one mid-flight):
    the delivery engine reading a person's channels, the audit rows it writes, the workspace status read. The org is
    always the membership's own, read off its row — never a request field."""
    from app.db import BridgedSession
    from app.tenancy import seam

    with seam.detached(), seam.bound_to(org_id), BridgedSession() as session:
        yield session


ADMIN_ONLY = "Only an Owner or Admin can add a Slack channel."


class ChannelRefused(Exception):
    """An Add channel refused: the HTTP `status` and the sentence the person sees."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def add_slack_channel(
    session: Session, user: AdminUser, *, team_id: str, conversation_id: str, name: str
) -> NotificationChannel:
    """Add the org workspace's channel `conversation_id` (in `team_id`, `""` on the operator plane) as `user`'s Slack
    channel, with default settings. Only an Owner or Admin (or the operator) may: a channel post reaches everyone in
    it, the rule the agent `org_notify` post keeps. `name` is the channel's as Slack named it to the server
    (`slack_conversations.conversation`), never the request's. A task alert it receives is still gated by `user`'s
    own reading rights, as their DM's is. Raises `ChannelRefused`; staged, flushed, not committed."""
    if not viewer_is_admin(session, user):
        raise ChannelRefused(403, ADMIN_ONLY)
    if any(
        c.slack_kind == SLACK_CHANNEL and c.slack_target_id == conversation_id and (c.slack_team_id or "") == team_id
        for c in channels_of(session, user, SLACK)
    ):
        raise ChannelRefused(409, f"#{name} is already added.")
    row = NotificationChannel(
        org_id=user.org_id, admin_user_id=user.id, type=SLACK, slack_kind=SLACK_CHANNEL, settings={},
        slack_team_id=team_id, slack_target_id=conversation_id, name=f"#{name}",
    )
    session.add(row)
    session.flush()
    return row


def slack_dm_team(user_id: UUID, org_id: Optional[UUID]) -> Optional[str]:
    """The workspace membership `user_id`'s Slack DM channel was proven in, or None when it has none — on a short
    session of its own (`org_session`), for a caller holding no session (the workspace status read)."""
    with org_session(org_id) as session:
        channel = session.scalars(
            select(NotificationChannel).where(
                NotificationChannel.admin_user_id == user_id, NotificationChannel.type == SLACK,
                NotificationChannel.slack_kind == SLACK_DM,
            ).order_by(NotificationChannel.created_at)
        ).first()
        return None if channel is None else (channel.slack_team_id or "")


def set_telegram(session: Session, user: AdminUser, link: TelegramLink) -> NotificationChannel:
    """Give `user` a Telegram channel at their person's `link` (one per membership and chat), keeping its settings
    when one exists, named as the chat is. Staged, not committed."""
    row = next((c for c in channels_of(session, user, TELEGRAM) if c.telegram_link_id == link.id), None)
    if row is None:
        row = NotificationChannel(
            org_id=user.org_id, admin_user_id=user.id, type=TELEGRAM, settings={}, telegram_link_id=link.id,
        )
        session.add(row)
    row.name = link.chat_label or "Telegram"
    session.flush()
    return row


def remove(session: Session, row: NotificationChannel, user: AdminUser) -> None:
    """Delete `user`'s channel `row`; a Telegram one also deletes its chat's link once no other membership of the
    person has a channel there (`telegram_link.release`). Staged, not committed."""
    link_id = row.telegram_link_id if row.type == TELEGRAM else None
    session.delete(row)
    session.flush()
    if link_id is None:
        return
    # Inline, before the import: an edition without Telegram ships no `telegram_*` module (the community tree's check).
    if not edition_features.enabled("telegram"):
        return
    from app import telegram_link

    telegram_link.release(session, link_id, user)


def release_member_links(session: Session, identity_id: Optional[UUID]) -> None:
    """After a membership of person `identity_id` is deleted (flushed; its channels CASCADE): delete the person's
    linked Telegram chats no remaining membership has a channel at (`telegram_link.release_orphans`). Staged, not
    committed."""
    if identity_id is None:
        return
    # Its own statement: the community tree's check reads only a bare edition exit before the import.
    if not edition_features.enabled("telegram"):
        return
    from app import telegram_link

    telegram_link.release_orphans(session, identity_id)


def label(row: NotificationChannel, user: AdminUser) -> str:
    """The full destination the panel names. The account email is read live: the login email can change."""
    if row.type == EMAIL and row.account_email:
        return user.email or row.name
    return row.name


def _link(session: Session, row: NotificationChannel) -> Optional[TelegramLink]:
    return session.get(TelegramLink, row.telegram_link_id) if row.telegram_link_id is not None else None


def status(session: Session, row: NotificationChannel, user: AdminUser) -> str:
    """Whether this channel can deliver now, and if not, the one fix (`Channel.status` on the wire)."""
    if row.type == EMAIL:
        if row.account_email:
            return ACTIVE if user.email_verified_at is not None else UNVERIFIED
        email = session.get(NotificationEmail, row.email_id) if row.email_id is not None else None
        return ACTIVE if email is not None and email.verified_at is not None else UNVERIFIED
    if row.type == TELEGRAM:
        link = _link(session, row)
        return ACTIVE if link is not None and link.chat_id is not None else UNLINKED
    # The community edition has no Slack: a stored Slack channel cannot deliver.
    if not edition_features.enabled("slack"):
        return NEEDS_RECONNECT
    from app import slack_workspaces
    from app.tenancy.ids import is_tenant_org

    if not is_tenant_org(user.org_id):
        return ACTIVE
    workspace = slack_workspaces.for_org(user.org_id)
    if workspace is None:
        return NEEDS_RECONNECT
    return ACTIVE if row.slack_team_id == workspace.team_id else WORKSPACE_MISMATCH


def channel_document(session: Session, row: NotificationChannel, user: AdminUser, admin: bool) -> dict[str, Any]:
    """One channel as the panel reads it (`Channel` in the web's types)."""
    stored = row.settings if isinstance(row.settings, dict) else {}
    chat_type = None
    if row.type == TELEGRAM:
        link = _link(session, row)
        chat_type = (link.chat_type if link is not None else None) or "private"
    return {
        "id": str(row.id),
        "type": row.type,
        "label": label(row, user),
        "account_email": bool(row.account_email),
        "slack_kind": row.slack_kind if row.type == SLACK else None,
        "telegram_chat_type": chat_type,
        "status": status(session, row, user),
        "settings": {
            "groups": dict(stored.get("groups") or {}),
            "events": dict(stored.get("events") or {}),
        },
        "effective": catalog.effective(row.type, stored, viewer_is_admin=admin, account_email=row.account_email),
    }


def _option(channel_type: str, kind: str, available: bool, reason: Optional[str]) -> dict[str, Any]:
    return {"type": channel_type, "kind": kind, "available": available, "reason": reason}


def add_options(session: Session, user: AdminUser, actor: AdminUser) -> list[dict[str, Any]]:
    """What the Add channel picker offers for `user`, read by `actor` (`AddOption` in the web's types). An option
    with `available=false` and a `reason` is shown disabled with that reason; one with no reason is not shown
    (not in this edition, or not built yet). Only the person themself connects a chat (the Connect flows are the
    signed-in person's), so someone else's panel offers every option disabled; Email is the operator's too."""
    from app import notification_emails

    options: list[dict[str, Any]] = []
    own = actor.id == user.id
    elsewhere = None if own else "Only they can add channels"
    # Email: any address, proven by its own mail (`app/notification_emails.py`) — the person's own act, or the
    # operator's; typing the login address while its channel is gone brings that channel back.
    if notification_emails.may_prove(user, actor):
        options.append(_option(EMAIL, "address", True, None))
    else:
        options.append(_option(EMAIL, "address", False, elsewhere))

    if edition_features.enabled("slack"):
        dm = _slack_dm_option(session, user, elsewhere)
        options.append(dm)
        channel = _slack_channel_option(session, user, dm, elsewhere)
        if channel is not None:
            options.append(channel)
    if edition_features.enabled("telegram"):
        options.append(_telegram_option(elsewhere))
    return options


def _slack_dm_option(session: Session, user: AdminUser, elsewhere: Optional[str]) -> dict[str, Any]:
    from app import notify_targets, plan_terms
    from app.tenancy.ids import is_tenant_org

    if is_tenant_org(user.org_id) and not plan_terms.switch_on(session, user.org_id, plan_terms.SLACK):
        return _option(SLACK, SLACK_DM, False, "Not on your plan")
    try:
        token, reason = notify_targets.slack_bot_token(user.org_id)
    except Exception:
        log.exception("notification channels: Slack could not be resolved for the picker")
        token, reason = None, notify_targets.REASON_CREDENTIAL_MISSING
    if token is None:
        return _option(
            SLACK, SLACK_DM, False,
            "Slack isn't connected" if reason == notify_targets.REASON_WORKSPACE_NOT_CONNECTED else "Slack isn't set up",
        )
    return _option(SLACK, SLACK_DM, elsewhere is None, elsewhere)


def _slack_channel_option(
    session: Session, user: AdminUser, dm: dict[str, Any], elsewhere: Optional[str]
) -> Optional[dict[str, Any]]:
    """A Slack channel: an Owner's or Admin's only (a member is not offered it). It needs what a DM needs (the same
    refusal), and a workspace that granted the channel scopes; unknown is offered, and the list probes it."""
    if not edition_features.enabled("slack"):
        return None
    from app import slack_conversations

    if not viewer_is_admin(session, user):
        return None
    if not dm["available"] and dm["reason"] != elsewhere:
        return _option(SLACK, SLACK_CHANNEL, False, dm["reason"])
    if slack_conversations.has_scopes(user.org_id) is False:
        return _option(SLACK, SLACK_CHANNEL, False, slack_conversations.RECONNECT)
    return _option(SLACK, SLACK_CHANNEL, elsewhere is None, elsewhere)


def _telegram_option(elsewhere: Optional[str]) -> dict[str, Any]:
    if not edition_features.enabled("telegram"):
        return _option(TELEGRAM, "private", False, None)
    from app import telegram_bot

    try:
        token, _ = telegram_bot.bot_token()
    except Exception:
        log.exception("notification channels: the Telegram bot could not be resolved for the picker")
        token = None
    if not token:
        return _option(TELEGRAM, "private", False, "Telegram isn't set up")
    # Never "Added": a person links further chats and groups.
    return _option(TELEGRAM, "private", elsewhere is None, elsewhere)


def channels_document(session: Session, user: AdminUser, actor: AdminUser) -> dict[str, Any]:
    """`GET …/notification-channels`: `user`'s channels, the catalog they see, and what they may add."""
    admin = viewer_is_admin(session, user)
    return {
        "channels": [channel_document(session, row, user, admin) for row in channels_of(session, user)],
        "catalog": catalog.catalog_document(admin),
        "add_options": add_options(session, user, actor),
    }


def change_settings(
    session: Session, row: NotificationChannel, user: AdminUser, actor: AdminUser, *,
    settings: Optional[dict[str, Any]] = None, group: Optional[str] = None, event: Optional[str] = None,
    level: Optional[str] = None,
) -> None:
    """Apply one PATCH to `row` — a whole settings document, or one group's or event's level — judged against the
    OWNER's role (the alerts are theirs). Raises `notification_catalog.ChoiceRefused`. Staged, not committed."""
    admin = viewer_is_admin(session, user)
    if settings is not None:
        if group is not None or event is not None or level is not None:
            raise catalog.ChoiceRefused("send either settings or one group/event level, not both")
        row.settings = catalog.validate_document(settings, viewer_is_admin=admin)
    else:
        if level is None:
            raise catalog.ChoiceRefused("level is required")
        row.settings = catalog.apply_choice(row.settings, level=level, viewer_is_admin=admin, group=group, event=event)
