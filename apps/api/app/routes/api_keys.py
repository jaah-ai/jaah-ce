"""Jaah API keys: the console's management API. Every rule is `app/api_keys.py`'s.

All `@org_admin` on the caller's own org and `@session_only`: a key never manages keys. Every Org Owner and Org Admin
lists every key of the org (never a secret) and may revoke any; only the owner edits or rotates one. Every one of them
may create a key, the operator too — in their own org, or acting as a tenant org, where the key runs as that org's
Owner seat (`app/api_keys.py`); only a tenant org's superadmin may not (its keys never work). The secret is answered
once, by create and rotate, with `Cache-Control: no-store`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import api_keys
from app.actors import actor_names, stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin, session_only
from app.authz.principal import Principal, current_principal
from app.clock import utcnow
from app.db import get_session
from app.entity_shape import iso_or_none, row_by_uuid_or_404
from app.models.api_keys import ApiKey, ApiKeySecret
from app.models.identity import AdminUser
from app.tenancy.ids import is_operator_plane
from app.user_activity import record_write

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])

_NAME_MAX = 100
_DESCRIPTION_MAX = 1000


class PersonRef(BaseModel):
    id: UUID
    label: str


class ApiKeyOut(BaseModel):
    id: UUID
    name: str
    description: str
    owner: PersonRef
    #: `jaah_…<last4>` of the current secret: never the secret.
    display: str
    grants_all: bool
    permissions: list[str]
    org_admin: bool
    ip_allowlist: list[str]
    expires_at: Optional[datetime]
    created_at: datetime
    created_by: PersonRef
    updated_at: datetime
    last_used_at: Optional[datetime]
    last_used_ip: Optional[str]
    status: Literal["active", "expiring", "expired", "revoked", "suspended"]
    revoked_at: Optional[datetime]
    revoked_by: Optional[PersonRef]
    revoke_reason: Optional[str]
    #: When the secret replaced by an overlapping rotation stops working, while it still works.
    previous_secret_valid_until: Optional[datetime]
    can_edit: bool
    can_revoke: bool


class ApiKeySecretOut(BaseModel):
    key: ApiKeyOut
    #: The secret, shown this once.
    secret: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    description: str = Field(default="", max_length=_DESCRIPTION_MAX)
    grants_all: bool = True
    permissions: list[str] = Field(default_factory=list)
    org_admin: bool = True
    ip_allowlist: list[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None


class ApiKeyUpdate(BaseModel):
    """No `expires_at`: a key's expiry is fixed at create and cannot be extended."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)
    grants_all: Optional[bool] = None
    permissions: Optional[list[str]] = None
    org_admin: Optional[bool] = None
    ip_allowlist: Optional[list[str]] = None


class ApiKeyRotate(BaseModel):
    overlap: Literal["none", "1h", "24h", "7d"] = "none"


# ── Helpers ───────────────────────────────────────────────────────────────────────────────────────────


def _out(session: Session, row: ApiKey, caller: AdminUser, principal: Principal) -> ApiKeyOut:
    now = utcnow()
    # An operator's key in a tenant org: "<name>, acting as <org>" to them, "Jaah support" to the org.
    names = actor_names(session, (row.owner_id, row.created_by_id, row.revoked_by_id), name_acting=True)
    secrets = session.scalars(select(ApiKeySecret).where(ApiKeySecret.api_key_id == row.id)).all()
    current = next((s for s in sorted(secrets, key=lambda s: s.created_at, reverse=True) if s.valid_until is None), None)
    overlapping = [s.valid_until for s in secrets if s.valid_until is not None and s.valid_until > now]
    revoked_by = PersonRef(id=row.revoked_by_id, label=names.get(row.revoked_by_id, "")) if row.revoked_by_id else None
    owner_ok = api_keys.owner_ok(session, api_keys.owner_row(session, row.owner_id), row.org_id)
    status = api_keys.status(row, owner_ok=owner_ok, now=now)
    live = status not in ("revoked", "expired")
    return ApiKeyOut(
        id=row.id, name=row.name, description=row.description,
        owner=PersonRef(id=row.owner_id, label=names.get(row.owner_id, "")),
        display=api_keys.display(current.last4) if current is not None else api_keys.PREFIX,
        grants_all=row.grants_all,
        permissions=sorted(set(row.permissions or ()) & api_keys.PERMISSION_KEYS),
        org_admin=row.org_admin,
        ip_allowlist=[str(cidr) for cidr in row.ip_allowlist or ()],
        expires_at=row.expires_at, created_at=row.created_at,
        created_by=PersonRef(id=row.created_by_id, label=names.get(row.created_by_id, "")),
        updated_at=row.updated_at, last_used_at=row.last_used_at,
        last_used_ip=str(row.last_used_ip) if row.last_used_ip is not None else None,
        status=status, revoked_at=row.revoked_at, revoked_by=revoked_by, revoke_reason=row.revoke_reason,
        previous_secret_valid_until=max(overlapping) if overlapping else None,
        can_edit=live and _is_owner(row, caller, principal),
        can_revoke=row.revoked_at is None,
    )


def _is_owner(row: ApiKey, caller: AdminUser, principal: Principal) -> bool:
    """The caller's own key in the org they are in — an acting operator's seat keeps their id."""
    return row.owner_id == caller.id and row.org_id == principal.org_id


def _key(session: Session, key_id: str, principal: Principal) -> ApiKey:
    """The org's key, or 404 (another org's key is a 404 too)."""
    row = row_by_uuid_or_404(session, ApiKey, key_id, detail="unknown API key")
    if row.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail="unknown API key")
    return row


def _owned(session: Session, key_id: str, caller: AdminUser, principal: Principal) -> ApiKey:
    """`_key`, for the owner's own changes: 403 for anyone else, 409 once the key has ended."""
    row = _key(session, key_id, principal)
    if not _is_owner(row, caller, principal):
        raise HTTPException(status_code=403, detail="Only the key's owner can change it. You can revoke it.")
    if row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= utcnow()):
        raise HTTPException(status_code=409, detail="This key has ended. Create a new one.")
    return row


def _validated(check, value):
    try:
        return check(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _audit_data(row: ApiKey, **extra) -> dict:
    return {"api_key_id": row.id, "api_key_name": row.name, **extra}


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ApiKeyOut])
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def list_api_keys(
    caller: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[ApiKeyOut]:
    """Every key of the org, newest first."""
    rows = session.scalars(
        select(ApiKey).where(ApiKey.org_id == principal.org_id).order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
    ).all()
    return [_out(session, row, caller, principal) for row in rows]


@router.post("", response_model=ApiKeySecretOut, status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def create_api_key(
    body: ApiKeyCreate,
    response: Response,
    caller: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ApiKeySecretOut:
    """A new key owned by the caller in the org they are in (acting: the acted org); the secret is answered once."""
    if caller.is_superadmin and not is_operator_plane(caller.org_id, caller.is_superadmin):
        raise HTTPException(status_code=403, detail="A superadmin of this organization cannot own an API key.")
    if body.expires_at is not None:
        if body.expires_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="expires_at needs a time zone offset.")
        if body.expires_at <= utcnow():
            raise HTTPException(status_code=422, detail="The expiry must be in the future.")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name the key.")
    row = ApiKey(
        org_id=principal.org_id, owner_id=caller.id, name=name, description=body.description.strip(),
        grants_all=body.grants_all,
        permissions=_validated(api_keys.validate_permissions, body.permissions),
        org_admin=body.org_admin,
        ip_allowlist=_validated(api_keys.validate_allowlist, body.ip_allowlist),
        expires_at=body.expires_at,
    )
    stamp_actor(row, caller, created=True)
    session.add(row)
    session.flush()
    secret = api_keys.add_secret(session, row)
    record_write(
        session, caller, "api_key_created", f"Created API key {row.name}",
        data=_audit_data(
            row, display=api_keys.display(secret[-4:]), grants_all=row.grants_all, org_admin=row.org_admin,
            permissions=list(row.permissions), ip_allowlist=list(row.ip_allowlist),
            expires_at=iso_or_none(row.expires_at),
        ),
    )
    session.commit()
    session.refresh(row)
    response.headers["Cache-Control"] = "no-store"
    return ApiKeySecretOut(key=_out(session, row, caller, principal), secret=secret)


@router.get("/{key_id}", response_model=ApiKeyOut)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def get_api_key(
    key_id: str,
    caller: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ApiKeyOut:
    return _out(session, _key(session, key_id, principal), caller, principal)


@router.patch("/{key_id}", response_model=ApiKeyOut)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def edit_api_key(
    key_id: str,
    body: ApiKeyUpdate,
    caller: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ApiKeyOut:
    """Edit in place — the secret is unchanged and the change holds from the next request. Owner only."""
    row = _owned(session, key_id, caller, principal)
    provided = body.model_fields_set
    wanted: dict = {}
    if "name" in provided and body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name the key.")
        wanted["name"] = name
    if "description" in provided and body.description is not None:
        wanted["description"] = body.description.strip()
    for flag in ("grants_all", "org_admin"):
        if flag in provided and getattr(body, flag) is not None:
            wanted[flag] = getattr(body, flag)
    if "permissions" in provided and body.permissions is not None:
        wanted["permissions"] = _validated(api_keys.validate_permissions, body.permissions)
    if "ip_allowlist" in provided and body.ip_allowlist is not None:
        wanted["ip_allowlist"] = _validated(api_keys.validate_allowlist, body.ip_allowlist)
    changed = sorted(
        field for field, value in wanted.items()
        if (sorted(map(str, getattr(row, field) or ())) if isinstance(value, list) else getattr(row, field))
        != (sorted(value) if isinstance(value, list) else value)
    )
    if changed:
        for field in changed:
            setattr(row, field, wanted[field])
        stamp_actor(row, caller)
        record_write(
            session, caller, "api_key_edited", f"Edited API key {row.name} ({', '.join(changed)})",
            data=_audit_data(row, changed=changed),
        )
        session.commit()
        session.refresh(row)
    return _out(session, row, caller, principal)


@router.post("/{key_id}/rotate", response_model=ApiKeySecretOut)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def rotate_api_key(
    key_id: str,
    body: ApiKeyRotate,
    response: Response,
    caller: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ApiKeySecretOut:
    """A new secret, same settings and expiry; the old one keeps working for `overlap`. Owner only; 409 once ended."""
    row = _owned(session, key_id, caller, principal)
    secret = api_keys.rotate(session, row, body.overlap)
    stamp_actor(row, caller)
    row.updated_at = utcnow()
    record_write(
        session, caller, "api_key_rotated", f"Rotated API key {row.name} (overlap {body.overlap})",
        data=_audit_data(row, overlap=body.overlap, display=api_keys.display(secret[-4:])),
    )
    session.commit()
    session.refresh(row)
    response.headers["Cache-Control"] = "no-store"
    return ApiKeySecretOut(key=_out(session, row, caller, principal), secret=secret)


@router.post("/{key_id}/revoke", response_model=ApiKeyOut)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def revoke_api_key(
    key_id: str,
    caller: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ApiKeyOut:
    """End a key now, for good. Any Org Owner or Admin; idempotent."""
    row = _key(session, key_id, principal)
    if api_keys.revoke(row, caller):
        # An operator-owned key's owner reads None here (their row is outside the org): no address reaches the org's audit.
        owner = api_keys.user_row(session, row.owner_id)
        record_write(
            session, caller, "api_key_revoked", f"Revoked API key {row.name}",
            data=_audit_data(row, reason=api_keys.REVOKED, owner_email=owner.email if owner is not None else None),
        )
        session.commit()
        session.refresh(row)
    return _out(session, row, caller, principal)
