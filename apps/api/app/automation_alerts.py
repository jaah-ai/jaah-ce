"""Telling an org's Owners and Admins (operators, on the operator plane) that one of its automations is failing or
stalling.

Three moments, each told once per episode:
- an event trigger the system stopped (`trigger_notices.notify_failed`, from `trigger_health.mark_failed`) — not the
  idle rule's stop, which the owner opted into and alone hears about;
- a schedule whose run lock outlived its timeout (`cron_runtime._settle`, the abandoned firing's own row): Stuck,
  when that firing opens the episode (an overrun is itself a wedged firing, so a recurring one is the same episode);
- a schedule whose wedge counter (`crons.consecutive_skips`: errors, "previous task still open", and
  "already running" without a min-gap floor) reaches its threshold — `automation_alert_fast_runs` in a row, or
  `automation_alert_slow_runs` for a schedule firing hourly or slower. The counter rises by one per firing and
  resets on a firing that ran, so "equal to the threshold" is once per episode with no stamp of its own.

Each Owner and Admin gets one `automation_alert` `trigger_notices` row (the bell, `routes/inbox.py`) and one
`user_notify.deliver(..., important=True)`: email to the verified address whatever their preferences say, Slack
and Telegram as they set the `automation_failures` event. A system schedule lives on the operator plane, where an org
role administers nothing, so its notice goes to every live operator instead (the support knowledge base's
embeddings once failed for hours and told nobody); the wedge alarm Task (`cron_runtime._deliver_skip_alarm`)
still files beside it. A system schedule's overrun alerts only once it wedges: the sweeps are many and one
overrun is not an outage.
A notice carries display data only — a name, a reason, a link.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import alert_relay, email_notify, user_notify
from app.crons import RUN_OUTCOME_ERROR, SKIP_ALREADY_RUNNING, SKIP_PREVIOUS_TASK_OPEN
from app.models.automations import Automation, Cron, TriggerNotice
from app.models.identity import AdminUser
from app.one_line import one_line
from app.triggers import NOTICE_AUTOMATION

log = logging.getLogger("app.automation_alerts")

#: The `user_notify` event (a `preferences.NOTIFICATION_EVENTS` key, Owners and Admins only).
EVENT = "automation_failures"
#: The two operator knobs (`operator_settings/catalog_orgs.py`), and the gap from which a schedule is "slow".
FAST_RUNS_KEY = "automation_alert_fast_runs"
SLOW_RUNS_KEY = "automation_alert_slow_runs"
SLOW_GAP_SECONDS = 3600
#: A reason is quoted into a notice and an email; past this it is cut (the run log keeps it whole).
MAX_DETAIL_CHARS = 200


def automation_path(automation_id: UUID) -> str:
    return f"/automations/{automation_id}?tab=runs"


def admins(session: Session, org_id: UUID) -> list[AdminUser]:
    """Who is told: the org's live Owners and Admins (`access.owners_and_admins`); on the operator plane, every live
    operator (a superadmin there, the `Principal.is_operator` verdict), since an org role administers nothing there."""
    from app import access
    from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

    if org_id in OPERATOR_PLANE_ORG_IDS:
        return access.live_operators(session)
    return access.owners_and_admins(session, org_id)


def stage(
    session: Session, org_id: UUID, *, text: str, detail: str, href: str, trigger_id: Optional[UUID] = None
) -> tuple[list[UUID], Optional[Callable[[], None]]]:
    """Add a bell row for everyone `admins` names to `session` (the caller commits); return who they are and the
    delivery to run once it has committed (None when nobody is told). Raises like any read: the caller decides."""
    people = [user.id for user in admins(session, org_id)]
    if not people:
        return people, None
    detail = one_line(detail, MAX_DETAIL_CHARS)
    for user_id in people:
        session.add(TriggerNotice(
            org_id=org_id, user_id=user_id, trigger_id=trigger_id, kind=NOTICE_AUTOMATION,
            summary={"text": text, "detail": detail, "href": href},
        ))
    event = alert_relay.message(EVENT, f"{text}. {detail}", payload={"console_url": email_notify.console_path_link(href)})
    return people, lambda: _deliver(people, event)


def _deliver(user_ids: list[UUID], event: dict) -> None:
    """Each person's important delivery and its `notification_sent` row, in a fresh session on the caller's plane
    (the cron tick's operator scope, a trigger's org). Never raises: the bell rows have already committed."""
    from app.db import SessionLocal

    try:
        with SessionLocal() as session:
            for user in session.scalars(select(AdminUser).where(AdminUser.id.in_(user_ids))):
                user_notify.deliver(session, user, event, event_name=EVENT, important=True)
            session.commit()
    except Exception:  # noqa: BLE001 — the bell already holds the notice; a lost send must not fail its caller
        log.exception("automation alert: could not deliver %r", event.get("reason"))


def alert(session_factory, org_id: UUID, *, text: str, detail: str, href: str) -> None:
    """`stage` in its own session, commit, then deliver. Never raises: the firing has already committed."""
    try:
        with session_factory() as session:
            _people, send = stage(session, org_id, text=text, detail=detail, href=href)
            session.commit()
    except Exception:  # noqa: BLE001 — a lost notice must not fail the firing that raised it
        log.exception("automation alert: could not tell the admins of org %s (%s)", org_id, text)
        return
    if send is not None:
        send()


# ── Schedules ─────────────────────────────────────────────────────────────────────────────────────────


def threshold(cron: Cron) -> int:
    """How many wedged firings in a row alert: the slow knob for a schedule firing hourly or slower, else the fast
    one. A schedule whose cadence cannot be measured counts as fast (the earlier alert)."""
    from app import cron_min_period
    from app.operator_settings import reads

    try:
        gap = cron_min_period.schedule_gap(cron.schedule_kind, cron.cron_expression, cron.interval_seconds)
    except Exception:  # noqa: BLE001 — an unmeasurable schedule still alerts
        gap = 0.0
    return reads.get_int(SLOW_RUNS_KEY if gap >= SLOW_GAP_SECONDS else FAST_RUNS_KEY)


def _wedge_detail(count: int, outcome: str, skip_reason: Optional[str]) -> tuple[str, str]:
    """The headline verb and the second line for a schedule whose counter reached its threshold."""
    if outcome == RUN_OUTCOME_ERROR:
        # Not the error itself: it is often an exception's repr, and this line leaves the console by email.
        return "failing", f"{count} runs in a row failed. The last error is on Runs"
    if skip_reason == SKIP_PREVIOUS_TASK_OPEN:
        return "stalled", f"{count} runs in a row skipped: previous task still open"
    if skip_reason == SKIP_ALREADY_RUNNING:
        return "stalled", f"{count} runs in a row skipped: previous run still running"
    return "stalled", f"{count} runs in a row failed or skipped"


def after_firing(
    session_factory, cron_id: UUID, *, skips: Optional[int], outcome: str, skip_reason: Optional[str], stuck: bool,
) -> None:
    """`cron_runtime._settle`'s hook, after its commit: alert on the Stuck firing, or on the firing whose wedge
    count equals the schedule's threshold. A healthy firing (`skips` None or 0) pays no round trip."""
    if not skips:
        return
    try:
        with session_factory() as session:
            cron = session.scalar(select(Cron).where(Cron.id == cron_id))
            if cron is None or cron.automation_id is None:
                return
            # A stuck firing is itself wedged (its timeout `error` counts), so it alerts only as an episode's first;
            # an overrun that recurs without a good firing between is the same episode.
            if stuck and skips == 1 and not cron.is_system:
                verb, line = "stuck", "A run outlived its timeout and still holds the lock. Release it on Runs."
            elif skips == threshold(cron):
                verb, line = _wedge_detail(skips, outcome, skip_reason)
            else:
                return
            name = session.scalar(select(Automation.name).where(Automation.id == cron.automation_id)) or cron.name
            org_id, automation_id = cron.org_id, cron.automation_id
    except Exception:  # noqa: BLE001 — bookkeeping after a committed firing never raises
        log.exception("automation alert: could not read cron %s", cron_id)
        return
    alert(
        session_factory, org_id, text=f"Automation {verb}: {name}", detail=line, href=automation_path(automation_id),
    )
