"""The `user_registered` trigger source: a person registering, raised by the control plane itself.

Two scopes (`triggers.USER_SCOPES`). PLATFORM: a self-service sign-up — the application that creates a pending org
(`signup.create_signup`, both doors) and, as a second moment, its approval (`org_approval`). Watched by the operator
org's triggers only (`routes/triggers.py::_source_allowed_or_refuse`), since a sign-up belongs to no tenant yet.
ORG: a person joining the trigger's OWN org — an admin creating their account, an invitation accepted, a domain
join. A new org's own owner is nobody's member to watch: no trigger can exist there yet.

Each hook here is called AFTER the registering transaction committed, never inside it, and never raises: a
trigger must not fail a sign-up. The platform hooks ride the sign-up's durable post-answer work
(`notify_outbox.after_answer`: the answer goes first); the member hooks run in the request, after its commit — one short session and a cron wake, cheaper
than a second code path, and a fault in them is logged and never reaches the caller. It builds the `{{user.*}}` event (`USER_EVENT_KEYS`), then
`trigger_intake.ingest_platform_signup` / `ingest_member_joined` write one delivery per trigger of that scope and org — every
such trigger, unlike the first-match routing of a global mailbox or Slack channel, because the event is the
org's own. The optional filters (`trigger_config.UserRegisteredConfig`) are applied by the processor
(`user_filter_reason`), so a non-matching registration is logged `filtered` with its reason, as a GitHub event is.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import access, identities
from app.config import get_settings
from app.entity_shape import iso_or_none
from app.db import SessionLocal
from app.identities import display_name
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser, UserIdentity
from app.models.orgs import Org, OrgSignup
from app.tenancy import seam
from app.triggers import USER_MOMENT_APPLIED, USER_MOMENT_APPROVED, USER_SCOPE_ORG, USER_SCOPE_PLATFORM

log = logging.getLogger("app.trigger_user_registered")

# How an org member arrived (`user.via`); a platform sign-up's is its door.
VIA_SIGNUP = "signup"
VIA_SELF_CREATE = "self_create"
VIA_ADMIN = "admin"
VIA_INVITATION = "invitation"
VIA_DOMAIN_JOIN = "domain_join"
# An org member's one moment: the join (`user.moment`).
MOMENT_JOINED = "joined"

# Every key `{{user.*}}` renders, always present (empty when the path has no value for it) so a template never
# names a missing path for a key another path fills.
USER_EVENT_KEYS = (
    "email", "first_name", "last_name", "name", "provider", "scope", "moment", "via",
    "org_id", "org_name", "org_slug", "account_type", "role", "intended_use", "plan", "url", "registered_at",
)


def _frontend() -> str:
    return get_settings().frontend_url.rstrip("/")


def _event(**values: Any) -> dict[str, Any]:
    user = {key: "" for key in USER_EVENT_KEYS}
    for key, value in values.items():
        user[key] = "" if value is None else value
    return {"user": user}


def _provider_of(identity: Optional[UserIdentity], fallback: str = "") -> str:
    return identities.identity_provider(identity) if identity is not None else fallback


def _signup_facts(session: Session, org_id: UUID, *, moment: str) -> Optional[dict[str, Any]]:
    """The platform event for `org_id`'s sign-up, or None for an org with no sign-up row (an operator-created
    org has nothing to register). Read under the operator scope: the org, its sign-up and its owner."""
    org = session.scalar(select(Org).where(Org.id == org_id))
    row = session.scalar(select(OrgSignup).where(OrgSignup.org_id == org_id))
    if org is None or row is None:
        return None
    owner = session.scalar(select(AdminUser).where(AdminUser.id == row.owner_id))
    first = owner.first_name if owner is not None else ""
    last = owner.last_name if owner is not None else ""
    name = display_name(owner) if owner is not None else row.email
    return _event(
        email=row.email, first_name=first, last_name=last, name=name, provider=row.provider,
        scope=USER_SCOPE_PLATFORM, moment=moment, via=_signup_door(session, org_id),
        org_id=str(org.id), org_name=org.name, org_slug=org.slug, account_type=org.account_type,
        role=row.job_title or row.occupation, intended_use=row.intended_use, plan=row.plan_key,
        url=f"{_frontend()}/customers/{org.id}",
        registered_at=iso_or_none(row.decided_at if moment == USER_MOMENT_APPROVED else row.signed_up_at) or "",
    )


def _signup_door(session: Session, org_id: UUID) -> str:
    """Which door the sign-up came through: the `org_signup` audit row `signup._create_signup` writes records
    `method` — a visitor's provider, or `self_create` for a signed-in person's second org — and nothing else
    persists it (the sign-up row's `provider` is the same for both)."""
    data = session.scalar(
        select(ActivityEvent.data)
        .where(ActivityEvent.org_id == org_id, ActivityEvent.kind == "org_signup")
        .order_by(ActivityEvent.id.desc())
        .limit(1)
    )
    return VIA_SELF_CREATE if (data or {}).get("method") == VIA_SELF_CREATE else VIA_SIGNUP


def _member_facts(session: Session, user: AdminUser, org: Org, *, via: str) -> dict[str, Any]:
    """The org event for a member who just joined `org`. Read under that org; the person's provider is read off
    the identity the membership row carries (`AdminUser.identity`, platform plane)."""
    return _event(
        email=user.email, first_name=user.first_name, last_name=user.last_name, name=display_name(user),
        provider=_provider_of(user.identity), scope=USER_SCOPE_ORG, moment=MOMENT_JOINED, via=via,
        org_id=str(org.id), org_name=org.name, org_slug=org.slug,
        url=f"{_frontend()}/users/{user.id}",
        registered_at=iso_or_none(user.activated_at or user.created_at or datetime.now(timezone.utc)) or "",
    )


# ── the filters, applied by the processor ────────────────────────────────────────────────────────────


def user_filter_reason(config: Mapping[str, Any], event: Mapping[str, Any]) -> Optional[str]:
    """Why the trigger's filters pass on this registration, or None when every set filter matches. Each set
    list is an any-of; an empty one matches everyone."""
    user = event.get("user") or {}
    providers = [str(v) for v in (config.get("providers") or ())]
    if providers and str(user.get("provider") or "") not in providers:
        return f"provider {user.get('provider') or 'unknown'} is not one of {', '.join(providers)}"
    account_types = [str(v) for v in (config.get("account_types") or ())]
    if account_types and str(user.get("account_type") or "") not in account_types:
        return f"account type {user.get('account_type') or 'unknown'} is not one of {', '.join(account_types)}"
    domains = [str(v).lower() for v in (config.get("email_domains") or ())]
    if domains:
        domain = access.email_domain(str(user.get("email") or "")) or ""
        if domain not in domains:
            return f"email domain {domain or 'unknown'} is not one of {', '.join(domains)}"
    return None


# ── the hooks, each after its commit ─────────────────────────────────────────────────────────────────


def on_signup_applied(org_id: UUID | str) -> None:
    """A sign-up's application committed (`routes/signup.py::notify_operators`, as post-answer work whose id
    arrives as text): the operator's `applied` triggers. Never raises."""
    _platform(UUID(str(org_id)), moment=USER_MOMENT_APPLIED)


def on_signup_approved(org_id: UUID) -> None:
    """A sign-up's approval committed (`org_approval._after_approval`): the operator's `approved` triggers.
    Never raises."""
    _platform(org_id, moment=USER_MOMENT_APPROVED)


def _platform(org_id: UUID, *, moment: str) -> None:
    from app import trigger_intake

    try:
        with SessionLocal() as session, seam.detached(), seam.operator_scope():
            event = _signup_facts(session, org_id, moment=moment)
        if event is None:
            return
        trigger_intake.ingest_platform_signup(event, moment=moment, dedup_key=f"signup:{org_id}:{moment}")
    except Exception:  # noqa: BLE001 — the sign-up stands; a trigger must never fail it
        log.exception("user-registered: the %s sign-up of org %s could not reach its triggers", moment, org_id)


def on_member_joined(user_id: UUID, org_id: UUID, *, via: str) -> None:
    """A membership in `org_id` committed as a real member — created by an admin (`routes/users.py::create_user`),
    an invitation accepted (`org_invitations.accept`) or a domain join (`signup.join_by_domain`,
    `routes/auth.py::_join_by_domain`): that org's `org`-scope triggers. Never raises."""
    from app import trigger_intake

    try:
        with SessionLocal() as session, seam.detached(), seam.bound_to(org_id):
            user = session.scalar(select(AdminUser).where(AdminUser.id == user_id, AdminUser.org_id == org_id))
            org = session.scalar(select(Org).where(Org.id == org_id))
            if user is None or org is None:
                return
            event = _member_facts(session, user, org, via=via)
        trigger_intake.ingest_member_joined(event, org_id, dedup_key=f"member:{user_id}")
    except Exception:  # noqa: BLE001 — the join stands; a trigger must never fail it
        log.exception("user-registered: member %s joining org %s could not reach its triggers", user_id, org_id)
