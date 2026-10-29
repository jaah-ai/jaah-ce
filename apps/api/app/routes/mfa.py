"""The second-factor routes (G-01): verify a code, or enrol a factor, between the first factor and a session.

Authenticated by the pending cookie alone (`app/mfa.py::PENDING_COOKIE`), so they sit in
`app/auth.py::PUBLIC_PATHS`. A wrong code is a `login_failed` row (`bad_factor`); `MAX_BAD_CODES` of them
kill the pending sign-in (`mfa_expired`, back to the login page). The session is minted by the funnel
(`app/login_funnel.py::complete_second_factor`). No secret or code is logged or put in an event.

A proven factor may also trust this browser (`remember_device`, `app/mfa.py::trust_device`); the signed-in
user lists and forgets their trusted devices under `/api/auth/me/trusted-devices`.
"""
from __future__ import annotations

import logging
from typing import Optional

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from app import ip_allowlist, login_funnel, mfa
from app.auth import require_person, session_row_is_live
from app.authz.catalog import Tenancy
from app.authz.declarations import always, session_only
from app.db import get_session
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.routes.auth import AdminOut, _admin_out, _revoke_direct_console_grants
from app.routes.public import check_rate_limit
from app.tenancy import seam
from app.tenancy.ids import is_tenant_org
from app.user_activity import record_write

log = logging.getLogger("cockpit-mfa")

router = APIRouter(prefix="/api/auth", tags=["auth"])

_EXPIRED = {"code": "mfa_expired"}
_BAD = {"code": "bad_factor"}
_THROTTLED = {"code": "mfa_throttled"}


class PendingOut(BaseModel):
    stage: str  # `verify` | `enrol`
    email: str
    # How long "Trust this device" lasts, for the checkbox's label (`app/mfa.py::remember_days`); 0 hides it.
    remember_days: int
    # The org whose sign-in policy asks for the factor, for the enrol page: the user's own tenant org
    # (`app/mfa.py::org_skips` reads the same org's skip). None on verify, and where the platform requires it
    # instead (a superadmin, an operator-plane org).
    org_name: Optional[str] = None


class VerifyIn(BaseModel):
    code: Optional[str] = None
    recovery_code: Optional[str] = None
    remember_device: bool = False

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("send exactly one of code or recovery_code")
        return self


class EnrolStartOut(BaseModel):
    otpauth_uri: str
    secret: str


class ConfirmIn(BaseModel):
    code: str
    remember_device: bool = False


class ConfirmOut(BaseModel):
    recovery_codes: list[str]
    user: AdminOut


class RegenerateIn(BaseModel):
    code: str


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


class TrustedDeviceOut(BaseModel):
    id: int
    label: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: datetime
    # The browser making this request.
    current: bool


def _pending(request: Request, session: Session) -> tuple[mfa.Pending, AdminUser]:
    """The live pending sign-in and its user, or 401 `mfa_expired`."""
    pending = mfa.read_pending(request.cookies.get(mfa.PENDING_COOKIE))
    if pending is None:
        raise HTTPException(status_code=401, detail=_EXPIRED)
    # Identity escape: this read resolves who is signing in (`app/auth.py::_session_user`'s reason).
    with seam.identity_scope():
        user = session.get(AdminUser, pending.admin_id)
    # Both epochs (`auth.session_row_is_live`): a sign-out, password change or reset since the first factor
    # retires the pending sign-in too, so nobody enrols a factor on a credential that was just rotated.
    if user is None or user.is_system or not session_row_is_live(user, pending.epoch, pending.iepoch):
        raise HTTPException(status_code=401, detail=_EXPIRED)
    return pending, user


def _refuse(session: Session, request: Request, user: AdminUser, pending: mfa.Pending, check: mfa.Check):
    """Record a refused second factor (committing the failure count with it) and raise its 401."""
    login_funnel.record_login_failure(session, request, user, method=pending.method, reason="bad_factor")
    log.info("mfa: refused user=%s expired=%s throttled=%s", user.id, check.expired, check.throttled)
    raise HTTPException(
        status_code=401, detail=_THROTTLED if check.throttled else _EXPIRED if check.expired else _BAD
    )


def _signed_in(
    session: Session, request: Request, response: Response, user, pending, amr: str, *, remember: bool
) -> None:
    session.commit()
    host = login_funnel.source_ip(request)
    refused = login_funnel.complete_second_factor(session, response, user, pending, amr, client_ip=host)
    if refused == ip_allowlist.REFUSAL_CODE:
        # The middleware's own 403 shape (`app/auth.py`), so the page lands on the blocked-network panel.
        login_funnel.record_login_failure(session, request, user, method=pending.method, reason=refused)
        raise ip_allowlist.IpNotAllowed(host)
    if refused is not None:
        raise HTTPException(status_code=401, detail=_EXPIRED)
    # An IdP door's org switch whose target demanded the factor ends here: its replaced session's streams close.
    login_funnel.announce_replaced_session(session, request, user)
    if remember:
        _trust_device(session, request, response, user)


def _trust_device(session: Session, request: Request, response: Response, user: AdminUser) -> None:
    days = mfa.remember_days()
    device = mfa.trust_device(
        session, response, user, request.headers.get("user-agent"), days, request.cookies.get(mfa.DEVICE_COOKIE)
    )
    record_write(
        session, user, "mfa_device_trusted", f"{user.email} trusted {device.label} for two-factor sign-in",
        data={"label": device.label, "days": days},
    )
    session.commit()


@router.get("/mfa/pending", response_model=PendingOut, dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def mfa_pending(request: Request, session: Session = Depends(get_session)):
    """Which second-factor page the pending sign-in is on, for the console to render."""
    _, user = _pending(request, session)
    stage = "verify" if mfa.is_enrolled(session, user) else "enrol"
    by_org = stage == "enrol" and not user.is_superadmin and user.org_id is not None and is_tenant_org(user.org_id)
    org = session.get(Org, user.org_id) if by_org else None
    return PendingOut(
        stage=stage, email=user.email,
        remember_days=mfa.remember_days(), org_name=org.name if org is not None else None,
    )


@router.post("/mfa/verify", response_model=AdminOut, dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def mfa_verify(body: VerifyIn, request: Request, response: Response, session: Session = Depends(get_session)):
    """Prove the second factor with a TOTP code or a recovery code; the session is minted on success."""
    pending, user = _pending(request, session)
    check = mfa.verify(session, user, pending.nonce, code=body.code, recovery_code=body.recovery_code)
    if not check.ok:
        _refuse(session, request, user, pending, check)
    if check.amr == "recovery":
        record_write(session, user, "mfa_recovery_used", f"{user.email} signed in with a recovery code")
    _signed_in(session, request, response, user, pending, check.amr, remember=body.remember_device)
    return _admin_out(user, session)


@router.post("/mfa/enrol/start", response_model=EnrolStartOut, dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def mfa_enrol_start(request: Request, session: Session = Depends(get_session)):
    """A new authenticator secret for a user with no factor yet. Calling again replaces it."""
    pending, user = _pending(request, session)
    if mfa.is_enrolled(session, user):
        raise HTTPException(status_code=409, detail={"code": "already_enrolled"})
    secret = mfa.start_enrolment(session, user, pending.nonce)
    if secret is None:
        raise HTTPException(status_code=401, detail=_EXPIRED)
    session.commit()
    return EnrolStartOut(otpauth_uri=mfa.otpauth_uri(secret, user.email), secret=secret)


@router.post("/mfa/enrol/confirm", response_model=ConfirmOut, dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
@session_only
def mfa_enrol_confirm(body: ConfirmIn, request: Request, response: Response, session: Session = Depends(get_session)):
    """Confirm the new factor with its first code: the factor goes live, the recovery codes are shown
    this once, and the session is minted."""
    pending, user = _pending(request, session)
    check, codes = mfa.confirm_enrolment(session, user, pending.nonce, body.code)
    if not check.ok:
        _refuse(session, request, user, pending, check)
    record_write(session, user, "mfa_enrolled", f"{user.email} set up two-factor sign-in")
    _signed_in(session, request, response, user, pending, check.amr, remember=body.remember_device)
    return ConfirmOut(recovery_codes=codes, user=_admin_out(user, session))


@router.post("/me/mfa/recovery-codes", response_model=RecoveryCodesOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def regenerate_recovery_codes(
    body: RegenerateIn, request: Request, response: Response, user: AdminUser = Depends(require_person),
    session: Session = Depends(get_session),
):
    """A fresh recovery-code set for the caller (the old one dies), stepped up by a current TOTP code so a
    stolen session cannot mint a lasting second factor. Ends every other session of theirs; this one is
    re-minted at the new epoch."""
    if not mfa.is_enrolled(session, user):
        raise HTTPException(status_code=409, detail="two-factor is not set up")
    check = mfa.verify_current_code(session, user, body.code)
    if not check.ok:
        session.commit()  # the failure count
        raise HTTPException(status_code=401, detail=_THROTTLED if check.throttled else _BAD)
    codes = mfa.regenerate_recovery_codes(session, user)
    record_write(session, user, "mfa_recovery_regenerated", f"{user.email} replaced their recovery codes")
    retired_epoch = user.session_epoch or 0
    user.session_epoch = retired_epoch + 1
    session.commit()
    _revoke_direct_console_grants(user.id, retired_epoch)
    login_funnel.reissue_session(request, response, user)
    return RecoveryCodesOut(recovery_codes=codes)


@router.get("/me/trusted-devices", response_model=list[TrustedDeviceOut])
@always(Tenancy.BOOTSTRAP)
@session_only
def list_trusted_devices(
    request: Request, user: AdminUser = Depends(require_person), session: Session = Depends(get_session)
):
    """The caller's live trusted devices, newest first; `current` marks the browser asking."""
    current = mfa.device_id_for(session, user, request.cookies.get(mfa.DEVICE_COOKIE))
    return [
        TrustedDeviceOut(
            id=d.id, label=d.label, created_at=d.created_at, last_used_at=d.last_used_at,
            expires_at=d.expires_at, current=d.id == current,
        )
        for d in mfa.trusted_devices(session, user)
    ]


def _forget(
    session: Session, request: Request, response: Response, user: AdminUser, device_id: Optional[int]
) -> None:
    """Forget devices and end the sessions they may hold: a lost laptop's live session must not outlive the
    revoke. The caller's own session is re-minted at the new epoch, as a recovery-code regeneration does."""
    labels = mfa.revoke_trusted_devices(session, user, device_id)
    if not labels:
        session.commit()
        return
    record_write(
        session, user, "mfa_device_revoked",
        f"{user.email} stopped trusting {labels[0] if len(labels) == 1 else f'{len(labels)} devices'}",
        data={"labels": labels},
    )
    retired_epoch = user.session_epoch or 0
    user.session_epoch = retired_epoch + 1
    session.commit()
    _revoke_direct_console_grants(user.id, retired_epoch)
    login_funnel.reissue_session(request, response, user)


@router.delete("/me/trusted-devices/{device_id}", status_code=204)
@always(Tenancy.BOOTSTRAP)
@session_only
def revoke_trusted_device(
    device_id: int, request: Request, response: Response, user: AdminUser = Depends(require_person),
    session: Session = Depends(get_session),
):
    """Stop trusting one of the caller's devices: it asks for the code at its next sign-in, and every other
    session of the caller's ends. Idempotent."""
    _forget(session, request, response, user, device_id)


@router.delete("/me/trusted-devices", status_code=204)
@always(Tenancy.BOOTSTRAP)
@session_only
def revoke_trusted_devices(
    request: Request, response: Response, user: AdminUser = Depends(require_person),
    session: Session = Depends(get_session),
):
    """Stop trusting every one of the caller's devices, ending every other session of theirs. Idempotent."""
    _forget(session, request, response, user, None)
