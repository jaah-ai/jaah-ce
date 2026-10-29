"""The community edition's first run: `GET /api/ce/setup` says whether its one user exists, `POST` creates it.

Unauthenticated by necessity (no user exists to sign in), and 404 outside the community edition, so the cloud
serves neither (PRD §7). The POST creates exactly one person — a superadmin seat in org zero through the
seed's own shape (`app/seed.py::create_admin`) — and signs them in through the password door's own finish
(`routes/auth.py::finish_first_factor`). Any person already present is a 409: the edition has one user, no
sign-up, no invites. Loopback only, and refused for a request addressed to or sent from another name
(`_from_this_machine`), so the first visitor is someone on the machine.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition, ce_admin
from app.authz.catalog import Tenancy
from app.authz.declarations import always, session_only
from app.db import get_session
from app.routes.public import check_rate_limit

router = APIRouter(prefix="/api/ce", tags=["ce"])


class SetupIn(BaseModel):
    email: str
    password: str
    #: The browser's IANA zone (`Intl`): the console runs on this machine, so it is the host's.
    timezone: Optional[str] = None


#: The names this machine's console reaches the API by: loopback, and the compose service a proxy forwards to.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_API_HOSTS = _LOOPBACK_HOSTS | {"api"}


def _ce_only() -> None:
    if not edition.IS_CE:
        raise HTTPException(status_code=404, detail="Not Found")


def _from_this_machine(request: Request) -> None:
    """403 unless the request was addressed to this machine and, when a browser sent it, from a page on it.

    "Loopback only" does not make every caller the owner: a page on any site can rebind its own name to
    127.0.0.1 and POST here same-origin, claiming the install. Its `Host` (or, behind a proxy, its `Origin`,
    which a browser always sends on a POST) names that site, so either one outside loopback is refused."""
    host = urlsplit(f"//{request.headers.get('host', '')}").hostname
    origin = request.headers.get("origin")
    if host not in _API_HOSTS or (origin is not None and urlsplit(origin).hostname not in _LOOPBACK_HOSTS):
        raise HTTPException(status_code=403, detail="Open the console on this machine to set it up.")


def _adopt_timezone(session: Session, zone: Optional[str]) -> None:
    """Keep cron times on the host's clock: the browser's zone when Settings offers it, else UTC — never the
    catalogue's cloud default (`cron_runtime.CRON_TIMEZONE_SETTING_KEY`). Settings > General > Timezone moves it."""
    from app.cron_runtime import CRON_TIMEZONE_SETTING_KEY
    from app.operator_settings import catalog, writes

    knob = next(knob for knob in catalog.CATALOG if knob.key == CRON_TIMEZONE_SETTING_KEY)
    offered = knob.choices or ()
    writes.apply_overrides(session, {CRON_TIMEZONE_SETTING_KEY: zone if zone in offered else "UTC"})


@router.get("/setup", dependencies=[Depends(_ce_only), Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
def setup_state(session: Session = Depends(get_session)) -> dict[str, bool]:
    """`needs_setup`: true until the one user exists. Says nothing else about anyone."""
    return {"needs_setup": ce_admin.the_user(session) is None}


@router.post(
    "/setup", status_code=201,
    dependencies=[Depends(_ce_only), Depends(_from_this_machine), Depends(check_rate_limit)],
)
@always(Tenancy.BOOTSTRAP)
@session_only
def create_the_user(body: SetupIn, request: Request, response: Response, session: Session = Depends(get_session)):
    """Create the one user and sign them in (module docstring). 409 once anyone exists; 422 with a readable
    reason for an unusable email or password."""
    from app import identities, login_funnel, seed
    from app.db import SessionLocal
    from app.models.orgs import Org
    from app.operator_settings import cache
    from app.routes.auth import _admin_out, finish_first_factor
    from app.tenancy import seam
    from app.tenancy.ids import ORG_ZERO_ID

    email = body.email.strip()
    local, _, domain = email.partition("@")
    if not local or "." not in domain or any(ch.isspace() for ch in email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    refusal = ce_admin.password_refusal(body.password, email)
    if refusal is not None:
        raise HTTPException(status_code=422, detail=refusal)
    # Created on a session of its own, on the operator plane, as the seed creates the bootstrap admin: the
    # request's session then signs the new person in on their own org (a session keeps one scope for life).
    with SessionLocal() as writer, seam.operator_scope():
        # Serializes two first runs racing: the second waits here, then finds the first one's user.
        writer.execute(select(Org.id).where(Org.id == ORG_ZERO_ID).with_for_update())
        if writer.scalars(ce_admin.people_query().limit(1)).first() is not None:
            raise HTTPException(status_code=409, detail="This install already has its user. Sign in instead.")
        # Password sign-in is this edition's only door, and its one user has no second factor to enrol on a
        # loopback-only machine (`auth.password_refusal` shuts a superadmin's door unless opened explicitly).
        seed.create_admin(writer, email, body.password, password_login=True, mfa_exempt=True)
        seed.bind_superadmin(writer, email)
        # Last: the settings writer commits the session itself, so nothing may follow it under the row lock.
        _adopt_timezone(writer, body.timezone)
        writer.commit()
    cache.invalidate_cache()
    person = identities.by_email(session, email)
    admin = login_funnel.pick_membership(session, person, "password", request=request) if person else None
    if admin is None:
        raise HTTPException(status_code=500, detail="The user was created but could not be signed in. Sign in instead.")
    challenge = finish_first_factor(session, request, response, admin, "password")
    return challenge or _admin_out(admin, session)
