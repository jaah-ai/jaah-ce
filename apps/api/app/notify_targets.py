"""Every DB question the Slack legs of the alert path ask: recipient, credential, response options,
and the two records that make an inbound reply resolvable.

A sibling of the DB-free `alert_relay`: `alert_record` asks this module for a recipient and hands the
relay a plain `SlackTarget`. Called by `alert_record` (outbound) and the Socket Mode consumer (inbound).

Correlation lives in `slack_threads`, not `alerts.payload`: an `alerts` row is withdrawn on
non-delivery and bucketed for 300s dedup, while a correlation is read hours later.

`SlackTarget.bot_token` is read from the `credentials` row on every alert, never cached or logged;
nor are member ids or the `preferences` blob.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import alert_relay, edition_features
from app.actors import actor_names
from app.authz.catalog import permission_key
from app.authz.object_access import can_access, key_scope_satisfied
from app.authz.principal import principal_for_user
from app.authz.visibility import TASK_SPEC, ObjectAccess
from app.credential_crypto import has_sealed_secret, open_secret
from app.entity_shape import optional_uuid
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.notifications import SlackEvent, SlackThread
from app.models.projects import Project
from app.models.tasks import Task, TaskQuestion
from app.preferences import NOTIFICATION_METHOD_TYPES, _SLACK_USER_ID_RE, UserPreferences
from app.task_ancestry import assignee_user_id, task_and_ancestors as _task_and_ancestors
from app.task_artifacts import visible_artifact_clause_joined
from app.task_awaiting_response_parser import parse_response_options, question_prose

log = logging.getLogger("app.notify_targets")

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
# It only escapes `resolve_slack_target` on the `done=True`/`assigned=True` single-candidate paths,
# where `alert_record` drops the alert (falling through would turn a mute into a redirect). For other
# kinds the mute is consumed inside the walk and the alert still reaches the webhook.
REASON_EVENT_MUTED = "slack-event-muted"

#: The events addressed to one person; the Notifications panel's operational rows are the complement.
#:
#: Not the chain-stop predicate: `resolve_slack_target` stops on its `done`/`assigned` flags. A
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
    # prose, options or buttons). Carried from their preferences (`_slack_destination`), not decided.
    # Defaults `"full"` since the relay reads it with `getattr` off a duck-typed target.
    detail: str = "full"
    # The tenant workspace (`team_id`) the recipient was checked against when this target was resolved; `""` on the
    # operator plane. The outbox re-checks it before the send (`durable/notify_outbox._slack_step`): a reconnect to
    # another workspace in between refuses the DM rather than send the member id into a workspace it was not proven in.
    team_id: str = ""


def resolve_slack_target(
    session: Session,
    task_id: Optional[int],
    *,
    kind: str = "",
    answerable: bool = False,
    question_id: Optional[int] = None,
    done: bool = False,
    assigned: bool = False,
    assigned_by_id: Optional[UUID] = None,
) -> tuple[Optional[SlackTarget], Optional[str]]:
    """Resolve the Slack destination for this alert (the Task's addressee when there is one), plus the
    Task's response options and question presentation.

    Returns `(target, None)`, or `(None, reason)` with `reason` in `REASON_KEYS`; `None` means fall
    through to the env-var channel, never "delivery failed". The chain (`_recipient_candidates`) tries
    each candidate in full; each rung is gated on the object model. `task_id` is optional; `answerable`
    gates the question card; `question_id` names the round (`None`: the oldest open one). `kind` is
    matched against the recipient's subscriptions; unknown events are delivered.

    Mutes: only `done=True`/`assigned=True` (single candidate: `_task_addressee`/`_task_assignee`, no
    question lookup) can return `REASON_EVENT_MUTED`, which `alert_record` drops. Elsewhere a muted
    candidate is skipped and not retained, so the alert still reaches the webhook; a mute is not
    confidentiality. The flags are exclusive by the caller's `kind`; if two arrive, `done` wins the
    candidate rule and `assigned` the card.
    """
    personal = done or assigned
    candidates = alert_candidates(session, task_id, answerable=answerable, done=done, assigned=assigned)
    if not candidates:
        return None, REASON_RECIPIENT_MISSING

    # Below the guard so an unconfigured recipient does not pay for this work per alert.
    question: Optional[SlackQuestion]
    done_card: Optional[SlackDone] = None
    assigned_card: Optional[SlackAssigned] = None
    if personal:
        # Function-local to keep the route layer (and httpx) off the alert hot path. The private name is the
        # single home of the console task URL.
        from app.task_external_comment import _console_task_url

        question, options = None, ()
        # `session.get` again is an identity-map hit. Written total anyway: an `AttributeError` here would
        # withdraw the dedup row and count a `relay_failed`.
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

    # The retained reason excludes a non-personal mute: `alert_record` drops on `REASON_EVENT_MUTED`, so a
    # last candidate's mute (the Task's owner) would otherwise read as a drop rather than a fall-through.
    # An exhausted chain reports a fall-through reason instead.
    reason: str = REASON_RECIPIENT_MISSING
    for user in candidates:
        destination, candidate_reason = _slack_destination(session, user, event=event)
        if destination is None:
            if candidate_reason == REASON_EVENT_MUTED:
                # `personal` comes from the flags, not `event in PERSONAL_EVENTS`: a child task's `done`
                # arrives with `done=False` and a full chain, and a question has a chain (see
                # `PERSONAL_EVENTS`).
                if personal:
                    # The chain stops for a card that is this person's; walking on would hand their card to
                    # someone else.
                    return None, REASON_EVENT_MUTED
                # An operational kind this candidate muted walks on and its mute is not retained; see
                # `PERSONAL_EVENTS` and the block above.
                continue
            reason = candidate_reason
            continue
        member_id, bot_token, detail, team_id = destination
        return (
            SlackTarget(
                admin_user_id=user.id,
                member_id=member_id,
                bot_token=bot_token,
                org_id=user.org_id,
                team_id=team_id,
                options=options,
                answerable=answerable,
                question=question if answerable else None,
                done=done_card,
                assigned=assigned_card,
                detail=detail,
            ),
            None,
        )
    return None, reason


def alert_candidates(
    session: Session, task_id: Optional[int], *, answerable: bool, done: bool, assigned: bool
) -> list[AdminUser]:
    """Who an alert may reach, in preference order — the one rule every channel's leg walks
    (`resolve_slack_target`, `app/email_notify.py`), so Slack and email never disagree about who may read
    the Task.

    A personal card (`done`/`assigned`) has no chain: one candidate or none, gated like the other rungs,
    since both cards carry the Task's title and provenance is not access. Everything else walks
    `_recipient_candidates`."""
    if done or assigned:
        addressee = _task_addressee(session, task_id) if done else _task_assignee(session, task_id)
        return [addressee] if may_read_task(session, addressee, task_id) else []
    return _recipient_candidates(session, task_id, question=answerable)


def alert_event(
    session: Session, kind: str, task_id: Optional[int], *, answerable: bool, question_id: Optional[int]
) -> str:
    """Which `preferences.NOTIFICATION_EVENTS` member an alert is, for a leg that renders no Slack card
    (`app/email_notify.py`): `question` exactly when `resolve_slack_target` would render a question card."""
    question = _question_for_task(session, task_id, question_id)[0] if answerable else None
    return _event_id(kind, question=question)


#: A channel's per-candidate resolver (`email_destination`, `telegram_destination`) and its send, as the email
#: and Telegram legs hand them to `deliver_alert`/`deliver_to_user`: `send(event, user, where, detail, event_name)`.
Destination = Callable[..., tuple[Optional[tuple[Any, str]], str]]
Send = Callable[[dict[str, Any], AdminUser, Any, str, str], bool]


def deliver_alert(
    session: Session, event: dict[str, Any], destination_of: Destination, send: Send, *,
    task_id: Optional[int], answerable: bool, question_id: Optional[int], done: bool, assigned: bool,
    skipped: Optional[list[str]] = None,
) -> bool:
    """Send `event` over one channel to the first candidate in the chain (`alert_candidates`) who takes it, under
    the `alert_event` the destination was resolved for. The one walk the email and Telegram legs share
    (`app/email_notify.py`, `app/telegram_notify.py`), so they stop at the same person. Never raises: a lookup
    fault must not reach `alert_record`, whose Slack leg and dedup row do not depend on this leg.

    `skipped` (an out-parameter) gains this channel's resolver name when its person was brief and the event a
    later question of a batch (`alert_relay.brief_skips`): told already, so not a route to fall through from."""
    try:
        candidates = alert_candidates(session, task_id, answerable=answerable, done=done, assigned=assigned)
        if not candidates:
            return False
        kind = str(event.get("kind") or "")
        event_name = alert_event(session, kind, task_id, answerable=answerable, question_id=question_id)
        for user in candidates:
            destination, _ = destination_of(user, event=event_name)
            if destination is not None:
                # The chain stops here either way: this person takes the alert, at the detail they chose.
                if alert_relay.brief_skips(event, destination[1]):
                    if skipped is not None:
                        skipped.append(destination_of.__name__)
                    return False
                return send(event, user, *destination, event_name)
    except Exception:
        log.exception("%s: recipient lookup failed (or the send did) for kind=%s — not sent", destination_of.__name__, event.get("kind"))
    return False


def deliver_to_user(
    user: AdminUser, event: dict[str, Any], destination_of: Destination, send: Send, *, event_name: str
) -> bool:
    """Send `event` over one channel to one named person (a mention, a notice): no chain and no fall-through, since
    nobody else may read their message. Never raises: what it announces is already committed."""
    try:
        destination, _ = destination_of(user, event=event_name)
        return destination is not None and send(event, user, *destination, event_name)
    except Exception:
        log.exception("%s: recipient lookup failed (or the send did) for kind=%s — not sent", destination_of.__name__, event.get("kind"))
        return False


def _event_id(kind: str, *, question: Optional[SlackQuestion]) -> str:
    """Which `preferences.NOTIFICATION_EVENTS` member this alert is, from its kind and its card.

    The one place `blocked` is split: a parked question and a plain blocked transition share a kind but
    are different events to the recipient, and a resolved question card is the only honest discriminator.
    """
    return "question" if kind == "blocked" and question is not None else kind


def _recipient_candidates(
    session: Session, task_id: Optional[int], *, question: bool
) -> list[AdminUser]:
    """Who this alert may be delivered to, in preference order: the project role a question is referred
    to, then the Task's owner (`_task_owner`). Deduplicated by row id, order preserved. There is no
    deployment-wide last-resort human: an alert nobody up the chain may read reaches no person.

    `question` gates the first rung only; the caller passes its own `answerable`. The owner rung is gated
    on reading the Task (`may_read_task`), the role rung on writing it. A task-less alert has no rung.

    Returns `[]` when no rung exists or none may read the Task: the fall-through, not an error. The
    env-var operator channel it reaches is outside the grant model, a known blind spot.
    """
    owner = _task_owner(session, task_id)
    rungs = (
        _role_recipient(session, task_id) if question else None,
        owner if may_read_task(session, owner, task_id) else None,
    )
    candidates: list[AdminUser] = []
    seen: set[UUID] = set()
    for user in rungs:
        if user is not None and user.id not in seen:
            seen.add(user.id)
            candidates.append(user)
    return candidates


# The two `projects` columns the Humans section writes, as attribute names for `_role_recipient`'s `getattr`.
_ROLE_BUSINESS_ANALYST = "business_analyst_id"
_ROLE_ARCHITECT = "architect_id"


def _referred_role(session: Session, task: Task) -> Optional[str]:
    """Which project role a question on this Task is referred to: a `projects` column name, or `None` for
    "use the assignee chain" (`_task_owner`).

    Implementation home; the Slack integration doc's "Who a question is sent to" declares it too.
    Edit both. A system Task goes to the architect. Inside an epic, `business_analysis` goes to the
    business analyst and everything else (the epic included) to the architect. A standalone Task uses its
    assignee. The import is function-local to keep the route layer off the alert path.
    """
    from app.routes.tasks.policy import PERSONA_BUSINESS_ANALYSIS, PERSONA_EPIC
    from app.persona_registry import SYSTEM_PERSONAS

    if task.persona in SYSTEM_PERSONAS:
        return _ROLE_ARCHITECT
    # `any`, not `parent_task_id is not None`: a parent is not necessarily an epic. The walk bound
    # (`task_ancestry.MAX_OWNER_WALK_HOPS`) is borrowed; beyond it an epic reads as none and the referral is
    # silently skipped.
    if not any(
        ancestor.persona == PERSONA_EPIC for ancestor in _task_and_ancestors(session, task.id)
    ):
        return None
    return (
        _ROLE_BUSINESS_ANALYST
        if task.persona == PERSONA_BUSINESS_ANALYSIS
        else _ROLE_ARCHITECT
    )


def _role_recipient(session: Session, task_id: Optional[int]) -> Optional[AdminUser]:
    """The human named in this Task's project for the role its question is referred to, or `None` when
    there is no such Task, no role applies, or the column is unset.

    Unset is the ordinary, silent state. The system actor is skipped even though the write path refuses
    it. An analyst with unconfigured Slack falls through to the assignee via the caller's chain.
    """
    if task_id is None:
        return None
    task = session.get(Task, task_id)
    if task is None:
        return None
    role = _referred_role(session, task)
    if role is None:
        return None
    project = session.get(Project, task.project_id)
    if project is None:
        return None
    user_id = getattr(project, role)
    if user_id is None:
        return None
    user = session.get(AdminUser, user_id)
    if user is None or user.is_system:
        return None
    # The referral must name the scope the answer will be judged on: a named role confers no task write,
    # so a role holder without `tasks:edit` on the task's project would receive another
    # audience's question and have their reply refused, and the chain would stop there. Asked through the
    # whole decision `slack_inbound._may_call` applies (object and key probes; the object half matters for
    # hidden system rows), and the write question, not the read one, deliberately (guarded by
    # `test_the_referral_asks_the_WRITE_question_and_deliberately_not_the_READ_one`).
    principal = principal_for_user(session, user)
    if not can_access(session, principal, "task", task.id, mode=ObjectAccess.WRITE):
        return None
    if not key_scope_satisfied(
        session,
        principal,
        TASK_SPEC,
        task.id,
        key=permission_key("tasks", "edit"),
        mode=ObjectAccess.WRITE,
    ):
        return None
    return user


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
    """The human this Task is addressed to: its own `assigned_to_id`, else its own `created_by_id`;
    `None` when neither names a human.

    Assignee first per the operator's rule; a NULL assignee is a machine-filed row or a deleted user. Not
    `_task_owner`: the done card is posted only for parentless Tasks, and walking would hide a regression
    in that gate. No fallback rung: an unsent "shipped" DM costs little, while a fallback would flood the
    address misconfiguration warnings go to. Provenance, not access (`may_read_task` is conjoined).
    """
    if task_id is None:
        return None
    task = session.get(Task, task_id)
    if task is None:
        return None
    return _human(session, task.assigned_to_id) or _human(session, task.created_by_id)


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
    from app.db import SessionLocal
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

    with seam.detached(), seam.operator_scope(), SessionLocal() as operator_session:
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


def available_notification_methods(org_id: Optional[UUID] = None) -> list[str]:
    """The `preferences.NOTIFICATION_METHOD_TYPES` this deployment can actually deliver right now to members of
    `org_id`.

    Served in the identity payload (`routes/auth.AdminOut.notification_methods_available`) so the panel
    can flag an unconfigured channel. Slack shares `slack_bot_token`'s check (the org's own workspace for a
    tenant, the designated credential on the operator plane) and email asks `system_mail.configured`. Returns
    types only: no id, name or reason, since every principal reads it.
    """
    # A capability probe must not 500 the identity payload: it rides every `AdminOut` constructor
    # unwrapped, so a raise here logs every user out, including from Settings where the fix lives. Each
    # channel is probed on its own, so one channel's fault never hides the other. `log.exception`, not a
    # silent swallow.
    available: list[str] = []
    try:
        bot_token, _ = slack_bot_token(org_id)
        if bot_token:
            available.append("slack")
    except Exception:
        log.exception(
            "notifications: the deployment's Slack credential could not be resolved — reporting Slack "
            "unavailable rather than failing the identity response"
        )
    try:
        from app import system_mail

        if system_mail.configured():
            available.append("email")
    except Exception:
        log.exception("notifications: the email settings could not be read — reporting email unavailable")
    if edition_features.enabled("telegram"):
        try:
            from app import telegram_bot

            token, _ = telegram_bot.bot_token()
            if token:
                available.append("telegram")
        except Exception:
            log.exception("notifications: the deployment's Telegram bot could not be resolved — reporting it unavailable")
    return [method for method in NOTIFICATION_METHOD_TYPES if method in available]


def _slack_destination(
    session: Session, user: AdminUser, *, event: str, muteable: bool = True
) -> tuple[Optional[tuple[str, str, str, str]], str]:
    """One candidate's `(member_id, bot_token, detail, team_id)` for this event, or `(None, reason)` naming what
    is missing. `team_id` is the tenant workspace checked (`SlackTarget.team_id`), `""` on the operator plane.

    Split out so the chain can try another candidate; the second element is meaningful only when the
    first is `None`. `detail` (`"full"`/`"brief"`) comes from the same `from_stored` read as the address.

    Order of refusals: address, then mute, then credential. Address first, so a user with no Slack cannot
    silence an alert by muting. Mute before credential, because `REASON_CREDENTIAL_MISSING` falls through
    to the shared webhook with the full text; a misconfiguration must never defeat a privacy choice.
    `muteable=False` skips the mute: a page (`alert_relay.PAGE_KINDS`) is not the recipient's to decline.
    """
    # The community edition has no Slack: a stored Slack method is skipped like an absent one.
    if not edition_features.enabled("slack"):
        return None, REASON_CHANNEL_NOT_SLACK
    # A membership still waiting on an invitation has no address here: nothing of this org reaches the
    # person before they accept (`AdminUser.invitation_id`).
    if user.invitation_id is not None:
        return None, REASON_RECIPIENT_MISSING
    # `from_stored` is the read-tolerant seam (one home).
    notifications = UserPreferences.from_stored(user.preferences).notifications
    method = notifications.method_for("slack")
    if method is None:
        return None, REASON_CHANNEL_NOT_SLACK
    if not method.user_id:
        # A Slack method with no member id has no address. Refused by the write path; reachable only for old
        # or hand-edited rows.
        return None, REASON_RECIPIENT_MISSING
    if not _SLACK_USER_ID_RE.match(method.user_id):
        # A malformed stored id would be posted as `channel`, fail, and (Slack failures do not fall through)
        # silence the recipient; refuse it as a fixable misconfiguration, not an absence.
        return None, REASON_RECIPIENT_MALFORMED

    if muteable and not notifications.wants(event):
        return None, REASON_EVENT_MUTED

    # The recipient's org decides the app (`slack_bot_token`); a tenant member must have proven their id in THAT
    # org's workspace, or the DM would go to whoever holds that member id in another workspace.
    from app import slack_workspaces
    from app.tenancy.ids import is_tenant_org

    if is_tenant_org(user.org_id):
        workspace = slack_workspaces.for_org(user.org_id)
        if workspace is None:
            return None, REASON_WORKSPACE_NOT_CONNECTED
        if method.team_id != workspace.team_id:
            return None, REASON_WORKSPACE_MISMATCH
        bot_token, team_id = workspace.bot_token, workspace.team_id
    else:
        designated, reason = _org_slack_bot_token()
        if designated is None:
            return None, reason
        bot_token, team_id = designated, ""

    return (method.user_id, bot_token, notifications.detail_for(event), team_id), REASON_RECIPIENT_MISSING


def page_targets(session: Session) -> list[SlackTarget]:
    """Every enabled superadmin's Slack DM for a page (`alert_relay.PAGE_KINDS`), oldest account first.

    The operators are who a CloudWatch alarm is for (`app/alarm_relay.py`), and a page has no task to
    walk a chain from. Unmuteable, always `"full"`: a page is never reduced to a headline. A superadmin with no
    linked Slack method is simply not a recipient; the env channel (`ALERT_WEBHOOK_URL`) is the caller's.
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
        resolved, _reason = _slack_destination(session, user, event=PAGE_KINDS[0], muteable=False)
        if resolved is not None:
            member_id, bot_token, _detail, team_id = resolved
            targets.append(
                SlackTarget(
                    admin_user_id=user.id, member_id=member_id, bot_token=bot_token, org_id=user.org_id, team_id=team_id
                )
            )
    return targets


def email_destination(user: AdminUser, *, event: str) -> tuple[Optional[tuple[str, str]], str]:
    """One candidate's `(address, detail)` for this event over email, or `(None, reason)`.

    The address is the login email, never a stored one (`preferences.NOTIFICATION_METHOD_TYPES`), and only
    once a Google, Microsoft or GitHub sign-in has proven it (`AdminUser.email_verified_at`, cleared on
    every change). Without that proof system mail would go to whatever address the row
    holds. Same refusal order as `_slack_destination`: the method, then the address, then the mute. The
    deployment's system mail is asked by the caller, once per alert rather than per candidate."""
    from app.mail_message import single_address

    # As `_slack_destination`: a membership waiting on an invitation is sent nothing.
    if user.invitation_id is not None:
        return None, REASON_RECIPIENT_MISSING
    notifications = UserPreferences.from_stored(user.preferences).notifications
    if notifications.method_for("email") is None:
        return None, REASON_CHANNEL_NOT_EMAIL
    address = single_address(user.email or "")
    if address is None:
        return None, REASON_RECIPIENT_MISSING
    if user.email_verified_at is None:
        return None, REASON_EMAIL_UNVERIFIED
    if not notifications.wants(event) or not notifications.emails(event):
        return None, REASON_EVENT_MUTED
    return (address, notifications.detail_for(event)), REASON_RECIPIENT_MISSING


def telegram_destination(user: AdminUser, *, event: str) -> tuple[Optional[tuple[int, str]], str]:
    """One candidate's `(chat_id, detail)` for this event over Telegram, or `(None, reason)`.

    The chat is the person's proven link (`telegram_links`, `app/telegram_link.py`), never a stored id; the
    membership's Telegram method says only that this org may send there. Same refusal order as
    `email_destination`. Read in its own session, since the link is the person's and callers hold an org-bound
    one or none. The bot is asked by the caller, once per alert."""
    # The community edition has no Telegram: a stored Telegram method is skipped like an absent one.
    if not edition_features.enabled("telegram"):
        return None, REASON_CHANNEL_NOT_TELEGRAM
    from app import telegram_link
    from app.db import SessionLocal

    if user.invitation_id is not None:
        return None, REASON_RECIPIENT_MISSING
    notifications = UserPreferences.from_stored(user.preferences).notifications
    if notifications.method_for("telegram") is None:
        return None, REASON_CHANNEL_NOT_TELEGRAM
    if user.identity_id is None:
        return None, REASON_TELEGRAM_NOT_LINKED
    with SessionLocal() as link_session:
        chat_id = telegram_link.linked_chat(link_session, user)
    if chat_id is None:
        return None, REASON_TELEGRAM_NOT_LINKED
    if not notifications.wants(event):
        return None, REASON_EVENT_MUTED
    return (chat_id, notifications.detail_for(event)), REASON_RECIPIENT_MISSING


def target_for_user(
    session: Session, user: AdminUser, *, event: str
) -> tuple[Optional[SlackTarget], str]:
    """This one person's Slack destination for `event`, or `(None, reason)` naming what is missing.

    The chain-free twin of `resolve_slack_target`: a mention names its recipient outright
    (`app/comment_notify.py`). The caller must not fall through on `None`, since the shared operator
    channel would broadcast one person's mention; an unresolved mention is not sent.
    """
    resolved, reason = _slack_destination(session, user, event=event)
    if resolved is None:
        return None, reason
    member_id, bot_token, detail, team_id = resolved
    return SlackTarget(
        admin_user_id=user.id, member_id=member_id, bot_token=bot_token, org_id=user.org_id, detail=detail,
        team_id=team_id,
    ), reason


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
    from app.task_external_comment import _console_task_url

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


def oldest_open_round(session: Session, task_id: int) -> Optional[TaskQuestion]:
    """The `task_questions` round `routes/tasks.answer_task` would resolve, or `None` when none is open.

    One home, read by `_question_for_task` and `slack_inbound` at click time. Ordering and Reset filter
    must match `routes/tasks._open_round_or_409`.
    """
    return session.scalars(
        # Same join and reason as `open_round`.
        select(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
        .order_by(TaskQuestion.created_at, TaskQuestion.id)
        .limit(1)
    ).first()


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
                # inside the DML (as `fleet_verdicts._persist_intents`), so it resolves on the consumer's
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

    The match is on a `methods` entry: removing the Slack method revokes the claim. jsonb `@>` on the
    array matches `type` and `user_id` within one element (an unnest could mix elements) and is
    GIN-indexable. A muted event does not revoke the claim; subscriptions are about being told.

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
    claimants = session.scalars(
        select(AdminUser)
        .where(
            AdminUser.preferences["notifications"]["methods"].contains(
                [{"type": "slack", "user_id": member_id}]
            )
        )
        .order_by(AdminUser.id)
    ).all()
    workspace = slack_workspaces.current()
    if workspace is None:
        return list(claimants)
    return [
        user for user in claimants
        if slack_workspaces.serves_user(workspace, user.org_id, _slack_team(user))
    ]


def _slack_team(user: AdminUser) -> str:
    """The workspace `user`'s Slack method was proven in (`NotificationMethod.team_id`), `""` when none."""
    method = UserPreferences.from_stored(user.preferences).notifications.method_for("slack")
    return "" if method is None else method.team_id


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
                SlackEvent.created_at < datetime.now(timezone.utc) - _EVENT_RETENTION
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        log.exception("could not prune expired slack_events rows — the table will keep growing")
