"""Which of the caller's OTHER orgs holds an object — the deep-link interstitial's one question (R9/AC13).

`GET /api/auth/locate/{object_type}/{object_id}` (`routes/auth.py::locate_object`) asks it when a detail page
404s under the active org. The answer is ONE org — id and name — and nothing of the object itself: no
title, no content, no count. Everything else is the plain 404, byte-identical whatever the cause (unknown
type, malformed or missing id, an org the person holds no live membership in, a disabled membership, an org
that may not be switched into, the active org itself, or an object their membership there could not open).

How an org is asked, and why it cannot leak: each candidate is one of the person's OWN live memberships
other than the active one, admitted by the same gate the switch applies (`login_funnel.switch_target`). Its
org is entered with `seam.bind_from` that membership's own row, on a fresh session (a session belongs to one
org for its whole life, `seam.check_session_stamp`), and there the object is probed with THAT membership's
own principal through
`authz.can_access` — the probe the guard runs. So an org the person is not a member of is never read, and
the interstitial never promises an object the switch would then refuse to open. The seam's per-class
criteria are the manifest's derivation (`app/tenancy/manifest.py`, `Stored` and `Derived` alike), which is
why every probed type must be tenant-owned: checked below at import, since a shared class would be found
under every org.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import identities, login_funnel
from app.authz.object_access import can_access, key_scope_satisfied
from app.authz.principal import principal_for_user
from app.authz.visibility import OBJECT_SPEC_BY_NAME, ObjectAccess
from app.db import SessionLocal
from app.models.identity import AdminUser
from app.tenancy import seam
from app.tenancy.labels import OrgRef, org_refs
from app.tenancy.manifest import TENANT_OWNED

_SHARED_TYPES = sorted(name for name, spec in OBJECT_SPEC_BY_NAME.items() if spec.entity not in TENANT_OWNED)
if _SHARED_TYPES:
    raise RuntimeError(
        f"object type(s) {_SHARED_TYPES} are not tenant-owned, so an org-bound probe would find them in EVERY "
        "org and the locate answer would name the wrong one (app/org_locate.py)"
    )

#: The key each READ-key-scoped type's detail route declares (`ObjectSpec.key_scoped_modes`): the
#: guard asks it on the row's own scope beside `can_access`, so the probe does too — else a B member without
#: it is offered an object the switch would then 404. A feature reads as its project's tasks (D9).
_DETAIL_READ_KEY = {"project": "projects:view", "feature": "tasks:view"}
_UNKEYED = sorted(
    name for name, spec in OBJECT_SPEC_BY_NAME.items()
    if spec.key_scoped_in(ObjectAccess.READ) and name not in _DETAIL_READ_KEY
)
if _UNKEYED:
    raise RuntimeError(f"object type(s) {_UNKEYED} key-scope READ but name no detail key here (app/org_locate.py)")

#: Postgres `bigint`: a larger integer id is no row, never a driver error (a 500 would break the one 404).
_MAX_INT_ID = 2**63 - 1


def _parsed_id(object_type: str, raw: str) -> Optional[int | UUID]:
    """`raw` in the type's id domain, or None — a malformed id is a not-found, never a 422."""
    spec = OBJECT_SPEC_BY_NAME.get(object_type)
    if spec is None:
        return None
    try:
        parsed = spec.id_type(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, int) and not 0 < parsed <= _MAX_INT_ID:
        return None
    return parsed


def _opens_in(member: AdminUser, object_type: str, object_id: int | UUID) -> bool:
    """Could `member` (a membership in its own org) open this object? Asked inside that org alone."""
    with SessionLocal() as session, seam.bind_from(session, AdminUser, member.id):
        principal = principal_for_user(session, member)
        key = _DETAIL_READ_KEY.get(object_type)
        return can_access(session, principal, object_type, object_id, mode=ObjectAccess.READ) and (
            key is None or key_scope_satisfied(
                session, principal, OBJECT_SPEC_BY_NAME[object_type], object_id, key=key, mode=ObjectAccess.READ
            )
        )


def locate(session: Session, caller: AdminUser, object_type: str, raw_id: str) -> Optional[OrgRef]:
    """The org — of `caller`'s person's live memberships OTHER than `caller`'s own — whose membership could
    open the object, or None (the caller's 404)."""
    object_id = _parsed_id(object_type, raw_id)
    if object_id is None or caller.identity_id is None:
        return None
    for row in identities.memberships(session, caller.identity_id):
        if row.org_id == caller.org_id:
            continue
        member = login_funnel.switch_target(session, caller, row.org_id)
        if member is not None and _opens_in(member, object_type, object_id):
            return org_refs(session, [member.org_id]).get(member.org_id)
    return None
