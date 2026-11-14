"""The forgot- and reset-password doors (the policy: `app/password_reset.py`). Public: no session yet.

* `POST /api/auth/password/forgot` — an email in; ALWAYS 202 `{"ok": true}`, whether or not an account has
  it (R-X1). Everything per-account — the lookup, the row, the `password_reset` email — runs after the
  answer, so the known and unknown arms do the same work before it and cannot be told apart by timing.
* `POST /api/auth/password/reset/check` — a link's token in; the email it resets and `first_password` (the
  person has none yet, so the page says "create"), plus `org_name` and `inviter_name` (null when unnamed) for
  an invitation's link, or 400 `reset_link_invalid`.
  What the set-new-password page reads first, so a dead link says so before anyone types a password.
* `POST /api/auth/password/reset` — token and new password in; 200 `{"ok": true, "email": ...}`. Refusals:
  400 `reset_link_invalid`, 403 `superadmin_password`, 422 the password rule's sentence.

The forgot door is limited per visitor, per address and across every visitor at once
(`PASSWORD_FORGOT_RATE_LIMIT`/`PASSWORD_FORGOT_EMAIL_RATE_LIMIT`/`PASSWORD_FORGOT_GLOBAL_RATE_LIMIT` override
the budgets), shared through the database so the budget is one across the fleet. The per-address budget
counts every address, known or not, so its 429 says nothing about whether an account exists. The two reset
doors share a per-visitor budget (`PASSWORD_RESET_RATE_LIMIT`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import db_async, onboarding_mail, password_reset
from app.auth import PASSWORD_PROBLEM_MESSAGES, hash_password, password_problem
from app.offload import run_cpu
from app.authz.catalog import Tenancy
from app.authz.declarations import always, session_only
from app.db_async import DbDep, on_session
from app.durable import notify_outbox
from app.login_funnel import source_ip
from app.ratelimit import SharedRateLimiter
from app.routes.auth import _revoke_direct_console_grants

security_log = logging.getLogger("app.security")

router = APIRouter(prefix="/api/auth/password", tags=["auth"])

_HOUR = 3600.0
_TOO_MANY = "Too many password reset requests. Try again in an hour."
_RESET_LINK_INVALID = {"code": "reset_link_invalid"}
#: The forgot door's per-account read and write, run from the outbox's worker thread on the serving loop.
_ISSUE_TIMEOUT_SECONDS = 30.0

_forgot_ip_limiter = SharedRateLimiter(
    limit=int(os.environ.get("PASSWORD_FORGOT_RATE_LIMIT", "10")), window=_HOUR, bucket="password-forgot-ip"
)
_forgot_email_limiter = SharedRateLimiter(
    limit=int(os.environ.get("PASSWORD_FORGOT_EMAIL_RATE_LIMIT", str(password_reset.PER_ACCOUNT_PER_HOUR))),
    window=_HOUR,
    bucket="password-forgot-email",
)
_forgot_global_limiter = SharedRateLimiter(
    limit=int(os.environ.get("PASSWORD_FORGOT_GLOBAL_RATE_LIMIT", "2000")), window=_HOUR,
    bucket="password-forgot-global",
)
_reset_ip_limiter = SharedRateLimiter(
    limit=int(os.environ.get("PASSWORD_RESET_RATE_LIMIT", "30")), window=_HOUR, bucket="password-reset-ip"
)


def _visitor(request: Request) -> str:
    return source_ip(request) or "unknown"


def _forgot_rate(request: Request) -> None:
    if not _forgot_ip_limiter.allow(_visitor(request)):
        raise HTTPException(status_code=429, detail=_TOO_MANY)


def _reset_rate(request: Request) -> None:
    if not _reset_ip_limiter.allow(_visitor(request)):
        raise HTTPException(status_code=429, detail=_TOO_MANY)


class ForgotIn(BaseModel):
    email: str = Field(max_length=320)


class TokenIn(BaseModel):
    token: str = Field(max_length=256)


class ResetIn(TokenIn):
    # Generous: the rule's own ceiling (`auth.PASSWORD_MAX`) answers with a sentence; this only bounds the body.
    password: str = Field(max_length=1024)


def _issue_and_send(email: str, ip: Optional[str]) -> None:
    """The forgot door's per-account work, after the answer (`notify_outbox.after_answer`), on a session of its
    own: the link if the account may have one, then its email. Called on a worker's thread: its session is
    reached on the serving loop (`db_async.run_on_loop`)."""
    mail = db_async.run_on_loop(
        db_async.run_in_session(password_reset.request_reset, email, ip), timeout=_ISSUE_TIMEOUT_SECONDS
    )
    if mail is not None:
        onboarding_mail.send("password_reset", mail.to, mail.values, button_url=mail.reset_url)


def _refused(reason: str, token: str) -> None:
    """The `app.security` line for a refused link: the row id it named, never the secret."""
    security_log.info(json.dumps({"event": "password_reset_refused", "reason": reason, "reset_id": token.partition(".")[0][:64]}))


def _link_refusal(exc: Exception, token: str) -> HTTPException:
    """The answer for a link `password_reset` would not honour, logged through `_refused` first."""
    if isinstance(exc, password_reset.SuperadminRefused):
        _refused("superadmin_password", token)
        return HTTPException(status_code=403, detail={"code": "superadmin_password"})
    _refused("reset_link_invalid", token)
    return HTTPException(status_code=400, detail=_RESET_LINK_INVALID)


@router.post("/forgot", status_code=202, dependencies=[Depends(_forgot_rate)])
@always(Tenancy.BOOTSTRAP)
@session_only
async def forgot_password(body: ForgotIn, request: Request):
    """No route session: the limiters and the outbox enqueue each open their own, on a thread."""
    return await asyncio.to_thread(_forgot_password, body, request)


def _forgot_password(body: ForgotIn, request: Request):
    """`forgot_password`'s body, on a worker thread (its limiters and enqueue block on their own sessions)."""
    # The fleet-wide budget first, so a refusal there does not spend the address's. Keyed on a hash, so the
    # limiter's rows never hold an address.
    address = hashlib.sha256(password_reset.normalized_email(body.email).encode()).hexdigest()
    if not _forgot_global_limiter.allow("*") or not _forgot_email_limiter.allow(address):
        raise HTTPException(status_code=429, detail=_TOO_MANY)
    notify_outbox.after_answer("password_reset", body.email, source_ip(request))
    return {"ok": True}


@router.post("/reset/check", dependencies=[Depends(_reset_rate)])
@always(Tenancy.BOOTSTRAP)
@session_only
@on_session
def check_reset_link(session: Session, body: TokenIn):
    try:
        link = password_reset.check_link(session, body.token)
    except (password_reset.SuperadminRefused, password_reset.ResetLinkInvalid) as exc:
        raise _link_refusal(exc, body.token) from None
    out = {"email": link.email, "first_password": link.first_password}
    if link.org_name is not None:
        out.update(org_name=link.org_name, inviter_name=link.inviter_name)
    return out


@router.post("/reset", dependencies=[Depends(_reset_rate)])
@always(Tenancy.BOOTSTRAP)
@session_only
async def reset_password(
    db: DbDep,
    body: ResetIn,
):
    """The new password is hashed off the loop first (bcrypt, `run_cpu`) when the length rule admits it, so no
    connection or row lock is held across the hash; `password_reset.complete_reset` still judges it for the account."""
    password_hash = (
        await run_cpu(hash_password, body.password) if password_problem(body.password, None) is None else None
    )
    out, user_id, retired_epoch = await db.run_sync(_reset_password, body, password_hash)
    # After the commit, as `update_me`: a rolled-back reset never revokes grants of a session still valid.
    await run_in_threadpool(_revoke_direct_console_grants, user_id, retired_epoch)
    return out


def _reset_password(
    session: Session,
    body: ResetIn,
    password_hash: str | None,
):
    """`reset_password`'s body, over the route's async session (`run_sync`)."""
    try:
        done = password_reset.complete_reset(session, body.token, body.password, password_hash=password_hash)
    except (password_reset.SuperadminRefused, password_reset.ResetLinkInvalid) as exc:
        raise _link_refusal(exc, body.token) from None
    except password_reset.PasswordRejected as rejected:
        raise HTTPException(status_code=422, detail=PASSWORD_PROBLEM_MESSAGES[rejected.code]) from rejected
    return {"ok": True, "email": done.email}, done.user_id, done.retired_epoch
