"""The notification outbox: every outbound notification send — Slack, email, Telegram — as durable DBOS work.

The senders resolve WHO and WHAT synchronously (cheap reads, where the caller's rows are loaded) and hand the
network send here, so no request, listener or cron waits on Slack, SES or Telegram, and a send survives a
redeploy. One leaf per channel, each the one place its channel's network call is made for a notification:

    slack(event, target)            → `alert_relay.relay_alert`         (`deliver_slack`)
    email(address, …)               → `system_mail.send` (SES), then once `system_mail.fallback_send` (the
                                      operator org's Gmail) if SES answered a final no (`deliver_system_email`)
    telegram(chat_id, text, what)   → `telegram_notify._send`           (`deliver_telegram`)
    forward(kind, *args)            → a named send that records its own outcome — a lead's Slack/HubSpot
                                      forward, the access-request cards (`deliver_forward`, `FORWARDS`)
    after_answer(kind, *args)       → a door's post-answer work, sealed — the sign-up and password-reset
                                      emails, the operators' notices (`deliver_after_answer`, `AFTER_ANSWER`)

Already inside a workflow (the `task-notify` queue's transition alerts, the support queue), a leaf sends
inline: that work is already asynchronous and durable, and `alert_record` needs the Slack receipt back.

NO SECRET IS EVER AN ARGUMENT: workflow inputs are persisted. A Slack target travels with its bot token blanked
and the workflow re-reads the designated credential (`notify_targets._org_slack_bot_token`); the SES settings
and the Telegram bot token are re-read the same way. An email's body travels SEALED (`credential_crypto.seal` under
`_EMAIL_AAD`, bound to its delivery id): it can carry a password-reset link or a sign-up code; its address stays on
its `email_deliveries` row (`app/email_deliveries.py`), never a workflow input. Each send is one `@DBOS.step`, so a recovered workflow does
not send twice once the step recorded its outcome. Ids are random: each call is one send, never deduplicated.

A leaf answers True when the send was handed off (or, inline, when it went out); False when the engine refused
the enqueue — logged, never raised: the caller's write has committed and a notification must not fail it.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import uuid
from typing import Any, Optional, Sequence

from dbos import DBOS

from app import edition_features
from app.durable import runtime
from app.durable.runtime import enqueue_or_warn
from app.email_unsubscribe import UnsubscribeTarget

log = logging.getLogger("notify-outbox")

#: One queue per channel, so a burst on one (a billing fan-out by email) never queues another's. Each takes up to
#: two sends a second: under Slack's per-channel and Telegram's per-chat limits for any one recipient, and above
#: any human fan-out. Polled as work a person waits on.
_CHANNEL_QUEUE: dict[str, Any] = {
    "concurrency": 4, "limiter": {"limit": 120, "period": 60.0}, "polling_interval_sec": runtime.INTERACTIVE_POLL_SEC,
}
SLACK_QUEUE = runtime.declare_queue("notify-slack", **_CHANNEL_QUEUE)
#: Post-answer work.
EMAIL_QUEUE = runtime.declare_queue("notify-email", **_CHANNEL_QUEUE)
#: System mail (`deliver_system_email`): its own queue, which a still-running release from before SES never polls, so
#: during a deploy's overlap no old task dequeues a send it cannot read.
SYSTEM_EMAIL_QUEUE = runtime.declare_queue("notify-system-email", **_CHANNEL_QUEUE)
TELEGRAM_QUEUE = runtime.declare_queue("notify-telegram", **_CHANNEL_QUEUE)
#: Lead forwards and access-request cards: a vendor round trip each (HubSpot, several Slack posts).
FORWARD_QUEUE = runtime.declare_queue("notify-forward", **runtime.VENDOR_QUEUE)

#: A send's retry ladder: a refusal or an unreachable vendor is tried three times, 2s then 4s apart, before it is
#: logged as failed.
_SEND_RETRY: dict[str, Any] = {**runtime.DB_RETRY, "backoff_rate": 2.0}
#: A failed email's reason when SES's own words are not to hand.
_SES_REFUSED = "SES refused the message"


def in_workflow() -> bool:
    """Whether this call already runs inside a durable workflow (then a leaf sends inline)."""
    try:
        return DBOS.workflow_id is not None
    except Exception:  # noqa: BLE001 — no DBOS context at all reads as "not in a workflow"
        return False


def _enqueue(queue: str, func: Any, *args: Any, what: str) -> bool:
    return enqueue_or_warn(
        queue, f"{queue}:{uuid.uuid4()}", func, *args, log=log,
        failure=f"notify-outbox: could not enqueue {what}", consequence="it is not sent",
    )


# ── Slack ───────────────────────────────────────────────────────────────────────────────────────────


def slack(event: dict[str, Any], target: Any = None, *, withdraw_alert_id: Optional[int] = None) -> bool:
    """Send one alert through `alert_relay.relay_alert` (a resolved `notify_targets.SlackTarget`, or `None` for the
    operator channel). Inline in a workflow, where it may raise `AlertRelayError` as `relay_alert` does; otherwise
    handed to `deliver_slack` with the target's token blanked. `withdraw_alert_id`: the `alerts` dedup row the
    worker deletes if the send finally fails, so a retry in its window is not suppressed (`alert_record`)."""
    from app import alert_relay

    if in_workflow():
        return alert_relay.relay_alert(event, target=target)
    wire = dataclasses.replace(target, bot_token="") if dataclasses.is_dataclass(target) else target
    return _enqueue(
        SLACK_QUEUE, deliver_slack, event, wire, withdraw_alert_id, what=f"a Slack {event.get('kind') or 'alert'}"
    )


@DBOS.workflow()
def deliver_slack(event: dict[str, Any], target: Any, withdraw_alert_id: Optional[int] = None) -> str:
    """One Slack send. Returns `sent`, `not_relayed` (the relay is off, a plan drop, no channel), `no_token` (the
    designated credential is gone since the enqueue), `workspace_mismatch` (the org's workspace is no longer the one
    the target was checked against) or `failed` (Slack refused every try; logged, and the alert's
    dedup row withdrawn)."""
    from app import alert_relay

    try:
        return _slack_step(event, target)
    except Exception as exc:  # noqa: BLE001 — the step's tries are spent; this is the final outcome
        reason = str(exc) if isinstance(exc, alert_relay.AlertRelayError) else type(exc).__name__
        log.warning("notify-outbox: Slack refused a %s (%s)", event.get("kind"), reason)
        if withdraw_alert_id is not None:
            _withdraw_alert(withdraw_alert_id)
        return "failed"


@DBOS.step(**_SEND_RETRY)
def _slack_step(event: dict[str, Any], target: Any) -> str:
    from app import alert_relay, notify_targets

    if dataclasses.is_dataclass(target):
        # The workspace checked at enqueue (`SlackTarget.team_id`) must still be the org's: a reconnect in between
        # would hand the member id to a workspace it was never proven in.
        token, reason = notify_targets.slack_bot_token(
            getattr(target, "org_id", None), team_id=getattr(target, "team_id", "")
        )
        if token is None:
            log.info("notify-outbox: Slack %s not sent — %s", event.get("kind"), reason)
            return "workspace_mismatch" if reason == notify_targets.REASON_WORKSPACE_MISMATCH else "no_token"
        target = dataclasses.replace(target, bot_token=token)
    # A refusal raises `AlertRelayError`, which the step's ladder retries.
    return "sent" if alert_relay.relay_alert(event, target=target) else "not_relayed"


def _withdraw_alert(alert_id: int) -> None:
    """Delete an undelivered alert's dedup row (`alert_record._withdraw_undelivered_alert`). Operator scope: the id
    is the one `alert_record` wrote, whatever its org (a node's alert may carry none)."""
    from app import alert_record
    from app.tenancy import seam

    caller = "durable:notify_outbox.withdraw_alert"
    with seam.detached(), seam.operator_scope(), runtime.step_session(caller) as session:
        alert_record._withdraw_undelivered_alert(session, alert_id)


# ── Email ──────────────────────────────────────────────────────────────────────────────────────────────


#: The domain separator an outbox email is sealed under, bound to its delivery id: its ciphertext opens nowhere else.
_EMAIL_AAD = b"jaah-notify-outbox-email-v2"


def _email_aad(delivery_id: str) -> bytes:
    return _EMAIL_AAD + b":" + delivery_id.encode()


@DBOS.step()
def _queue_delivery(
    address: str, subject: str, mail_type: str, essential: bool, recipient_user_id: Optional[str]
) -> str:
    """The `queued` delivery-log row, as a step so a recovered workflow does not log one send twice."""
    from app import email_deliveries

    return email_deliveries.create(
        recipient=address, subject=subject, mail_type=mail_type, essential=essential,
        recipient_user_id=uuid.UUID(recipient_user_id) if recipient_user_id else None,
    ).hex


def email(
    address: str,
    subject: str,
    text: str,
    html: Optional[str],
    *,
    what: str,
    mail_type: str,
    essential: bool,
    unsubscribe: Optional[UnsubscribeTarget] = None,
    recipient_user_id: Optional[uuid.UUID] = None,
    secrets: Sequence[str] = (),
) -> bool:
    """Send one already-rendered system email (`app/system_mail.py`, over SES) to one address.

    `mail_type` is the `app/email_templates.py` type it was rendered from and `essential` PRD R6's flag; both go on
    the `email_deliveries` row written `queued` here, before anything else, so no outcome is silent. `unsubscribe`
    names whose preference a one-click unsubscribe turns off: an optional email gets RFC 8058's headers for it
    (`app/email_unsubscribe.py`; the caller rendered the same link into the footer); `recipient_user_id` (else the
    target's user) is recorded on the row; `secrets` are values (a code, a tokened link) masked in the row's subject —
    the real subject travels sealed with the body. Inline in a workflow (True when it went out); otherwise the body is sealed
    and handed to `deliver_system_email` with the row's id. Never raises."""
    from app import credential_crypto, email_deliveries, email_unsubscribe

    user_id = recipient_user_id or (unsubscribe.user_id if unsubscribe is not None else None)
    logged_subject = email_deliveries.redacted(subject, secrets)
    try:
        delivery_id = _queue_delivery(
            address, logged_subject, mail_type, essential, str(user_id) if user_id else None
        )
    except Exception:  # noqa: BLE001 — a DB fault must not fail the caller's committed write
        log.exception("notify-outbox: the %s email could not be logged — not sent", what)
        return False
    try:
        headers = {} if essential else email_unsubscribe.headers(email_unsubscribe.url_for(unsubscribe))
    except Exception:  # noqa: BLE001 — a link that cannot be built sends the email without it, never fails it
        log.exception("notify-outbox: the %s email's unsubscribe link could not be built — sent without", what)
        headers = {}
    if in_workflow():
        return _email_step(delivery_id, subject, text, html, what, headers) == "sent"
    try:
        sealed = credential_crypto.seal(
            {"subject": subject, "text": text, "html": html, "headers": headers}, aad=_email_aad(delivery_id)
        )
    except Exception as exc:  # noqa: BLE001 — a missing key must not fail the caller's committed write
        log.warning("notify-outbox: the %s email could not be sealed (%s) — not sent", what, type(exc).__name__)
        email_deliveries.record(uuid.UUID(delivery_id), "failed", detail="the message could not be sealed for the queue")
        return False
    if not _enqueue(SYSTEM_EMAIL_QUEUE, deliver_system_email, delivery_id, sealed, what, what=f"the {what} email"):
        email_deliveries.record(uuid.UUID(delivery_id), "failed", detail="the send could not be queued")
        return False
    return True


@DBOS.workflow()
def deliver_system_email(delivery_id: str, sealed: dict[str, Any], what: str) -> str:
    """One sealed email. Returns `sent`, `not_configured` (SES sending was switched off since), `suppressed` or
    `failed`."""
    from app import credential_crypto, email_deliveries

    try:
        mail = credential_crypto.unseal(sealed, aad=_email_aad(delivery_id), label="a notify-outbox email")
    except ValueError as exc:
        log.warning("notify-outbox: the %s email could not be opened (%s) — not sent", what, exc)
        email_deliveries.record(uuid.UUID(delivery_id), "failed", detail="the queued message could not be opened")
        return "failed"
    # `headers` is absent from a message the release before unsubscribe sealed.
    return _email_step(delivery_id, mail["subject"], mail["text"], mail["html"], what, mail.get("headers") or {})


class _RetrySend(Exception):
    """A try SES may accept later (unreachable, a 5xx, throttled): raised so the step's ladder tries again. Its text
    is the status and code only — DBOS records it — while `detail`, SES's own words, goes to the row alone. `status`
    is SES's HTTP status, None when SES never answered (unreachable, a timeout): it may have accepted the message."""

    def __init__(self, detail: str, *, status: Optional[int] = None, code: Optional[str] = None) -> None:
        super().__init__(f"SES status {status}, {code or 'no code'}")
        self.detail = detail
        self.status = status


def _queued_row_or_outcome(row_id: uuid.UUID, what: str) -> tuple[Any, str]:
    """The still-`queued` delivery row a send step may act on, or `(None, outcome)` when there is nothing left to do:
    the row is gone (`failed`), or a recovered run finds its outcome already on the row (it committed, the step's
    checkpoint did not) — never twice. Plain Python inside `_email_attempt` and `_email_fallback`, not a step."""
    from app import email_deliveries

    row = email_deliveries.load(row_id)
    if row is None:
        log.warning("notify-outbox: email %s not sent — its delivery row is gone", what)
        return None, "failed"
    if row.provider_message_id:
        return None, "sent"
    if row.status != "queued":
        return None, row.status if row.status in email_deliveries.FINAL_STATUSES else "sent"
    return row, ""


@DBOS.step(**_SEND_RETRY)
def _email_attempt(
    delivery_id: str, subject: str, text: str, html: Optional[str], what: str,
    headers: Optional[dict[str, str]] = None,
) -> str:
    """One try at the send, its outcome on the row. A retryable refusal raises (and is retried); not configured,
    a suppressed recipient (no SES call), or a row already swept is final. A final refusal (a 4xx) answers
    `refused` with SES's words on the still-`queued` row, for `_email_fallback`."""
    from app import email_deliveries, mail_message, system_mail

    row_id = uuid.UUID(delivery_id)
    row, settled = _queued_row_or_outcome(row_id, what)
    if row is None:
        return settled
    # PRD R4: a suppressed address is never sent to, and the row says so.
    if email_deliveries.suppressed(row.recipient):
        log.warning("notify-outbox: email %s not sent — its recipient is suppressed", what)
        email_deliveries.record(row_id, "suppressed", detail=email_deliveries.SUPPRESSED_DETAIL)
        return "suppressed"
    email_deliveries.attempted(row_id)
    message = mail_message.OutboundEmail(
        to=row.recipient, subject=subject, text=text, html=html, mail_type=row.mail_type, essential=row.essential,
        headers={} if row.essential else dict(headers or {}),
    )
    try:
        message_id = system_mail.send(message, delivery_id=row_id)
    except system_mail.NotConfigured as exc:
        log.warning("notify-outbox: email %s not sent — system mail is not configured", what)
        email_deliveries.record(row_id, "not_configured", detail=str(exc))
        return "not_configured"
    except system_mail.SendError as exc:
        if exc.retryable:
            raise _RetrySend(str(exc), status=exc.status, code=exc.code) from None
        # The status and code only in the log: the row carries the provider's words for the operator. Kept there,
        # not in the step's (persisted) result, for a recovered `_email_fallback` to read.
        log.warning("notify-outbox: email %s refused (status %s, %s)", what, exc.status, exc.code)
        email_deliveries.record(row_id, "queued", detail=str(exc))
        return "refused"
    email_deliveries.record(row_id, "sent", provider_message_id=message_id)
    return "sent"


def _email_step(
    delivery_id: str, subject: str, text: str, html: Optional[str], what: str,
    headers: Optional[dict[str, str]] = None,
) -> str:
    """The send with its retries, and the final outcome: `sent`, `not_configured`, `suppressed` or `failed`; never
    raises (`email`'s contract). SES's final refusal, or a ladder spent on answers SES gave (a 5xx, throttled), tries
    `_email_fallback` once. A ladder whose last try had no answer (unreachable, a timeout) never falls back: SES may
    already have accepted it. `headers`: an optional email's unsubscribe headers (`email`)."""
    from app import email_deliveries

    try:
        outcome = _email_attempt(delivery_id, subject, text, html, what, headers)
    except Exception as exc:  # noqa: BLE001 — the step's tries are spent
        # Spent, DBOS raises its own `DBOSMaxStepRetriesExceeded`, carrying each try's exception on `.errors`.
        errors = getattr(exc, "errors", None)
        tries = errors if isinstance(errors, list) and errors else [exc]
        last = tries[-1]
        detail = last.detail if isinstance(last, _RetrySend) else f"the send raised {type(last).__name__}"
        log.warning("notify-outbox: email %s not sent after its retries (%s)", what, type(last).__name__)
        # Any try SES never answered (a timeout) may have been accepted: Gmail must not send a second copy.
        if not all(isinstance(t, _RetrySend) and t.status is not None for t in tries):
            email_deliveries.record(uuid.UUID(delivery_id), "failed", detail=f"every try failed: {detail}")
            return "failed"
        return _fallback_or_failed(delivery_id, subject, text, html, what, headers, f"every try failed: {detail}")
    if outcome == "refused":
        return _fallback_or_failed(delivery_id, subject, text, html, what, headers)
    return outcome


def _fallback_or_failed(
    delivery_id: str, subject: str, text: str, html: Optional[str], what: str,
    headers: Optional[dict[str, str]], ses_detail: Optional[str] = None,
) -> str:
    """`_email_fallback`, never raising: a fault in it fails the row with SES's reason (`ses_detail`, else the one on
    the row) and answers `failed`."""
    from app import email_deliveries

    try:
        return _email_fallback(delivery_id, subject, text, html, what, headers, ses_detail)
    except Exception as exc:  # noqa: BLE001 — `email` never raises
        log.warning("notify-outbox: the Gmail fallback for email %s raised %s", what, type(exc).__name__)
        detail = ses_detail
        if detail is None:
            with contextlib.suppress(Exception):
                row = email_deliveries.load(uuid.UUID(delivery_id))
                detail = row.status_detail if row is not None else None
            if detail and detail.startswith(email_deliveries.FALLBACK_PENDING):
                detail = detail[len(email_deliveries.FALLBACK_PENDING):]
        email_deliveries.record(uuid.UUID(delivery_id), "failed", detail=detail or _SES_REFUSED)
        return "failed"


@DBOS.step()
def _email_fallback(
    delivery_id: str, subject: str, text: str, html: Optional[str], what: str,
    headers: Optional[dict[str, str]] = None, ses_detail: Optional[str] = None,
) -> str:
    """One try from the operator org's Gmail (`system_mail.fallback_send`) once SES refused for good, never retried:
    `sent`, `suppressed` or `failed`. `ses_detail` is SES's reason (None: the one `_email_attempt` left on the row).
    With no Gmail connection the row fails with SES's words alone, as it would without a fallback.

    At most once: the row is claimed first (`email_deliveries.claim_fallback`, which raises on a DB fault), and a
    re-run that finds the claim's marker still on a `queued` row cannot know whether Gmail sent, so it fails the row
    ("Gmail fallback outcome unknown") and never sends again."""
    from app import email_deliveries, mail_message, system_mail

    row_id = uuid.UUID(delivery_id)
    row, settled = _queued_row_or_outcome(row_id, what)
    if row is None:
        return settled
    pending = email_deliveries.FALLBACK_PENDING
    if row.status_detail and row.status_detail.startswith(pending):
        log.warning("notify-outbox: email %s — a Gmail fallback was already tried, its outcome unknown", what)
        email_deliveries.record(
            row_id, "failed", detail=f"{row.status_detail[len(pending):]}; Gmail fallback outcome unknown"
        )
        return "failed"
    # PRD R4, again: an address suppressed since SES's try is not sent to from Gmail either.
    if email_deliveries.suppressed(row.recipient):
        log.warning("notify-outbox: email %s not sent — its recipient is suppressed", what)
        email_deliveries.record(row_id, "suppressed", detail=email_deliveries.SUPPRESSED_DETAIL)
        return "suppressed"
    detail = ses_detail or row.status_detail or _SES_REFUSED
    if not email_deliveries.claim_fallback(row_id, row.status_detail, detail):
        log.warning("notify-outbox: email %s — its delivery row moved before the Gmail fallback; not sent", what)
        return "failed"
    message = mail_message.OutboundEmail(
        to=row.recipient, subject=subject, text=text, html=html, mail_type=row.mail_type, essential=row.essential,
        headers={} if row.essential else dict(headers or {}),
    )
    try:
        gmail_id, sender = system_mail.fallback_send(message)
    except system_mail.NoFallback:
        email_deliveries.record(row_id, "failed", detail=detail)
        return "failed"
    except system_mail.SendError as exc:
        log.warning("notify-outbox: email %s refused by the Gmail fallback too (status %s)", what, exc.status)
        email_deliveries.record(row_id, "failed", detail=f"{detail}; Gmail fallback: {exc}")
        return "failed"
    except Exception as exc:  # noqa: BLE001 — a fault in the fallback leaves SES's verdict standing
        log.warning("notify-outbox: the Gmail fallback for email %s raised %s", what, type(exc).__name__)
        email_deliveries.record(row_id, "failed", detail=f"{detail}; Gmail fallback raised {type(exc).__name__}")
        return "failed"
    log.info("notify-outbox: email %s sent through the Gmail fallback after SES refused it", what)
    email_deliveries.record(
        row_id, "sent", provider_message_id=f"gmail:{gmail_id}",
        detail=f"Sent from {sender} through Gmail after SES refused: {detail}",
    )
    return "sent"


# ── Telegram ────────────────────────────────────────────────────────────────────────────────────────


def telegram(chat_id: int, text: str, what: str) -> bool:
    """Send one plain-text message to a linked chat as the deployment's bot. Inline in a workflow (True when it
    went out); otherwise handed to `deliver_telegram`. Never raises."""
    if in_workflow():
        return _telegram_step(chat_id, text, what) == "sent"
    return _enqueue(TELEGRAM_QUEUE, deliver_telegram, chat_id, text, what, what=f"the Telegram {what}")


@DBOS.workflow()
def deliver_telegram(chat_id: int, text: str, what: str) -> str:
    """One Telegram message. Returns `sent`, `no_bot` (the bot was cleared since) or `failed`."""
    return _telegram_step(chat_id, text, what)


@DBOS.step()
def _telegram_step(chat_id: int, text: str, what: str) -> str:
    # The community edition has no Telegram: no bot to send as.
    if not edition_features.enabled("telegram"):
        return "no_bot"
    from app import telegram_bot, telegram_notify

    try:
        token, _ = telegram_bot.bot_token()
    except Exception:
        log.exception("notify-outbox: the Telegram bot could not be resolved — %s not sent", what)
        return "failed"
    if token is None:
        return "no_bot"
    return "sent" if telegram_notify._send(token, chat_id, text, what) else "failed"


# ── Named sends: lead forwards, access-request cards ─────────────────────────────────────────────


#: The sends a caller may hand off by name: each makes its own network calls and writes its own outcome onto
#: its row (the sales channel and HubSpot for a lead; each approver's card for an access request), so the whole
#: send is the durable unit. A name, not a callable, crosses the queue: nothing outside this list is enqueued.
FORWARDS: dict[str, tuple[str, str]] = {
    "site_enquiry": ("app.site_enquiry", "forward_stored"),
    "signup": ("app.signup_notify", "forward"),
    "trust_request": ("app.trust_requests", "forward"),
    "access_request_cards": ("app.slack_access_cards", "post_cards_for"),
}


def forward(kind: str, *args: Any, **kwargs: Any) -> bool:
    """Hand off one lead's forward (`FORWARDS[kind]` called with these arguments). Never raises."""
    if kind not in FORWARDS:
        raise ValueError(f"unknown forward {kind!r}")
    if in_workflow():
        return _forward_step(kind, args, kwargs) == "done"
    return _enqueue(FORWARD_QUEUE, deliver_forward, kind, args, kwargs, what=f"the {kind} forward")


@DBOS.workflow()
def deliver_forward(kind: str, args: tuple, kwargs: dict) -> str:
    """One lead's forward. The forward records its own outcome on the row and never raises."""
    return _forward_step(kind, args, kwargs)


@DBOS.step()
def _forward_step(kind: str, args: tuple, kwargs: dict) -> str:
    import importlib

    module, name = FORWARDS[kind]
    try:
        getattr(importlib.import_module(module), name)(*args, **kwargs)
    except Exception:
        log.exception("notify-outbox: the %s forward raised — its row keeps no outcome", kind)
        return "failed"
    return "done"


# ── After the answer: a public door's mail and notices ───────────────────────────────────────────────


#: The work a public door (sign-up, password reset) owes after it answers, handed off by name so it survives a
#: redeploy and the answer waits on none of it. Its arguments are sealed (`_AFTER_AAD`): an address, a sign-up
#: code, a request's IP. The target runs in the WORKFLOW BODY, not a step: DBOS refuses to start a workflow from
#: inside a step, and a target may (the auto-approval's provisioning), while every send it makes is itself a
#: recorded step (`email`'s inline branch, with its retries), so a recovered run sends nothing twice. ACCEPTED:
#: a run recovered mid-way repeats its unrecorded reads and writes — at worst one more unused reset link (inside
#: the account's hourly budget) or a second welcome audit row; the BackgroundTasks it replaced lost the whole run.
#: An operator's decision owes the same kind of work after its commit: the approval's billing start (its one
#: subscription keyed, so a repeat creates nothing) and the rejection's email and card detach.
AFTER_ANSWER: dict[str, tuple[str, str]] = {
    "onboarding_mail": ("app.onboarding_mail", "send_after_answer"),
    "signup_alert": ("app.routes.signup", "_alert_admins"),
    "signup_applied": ("app.trigger_user_registered", "on_signup_applied"),
    "signup_approved": ("app.org_approval", "after_signup_approval"),
    "signup_billing_start": ("app.org_approval", "start_signup_billing"),
    "org_rejected": ("app.org_approval", "after_rejection"),
    "password_reset": ("app.routes.password_reset", "_issue_and_send"),
}

_AFTER_AAD = b"jaah-notify-outbox-after-answer-v1"


def _after_aad(kind: str) -> bytes:
    """The envelope's domain separator, bound to its `kind`: arguments sealed for one work open for no other."""
    return _AFTER_AAD + b":" + kind.encode()


def _run_after(kind: str, args: list[Any]) -> str:
    import importlib

    module, name = AFTER_ANSWER[kind]
    try:
        getattr(importlib.import_module(module), name)(*args)
    except Exception:
        log.exception("notify-outbox: the %s work after the answer raised", kind)
        return "failed"
    return "done"


def after_answer(kind: str, *args: Any) -> bool:
    """Hand off one door's post-answer work (`AFTER_ANSWER[kind]` called with `args`, JSON values only). Inline
    in a workflow. Never raises past an unknown `kind`."""
    if kind not in AFTER_ANSWER:
        raise ValueError(f"unknown after-answer work {kind!r}")
    if in_workflow():
        return _run_after(kind, list(args)) == "done"
    from app import credential_crypto

    try:
        sealed = credential_crypto.seal({"args": list(args)}, aad=_after_aad(kind))
    except Exception as exc:  # noqa: BLE001 — a missing key must not fail the caller's committed write
        log.warning("notify-outbox: the %s work could not be sealed (%s) — not run", kind, type(exc).__name__)
        return False
    return _enqueue(EMAIL_QUEUE, deliver_after_answer, kind, sealed, what=f"the {kind} work")


@DBOS.workflow()
def deliver_after_answer(kind: str, sealed: dict[str, Any]) -> str:
    """One door's post-answer work. Returns `done` or `failed` (unopenable, or the work raised; logged)."""
    from app import credential_crypto

    try:
        args = credential_crypto.unseal(sealed, aad=_after_aad(kind), label="notify-outbox after-answer work")["args"]
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("notify-outbox: the %s work could not be opened (%s) — not run", kind, type(exc).__name__)
        return "failed"
    return _run_after(kind, args)
