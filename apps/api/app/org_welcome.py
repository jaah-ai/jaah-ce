"""The welcome email an operator sends a self-service org's owner on approval, and on "Send welcome email"
(R9); the system sends it on an automatic approval at sign-up (`app/org_approval.py`).

One `welcome` onboarding email (`app/onboarding_mail.py`) to the owner: a button to the console's `/login`
with their email prefilled (`login_url`), how they signed up, and the tutorials. It carries no token: the
link only opens the sign-in page. Sent synchronously, so the operator's dialog can show what happened.

Every attempt is recorded twice, in the caller's transaction, committed here: `org_signups.welcome_*` is the
latest attempt (when, by whom, its failure; NULL error = sent), and an `org_welcome_sent` activity row per
attempt is the full history the org page lists (`sends`). Neither row ever names the recipient's address.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import onboarding_mail
from app.login_oauth import frontend_origin
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser
from app.models.orgs import Org, OrgSignup
from app.user_activity import record_write

#: `onboarding_mail.Outcome`, plus `skipped`: an org with no sign-up row (operator-created or legacy) has no
#: applicant to welcome, which is never a failure of the approval that asked.
Outcome = Literal["sent", "not_configured", "failed", "skipped"]

KIND = "org_welcome_sent"

#: The `sign_in_method` placeholder, by `org_signups.provider` (`SIGNUP_PROVIDERS`): it completes the
#: template's "Sign in with {{sign_in_method}}".
SIGN_IN_METHOD = {
    "email": "your email and password",
    "google": "Google",
    "github": "GitHub",
    "microsoft": "Microsoft",
}

#: `welcome_error`, in words the org page shows as they are.
_ERROR = {
    "not_configured": "Email sending is not set up.",
    "failed": "The email could not be sent.",
}

_SUMMARY = {"sent": "sent", "not_configured": "not sent: email sending is not set up", "failed": "failed"}


def login_url(email: str) -> str:
    """The console's sign-in page with `email` prefilled (`Login.tsx` reads `?email=`)."""
    return f"{frontend_origin()}/login?email={quote(email, safe='')}"


def send(session: Session, org: Org, actor: AdminUser) -> Outcome:
    """Email `org`'s owner the welcome, record the attempt and commit. Never raises on a mail fault (the
    outcome says it); `skipped` when `org` has no sign-up row. The caller has checked the org may be
    welcomed (active), and holds no row lock: the send is a network call."""
    signup = session.scalar(select(OrgSignup).where(OrgSignup.org_id == org.id))
    if signup is None:
        return "skipped"
    owner = session.scalar(select(AdminUser).where(AdminUser.id == signup.owner_id))
    # The owner's sign-in email is what `/login` must be prefilled with; the application's is its fallback.
    email = owner.email if owner is not None and owner.email else signup.email
    url = login_url(email)
    outcome = onboarding_mail.send(
        "welcome", email,
        {
            "first_name": (owner.first_name if owner is not None else None) or "there",
            "email": email,
            "org_name": org.name,
            "login_url": url,
            "sign_in_method": SIGN_IN_METHOD.get(signup.provider, signup.provider),
        },
        button_url=url,
    )
    signup.welcome_sent_at = datetime.now(timezone.utc)
    signup.welcome_sent_by_id = actor.id
    signup.welcome_error = _ERROR.get(outcome)
    record_write(
        session, actor, KIND, f"Welcome email for {org.name} ({org.slug}) {_SUMMARY[outcome]}",
        data={"org_id": str(org.id), "slug": org.slug, "outcome": outcome},
    )
    session.commit()
    return outcome


@dataclass(frozen=True)
class Sent:
    """One recorded attempt: when, who (`by_id`, else the label recorded), and its `onboarding_mail.Outcome`."""

    at: datetime
    by_id: Optional[UUID]
    by_label: Optional[str]
    outcome: str


def sends(session: Session, signups: Iterable[OrgSignup]) -> dict[UUID, list[Sent]]:
    """Every recorded welcome attempt per sign-up's org, newest first, in one query. The rows are the actor's
    org's (`record_write`), so they are found by the org id their data names; only orgs ever sent one are
    asked for, and no row older than the earliest of their sign-ups is read."""
    sent = [s for s in signups if s.welcome_sent_at is not None]
    if not sent:
        return {}
    wanted = {str(s.org_id): s.org_id for s in sent}
    rows = session.scalars(
        select(ActivityEvent)
        .where(
            ActivityEvent.action_type == "write",  # `ix_activity_events_browse` leads the scan
            ActivityEvent.kind == KIND,
            ActivityEvent.created_at >= min(s.signed_up_at for s in sent),
            ActivityEvent.data["org_id"].astext.in_(list(wanted)),
        )
        .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
    )
    out: dict[UUID, list[Sent]] = {}
    for row in rows:
        data = row.data or {}
        out.setdefault(wanted[data["org_id"]], []).append(Sent(
            at=row.created_at, by_id=row.user_id, by_label=row.actor_label,
            outcome=str(data.get("outcome") or ""),
        ))
    return out
