"""An operator acting AS a tenant org: one tab of the operator's console served as that org's Owner
seat — every route, every plane — until the tab leaves it.

THE HEADER IS THE WHOLE STATE. The tab sends `X-Jaah-Act-As: <org id>` on every `/api` request
(`apps/web/src/lib/actAs.ts`). Nothing is stored server-side and the session cookie is untouched, so another
tab, another browser and the next sign-in stay on the operator plane until they send it too.

WHO MAY: an operator (`is_operator_plane`). From anyone else the header is IGNORED — never an error, never a
widening — so a crafted header changes nothing for a tenant (the `X-Jaah-Org-Scope` rule, `authz/`).

WHAT IT BECOMES: the request's principal is the target org's Owner seat (`authz.owner_seat`) and
`require_user` hands handlers a detached copy of the operator's row carrying that org and no superadmin flag
(`seat_user`). So every decision downstream — the guard's plane, `entity_shape.tenant_org_or_404`,
`require_operator`, a create's stamped org — answers exactly as it does for that org's Owner, and the operator's
own routes refuse as they do for one.

WHO DID IT: the person stays the operator (`user_id`, the audit row's actor). Every audit row is filed in the
target org with `data.acting_as` naming the operator's own org too (`user_activity.record_write`, the write
capture), so a row says who did it and on whose behalf.

A target that is not a live tenant org (malformed, unknown, disabled, the operator org itself) is refused 409
`act_as_unavailable` on every request, so the tab leaves act-as instead of being served as anything.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Mapping, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity_shape import optional_uuid
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.tenancy import seam
from app.tenancy.ids import is_operator_plane, is_tenant_org

ACT_AS_HEADER = "x-jaah-act-as"
#: The 409 detail for a target that cannot be acted as; the web leaves act-as on it.
ACT_AS_UNAVAILABLE = "act_as_unavailable"

#: Set on a seat copy (never on a persistent row): the operator's own org, the half of "on whose behalf" the
#: copy's `org_id` no longer carries.
_OPERATOR_ORG_ATTR = "_act_as_operator_org"
#: Set on a seat copy too: the persistent row it was copied from (`person`).
_PERSON_ATTR = "_act_as_person"

#: The guard's answer for THIS request: True when its reader is an operator's tab acting as an org, False when
#: the guard resolved it as not acting, `None` when no guard resolved it (off a request, or an `@always`/infra route
#: the guard answers without a principal). Set once by `authz._bind_read_scope`, never outside a request. Read by
#: `actors.actor_names`, so the operator reads a colleague's name where the org reads "Jaah support", and by
#: `request_caller.current`.
_READER_ACTING: ContextVar[Optional[bool]] = ContextVar("act_as_reader_acting", default=None)


def mark_reader(acting: bool) -> None:
    """Record whether this request's reader is an acting operator. **`async def` ONLY**, as `seam.bind`: a
    `ContextVar` set from a worker thread never reaches the handler. Set, never restored: a request's context is
    discarded whole."""
    _READER_ACTING.set(acting)


def reader_acting() -> bool:
    """True when this request's reader is an operator acting as an org (`mark_reader`). Attribution only,
    never an access decision."""
    return _READER_ACTING.get() is True


def reader_resolved() -> bool:
    """True when the guard decided this request's acting state (`mark_reader`), either way."""
    return _READER_ACTING.get() is not None


def requested(headers: Mapping[str, str]) -> Optional[str]:
    """The raw header value, or `None` when absent or blank."""
    raw = (headers.get(ACT_AS_HEADER) or "").strip()
    return raw or None


def target_org(session: Session, user: AdminUser, raw: Optional[str]) -> Optional[UUID]:
    """The org `user` acts as on this request, or `None` when it does not act.

    `None` for no header and for a caller who is not an operator (ignored, never refused). An operator naming
    anything but a live tenant org is refused 409 `act_as_unavailable`. `orgs` is not tenant-owned, so the read
    needs no scope."""
    if raw is None or not is_operator_plane(user.org_id, user.is_superadmin):
        return None
    org_id = optional_uuid(raw)
    if org_id is None:
        raise HTTPException(status_code=409, detail=ACT_AS_UNAVAILABLE)
    org = session.get(Org, org_id)
    if org is None or not org.enabled or not is_tenant_org(org.id):
        raise HTTPException(status_code=409, detail=ACT_AS_UNAVAILABLE)
    return org.id


def _detached_copy(user: AdminUser) -> AdminUser:
    columns: dict[str, Any] = {attr.key: getattr(user, attr.key) for attr in sa_inspect(AdminUser).column_attrs}
    return AdminUser(**columns)


def seat_user(user: AdminUser, org_id: UUID) -> AdminUser:
    """A DETACHED copy of the operator's row as `org_id`'s Owner seat: same id and email (it is still them),
    that org, no superadmin flag. Never added to a session — writing the operator's own row (profile,
    preferences) goes through the real row, not this one."""
    seat = _detached_copy(user)
    seat.org_id = org_id
    seat.is_superadmin = False
    setattr(seat, _OPERATOR_ORG_ATTR, user.org_id)
    setattr(seat, _PERSON_ATTR, user)
    return seat


def plain_user(user: AdminUser) -> AdminUser:
    """A DETACHED copy of `user` without the superadmin flag — an operator's API key that does not carry their
    rights (`api_keys.key_user`). Same org, so no act-as audit; and no `person` link, so `require_operator_acting`
    refuses it as every operator gate does."""
    copy = _detached_copy(user)
    copy.is_superadmin = False
    return copy


def person(user: AdminUser) -> AdminUser:
    """The operator's own, persistent row behind a seat copy (`seat_user`), else `user` itself — for whatever is
    about the PERSON rather than the org: minting their session cookie, their second factor, their devices."""
    return getattr(user, _PERSON_ATTR, user)


def own_row(session: Session, principal: Any) -> Optional[AdminUser]:
    """The caller's own `AdminUser` row for a handler that reads it off the principal under the principal's org —
    as the seat (`seat_user`) when the principal is an acting operator, whose row lives in their own org and so is
    invisible under the one acted as."""
    if not getattr(principal, "acting", False):
        return session.scalar(select(AdminUser).where(AdminUser.id == principal.user_id))
    with seam.identity_scope():
        row = session.get(AdminUser, principal.user_id)
    return seat_user(row, principal.org_id) if row is not None else None


def operator_org(user: Optional[AdminUser]) -> Optional[UUID]:
    """The operator's own org when `user` is a seat copy (`seat_user`), else `None` — the "is this act-as?"
    question every audit writer asks."""
    return getattr(user, _OPERATOR_ORG_ATTR, None) if user is not None else None


def audit_data(org_id: UUID, operator_org_id: UUID) -> dict[str, str]:
    """`data.acting_as` on an audit row written while acting: the org acted as and the operator's own."""
    return {"org_id": str(org_id), "operator_org_id": str(operator_org_id)}
