"""The org-lifecycle gate: what a pending org may not do yet, and how an unapproved org's sign-in is refused.

`app/models/orgs.py::Org.lifecycle` is the state. Two refusals live here and nowhere else:

* `require_org_active` — every creation door a pending org must not pass answers 403 "Available after
  approval" (the dispatch seam's queued wording is `app/org_limits.py::wait_reason`'s, same text). Which
  doors call it is held by a census test; the dispatch roster
  (`app/pool_state.py::_node_org_in_service`) is the backstop behind a door that forgets it.
* `refuse_unapproved_sign_in` — THE ONE refusal every sign-in door gives a user whose org is not approved,
  decided by `sign_in_refusal`. A REJECTED org: a redirect door gets a 303 to
  `/login?login_error=org_not_approved&login_detail=<reason>`, a POST door a 403 whose detail is
  `{"code": "org_not_approved", "reason": str | null}`. A PENDING org (an application awaiting review): the
  same shapes with code `org_pending` and no reason; a soft-DELETED org (`orgs.deleted_at`), code `org_deleted`. The login page renders the first as a banner
  (`apps/web/src/pages/Login.tsx::LOGIN_ERROR_COPY`) and the second as its "Under review" panel. The
  funnel's `org_approval_policy` (`app/login_funnel.py`) reads `sign_in_refusal` too, the backstop behind
  a door that forgets the gate.

The reason is the operator's own `org_signups.rejection_reason`, capped at `REASON_MAX` characters; an
operator-rejected org with no sign-up row (or no reason) carries `null`.
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Optional
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orgs import ORG_ACTIVE, ORG_PENDING, ORG_REJECTED, Org, OrgSignup
from app.org_limits import AWAITING_APPROVAL
from app.tenancy import seam

#: A creation door's refusal for a soft-deleted org.
ORG_DELETED_DETAIL = "This org is deleted."

#: The longest rejection reason a refusal carries (a URL parameter, then a banner line).
REASON_MAX = 200


def _org(session: Session, org_id: UUID) -> Optional[Org]:
    return session.get(Org, org_id)


def org_is_deleted(session: Session, org_id: UUID) -> bool:
    """`app/models/orgs.py::in_live_org` for one row already read: a detail route answers 404 for what its list hides."""
    org = _org(session, org_id)
    return org is not None and org.deleted_at is not None


def org_is_active(session: Session, org_id: UUID) -> bool:
    """Whether `org_id` is approved (or was never gated) and not deleted. A missing org is not active."""
    org = _org(session, org_id)
    return org is not None and org.lifecycle == ORG_ACTIVE and org.deleted_at is None


def require_org_active(session: Session, org_id: UUID) -> None:
    """403 "Available after approval" unless `org_id` is active — "This org is deleted." for a soft-deleted one.
    Called BEFORE the write it guards."""
    if not org_is_active(session, org_id):
        org = _org(session, org_id)
        raise HTTPException(
            status_code=403, detail=ORG_DELETED_DETAIL if org is not None and org.deleted_at is not None else AWAITING_APPROVAL,
        )


def rejection_reason(session: Session, org_id: UUID) -> Optional[str]:
    """The operator's stated reason for rejecting `org_id`, stripped and capped; None when there is none."""
    # `select`, never `session.get`: the read seam scopes a select, and a PK get would bypass it.
    signup = session.scalar(select(OrgSignup).where(OrgSignup.org_id == org_id))
    reason = (signup.rejection_reason or "").strip() if signup is not None else ""
    return reason[:REASON_MAX] or None


def sign_in_refusal(session: Session, org_id: UUID) -> Optional[str]:
    """Why a verified identity in `org_id` may not sign in: `org_deleted` (soft-deleted), `org_not_approved`
    (rejected), `org_pending` (awaiting review), or None (active, or no such org). The one decision the doors
    and the funnel read."""
    # Imported here: the sign-in doors in `app/login_oauth.py`'s orbit import this module.
    from app.login_oauth import ORG_DELETED, ORG_NOT_APPROVED, ORG_PENDING_REVIEW

    org = _org(session, org_id)
    if org is None:
        return None
    if org.deleted_at is not None:
        return ORG_DELETED
    return {ORG_REJECTED: ORG_NOT_APPROVED, ORG_PENDING: ORG_PENDING_REVIEW}.get(org.lifecycle)


def refuse_unapproved_sign_in(
    session: Session,
    org_id: UUID,
    *,
    redirect_origin: Optional[str] = None,
    provider: Optional[str] = None,
) -> Optional[RedirectResponse]:
    """Refuse a sign-in into a REJECTED or PENDING org (`sign_in_refusal`); None otherwise (the door carries on).

    With `redirect_origin` (a redirect door) the refusal is RETURNED as the 303 to that origin's login page,
    `provider` riding along as `login_provider` when given; an origin outside `console_origins()` goes to
    `frontend_origin()` instead, so no caller can turn this into an open redirect. Without it (a POST door)
    it is RAISED as the 403 carrying `{"code": <the refusal>, ...}` — a rejection with its `reason`.

    Call it only AFTER the door has verified the identity: the refusal names the org's fate, which a
    stranger must not learn."""
    from app.login_oauth import ORG_NOT_APPROVED, console_origins, frontend_origin

    code = sign_in_refusal(session, org_id)
    if code is None:
        return None
    params = {"login_error": code}
    if provider:
        params["login_provider"] = provider
    if code == ORG_NOT_APPROVED:
        # The sign-in doors are `BOOTSTRAP` and bind nothing, so the sign-up read enters the signer's own org
        # (the one just verified); unbound it would read `false()` and drop the operator's reason. The
        # operator plane already reads every org.
        with nullcontext() if seam.current_scope() is seam.OPERATOR else seam.bound_to(org_id):
            reason = rejection_reason(session, org_id)
        if redirect_origin is None:
            raise HTTPException(status_code=403, detail={"code": code, "reason": reason})
        if reason:
            params["login_detail"] = reason
    elif redirect_origin is None:
        raise HTTPException(status_code=403, detail={"code": code})
    origin = redirect_origin if redirect_origin in console_origins() else frontend_origin()
    return RedirectResponse(f"{origin}/login?{urlencode(params)}", status_code=303)
