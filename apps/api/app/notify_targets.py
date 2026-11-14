"""The delivery engine's DB questions: who an alert may reach, which of each person's channels
(`app/notification_channels.py`) want it and at what level, the Slack credential, response options, and the two
records that make an inbound reply resolvable.

Every resolver answers a LIST — one route per channel that wants the event, plus each skipped channel with why
(`Resolved`) — and every walk sends to all of them (`deliver_alert`, `deliver_to_user`). No code outside this module
resolves a single destination. One `notification_sent` activity per resolved recipient names each channel's outcome
(`Outcomes`, `notification_sent`).

A sibling of the DB-free `alert_relay`: `alert_record` asks this module for a recipient and hands the
relay a plain `SlackTarget`. Called by `alert_record` (outbound) and the Socket Mode consumer (inbound).

Correlation lives in `slack_threads`, not `alerts.payload`: an `alerts` row is withdrawn on
non-delivery and bucketed for 300s dedup, while a correlation is read hours later.

`SlackTarget.bot_token` is read from the `credentials` row on every alert, never cached or logged;
nor are member ids.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterator, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, object_session

from app import alert_relay, edition_features, notification_catalog, notification_channels, task_done_notice
from app.actors import actor_names
from app.authz.object_access import can_access
from app.authz.principal import principal_for_user
from app.authz.visibility import ObjectAccess
from app.clock import utcnow
from app.credential_crypto import has_sealed_secret, open_secret
from app.entity_shape import optional_uuid
from app.models.audit import ActivityEvent
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.notifications import NotificationChannel, SlackEvent, SlackThread
from app.models.tasks import Task, TaskQuestion
from app.preferences import _SLACK_USER_ID_RE
from app.task_ancestry import assignee_user_id
from app.task_artifacts import visible_artifact_clause_joined
from app.task_awaiting_response_parser import parse_response_options, question_prose

log = logging.getLogger("app.notify_targets")

EMAIL, SLACK, TELEGRAM = notification_catalog.CHANNEL_TYPES

# The ancestor walk and its bound live in `app/task_ancestry.py` (three callers, none owns it).

# Reason keys are a fixed finite set: each goes to `alert_relay._warn_once`, whose process-lifetime
# `_warned` set would grow without bound on an interpolated key. Add a named constant instead.
REASON_RECIPIENT_MISSING = "slack-recipient-missing"
# A stored Slack member id that is not one: fixable by the recipient, unlike a missing address.
REASON_RECIPIENT_MALFORMED = "slack-recipient-malformed"
REASON_CHANNEL_NOT_SLACK = "slack-channel-not-slack"
REASON_CHANNEL_NOT_EMAIL = "email-channel-not-email"
REASON_CHANNEL_NOT_TELEGRAM = "telegram-channel-not-telegram"
# The person never opened the bot's deep link, or unlinked (`app/telegram_link.py`).
REASON_TELEGRAM_NOT_LINKED = "telegram-not-linked"
# The login email has not been proven by an SSO sign-in since it last changed.
REASON_EMAIL_UNVERIFIED = "email-unverified"
REASON_CREDENTIAL_MISSING = "slack-credential-missing"
REASON_CREDENTIAL_WRONG_TYPE = "slack-credential-wrong-type"
REASON_BOT_TOKEN_MISSING = "slack-bot-token-missing"
# A tenant org has no connected Slack workspace (`app/slack_workspaces.py`): its alerts have no app to post with.
REASON_WORKSPACE_NOT_CONNECTED = "slack-workspace-not-connected"
# The member id was proven in another workspace than the org's (connected before a reconnect, or before workspaces).
REASON_WORKSPACE_MISMATCH = "slack-workspace-mismatch"
# Not a misconfiguration: the recipient asked not to be told, so it is never passed to `_warn_once`.
# It only escapes `resolve_slack_targets` on the `done=True`/`assigned=True` single-candidate paths,
# where `alert_record` drops the alert (falling through would turn a mute into a redirect). For other
# kinds the mute is consumed inside the walk and the alert still reaches the webhook.
REASON_EVENT_MUTED = "slack-event-muted"

#: The events addressed to one person; the Notifications panel's operational rows are the complement.
#:
#: Not the chain-stop predicate: `resolve_slack_targets` stops on its `done`/`assigned` flags. A
#: question's first rung is a project role, so a mute stopping there would silence questions for every
#: task naming that role. For operational kinds a mute skips the candidate and the chain continues to
#: the operator channel, so a self-configured assignee cannot suppress alerts for everyone.
PERSONAL_EVENTS: frozenset[str] = frozenset({"done", "question", "assigned"})

#: The `_warn_once` key for "the designated Slack credential is not on the operator plane".
#:
#: Namespaced per wording (as `alert_record._DONE_DROP_WARN_PREFIX`): the fall-through arm warns under
#: the bare `REASON_CREDENTIAL_WRONG_TYPE` right after, and a shared key would demote this actionable
#: message to debug. A warn key, not a `REASON_KEYS` member.
_OFF_PLANE_WARN_KEY = f"off-plane:{REASON_CREDENTIAL_WRONG_TYPE}"

#: Every reason key this module can return, so "fixed, finite set" is testable.
REASON_KEYS: frozenset[str] = frozenset(
    {
        REASON_RECIPIENT_MISSING,
        REASON_RECIPIENT_MALFORMED,
        REASON_CHANNEL_NOT_SLACK,
        REASON_CHANNEL_NOT_EMAIL,
        REASON_CHANNEL_NOT_TELEGRAM,
        REASON_TELEGRAM_NOT_LINKED,
        REASON_EMAIL_UNVERIFIED,
        REASON_CREDENTIAL_MISSING,
        REASON_CREDENTIAL_WRONG_TYPE,
        REASON_BOT_TOKEN_MISSING,
        REASON_WORKSPACE_NOT_CONNECTED,
        REASON_WORKSPACE_MISMATCH,
        REASON_EVENT_MUTED,
    }
)

# `slack_events` retention: no CASCADE reclaims it, so the claim prunes past the horizon on its own
# session. Slack redelivers within minutes; seven days is ample.
_EVENT_RETENTION = timedelta(days=7)
# At most one prune per hour per process. Unsynchronized: a race costs one redundant empty DELETE.
_PRUNE_INTERVAL_SECONDS = 3600.0
_last_prune_monotonic: Optional[float] = None


@dataclass(frozen=True)
class SlackQuestion:
    """One Task's question as the three separately-renderable pieces a Block Kit card needs (header, body,
    footer — `app/slack_blocks.py`).

    Read here because the relay only gets `format_message`'s joined, truncated line, which cannot be
    re-split reliably. `title`/`body` come from the Task's own columns (not `alerts.payload`), but are
    runner-authored untrusted content the renderer sanitizes. `body` is uncapped; Slack's limits are the
    renderer's.
    """

    task_id: int
    title: str
    body: str
    #: Where this Task lives in the console, or `None` when it could not be composed. Exists for the
    #: `detail="brief"` arm, where the link is the message's whole remaining value; the full card reads
    #: `event["url"]`.
    url: str | None = None
    #: The `task_questions` round this card renders, or `None` for a roundless question. Rides in each
    #: button's `action_id` so a click is refused once that round is no longer the one `answer_task`
    #: would resolve.
    question_id: int | None = None
    #: Whether the round accepts several options (`multiSelect`): tickboxes plus Submit. A DB-read flag, so
    #: branched on unsanitized. Defaults false, the cheaper mistake.
    multi_select: bool = False
    #: The option authors' annotations (`routes/tasks.QuestionOptionOut`), index-aligned with
    #: `SlackTarget.options` since both come from one normalizer; `slack_blocks._option_detail` reads by
    #: source index. Typed `Any` to avoid importing `app.routes.tasks`. Sanitized like a label. Empty for
    #: the roundless path and older rounds.
    option_details: tuple[Any, ...] = ()
    #: The round's short name inside a batch (`TaskQuestion.header`), for the card headline. `None` means
    #: the round carries no header. Runner-authored: sanitized and clipped.
    header: str | None = None


@dataclass(frozen=True)
class SlackDone:
    """One shipped Task as the pieces the "task shipped" card renders: `SlackQuestion`'s sibling for the
    alert that asks nothing.

    A separate value object so the renderer's "which card" question stays answerable and a done card
    cannot inherit stale `options`. `title` is the Task's own column, still untrusted and inerted. `url`
    is optional; its safety in `<url|label>` is `slack_blocks.link_url`'s judgement, falling back to a
    bold `*Task NNNN*`.
    """

    task_id: int
    title: str
    url: str | None


@dataclass(frozen=True)
class SlackAssigned:
    """One Task just assigned to the recipient, as the pieces the "assigned to you" card renders:
    `SlackDone`'s sibling for the alert whose subject is the reader.

    `assigned_by` is the actor's display name via `actors.actor_names`, or `"someone"` when unknown (a
    blank reads as a truncated card). Operator-edited free text, inerted by the renderer. `title`/`url`
    as for `SlackDone`.
    """

    task_id: int
    title: str
    url: str | None
    assigned_by: str


@dataclass(frozen=True)
class SlackTarget:
    """Everything `relay_alert` needs to post one Slack DM, resolved in one DB step and handed over as a
    plain value, so the relay needs no session. Frozen so the relay cannot "fix up" a recipient.
    """

    admin_user_id: UUID
    member_id: str
    # Read from the `credentials` row on every alert; never cached or logged. `repr=False` because the
    # synthesized `__repr__` would otherwise print the token in any `%r` log.
    bot_token: str = field(repr=False)
    # The recipient's org: its plan decides whether this DM goes out at all (`alert_relay._slack_post`).
    org_id: Optional[UUID]
    # Parsed in this DB step rather than carried in `payload`, which a `NODE_TOKEN` holder writes. A tuple
    # so the frozen dataclass is actually immutable.
    options: tuple[str, ...] = ()
    # Whether the message may promise a thread reply answers the Task, i.e. whether `alert_record` will
    # write the `slack_threads` row. Carried, not decided. Defaults `False`: a false promise costs more than
    # a quiet unanswerable alert.
    answerable: bool = False
    # The question card, or `None` for plain text. Populated exactly when `answerable` is true.
    question: Optional[SlackQuestion] = None
    # The shipped-Task card, or `None` for every alert that is not a top-level `done`. Mutually exclusive
    # with `question` by the alert's `kind`, not enforced here; the renderer asks question first.
    done: Optional[SlackDone] = None
    # The "assigned to you" card, or `None`. Third of the mutually exclusive cards, by `kind`; the renderer
    # asks for it last (`alert_relay._done_card`).
    assigned: Optional[SlackAssigned] = None
    # How much of this alert the recipient asked for: `"full"`, or `"brief"` (headline plus link, no
    # prose, options or buttons). Carried from the channel's settings (`slack_routes`), not decided.
    # Defaults `"full"` since the relay reads it with `getattr` off a duck-typed target.
    detail: str = "full"
    # The tenant workspace (`team_id`) the recipient was checked against when this target was resolved; `""` on the
    # operator plane. The outbox re-checks it before the send (`durable/notify_outbox._slack_step`): a reconnect to
    # another workspace in between refuses the DM rather than send the member id into a workspace it was not proven in.
    team_id: str = ""
    # The `notification_channels` row this target is (the audit names it); `None` for a destination with no row.
    channel_id: Optional[UUID] = None


def resolve_slack_targets(
    session: Session,
    task_id: Optional[int],
    *,
    kind: str = "",
    answerable: bool = False,
    question_id: Optional[int] = None,
    done: bool = False,
    assigned: bool = False,
    assigned_by_id: Optional[UUID] = None,
    outcomes: Optional["Outcomes"] = None,
) -> tuple[list[SlackTarget], Optional[str]]:
    """Resolve the Slack destinations for this alert (the Task's addressee's channels when there is one), plus the
    Task's response options and question presentation.

    Returns `(targets, None)`, or `([], reason)` with `reason` in `REASON_KEYS`; `[]` means fall through to the
    env-var channel, never "delivery failed". The chain (`_recipient_candidates`) tries each candidate in full: the
    first with a non-empty list of Slack channels wanting the event takes the alert, and it goes to every one at
    its own level. Only a DM carries the question card and the reply promise (`answerable`); every other channel
    gets the plain text, and a question there its brief text (no options, no buttons: answers stay in DMs). `kind` is matched against each channel's settings; unknown events are delivered.

    Mutes: only `done=True`/`assigned=True` (single candidate) can return `REASON_EVENT_MUTED`, which `alert_record`
    drops. Elsewhere a muted candidate is skipped, so the alert still reaches the webhook. `outcomes` (when given)
    gains each examined candidate's per-channel skips (`Outcomes`).
    """
    personal = done or assigned
    candidates = alert_candidates(session, task_id, answerable=answerable, done=done, assigned=assigned)
    if not candidates:
        return [], REASON_RECIPIENT_MISSING

    # Below the guard so an unconfigured recipient does not pay for this work per alert.
    question: Optional[SlackQuestion]
    done_card: Optional[SlackDone] = None
    assigned_card: Optional[SlackAssigned] = None
    if personal:
        # Function-local to keep the route layer (and httpx) off the alert hot path. The private name is the
        # single home of the console task URL.
        from app.trackers.writeback import task_url as _console_task_url

        question, options = None, ()
        task = session.get(Task, task_id) if task_id is not None else None
        if task is not None and done:
            done_card = SlackDone(
                task_id=task.id, title=task.title, url=_console_task_url(task.id)
            )
        elif task is not None:
            # The actor's name via `actor_names` (which owns the tenant seam's "withheld" fill), so the card
            # and a provenance cell agree. An unresolved actor reads "someone".
            assigned_by = (
                actor_names(session, (assigned_by_id,)).get(assigned_by_id)
                if assigned_by_id is not None else None
            )
            assigned_card = SlackAssigned(
                task_id=task.id, title=task.title, url=_console_task_url(task.id),
                assigned_by=assigned_by or "someone",
            )
    else:
        question, options = _question_for_task(session, task_id, question_id)

    # The card decides the event: `kind` alone cannot tell a parked question from a plain blocked transition.
    event = _event_id(kind, question=question if answerable else None)

    reason: str = REASON_RECIPIENT_MISSING
    for user in candidates:
        resolved = slack_routes(session, user, event=event)
        if outcomes is not None:
            outcomes.skipped(user, SLACK, resolved.skipped)
        if not resolved.routes:
            candidate_reason = resolved.reason()
            if candidate_reason == REASON_EVENT_MUTED:
                # `personal` comes from the flags, not the event: a child task's `done` arrives with `done=False`
                # and a full chain, and a question has a chain (see `PERSONAL_EVENTS`).
                if personal:
                    # The chain stops for a card that is this person's; walking on would hand their card to
                    # someone else.
                    return [], REASON_EVENT_MUTED
                # An operational kind this candidate muted walks on and its mute is not retained.
                continue
            reason = candidate_reason
            continue
        targets: list[SlackTarget] = []
        for route in resolved.routes:
            member_id, bot_token, team_id = route.address
            card = answerable and route.channel is not None and route.channel.slack_kind == "dm"
            targets.append(
                SlackTarget(
                    admin_user_id=user.id,
                    member_id=member_id,
                    bot_token=bot_token,
                    org_id=user.org_id,
                    team_id=team_id,
                    # The options are information every alert prints; only an answerable question's card makes them
                    # buttons, and that card lives on a DM alone — so a channel drops them only off a question card.
                    options=options if (card or not answerable) else (),
                    answerable=card,
                    question=question if card else None,
                    done=done_card,
                    assigned=assigned_card,
                    # A question reaches a channel as its brief text: its answers are the DM's alone.
                    detail=notification_catalog.BRIEF if answerable and not card else route.level,
                    channel_id=route.channel.id if route.channel is not None else None,
                )
            )
        # A DM (the card's home) goes first: `alert_record` keys the reply thread on the first target's receipt.
        targets.sort(key=lambda target: not target.answerable)
        return targets, None
    return [], reason


def alert_candidates(
    session: Session, task_id: Optional[int], *, answerable: bool, done: bool, assigned: bool
) -> list[AdminUser]:
    """Who an alert may reach, in preference order — the one rule every channel's leg walks
    (`resolve_slack_targets`, `app/email_notify.py`), so Slack and email never disagree about who may read
    the Task.

    A personal card (`done`/`assigned`) has no chain: one candidate or none, gated like the other rungs,
    since both cards carry the Task's title and provenance is not access. Everything else walks
    `_recipient_candidates`."""
    if done or assigned:
        addressee = _task_addressee(session, task_id) if done else _task_assignee(session, task_id)
        return [addressee] if may_read_task(session, addressee, task_id) else []
    return _recipient_candidates(session, task_id)


def alert_event(
    session: Session, kind: str, task_id: Optional[int], *, answerable: bool, question_id: Optional[int]
) -> str:
    """Which catalog event (`app/notification_catalog.py`) an alert is, for a leg that renders no Slack card
    (`app/email_notify.py`): `question` exactly when `resolve_slack_targets` would render a question card."""
    question = _question_for_task(session, task_id, question_id)[0] if answerable else None
    return _event_id(kind, question=question)


class Route(NamedTuple):
    """One channel a resolver found for one person and event: the channel row (None for a destination with no row:
    an operator page), where to send (an email address, a chat id, or the Slack `(member_id, bot_token, team_id)`),
    and the level (`brief`/`full`) that channel wants."""

    channel: Optional[NotificationChannel]
    address: Any
    level: str


@dataclass
class Resolved:
    """What one person's channels of one type came to for one event: the routes to send on, and each channel that
    was skipped with why (`channel` None when the skip is the person's, not a channel's: no channel at all, a
    pending invitation)."""

    routes: list[Route] = field(default_factory=list)
    skipped: list[tuple[Optional[NotificationChannel], str]] = field(default_factory=list)

    def reason(self) -> str:
        """The skip to report when nothing routed: a channel-less reason first, else the first channel's."""
        return self.skipped[0][1] if self.skipped else REASON_RECIPIENT_MISSING


class Outcomes:
    """Each examined recipient's per-channel outcome for one notification, written as ONE `notification_sent`
    activity per recipient (`activities`). A leg reports a skipped channel (`skipped`) and a send's outcome
    (`sent`); the order a channel first appears in is kept."""

    def __init__(self) -> None:
        self._people: dict[UUID, tuple[AdminUser, list[dict[str, Any]]]] = {}

    def _rows(self, user: AdminUser) -> list[dict[str, Any]]:
        return self._people.setdefault(user.id, (user, []))[1]

    def skipped(self, user: AdminUser, channel_type: str, skips: list[tuple[Optional[NotificationChannel], str]]) -> None:
        rows = self._rows(user)
        for channel, reason in skips:
            rows.append({
                "channel_id": str(channel.id) if channel is not None else None, "type": channel_type,
                "outcome": f"skipped: {reason}",
            })

    def outcome(self, user: AdminUser, channel_type: str, channel: Optional[NotificationChannel], outcome: str) -> None:
        self._rows(user).append({
            "channel_id": str(channel.id) if channel is not None else None, "type": channel_type, "outcome": outcome,
        })

    def outcome_by_id(self, user: AdminUser, channel_type: str, channel_id: Optional[UUID], outcome: str) -> None:
        self._rows(user).append({
            "channel_id": str(channel_id) if channel_id is not None else None, "type": channel_type, "outcome": outcome,
        })

    def rows_for(self, user: AdminUser) -> list[dict[str, Any]]:
        """`user`'s channel outcomes so far, in order."""
        found = self._people.get(user.id)
        return list(found[1]) if found is not None else []

    def activities(self, event_name: str, *, important: bool = False) -> list[ActivityEvent]:
        return [
            notification_sent(user, event_name, rows, important=important)
            for user, rows in self._people.values() if rows
        ]


#: The `activity_events.kind` of one recipient's delivery record.
NOTIFICATION_SENT = "notification_sent"


def notification_sent(
    user: AdminUser, event_name: str, channels: list[dict[str, Any]], *, important: bool = False,
    subject: Optional[dict[str, Any]] = None,
) -> ActivityEvent:
    """One recipient's `notification_sent` activity (shown on `/audit`): every channel's outcome
    (`channels: [{channel_id, type, outcome}]`), plus the per-type fields the release before channels wrote
    (`slack`/`email`/`telegram`, each the first channel of that type's outcome). The system acted, so no actor; the
    person is `recipient_id`. `subject`: what the notice was about (`{project_id, ...}`), so a notifier can tell it
    already sent one (`app/releases/notify.py::sent`). Not added to any session: the caller stages it."""
    legacy = {
        channel_type: next((row["outcome"] for row in channels if row["type"] == channel_type), None)
        for channel_type in (SLACK, EMAIL, TELEGRAM)
    }
    legs = ", ".join(f"{row['type']} {row['outcome']}" for row in channels)
    return ActivityEvent(
        user_id=None,
        action_type="write",
        kind=NOTIFICATION_SENT,
        summary=f"{event_name} notification{' (important)' if important else ''}: {legs}",
        data={
            "recipient_id": str(user.id), "event_name": event_name, "important": important,
            "channels": channels, **{key: value for key, value in legacy.items() if value is not None},
            **({"subject": subject} if subject else {}),
        },
        org_id=user.org_id,
    )


def record_outcomes(org_id: Optional[UUID], outcomes: Outcomes, event_name: str) -> None:
    """Write `outcomes`' activities in a session of their own, bound to `org_id` — for a caller (`alert_record`)
    whose session is mid-flight. Best-effort: an audit row lost never costs the notice. Never raises."""
    rows = outcomes.activities(event_name)
    if not rows:
        return
    try:
        with notification_channels.org_session(org_id) as audit:
            audit.add_all(rows)
            audit.commit()
    except Exception:
        log.exception("notifications: the notification_sent rows for %s could not be written", event_name)


#: A channel's per-person resolver (`email_routes`, `telegram_routes`) and its send, as the email and Telegram legs
#: hand them to `deliver_alert`/`deliver_to_user`: `send(event, user, route, event_name)`.
Resolver = Callable[..., Resolved]
Send = Callable[[dict[str, Any], AdminUser, Route, str], bool]


def _send_routes(
    event: dict[str, Any], user: AdminUser, resolved: Resolved, send: Send, event_name: str, channel_type: str,
    outcomes: Optional[Outcomes], *, skipped: Optional[list[str]] = None, resolver_name: str = "",
) -> bool:
    """Send `event` on every route of `resolved` at that route's level; True when any send was handed off."""
    sent_any = False
    for route in resolved.routes:
        if skipped is not None and alert_relay.brief_skips(event, route.level):
            # Told already by the batch's first question: not a route to fall through from.
            skipped.append(resolver_name)
            if outcomes is not None:
                outcomes.outcome(user, channel_type, route.channel, "skipped: brief-batch")
            continue
        try:
            ok = send(event, user, route, event_name)
        except Exception:
            log.exception("%s: the send raised for kind=%s — not sent", resolver_name, event.get("kind"))
            ok = False
        sent_any = sent_any or ok
        if outcomes is not None:
            outcomes.outcome(user, channel_type, route.channel, "sent" if ok else "failed")
    return sent_any


def deliver_alert(
    session: Session, event: dict[str, Any], resolver: Resolver, send: Send, *,
    task_id: Optional[int], answerable: bool, question_id: Optional[int], done: bool, assigned: bool,
    skipped: Optional[list[str]] = None, channel_type: str = "", outcomes: Optional[Outcomes] = None,
) -> bool:
    """Send `event` over one channel type to the first candidate in the chain (`alert_candidates`) with a non-empty
    list of channels wanting the `alert_event`; it goes to each of those channels at its own level. The one walk
    the email and Telegram legs share, so they stop at the same person. Never raises: a lookup fault must not reach
    `alert_record`, whose Slack leg and dedup row do not depend on this leg.

    `skipped` (an out-parameter) gains this channel's resolver name when its person was brief and the event a later
    question of a batch (`alert_relay.brief_skips`): told already, so not a route to fall through from. A personal
    card (`done`/`assigned`) has one candidate, so a mute there sends nothing; an operational alert walks past one."""
    try:
        candidates = alert_candidates(session, task_id, answerable=answerable, done=done, assigned=assigned)
        if not candidates:
            return False
        kind = str(event.get("kind") or "")
        event_name = alert_event(session, kind, task_id, answerable=answerable, question_id=question_id)
        for user in candidates:
            resolved = resolver(user, event=event_name, session=session)
            if outcomes is not None:
                outcomes.skipped(user, channel_type, resolved.skipped)
            if resolved.routes:
                # The chain stops here either way: this person takes the alert, on every channel that wants it.
                return _send_routes(
                    event, user, resolved, send, event_name, channel_type, outcomes,
                    skipped=[] if skipped is None else skipped, resolver_name=resolver.__name__,
                )
    except Exception:
        log.exception("%s: recipient lookup failed (or the send did) for kind=%s — not sent", resolver.__name__, event.get("kind"))
    return False


def deliver_to_user(
    user: AdminUser, event: dict[str, Any], resolver: Resolver, send: Send, *, event_name: str,
    channel_type: str = "", outcomes: Optional[Outcomes] = None, session: Optional[Session] = None,
) -> bool:
    """Send `event` over one channel type to one named person (a mention, a notice), on every channel of theirs
    that wants it: no chain and no fall-through, since nobody else may read their message. Never raises: what it
    announces is already committed."""
    try:
        resolved = resolver(user, event=event_name, session=session)
        if outcomes is not None:
            outcomes.skipped(user, channel_type, resolved.skipped)
        return _send_routes(event, user, resolved, send, event_name, channel_type, outcomes, resolver_name=resolver.__name__)
    except Exception:
        log.exception("%s: recipient lookup failed (or the send did) for kind=%s — not sent", resolver.__name__, event.get("kind"))
        return False


@dataclass(frozen=True)
class Leg:
    """One delivery channel type's entry points, as `alert_record`, `comment_notify`, `user_notify` and the notice
    senders call them: the email and Telegram legs (`app/email_notify.py`, `app/telegram_notify.py`) each bind ONE
    instance and expose its methods as their module-level names. `gate` is the deployment's own "may this event go
    out at all"; `resolver` yields the per-person list resolver, read on every call so a test's patch of
    `email_routes` or `telegram_routes` holds; `send` is the leg's `Send`; `channel_type` names it in the audit."""

    gate: Callable[[dict[str, Any]], bool]
    resolver: Callable[[], Resolver]
    send: Send
    channel_type: str

    def can_send(self, event: dict[str, Any]) -> bool:
        """Whether this deployment may send `event` over this channel type at all."""
        return self.gate(event)

    def deliver_alert(
        self, session: Session, event: dict[str, Any], *,
        task_id: Optional[int], answerable: bool, question_id: Optional[int], done: bool, assigned: bool,
        skipped: Optional[list[str]] = None, outcomes: Optional[Outcomes] = None,
    ) -> bool:
        """Send this alert to the first candidate in the chain whose channels of this type want it, on each of them —
        the same walk on every type, so each stops at the same person. `task_id` is the trusted payload id
        (`alert_record`'s `answerable_task_id`). Never raises."""
        return self.gate(event) and deliver_alert(
            session, event, self.resolver(), self.send,
            task_id=task_id, answerable=answerable, question_id=question_id, done=done, assigned=assigned,
            skipped=skipped, channel_type=self.channel_type, outcomes=outcomes,
        )

    def deliver_to_user(
        self, user: AdminUser, event: dict[str, Any], *, event_name: str, outcomes: Optional[Outcomes] = None,
        session: Optional[Session] = None,
    ) -> bool:
        """Send to one named person (a mention, a notice) on each of their channels of this type that wants it. No
        chain and no fall-through: nobody else may read their message. Never raises."""
        return self.gate(event) and deliver_to_user(
            user, event, self.resolver(), self.send, event_name=event_name, channel_type=self.channel_type,
            outcomes=outcomes, session=session,
        )

    def routes(self, user: AdminUser, *, event_name: str, session: Optional[Session] = None) -> Resolved:
        """`user`'s channels of this type for `event_name`, resolved (the routes and the skips), without sending."""
        return self.resolver()(user, event=event_name, session=session)


def _event_id(kind: str, *, question: Optional[SlackQuestion]) -> str:
    """Which catalog event this alert is, from its kind and its card (`notification_catalog.event_for`, so `error`
    reads as `failed`).

    The one place `blocked` is split: a parked question and a plain blocked transition share a kind but
    are different events to the recipient, and a resolved question card is the only honest discriminator. A kind
    that is no longer a notification comes back as itself; `alert_record` stops it before any leg runs.
    """
    if kind == "blocked" and question is not None:
        return "question"
    return notification_catalog.event_for(kind) or kind


def _recipient_candidates(session: Session, task_id: Optional[int]) -> list[AdminUser]:
    """Who this alert may be delivered to: the Task's owner (`_task_owner`: its assignee, else its creator, walked
    up the ancestry), when they may read the Task (`may_read_task`). A question inside an epic goes there too,
    like any other Task's. There is no deployment-wide last-resort human: an alert nobody up the chain may read
    reaches no person. A task-less alert has no rung.

    Returns `[]` when no rung exists or none may read the Task: the fall-through, not an error. The
    env-var operator channel it reaches is outside the grant model, a known blind spot.
    """
    owner = _task_owner(session, task_id)
    return [owner] if owner is not None and may_read_task(session, owner, task_id) else []


def _human(session: Session, user_id: Optional[UUID]) -> Optional[AdminUser]:
    """The `AdminUser` at `user_id` when it names a human: `None` for NULL, a deleted row, or the system
    actor. The one filter for the three own-column readers below; `task_ancestry` applies its own.
    """
    if user_id is None:
        return None
    user = session.get(AdminUser, user_id)
    if user is None or user.is_system:
        return None
    return user


def _task_assignee(session: Session, task_id: Optional[int]) -> Optional[AdminUser]:
    """The human this Task is assigned to: its own `assigned_to_id` only; `None` when it names no human.

    The `assigned` card's only candidate: true of exactly one person, so a NULL (cleared or deleted
    assignee) sends nothing rather than misinforming the creator. Provenance, not access: the caller
    conjoins `may_read_task`.
    """
    if task_id is None:
        return None
    task = session.get(Task, task_id)
    return _human(session, task.assigned_to_id) if task is not None else None


def _task_addressee(session: Session, task_id: Optional[int]) -> Optional[AdminUser]:
    """The human this Task is addressed to: its own `assigned_to_id`, else — on a human-filed task only
    (`task_done_notice.human_filed`) — its own `created_by_id`; `None` when neither names a human.

    Assignee first per the operator's rule; a NULL assignee is a machine-filed row or a deleted user, and an
    automation's or system task's creator is not told about work nobody was assigned. Not
    `_task_owner`: the done card is posted only for parentless Tasks, and walking would hide a regression
    in that gate. No fallback rung: an unsent "shipped" DM costs little, while a fallback would flood the
    address misconfiguration warnings go to. Provenance, not access (`may_read_task` is conjoined).
    """
    if task_id is None:
        return None
    task = session.get(Task, task_id)
    if task is None:
        return None
    assignee = _human(session, task.assigned_to_id)
    if assignee is not None or not task_done_notice.human_filed(task.source, task.persona):
        return assignee
    return _human(session, task.created_by_id)


def _task_owner(session: Session, task_id: Optional[int]) -> Optional[AdminUser]:
    """The human this work is addressed to: the nearest ancestor's `assigned_to_id`, else the creator
    chain (the Task's `created_by_id`, else the nearest ancestor's).

    The walk matters because the Task asking is rarely the one a human created. NULL assignees up the
    whole chain fall back to the creator chain. The system actor is skipped like NULL. Delegates to
    `task_ancestry.assignee_user_id` (shared with dispatch confinement), which is why the `may_read_task`
    gate is applied by `_recipient_candidates`, not here.
    """
    user_id = assignee_user_id(session, task_id)
    return session.get(AdminUser, user_id) if user_id is not None else None


def may_read_task(
    session: Session, user: Optional[AdminUser], task_id: Optional[int]
) -> bool:
    """May this candidate be told about this Task? Public because `app/comment_mentions.py` asks the same.

    Does `visible_task_clause` admit them to the row whose title, prose and options the message carries?
    Provenance-derived rungs need this because grants can change after assignment.

    A read probe via `can_access` on the `"task"` spec, not a write probe (an ordinary owner holds only
    `tasks:view`) and never a local re-derivation. `task_id is None` is `True` (task-less alerts report
    the fleet itself); `user is None` is `False`. Not cached, so revocation takes effect.
    """
    if user is None:
        return False
    if task_id is None:
        return True
    if can_access(
        session, principal_for_user(session, user), "task", task_id, mode=ObjectAccess.READ
    ):
        return True
    # `can_access` answers a refusal and an absence alike, but a Task deleted between transition and relay
    # has no audience to withhold from, so absence admits the rung. A bare SELECT, since the identity map
    # may still hold a row this session deleted.
    return session.scalar(select(Task.id).where(Task.id == task_id)) is None


def _org_slack_bot_token() -> tuple[Optional[str], str]:
    """The bot token of the one `slack` credential this deployment posts every per-user notification
    with, or `(None, reason)` naming what is missing.

    Designated by the `notifications_slack_credential_id` setting, never guessed; blank is
    `REASON_CREDENTIAL_MISSING`. Read on the operator plane in its own session (as `git_access.app_config`):
    the caller's session carries a tenant scope, and a miss there would fall through and post a tenant's
    task text to the shared operator webhook. A designated row off the operator plane is refused.

    Takes no session so nobody passes a tenant-scoped one. Returns a plain string, read before the
    session closes; never logged. A nested checkout while the caller holds a connection: harmless at one
    indexed lookup, but `pool_timeout` is what bounds it if lengthened. Imports are function-local to keep
    the scheduler and relay off the alert path.
    """
    from app import alert_relay
    from app.operator_settings import reads as settings_reads
    from app.db import BridgedSession
    from app.tenancy import seam
    from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

    designated = settings_reads.get_text("notifications_slack_credential_id")
    if not designated:
        return None, REASON_CREDENTIAL_MISSING
    credential_id = optional_uuid(designated)
    if credential_id is None:
        # The knob's 0/36 bounds are a shape check, not validation, so a right-length non-uuid lands here;
        # same reason as a blank setting.
        return None, REASON_CREDENTIAL_MISSING

    with seam.detached(), seam.operator_scope(), BridgedSession() as operator_session:
        credential = operator_session.get(Credential, credential_id)
        if credential is None:
            # Designated, then deleted: nothing revalidates the setting on removal, so it is asked here.
            return None, REASON_CREDENTIAL_MISSING
        if credential.org_id not in OPERATOR_PLANE_ORG_IDS:
            # The reason key names nothing of the row; the log carries the ids for re-picking, never the
            # token. A NULL `org_id` (a schema violation) lands here too: fail closed.
            alert_relay._warn_once(
                _OFF_PLANE_WARN_KEY,
                "notifications: the Slack credential in Settings > Integrations > Slack names credential id=%s owned by "
                "org=%s, which is not the operator plane — refused. Re-pick it in that setting.",
                credential.id, credential.org_id,
            )
            return None, REASON_CREDENTIAL_WRONG_TYPE
        if credential.type != "slack":
            # A non-`slack` row: a setting written around the UI's filter. Its own reason because the fix is
            # to re-point the setting.
            return None, REASON_CREDENTIAL_WRONG_TYPE
        # `token` is the bot token (`app/credentials/registry.py`). Read fresh as a plain string before the
        # session closes, so a rotation applies on the next alert.
        config = credential.config or {}
        if not has_sealed_secret(config, "token"):
            return None, REASON_BOT_TOKEN_MISSING
        try:
            bot_token = open_secret(credential, "token")
        except ValueError:
            # An envelope this deployment cannot open reads as no token: the operator re-enters it.
            log.warning(
                "notifications: the bot token on Slack credential id=%s (%r) cannot be opened — "
                "re-enter it", credential.id, credential.name,
            )
            return None, REASON_BOT_TOKEN_MISSING
        if not bot_token.strip():
            return None, REASON_BOT_TOKEN_MISSING
        return bot_token, REASON_RECIPIENT_MISSING


def slack_bot_token(org_id: Optional[UUID], *, team_id: str = "") -> tuple[Optional[str], str]:
    """The bot token every per-user notification about `org_id`'s members posts with, or `(None, reason)`.

    A tenant org posts with its own connected workspace and nothing else (`slack_workspaces.for_org`),
    so one org's text can never go out with another's app. `team_id`, when given, is the workspace the caller
    checked earlier (`SlackTarget.team_id`): a different one connected since is `REASON_WORKSPACE_MISMATCH`. The
    operator plane keeps the designated credential (`_org_slack_bot_token`)."""
    # The community edition has no Slack: no app to post with.
    if not edition_features.enabled("slack"):
        return None, REASON_CREDENTIAL_MISSING
    from app import slack_workspaces
    from app.tenancy.ids import is_tenant_org

    if org_id is not None and is_tenant_org(org_id):
        workspace = slack_workspaces.for_org(org_id)
        if workspace is None:
            return None, REASON_WORKSPACE_NOT_CONNECTED
        if team_id and workspace.team_id != team_id:
            return None, REASON_WORKSPACE_MISMATCH
        return workspace.bot_token, REASON_RECIPIENT_MISSING
    return _org_slack_bot_token()


@contextmanager
def _reading(user: AdminUser, session: Optional[Session]) -> Iterator[Session]:
    """A session to read `user`'s channels on: the caller's, else the one `user` is loaded in, else a short one
    bound to `user`'s org."""
    if session is not None:
        yield session
        return
    attached = object_session(user)
    if attached is not None:
        yield attached
        return
    with notification_channels.org_session(user.org_id) as own:
        yield own


def _event_level(
    session: Session, user: AdminUser, channel: NotificationChannel, event: str, admin: Optional[bool]
) -> tuple[str, bool]:
    """The level `channel` resolves for `event` (a catalog event or producer kind) and the owner's admin verdict,
    computed once per person by the caller (`admin` None: ask now). A kind that is no longer a notification is off."""
    if admin is None:
        admin = notification_channels.viewer_is_admin(session, user)
    name = notification_catalog.event_for(event)
    if name is None:
        return notification_catalog.OFF, admin
    return notification_catalog.level(
        channel.type, channel.settings, name, viewer_is_admin=admin, account_email=channel.account_email,
    ), admin


def slack_routes(
    session: Session, user: AdminUser, *, event: str, muteable: bool = True
) -> Resolved:
    """`user`'s Slack channels for this event: each one's `(member_id, bot_token, team_id)` at its level, plus each
    skipped one with why. `team_id` is the tenant workspace checked (`SlackTarget.team_id`), `""` on the operator
    plane.

    Order of refusals per channel: address, then mute, then credential. Address first, so a user with no Slack cannot
    silence an alert by muting. Mute before credential, because `REASON_CREDENTIAL_MISSING` falls through to the
    shared webhook with the full text; a misconfiguration must never defeat a privacy choice. `muteable=False` skips
    the mute: a page (`alert_relay.PAGE_KINDS`) is not the recipient's to decline.
    """
    # The community edition has no Slack: a stored Slack channel is skipped like an absent one.
    if not edition_features.enabled("slack"):
        return Resolved(skipped=[(None, REASON_CHANNEL_NOT_SLACK)])
    # A membership still waiting on an invitation has no address here: nothing of this org reaches the
    # person before they accept (`AdminUser.invitation_id`).
    if user.invitation_id is not None:
        return Resolved(skipped=[(None, REASON_RECIPIENT_MISSING)])
    channels = notification_channels.channels_of(session, user, SLACK)
    if not channels:
        return Resolved(skipped=[(None, REASON_CHANNEL_NOT_SLACK)])

    from app import slack_workspaces
    from app.tenancy.ids import is_tenant_org

    resolved = Resolved()
    admin: Optional[bool] = None
    workspace: Any = None
    designated: tuple[Optional[str], str] | None = None
    for channel in channels:
        target = channel.slack_target_id or ""
        if not target:
            resolved.skipped.append((channel, REASON_RECIPIENT_MISSING))
            continue
        if channel.slack_kind == "dm" and not _SLACK_USER_ID_RE.match(target):
            # A malformed stored id would be posted as `channel`, fail, and (Slack failures do not fall through)
            # silence the recipient; refuse it as a fixable misconfiguration, not an absence.
            resolved.skipped.append((channel, REASON_RECIPIENT_MALFORMED))
            continue
        level = notification_catalog.FULL
        if muteable:
            level, admin = _event_level(session, user, channel, event, admin)
            if level == notification_catalog.OFF:
                resolved.skipped.append((channel, REASON_EVENT_MUTED))
                continue
        # The recipient's org decides the app (`slack_bot_token`); a tenant member must have proven their id in THAT
        # org's workspace, or the DM would go to whoever holds that member id in another workspace.
        if is_tenant_org(user.org_id):
            if workspace is None:
                workspace = slack_workspaces.for_org(user.org_id) or False
            if not workspace:
                resolved.skipped.append((channel, REASON_WORKSPACE_NOT_CONNECTED))
                continue
            if (channel.slack_team_id or "") != workspace.team_id:
                resolved.skipped.append((channel, REASON_WORKSPACE_MISMATCH))
                continue
            bot_token, team_id = workspace.bot_token, workspace.team_id
        else:
            if designated is None:
                designated = _org_slack_bot_token()
            if designated[0] is None:
                resolved.skipped.append((channel, designated[1]))
                continue
            bot_token, team_id = designated[0], ""
        resolved.routes.append(Route(channel, (target, bot_token, team_id), level))
    return resolved


def page_targets(session: Session) -> list[SlackTarget]:
    """Every enabled superadmin's Slack DM channels for a page (`alert_relay.PAGE_KINDS`), oldest account first.

    The operators are who a CloudWatch alarm is for (`app/alarm_relay.py`), and a page has no task to
    walk a chain from. Unmuteable, always `"full"`: a page is never reduced to a headline. A superadmin with no
    Slack DM channel is simply not a recipient; the env channel (`ALERT_WEBHOOK_URL`) is the caller's.
    """
    from app.alert_relay import PAGE_KINDS

    users = session.scalars(
        select(AdminUser)
        .where(
            AdminUser.is_superadmin.is_(True), AdminUser.disabled_at.is_(None), AdminUser.is_system.is_(False)
        )
        .order_by(AdminUser.created_at, AdminUser.id)
    ).all()
    targets: list[SlackTarget] = []
    for user in users:
        for route in slack_routes(session, user, event=PAGE_KINDS[0], muteable=False).routes:
            if route.channel is None or route.channel.slack_kind != "dm":
                continue
            member_id, bot_token, team_id = route.address
            targets.append(
                SlackTarget(
                    admin_user_id=user.id, member_id=member_id, bot_token=bot_token, org_id=user.org_id,
                    team_id=team_id, channel_id=route.channel.id,
                )
            )
    return targets


def email_routes(user: AdminUser, *, event: str, session: Optional[Session] = None) -> Resolved:
    """`user`'s Email channels for this event: each one's address at its level, plus each skipped one with why.

    The account-email channel sends to the login email, and only once it is proven (`AdminUser.email_verified_at`,
    cleared on every change: a Google, Microsoft or GitHub sign-in, or `app/notification_emails.py`'s link or code); a
    typed address only once the person proved it (`notification_emails.verified_at`). Without that proof system mail would go to whatever address the row
    holds. Refusal order per channel: the address, then the mute. The deployment's system mail is asked by the
    caller, once per alert rather than per candidate."""
    from app.mail_message import single_address
    from app.models.identity import NotificationEmail

    # As `slack_routes`: a membership waiting on an invitation is sent nothing.
    if user.invitation_id is not None:
        return Resolved(skipped=[(None, REASON_RECIPIENT_MISSING)])
    with _reading(user, session) as reader:
        channels = notification_channels.channels_of(reader, user, EMAIL)
        if not channels:
            return Resolved(skipped=[(None, REASON_CHANNEL_NOT_EMAIL)])
        resolved = Resolved()
        admin: Optional[bool] = None
        seen: set[str] = set()
        for channel in channels:
            if channel.account_email:
                address = single_address(user.email or "")
                proven = user.email_verified_at is not None
            else:
                typed = reader.get(NotificationEmail, channel.email_id) if channel.email_id is not None else None
                address = single_address(typed.address) if typed is not None else None
                proven = typed is not None and typed.verified_at is not None
            if address is None:
                resolved.skipped.append((channel, REASON_RECIPIENT_MISSING))
                continue
            if not proven:
                resolved.skipped.append((channel, REASON_EMAIL_UNVERIFIED))
                continue
            level, admin = _event_level(reader, user, channel, event, admin)
            if level == notification_catalog.OFF:
                resolved.skipped.append((channel, REASON_EVENT_MUTED))
                continue
            key = address.lower()
            if key in seen:
                # One mailbox named twice (a typed copy of the login email) gets one message, at the fuller level.
                if level == notification_catalog.FULL:
                    resolved.routes = [
                        route._replace(level=level) if route.address.lower() == key else route
                        for route in resolved.routes
                    ]
                continue
            seen.add(key)
            resolved.routes.append(Route(channel, address, level))
        return resolved


def telegram_routes(user: AdminUser, *, event: str, session: Optional[Session] = None) -> Resolved:
    """`user`'s Telegram channels for this event: each one's chat id at its level, plus each skipped one with why.

    The chat is the person's proven link (`telegram_links`, `app/telegram_link.py`), never a stored id. Same refusal
    order as `email_routes`. The bot is asked by the caller, once per alert."""
    # The community edition has no Telegram: a stored Telegram channel is skipped like an absent one.
    if not edition_features.enabled("telegram"):
        return Resolved(skipped=[(None, REASON_CHANNEL_NOT_TELEGRAM)])
    from app.models.identity import TelegramLink

    if user.invitation_id is not None:
        return Resolved(skipped=[(None, REASON_RECIPIENT_MISSING)])
    with _reading(user, session) as reader:
        channels = notification_channels.channels_of(reader, user, TELEGRAM)
        if not channels:
            return Resolved(skipped=[(None, REASON_CHANNEL_NOT_TELEGRAM)])
        resolved = Resolved()
        admin: Optional[bool] = None
        for channel in channels:
            link = reader.get(TelegramLink, channel.telegram_link_id) if channel.telegram_link_id is not None else None
            if link is None or link.chat_id is None:
                resolved.skipped.append((channel, REASON_TELEGRAM_NOT_LINKED))
                continue
            level, admin = _event_level(reader, user, channel, event, admin)
            if level == notification_catalog.OFF:
                resolved.skipped.append((channel, REASON_EVENT_MUTED))
                continue
            resolved.routes.append(Route(channel, link.chat_id, level))
        return resolved


def targets_for_user(
    session: Session, user: AdminUser, *, event: str, outcomes: Optional[Outcomes] = None
) -> tuple[list[SlackTarget], str]:
    """This one person's Slack destinations for `event`, or `([], reason)` naming what is missing.

    The chain-free twin of `resolve_slack_targets`: a mention names its recipient outright (`app/comment_notify.py`).
    The caller must not fall through on `[]`, since the shared operator channel would broadcast one person's
    mention; an unresolved mention is not sent.
    """
    resolved = slack_routes(session, user, event=event)
    if outcomes is not None:
        outcomes.skipped(user, SLACK, resolved.skipped)
    targets = [
        SlackTarget(
            admin_user_id=user.id, member_id=route.address[0], bot_token=route.address[1], org_id=user.org_id,
            detail=route.level, team_id=route.address[2],
            channel_id=route.channel.id if route.channel is not None else None,
        )
        for route in resolved.routes
    ]
    return targets, (REASON_RECIPIENT_MISSING if targets else resolved.reason())


def _question_for_task(
    session: Session, task_id: Optional[int], question_id: Optional[int] = None
) -> tuple[Optional[SlackQuestion], tuple[str, ...]]:
    """The Task's question and its selectable answers, from whichever of the two writers asked.

      * `AskUserQuestion` (the node's question-capture hook): one `task_questions` row per question with
        structured options; `awaiting_response_body` holds the text alone.
      * The roundless park (`task_pipeline_cp.block_task`): no round; the raw body holds both.
    Rounds first, body second.

    The round is the one `question_id` names (via `open_round`), else the oldest open one, matching
    `routes/tasks._open_round_or_409` so buttons answer the printed question. Returns `(None, ())` when
    there is nothing to show; empty `options` means a free-text question.
    """
    if task_id is None:
        return None, ()
    task = session.get(Task, task_id)
    if task is None:
        return None, ()

    if question_id is not None:
        # A named round that is no longer open renders nothing, never the cold path's unrelated body.
        round_row = open_round(session, task_id, question_id)
        if round_row is None:
            return None, ()
    else:
        round_row = oldest_open_round(session, task_id)
    if round_row is not None:
        body = round_row.question or ""
        options = tuple(_round_option_labels(round_row.options))
        # The same blob through the same normalizer, which makes the two lists index-aligned.
        option_details = tuple(_round_option_details(round_row.options))
        # `multiSelect` exists only on the round row, so the roundless arm leaves it false (as
        # `routes/tasks._gate_answer_or_422`).
        multi_select = bool(round_row.multi_select)
    else:
        # `question_prose` strips only a well-formed trailing `**Options:**` block.
        body = question_prose(task.awaiting_response_body)
        options = tuple(parse_response_options(task.awaiting_response_body) or ())
        multi_select = False
        # No round: the cold path's options are parsed strings, with no author annotations or header.
        option_details = ()

    if not body and not options:
        return None, ()
    # Function-local for the `done` arm's reason (alert hot path); the private name is the single home of
    # the console task URL.
    from app.trackers.writeback import task_url as _console_task_url

    return (
        SlackQuestion(
            task_id=task.id,
            title=task.title,
            body=body,
            url=_console_task_url(task.id),
            question_id=None if round_row is None else round_row.id,
            multi_select=multi_select,
            option_details=option_details,
            # Off the row, not `question_id`: the unnamed path resolves the oldest open round.
            header=None if round_row is None else round_row.header,
        ),
        options,
    )


def open_round(session: Session, task_id: int, question_id: int) -> Optional[TaskQuestion]:
    """The round `question_id` names, iff it belongs to `task_id`, is still `open` and is still shown;
    else `None`. Readers: `_question_for_task`, `slack_inbound._is_stale_thread`, and the click gate.

    `task_id` is in the WHERE because `question_id` arrives from untrusted Slack payloads and rows.
    Several rounds may be open at once, so this asks about the named round, not the oldest.
    """
    return session.scalars(
        # The join adds the Reset watermark (`task_artifacts`): a reset-away round stays `open` forever.
        select(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.id == question_id,
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
    ).first()


def open_rounds(task_id: int) -> Select[tuple[TaskQuestion]]:
    """The task's visible open `task_questions` rounds, oldest first: THE order a resolve targets, so the Slack
    card (`oldest_open_round`) and the console's answer route (`routes/tasks/answers._open_round_or_409`) aim at
    the same question. Same join and reason as `open_round`; soft-deleted rounds are out."""
    return (
        select(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
        .order_by(TaskQuestion.created_at, TaskQuestion.id)
    )


def oldest_open_round(session: Session, task_id: int) -> Optional[TaskQuestion]:
    """The `task_questions` round `routes/tasks.answer_task` would resolve, or `None` when none is open.

    One home, read by `_question_for_task` and `slack_inbound` at click time."""
    return session.scalars(open_rounds(task_id).limit(1)).first()


def _round_option_labels(options: object) -> list[str]:
    """A stored round's `options` blob as display strings, via the console's own projection.

    `_question_options_out` is the one normalizer of both writers' shapes; a copy here could disagree with
    the console. Function-local because `app.routes.tasks` pulls in the route layer and the Socket Mode
    consumer imports this module at start-up.
    """
    from app.routes.tasks.qa_render import _question_options_out

    return _question_options_out(options) or []


def _round_option_details(options: object) -> list[Any]:
    """The same blob as `_round_option_labels`, as full option records. Same import and reasons;
    index-aligned by construction. Returned as the projection's objects, which `slack_blocks` duck-types.
    """
    from app.routes.tasks.qa_render import _question_option_details

    return _question_option_details(options) or []


def record_thread(
    session: Session,
    *,
    task_id: int,
    admin_user_id: Optional[UUID],
    channel_id: str,
    thread_ts: str,
    question_id: Optional[int] = None,
    brief: bool = False,
) -> None:
    """Write the correlation row that makes a reply in this Slack thread answer this Task.

    `channel_id`/`thread_ts` must come from the `chat.postMessage` response (`channel`, `ts`), not the
    `U…` member id: replies arrive under the `D…` DM Slack returned. `question_id` binds the thread to its
    round; `None` for the roundless path (`models/notifications.py::SlackThread`). `brief` marks a brief
    recipient's line, which speaks for its whole batch. Keyword-only.

    Never raises: it runs after `record_alert`'s commit. A lost row costs one unmatched reply.
    """
    try:
        session.execute(
            pg_insert(SlackThread)
            .values(
                channel_id=channel_id,
                thread_ts=thread_ts,
                task_id=task_id,
                question_id=question_id,
                brief=brief,
                admin_user_id=admin_user_id,
                # The Task's own org, set here because the write seam's `before_flush` listener cannot see a
                # Core `pg_insert`; left NULL the IntegrityError would be swallowed below. A scalar subquery
                # inside the DML (as `fleet_verdicts._persist_kwargs`), so it resolves on the consumer's
                # unbound session.
                org_id=select(Task.org_id).where(Task.id == task_id).scalar_subquery(),
            )
            # `(channel_id, thread_ts)` is one Slack message. DO NOTHING rather than DO UPDATE: the first
            # writer saw the same response, and an IntegrityError would poison the session.
            .on_conflict_do_nothing(constraint="uq_slack_threads_channel_thread")
        )
        session.commit()
    except Exception:
        session.rollback()
        log.exception(
            "could not record the Slack thread correlation for task_id=%s — a reply in that thread "
            "will get the 'no matching Task' explanation instead of answering it",
            task_id,
        )


def newest_thread_for(
    session: Session, channel_id: str, thread_ts: str
) -> Optional[SlackThread]:
    """The correlation row a reply in `(channel_id, thread_ts)` resolves to, or `None` (an alert with no
    `task_id`, a pre-feature thread, a top-level DM).

    Returns the row so the handler can run the stale-thread check (is it the newest for its task?)
    itself. `ORDER BY id DESC LIMIT 1` keeps it total without trusting the unique constraint.
    """
    return session.scalars(
        select(SlackThread)
        .where(SlackThread.channel_id == channel_id, SlackThread.thread_ts == thread_ts)
        .order_by(SlackThread.id.desc())
        .limit(1)
    ).first()


def users_for_slack_member_id(session: Session, member_id: str) -> list[AdminUser]:
    """Every Jaah user who has claimed this Slack member id, ordered by id; `[]` for none.

    The claim is a Slack DM channel naming the member id (`notification_channels`): deleting that channel revokes
    it. A muted event does not revoke the claim; settings are about being told.

    A list: one Slack account may belong to several rows (one human on several orgs). Do not add
    uniqueness; the caller checks whether the thread's addressee is among them (`app/slack_inbound.py`),
    which is tighter. Order is for determinism only. The member id is not logged.

    Inside an inbound frame only the claims the frame's workspace serves count (`slack_workspaces.serves_user`):
    a tenant workspace's members of that org proven there, the home workspace's operator-plane users. So every
    inbound path (replies, clicks, Connect, unfurls, access cards, triggers) is scoped by one rule.
    """
    # The community edition has no Slack, so nobody's claim is served.
    if not edition_features.enabled("slack"):
        return []
    from app import slack_workspaces

    if not member_id:
        return []
    claims = session.execute(
        select(AdminUser, NotificationChannel.slack_team_id)
        .join(NotificationChannel, NotificationChannel.admin_user_id == AdminUser.id)
        .where(
            NotificationChannel.type == SLACK,
            NotificationChannel.slack_kind == "dm",
            NotificationChannel.slack_target_id == member_id,
        )
        .order_by(AdminUser.id)
    ).all()
    claimants: dict[UUID, tuple[AdminUser, str]] = {}
    for user, team_id in claims:
        claimants.setdefault(user.id, (user, team_id or ""))
    workspace = slack_workspaces.current()
    if workspace is None:
        return [user for user, _team in claimants.values()]
    return [
        user for user, team_id in claimants.values()
        if slack_workspaces.serves_user(workspace, user.org_id, team_id)
    ]


def claim_event(session: Session, event_id: str) -> bool:
    """Claim one inbound Slack envelope. `True` = this process may handle it; `False` = a duplicate,
    already claimed (possibly by this process before a restart): ack and drop.

    A DB row, not an in-process set: Slack may route to any of ten connections, replicas may overlap and
    restarts empty memory. `ON CONFLICT DO NOTHING … RETURNING`, as `alert_record.record_alert`.
    """
    claimed = session.execute(
        pg_insert(SlackEvent)
        .values(event_id=event_id)
        .on_conflict_do_nothing(constraint="uq_slack_events_event_id")
        .returning(SlackEvent.event_id)
    ).first()
    session.commit()
    if claimed is None:
        return False
    _prune_old_events(session)
    return True


def _prune_old_events(session: Session) -> None:
    """Opportunistic retention for `slack_events` (see `_EVENT_RETENTION`): at most hourly per process, only
    after a claim landed, and never raises.
    """
    global _last_prune_monotonic
    now = time.monotonic()
    if _last_prune_monotonic is not None and now - _last_prune_monotonic < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune_monotonic = now
    try:
        session.execute(
            delete(SlackEvent).where(
                SlackEvent.created_at < utcnow() - _EVENT_RETENTION
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        log.exception("could not prune expired slack_events rows — the table will keep growing")
