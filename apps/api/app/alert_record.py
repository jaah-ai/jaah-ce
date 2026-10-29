"""Record one alert into `alerts` and relay it to the operator channel — the DB half of the alert path.

Callers: `POST /api/agent/alerts` and `task_notify._deliver_task_transition`. This module owns the dedup
window and resolves the Slack recipient; `alert_relay` stays DB-free and only delivers. The email leg
(`app/email_notify.py`) runs beside Slack, best-effort. The row lands
before delivery and is not a delivery token: it is withdrawn for a failed relay, a `test`-flagged alert
or a dropped personal card, so a retry gets through; it stays when the relay is off or unconfigured.
Distinguish outcomes via `relayed`/`relay_failed`, the warning log and the health counters. The relay
runs after `commit()` and never propagates, so an alert never rolls back or precedes its mutation;
callers must pass a session with no uncommitted work.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import alert_relay, edition_features, email_notify, notify_targets
from app.durable import notify_outbox
from app.entity_shape import optional_uuid
from app.models.nodes import Node
from app.models.notifications import Alert
from app.models.tasks import Task
from app.node_identity import node_id_or_none

# The community edition has no Telegram: each read of this name sits behind the same check.
if edition_features.enabled("telegram"):
    from app import telegram_notify

log = logging.getLogger("app.alert_record")

# A tumbling 300s bucket: the unique key `uq_alerts_dedup` (columns on `models.Alert`) ends in the
# window start, and a unique key cannot express a sliding window.
ALERT_WINDOW_SECONDS = 300

#: `_warn_once` key prefix for the shipped-task and assignment-card drops, onto a `notify_targets` reason
#: key. A constant, never interpolated: `_warned` lives for the process, so the key set must stay finite.
_DONE_DROP_WARN_PREFIX = "done-drop:"


@dataclass(frozen=True)
class AlertOutcome:
    """Four independent facts, deliberately not collapsed: a single `delivered` flag once hid a dark
    alert path, and `deduped` read only as an absence meant two opposite things."""

    # A row survives for this dedup identity: this attempt owns the window. Not a delivery claim.
    delivered: bool
    # A channel accepted this alert. False with `relay_failed` false means a deliberate, logged
    # non-delivery (no channel, relay disabled, or `test`-flagged).
    relayed: bool
    # A channel was configured and delivery failed; the row is withdrawn, so a retry is not suppressed.
    # This is the field a client keys a retry on.
    relay_failed: bool
    # Lost the window to an earlier alert of the same identity. Split from deliberate non-delivery
    # because callers need opposite handling: a one-shot claim-holder keeps its claim on a non-delivery
    # (no retry fixes it) but hands it back on a dedup hit so the backstop sweep can still announce.
    deduped: bool
    window_start: datetime
    # The deliberate non-delivery was a fixable misconfigured recipient (never `REASON_RECIPIENT_MISSING`);
    # a claim-holder hands its claim back when set. Defaulted for test doubles (`getattr` in `task_notify`).
    recipient_misconfigured: bool = False
    # The deliberate non-delivery was the recipient's own mute of this event (`REASON_EVENT_MUTED`).
    muted: bool = False
    # A later question of a batch that reached nobody: its brief recipient was told by the first
    # (`alert_relay.brief_skips`), not a missing route.
    batch_follow_on: bool = False
    # The channels that accepted it, in send order (`slack`, `slack_channel`, `email`, `telegram`): what the task's Activity
    # names (`task_notify._note_delivery`). Empty for any non-delivery.
    channels: tuple[str, ...] = ()


def current_window_start(now: Optional[datetime] = None) -> datetime:
    """The tumbling bucket `now` falls in; single-homed so both producers align on one window."""
    now = now or datetime.now(timezone.utc)
    return datetime.fromtimestamp(
        (int(now.timestamp()) // ALERT_WINDOW_SECONDS) * ALERT_WINDOW_SECONDS, tz=timezone.utc
    )


def _identity_int(value: Any) -> int:
    """One of the two dedup-identity columns as a plain int, with `0` for "not scoped".

    Fails open to `0`: these columns only widen the identity, so an unreadable value degrades to a
    coarser alarm rather than losing the alert. A non-integer is an in-process producer bug; logged."""
    # `isinstance`, not `int()`: `int(12.9)` would silently name a different real task. `bool` is excluded
    # because `True` would read as task 1.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is not None:
        log.warning(
            "alert identity value %r is not an integer — recording it as 0 (unscoped), so this alert "
            "dedups on (repo, issue, kind, window) alone", value,
        )
    return 0


def _withdraw_undelivered_alert(session: Session, alert_id: int) -> None:
    """Remove the dedup row for an alert that was NOT delivered, so a retry inside the same 300s window
    is not suppressed. Best-effort: a failed DELETE is logged, never raised."""
    try:
        session.execute(delete(Alert).where(Alert.id == alert_id))
        session.commit()
    except Exception:
        session.rollback()
        log.exception(
            "could not withdraw the dedup row for undelivered alert id=%s — retries in this window "
            "will be suppressed until it expires",
            alert_id,
        )


def record_alert(
    session: Session,
    *,
    kind: str,
    node_id: Optional[str] = None,
    repo: Optional[str] = None,
    issue: Optional[str] = None,
    reason: Optional[str] = None,
    url: Optional[str] = None,
    test: bool = False,
    payload: Optional[dict[str, Any]] = None,
    task_id: Optional[int] = None,
    question_id: Optional[int] = None,
    tell_org_admins: bool = False,
) -> AlertOutcome:
    """Insert the dedup row, commit, then relay (order and row meaning: see the module docstring).

    `session` is committed here, so it must carry no uncommitted work; in-process callers should open a
    fresh session rather than reuse the mutation's. `kind` is assumed validated against
    `alert_relay.ALERT_KINDS`.

    `question_id` names the `task_questions` round this alert announces: it joins the dedup identity and
    binds the Slack card and correlation row. A parameter, not a `payload` key, because `payload` is
    writable by any `NODE_TOKEN` holder (cf. `AlertIn`'s `task_id` strip).

    `tell_org_admins` also sends the alert to the Owners and Admins of the alert's org (`_tell_org_admins`), on
    top of the operator channel. Set only by the in-process producers of `alert_relay.NODE_KINDS`: a parameter,
    never a `payload` key, so a node's own `POST /api/agent/alerts` never reaches a tenant's people; each producer
    bounds how often its node-reported facts can (`fleet_observe.emit_login_expiry_alerts`).

    Never raises for a delivery problem; faults are logged and reported through `relay_failed`.
    """
    payload = payload or {}
    window_start = current_window_start()
    # Hoisted out of `.values()` so the org walk below can reuse them.
    resolved_node = node_id_or_none(session, node_id)
    identity_task_id = _identity_int(task_id if task_id is not None else payload.get("task_id"))
    # The tenant, resolved here because this Core `pg_insert` bypasses the write seam's ORM stamping.
    # `alerts` is `Stored`, so: the task's org, else the node's, else the column is omitted and NOT NULL
    # refuses the insert; never a fallback org
    # (`test_tenant_create_surfaces.py::test_no_create_path_falls_back_to_a_well_known_org`). Scalar
    # subqueries in the one DML statement. `org_id` is not in `uq_alerts_dedup` (a separate open change).
    alert_org_terms: list[Any] = []
    if identity_task_id:
        alert_org_terms.append(
            select(Task.org_id).where(Task.id == identity_task_id).scalar_subquery()
        )
    if resolved_node is not None:
        alert_org_terms.append(
            select(Node.org_id).where(Node.id == resolved_node).scalar_subquery()
        )
    # Splatted so "no org" is the column's absence, letting NOT NULL speak; `org_id=None` would assert it.
    alert_org = {"org_id": func.coalesce(*alert_org_terms)} if alert_org_terms else {}
    result = session.execute(
        pg_insert(Alert)
        .values(
            **alert_org,
            # `node_id` arrives as a name and is resolved to the nullable uuid FK, failing open: an
            # unrecognised name stores NULL (already "not scoped to a node") so the alert still lands.
            # `uq_alerts_dedup` excludes `node_id`.
            node_id=resolved_node,
            repo=repo or "",
            issue=issue or "",
            kind=kind,
            # Coerced to the `0` sentinel (as `repo`/`issue` to `''`): NULLs are distinct in a unique key,
            # so a nullable column would stop producers that pass none from deduping.
            #
            # `task_id` falls back to `payload["task_id"]`. The parameter scopes the dedup identity only
            # (the backstop sweep's per-task alerts); answerability still reads `payload` alone.
            task_id=identity_task_id,
            question_id=_identity_int(question_id),
            window_start=window_start,
            reason=reason,
            # `url` is merged into `payload` (no column) unless the producer sent its own.
            payload=({"url": url, **payload} if url else payload),
        )
        .on_conflict_do_nothing(constraint="uq_alerts_dedup")
        .returning(Alert.id, Alert.org_id)
    ).first()
    session.commit()
    if result is None:
        # A dedup hit: already told in this window. Nothing relayed, nothing failed.
        return AlertOutcome(
            delivered=False, relayed=False, relay_failed=False, deduped=True,
            window_start=window_start,
        )

    # `task_id` is the dedup-scoping id, for `alert_relay.task_parts`' headline only; never the answerable
    # signal.
    event = alert_relay.message(kind, reason, repo=repo, issue=issue, url=url, test=test, task_id=task_id,
                                payload=payload)
    if tell_org_admins and not alert_relay.is_test_flagged(event):
        # Once per row that landed, whatever the operator relay does next: the producers of these kinds do not
        # retry, so tying it to the relay's outcome would let an operator Slack outage cost the tenant its alert.
        _tell_org_admins(session, result[1], event, kind)
    # The answerable rule, evaluated once and used for both the Slack reply line and the correlation row,
    # so the promise and the row cannot drift apart (the row also needs `receipt`, known only after posting).
    #
    # It keys on `payload["task_id"]`, the trust signal only an in-process producer can set (`AlertIn`
    # strips it), not on the `task_id` parameter, which any producer may set to scope dedup. Kept as a
    # separate name so the parameter is not shadowed.
    answerable_task_id = event["payload"].get("task_id")
    answerable = bool(answerable_task_id) and kind == "blocked"
    # The shipped-Task card's condition: the same trusted signal, so a `NODE_TOKEN` holder posting
    # `kind="done"` can never make the CP DM a "shipped" card for a Task it chose. Not folded into
    # `answerable`: `kind` selects one, and they drive opposite behaviour. The top-level-only rule is
    # `task_notify`'s and is not re-asked here.
    done_alert = kind == "done" and bool(answerable_task_id)
    # The assignment card's condition, `done_alert`'s twin: only `task_notify.announce_assignment` can
    # set `payload` `task_id` and `assigned_by_id` (both stripped at the HTTP boundary), so "by <name>" names
    # an authenticated actor. Coerced once; a malformed value is treated as unknown, never raised.
    assigned_alert = kind == "assigned" and bool(answerable_task_id)
    assigned_by_id: Optional[UUID] = None
    if assigned_alert:
        raw_assigned_by = event["payload"].get("assigned_by_id")
        assigned_by_id = optional_uuid(raw_assigned_by) if raw_assigned_by else None
    # The channels that accepted this alert, filled as each leg answers (`AlertOutcome.channels`).
    reached: list[str] = []
    # The email/Telegram legs whose person is brief and was told by the batch's first question
    # (`alert_relay.brief_skips`): a deliberate non-delivery, never a reason to fall through to the webhook.
    brief_skipped: list[str] = []

    def _email() -> bool:
        # The email leg (`app/email_notify.py`): best-effort, never raising. Called only on a path that ends
        # this attempt without `relay_failed` — a failed Slack post withdraws the row and the caller
        # retries, and emailing before that would send one email per attempt.
        # The Telegram leg (`app/telegram_notify.py`) rides the same call, on the same rule: either reaching
        # a person is the delivery.
        emailed = email_notify.deliver_alert(
            session, event, task_id=answerable_task_id, answerable=answerable, question_id=question_id,
            done=done_alert, assigned=assigned_alert, skipped=brief_skipped,
        )
        sent = edition_features.enabled("telegram") and telegram_notify.deliver_alert(
            session, event, task_id=answerable_task_id, answerable=answerable, question_id=question_id,
            done=done_alert, assigned=assigned_alert, skipped=brief_skipped,
        )
        reached.extend(channel for channel, ok in (("email", emailed), ("telegram", sent)) if ok)
        return emailed or sent

    try:
        # The Slack recipient, resolved here on this session and handed to the relay as a plain value (the
        # relay cannot import `models`). Inside the `try` so a lookup DB fault is handled like any relay
        # fault (withdraw, `relay_failed`, never raise). Named `fallthrough_reason` because `reason` is this
        # function's own parameter.
        target, fallthrough_reason = notify_targets.resolve_slack_target(
            session, answerable_task_id, kind=kind, answerable=answerable, question_id=question_id,
            done=done_alert, assigned=assigned_alert, assigned_by_id=assigned_by_id,
        )
        if fallthrough_reason == notify_targets.REASON_EVENT_MUTED:
            # The recipient muted this event: dropped, never fallen through to the shared channel (that
            # would turn a mute into a redirect). No warning (nothing to fix), but one `log.info` so the
            # suppression is auditable; `PERSONAL_EVENTS` bounds what a mute can suppress.
            #
            # The row is withdrawn: nothing was delivered, and the dedup identity carries no recipient,
            # so a held row would suppress the same alert for a different recipient.
            log.info(
                "alert: kind=%s repo=%s issue=%s dropped — the resolved recipient has muted this event",
                kind, event.get("repo"), event.get("issue"),
            )
            _withdraw_undelivered_alert(session, result[0])
            return AlertOutcome(
                delivered=False, relayed=False, relay_failed=False, deduped=False,
                window_start=window_start, muted=True,
            )
        if (done_alert or assigned_alert) and target is None and _email():
            # Slack has no way to reach the addressee, but email did: the card is delivered, the row stays.
            return AlertOutcome(
                delivered=True, relayed=True, relay_failed=False, deduped=False,
                window_start=window_start, channels=tuple(reached),
            )
        if (done_alert or assigned_alert) and target is None:
            # A shipped-Task or assignment card with no recipient is dropped, never fallen through to the
            # shared channel. `REASON_RECIPIENT_MISSING` (no human named) stays silent; other reasons warn and
            # set `AlertOutcome.recipient_misconfigured` so the caller releases its claim. The row is
            # withdrawn; neither `relayed` nor `relay_failed`.
            misconfigured = (
                fallthrough_reason is not None
                and fallthrough_reason != notify_targets.REASON_RECIPIENT_MISSING
            )
            if misconfigured:
                # Its own key namespace: the fall-through arm warns under the bare reason, and one
                # `_warn_once` set would let whichever fired first swallow the other's wording.
                alert_relay._warn_once(
                    f"{_DONE_DROP_WARN_PREFIX}{fallthrough_reason}",
                    "shipped-task or assignment card dropped: its addressee's Slack recipient is unusable "
                    "(%s), and these alert kinds never fall through to the operator channel — fix that "
                    "recipient's notification preferences or its credential and the next shipment or "
                    "assignment will reach them",
                    fallthrough_reason,
                )
            _withdraw_undelivered_alert(session, result[0])
            return AlertOutcome(
                delivered=False, relayed=False, relay_failed=False, deduped=False,
                window_start=window_start, recipient_misconfigured=misconfigured,
            )
        # Reasons that are the recipient's own choice or absence, not a deployment fault: nothing to fix.
        recipient_only = fallthrough_reason in (
            None, notify_targets.REASON_RECIPIENT_MISSING, notify_targets.REASON_RECIPIENT_MALFORMED,
            notify_targets.REASON_CHANNEL_NOT_SLACK,
        )
        if target is None and (_email() or brief_skipped):
            # No Slack recipient, but the alert reached a person by email (or a brief one was already told by
            # the batch's first question): that is the delivery, so it does not fall through to the shared
            # env-var channel. A deployment-level Slack fault still warns,
            # or an operator would lose the only sign that Slack is broken for everyone. The row stays.
            if not recipient_only:
                alert_relay._warn_once(
                    fallthrough_reason,
                    "slack alert recipient unusable (%s) — delivered by email or Telegram instead; fix the "
                    "deployment's Slack credential",
                    fallthrough_reason,
                )
            return AlertOutcome(
                delivered=True, relayed=bool(reached), relay_failed=False, deduped=False,
                window_start=window_start, channels=tuple(reached), batch_follow_on=not reached,
            )
        if fallthrough_reason is not None and not (
            fallthrough_reason == notify_targets.REASON_RECIPIENT_MISSING and not answerable_task_id
        ):
            # A task-less alert names nobody by design (no last-resort human), so it falls through
            # without a warning about a recipient that does not exist.
            # A misconfigured recipient falls through to the env-var channel but is never a silent drop.
            # The key is from the fixed `notify_targets.REASON_KEYS`; nothing alert-specific is interpolated.
            alert_relay._warn_once(
                fallthrough_reason,
                "slack alert recipient unusable (%s) — falling through to the env-var operator channel; "
                "fix the pinned recipient's notification preferences or its credential",
                fallthrough_reason,
            )
        # The out-parameter the correlation row is written from: `relay_alert` fills both keys or neither,
        # only after Slack answered `ok:true`.
        # The promise and row follow the card: `answerable` was computed before the round resolved, so a
        # named round that closed meanwhile gets no card, and dropping `answerable` retracts both.
        if question_id is not None and (target is None or target.question is None):
            # Named apart because diagnosed apart: no recipient configured vs the round closed.
            log.info(
                "alert: %s — sending this alert as plain text with no reply promise and no correlation row",
                "no Slack recipient resolved, so question round %s renders no card" % question_id
                if target is None
                else "the named question round %s no longer resolves a card" % question_id,
            )
            answerable = False
            if target is not None:
                target = replace(target, answerable=False, question=None)
        receipt: dict[str, str] = {}
        if answerable or notify_outbox.in_workflow():
            # Inline: the reply thread's correlation row needs the receipt, and inside a workflow (the
            # `task-notify` queue, where every question arrives) the send is already durable.
            relayed = alert_relay.relay_alert(event, target=target, receipt=receipt)
        else:
            # Handed off (`durable/notify_outbox.py`): no request or cron waits on Slack. The row stands once
            # the send is enqueued; a refusal there is retried, then the worker withdraws this row.
            relayed = notify_outbox.slack(event, target, withdraw_alert_id=result[0])
        if relayed:
            # With no addressee it fell through to the shared operator channel, which reached nobody in particular.
            reached.append("slack" if target is not None else "slack_channel")
        if target is not None:
            # After Slack accepted it (or deliberately did not): the attempt is final. With no Slack
            # target the email leg already ran above.
            relayed = _email() or relayed
    except alert_relay.AlertRelayError as exc:
        _withdraw_undelivered_alert(session, result[0])
        log.warning(
            "alert relay failed for kind=%s repo=%s issue=%s (%s) — dedup row withdrawn so a retry in "
            "this window is not suppressed",
            kind, repo, issue, exc,
        )
        # `delivered=False` because the row is gone: this attempt owns no window.
        return AlertOutcome(
            delivered=False, relayed=False, relay_failed=True, deduped=False,
            window_start=window_start,
        )
    except Exception:
        # An unexpected relay fault (our bug, not an outage): not propagated, but logged with a traceback.
        # Delivery is unproved, so the row is withdrawn.
        _withdraw_undelivered_alert(session, result[0])
        log.exception(
            "alert relay raised unexpectedly for kind=%s repo=%s issue=%s — dedup row withdrawn",
            kind, repo, issue,
        )
        return AlertOutcome(
            delivered=False, relayed=False, relay_failed=True, deduped=False,
            window_start=window_start,
        )
    if alert_relay.is_test_flagged(event):
        # A synthetic alert must not hold the dedup window. Asked of the relay so the two `test` shapes stay
        # single-homed in `alert_relay.is_test_flagged`.
        _withdraw_undelivered_alert(session, result[0])
        return AlertOutcome(
            delivered=False, relayed=False, relay_failed=False, deduped=False,
            window_start=window_start,
        )

    if receipt and answerable:
        # The correlation row letting a reply in this Slack thread answer this Task, keyed on the
        # `chat.postMessage` response's `D…` channel, never the `U…` member id. Written only on a non-empty
        # `receipt` and `answerable` (trusted `task_id`, `kind == "blocked"`). Best-effort: raising after a
        # delivered alert would withdraw the row for a message the operator is reading.
        bound_round = target.question.question_id if target is not None and target.question else None
        notify_targets.record_thread(
            session,
            task_id=answerable_task_id,
            # Bind the round the card rendered (off the resolved target), not the `question_id` parameter:
            # the transition leg names no round yet shows the oldest open one, and NULL would let a later
            # reply fall back to a different round. Still `None` for the `/block` path with no open round.
            # Never re-derived here: another round may have opened since the post.
            question_id=bound_round,
            admin_user_id=target.admin_user_id if target is not None else None,
            # A round-bound brief line speaks for its batch; a roundless one settles as any roundless card.
            brief=bound_round is not None and target is not None and target.detail == "brief",
            channel_id=receipt["channel"],
            thread_ts=receipt["ts"],
        )
    return AlertOutcome(
        delivered=True, relayed=relayed, relay_failed=False, deduped=False,
        window_start=window_start, channels=tuple(reached),
        batch_follow_on=not reached and alert_relay.brief_skips(event, "brief"),
    )


def _tell_org_admins(session: Session, org_id: Optional[UUID], event: dict[str, Any], kind: str) -> None:
    """Send `event` to each Owner and Admin of `org_id` (the alert row's) over every channel they turned on for
    `kind` (the mention DM's legs, `comment_notify.notify_mentioned`). Best-effort, never raises: the alert is
    already recorded. Reads only; leaves no work on `session`."""
    from app import access

    if org_id is None:
        return
    try:
        people = access.owners_and_admins(session, org_id)
    except Exception:
        session.rollback()
        log.exception("alert: could not read the Owners and Admins for kind=%s — not sent to them", kind)
        return
    for user in people:
        try:
            email_notify.deliver_to_user(user, event, event_name=kind)
            if edition_features.enabled("telegram"):
                telegram_notify.deliver_to_user(user, event, event_name=kind)
            target, _ = notify_targets.target_for_user(session, user, event=kind)
            if target is not None:
                notify_outbox.slack(event, target)
        except Exception:
            log.exception("alert: kind=%s not delivered to an org Owner or Admin (%s)", kind, user.id)
