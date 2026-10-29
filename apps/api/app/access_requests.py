"""Access requests — asking for a role at a scope, who may decide it, and deciding it (access-model C7).

A refused same-org caller is told which role at which scope would let them in (`request_hint`, attached to
the guard's 404/403 by `app/authz/enforce.py::_request_hint`); `create_request` files the ask. Its APPROVERS are
exactly the people `app/access.py::may_manage` lets write that binding — the scope's effective Project
Maintainers (org-wide or on its project) and the org's Owners/Admins; for an
account scope, the Owners/Admins alone (an account Maintainer only dispatches). One rule, two readers: the
Slack fan-out (`approvers`) and the Requests inbox (`pending_for`) cannot disagree about who is asked.

`approve` writes the binding through `access.add_binding` (the one binding write; idempotent) inside the
request row's own `SELECT … FOR UPDATE`, so a double approve, from any surface, yields one binding and the
same row. The expiry is optional and defaults to never (A2).

There is no notifications table (G6): an approver's bell item is a PENDING row they may decide, and the
requester's is a DECIDED row whose `requester_seen_at` is NULL (`routes/inbox.py`). The Slack DM cards are
`app/slack_access_cards.py`'s; every function here leaves committing and Slack to its caller.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import access
from app.access_catalog import (
    LEVEL_PROJECT,
    ROLES,
    ROLES_BY_SLUG,
    SCOPE_ACCOUNT,
    SCOPE_PROJECT,
)
from app.identities import label_expr
from app.models.access import AccessRequest
from app.models.identity import AdminUser
from app.clock import utcnow as _now

log = logging.getLogger("app.access_requests")

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
DECIDED = (APPROVED, DENIED)

#: The scopes a request may name: the ones a refusal can point at. An org role is an Owner's/Admin's to
#: hand out directly, never asked for.
REQUESTABLE_SCOPES = (SCOPE_PROJECT, SCOPE_ACCOUNT)

#: The roles a request may ask for — the project roles, least first (what `least_role` walks).
REQUESTABLE_ROLES = tuple(sorted((r for r in ROLES if r.level == LEVEL_PROJECT), key=lambda r: r.rank))

#: Roles whose holders can decide a request somewhere; `approvers` narrows each by `may_manage`.
_DECIDING_ROLES = frozenset({access.MAINTAINER_ROLE, "org_admin", access.OWNER_ROLE})

#: The longest note a requester may attach — a sentence of context, not a document.
NOTE_MAX = 500

#: The most open requests one person may have at once: each DMs every approver, so an unbounded asker
#: would flood the approvers' Slack and push others' requests out of the pending window below.
MAX_OPEN_PER_REQUESTER = 20

#: How many pending rows one inbox or `pending` read considers before narrowing by `may_manage`.
_PENDING_SCAN = 500

UNKNOWN_REQUEST_DETAIL = "unknown access request"
NOT_REQUESTABLE_DETAIL = "Access can be requested on a project or account."
ALREADY_DENIED_DETAIL = "This request was denied."
ALREADY_APPROVED_DETAIL = "This request was already approved."
TOO_MANY_OPEN_DETAIL = "Too many open requests. Wait for a decision on one first."


# ── The hint on a refusal ─────────────────────────────────────────────────────────────────────────


def _confers(role, scope_type: str, key: str) -> bool:
    """Does a binding of `role` at a `scope_type` scope confer `key` on that same resource? An account
    binding confers `accounts:view` whatever the role (`app/access.py::expand`)."""
    if scope_type == SCOPE_ACCOUNT:
        return key == access.ACCOUNTS_VIEW
    return key in role.keys  # project roles name their keys; none is `ALL_KEYS`


def least_role(scope_type: str, key: str | None) -> str | None:
    """The lowest-ranked project role whose binding at `scope_type` holds `key`, or None when none does
    (a widening or org-only key no request can grant). No key → the least role there is."""
    for role in REQUESTABLE_ROLES:
        if key is None or _confers(role, scope_type, key):
            return role.slug
    return None


def request_hint(session: Session, principal, spec, object_id, key: str | None) -> dict | None:
    """The `request` field a refusal of `key` on `spec`/`object_id` carries, or None.

    ONLY for a project or account of the CALLER'S OWN org: `session` is bound to that org,
    so another org's row and a row that does not exist both resolve to nothing and the refusal body is
    left byte-identical. The accepted disclosure is that a same-org member learns such an id exists —
    never its name. A personal account is never shareable, so it is never hinted."""
    if principal is None or principal.is_operator or spec is None or spec.name not in REQUESTABLE_SCOPES:
        return None
    try:
        scope = access.resolve_scope(session, spec.name, object_id)
    except HTTPException:
        return None
    if scope.org_id != principal.org_id or scope.personal:
        return None
    role = least_role(scope.type, key)
    if role is None:
        return None
    return {"scope": {"type": scope.type, "id": str(scope.id)}, "role": role}


# ── Who decides a request ─────────────────────────────────────────────────────────────────────────


def scope_of(session: Session, row: AccessRequest) -> access.Scope:
    """The resolved scope a request names (404 when it no longer resolves under this session's org)."""
    return access.binding_scope(session, row)  # the same exactly-one scope columns as a binding


def approvers(session: Session, scope: access.Scope, role_slug: str, *, exclude: UUID | None = None) -> list[AdminUser]:
    """Every user who may decide a `role_slug` request at `scope` — the effective holders of a deciding
    role there (inheritance and groups expanded, `access.members`), each confirmed by `may_manage` on
    their own principal, so this list IS the `pending_for` rule. Ordered by name; `exclude` drops the
    requester."""
    from app.authz.principal import principal_for_user

    candidate_ids: list[UUID] = []
    for row in access.members(session, scope):
        principal = row["principal"]
        if (
            principal["kind"] == "user" and row["status"] == "active"
            and row["role"]["slug"] in _DECIDING_ROLES
            and principal["id"] != exclude and principal["id"] not in candidate_ids
        ):
            candidate_ids.append(principal["id"])
    if not candidate_ids:
        return []
    users = session.scalars(
        select(AdminUser).where(AdminUser.id.in_(candidate_ids), AdminUser.is_system.is_(False))
        .order_by(label_expr(), AdminUser.id)
    ).all()
    return [u for u in users if access.may_manage(principal_for_user(session, u), scope, role_slug)]


def _role_slug(row: AccessRequest) -> str | None:
    role = access.ROLES_BY_ID.get(row.role_id)
    return role.slug if role is not None else None


def may_decide(session: Session, principal, row: AccessRequest, scope: access.Scope | None = None) -> bool:
    """May `principal` approve or deny `row`? `may_manage` of the binding approving it would write."""
    slug = _role_slug(row)
    if slug is None:
        return False
    try:
        scope = scope or scope_of(session, row)
    except HTTPException:
        return False
    return access.may_manage(principal, scope, slug)


# ── Filing a request ──────────────────────────────────────────────────────────────────────────────


def create_request(
    session: Session, requester: AdminUser, *, scope_type: str, scope_id: object,
    role_slug: str | None, note: str | None,
) -> tuple[AccessRequest, bool]:
    """File `requester`'s ask for `role_slug` at the scope — `(row, created)`. One open request per
    (requester, role, scope) (`uq_access_requests_pending`): an existing pending row is returned with
    `created=False`. No role → the least one (`least_role`). 404 for a scope this org cannot read; 422 for
    an org scope, a personal account, or a role the scope cannot carry; 429 past
    `MAX_OPEN_PER_REQUESTER`. The caller commits."""
    from app.actors import stamp_actor

    if scope_type not in REQUESTABLE_SCOPES:
        raise HTTPException(status_code=422, detail=NOT_REQUESTABLE_DETAIL)
    scope = access.resolve_scope(session, scope_type, scope_id)
    if scope.personal:
        raise HTTPException(status_code=422, detail=access.PERSONAL_ACCOUNT_DETAIL)
    slug = role_slug or least_role(scope.type, None)
    role = ROLES_BY_SLUG.get(slug or "")
    if role is None or role.level != LEVEL_PROJECT or scope.type not in role.valid_scopes:
        raise HTTPException(status_code=422, detail=f"unknown role {role_slug!r}")
    note = (note or "").strip()[:NOTE_MAX] or None

    def pending():
        column = getattr(AccessRequest, access.SCOPE_COLUMNS[scope.type])
        return session.scalar(select(AccessRequest).where(
            AccessRequest.requester_id == requester.id, AccessRequest.role_id == role.id,
            column == scope.id, AccessRequest.state == PENDING,
        ))

    existing = pending()
    if existing is not None:
        return existing, False
    open_count = session.scalar(select(func.count()).select_from(AccessRequest).where(
        AccessRequest.requester_id == requester.id, AccessRequest.state == PENDING,
    ))
    if open_count >= MAX_OPEN_PER_REQUESTER:
        raise HTTPException(status_code=429, detail=TOO_MANY_OPEN_DETAIL)
    row = AccessRequest(
        org_id=scope.org_id, requester_id=requester.id, role_id=role.id, note=note, state=PENDING,
        **{access.SCOPE_COLUMNS[scope.type]: scope.id},
    )
    stamp_actor(row, requester, created=True)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        # A concurrent identical ask won the partial unique key: the same answer as finding it above.
        existing = pending()
        if existing is None:
            raise
        return existing, False
    return row, True


# ── Deciding one ──────────────────────────────────────────────────────────────────────────────────


def _locked(session: Session, request_id: UUID) -> AccessRequest | None:
    return session.scalar(
        select(AccessRequest).where(AccessRequest.id == request_id).with_for_update()
        .execution_options(populate_existing=True)
    )


def decide(
    session: Session, actor: AdminUser, actor_principal, request_id: UUID, verdict: str, *,
    expires_at: datetime | None = None,
) -> tuple[AccessRequest, bool]:
    """Approve or deny one request as `actor` — `(row, changed)`. THE ONE DECISION every surface (the
    console route, the Slack button) takes.

    Under the row's own lock: 404 for a request this caller may not decide (or none — the same answer);
    a repeat of the verdict already reached returns the row unchanged (`changed=False`), so a double
    approve writes one binding; the opposite verdict is a 409. Approving writes the binding through
    `access.add_binding` (idempotent, audited) with `expires_at`, never forced (A2). The caller commits."""
    if verdict not in DECIDED:
        raise HTTPException(status_code=422, detail="a decision is approve or deny")
    row = _locked(session, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail=UNKNOWN_REQUEST_DETAIL)
    try:
        scope = scope_of(session, row)
    except HTTPException:
        raise HTTPException(status_code=404, detail=UNKNOWN_REQUEST_DETAIL)
    if not may_decide(session, actor_principal, row, scope):
        raise HTTPException(status_code=404, detail=UNKNOWN_REQUEST_DETAIL)
    if row.state == verdict:
        return row, False
    if row.state != PENDING:
        raise HTTPException(
            status_code=409, detail=ALREADY_DENIED_DETAIL if row.state == DENIED else ALREADY_APPROVED_DETAIL
        )
    if verdict == APPROVED:
        holder = access.resolve_holder(session, "user", row.requester_id)
        binding, _created = access.add_binding(
            session, actor, actor_principal,
            principal=holder, role_slug=_role_slug(row) or "", scope=scope, expires_at=expires_at,
        )
        row.binding_id = binding.id
    row.state = verdict
    row.decided_by_id = actor.id
    row.decided_at = _now()
    row.requester_seen_at = None
    row.updated_at = row.decided_at
    from app.actors import stamp_actor

    stamp_actor(row, actor)
    session.flush()
    return row, True


# ── Reads: the boxes, and the wire form ───────────────────────────────────────────────────────────


def pending_for(session: Session, principal, *, limit: int = 200) -> list[tuple[AccessRequest, access.Scope]]:
    """Every pending request `principal` may decide, newest first — the approver's inbox and the
    `pending` box. Narrowed by `may_decide` after the read, so it is the approvers rule exactly."""
    rows = session.scalars(
        select(AccessRequest).where(AccessRequest.state == PENDING)
        .order_by(AccessRequest.created_at.desc(), AccessRequest.id.desc()).limit(_PENDING_SCAN)
    ).all()
    return _decidable(session, principal, rows)[:limit]


def history_for(session: Session, principal, *, limit: int = 200) -> list[tuple[AccessRequest, access.Scope]]:
    """Decided requests `principal` may decide — the approver's history, newest decision first."""
    rows = session.scalars(
        select(AccessRequest).where(AccessRequest.state.in_(DECIDED))
        .order_by(AccessRequest.decided_at.desc().nulls_last(), AccessRequest.id.desc()).limit(_PENDING_SCAN)
    ).all()
    return _decidable(session, principal, rows)[:limit]


def mine(session: Session, user_id: UUID, *, limit: int = 200) -> list[AccessRequest]:
    """The caller's own requests, every state, newest first."""
    return list(session.scalars(
        select(AccessRequest).where(AccessRequest.requester_id == user_id)
        .order_by(AccessRequest.created_at.desc(), AccessRequest.id.desc()).limit(limit)
    ).all())


def decisions(session: Session, user_id: UUID, *, unread_only: bool = True, limit: int = 200) -> list[AccessRequest]:
    """The caller's decided requests — the requester's bell items: those not seen yet, or every one."""
    query = (
        select(AccessRequest)
        .where(AccessRequest.requester_id == user_id, AccessRequest.state.in_(DECIDED))
        .order_by(AccessRequest.decided_at.desc().nulls_last(), AccessRequest.id.desc())
        .limit(limit)
    )
    if unread_only:
        query = query.where(AccessRequest.requester_seen_at.is_(None))
    return list(session.scalars(query).all())


def mark_decisions_seen(session: Session, user_id: UUID, request_ids: Iterable[UUID]) -> int:
    """Stamp the caller's own decided requests seen. Scoped to the requester in the statement."""
    ids = list(request_ids)[:200]
    if not ids:
        return 0
    rows = session.scalars(select(AccessRequest).where(
        AccessRequest.id.in_(ids), AccessRequest.requester_id == user_id,
        AccessRequest.state.in_(DECIDED), AccessRequest.requester_seen_at.is_(None),
    )).all()
    now = _now()
    for row in rows:
        row.requester_seen_at = now
    return len(rows)


def mark_decisions_unseen(session: Session, user_id: UUID, request_ids: Iterable[UUID]) -> int:
    """Clear the caller's seen stamp on their own decided requests — "mark as unread"."""
    ids = list(request_ids)[:200]
    if not ids:
        return 0
    rows = session.scalars(select(AccessRequest).where(
        AccessRequest.id.in_(ids), AccessRequest.requester_id == user_id,
        AccessRequest.state.in_(DECIDED), AccessRequest.requester_seen_at.is_not(None),
    )).all()
    for row in rows:
        row.requester_seen_at = None
    return len(rows)


def _decidable(session: Session, principal, rows) -> list[tuple[AccessRequest, access.Scope]]:
    scopes: dict[tuple[str, object], access.Scope | None] = {}
    out = []
    for row in rows:
        key = access._scope_of(row)
        if key not in scopes:
            try:
                scopes[key] = access.resolve_scope(session, *key)
            except HTTPException:
                scopes[key] = None
        scope = scopes[key]
        if scope is not None and may_decide(session, principal, row, scope):
            out.append((row, scope))
    return out


def names_of(session: Session, user_ids: Iterable[UUID | None]) -> dict[UUID, str]:
    ids = sorted({uid for uid in user_ids if uid is not None}, key=str)
    if not ids:
        return {}
    return dict(session.execute(select(AdminUser.id, label_expr()).where(AdminUser.id.in_(ids))).all())


def wire(row: AccessRequest, *, scope: access.Scope | None, names: dict[UUID, str], show_name: bool) -> dict:
    """One request as the API serves it. The scope's NAME only when `show_name` (the caller may decide or
    read the scope): a requester who could not see the project learns no name by asking for it."""
    role = access.ROLES_BY_ID.get(row.role_id)
    scope_type, scope_id = access._scope_of(row)
    return {
        "id": row.id,
        "requester": {"id": row.requester_id, "name": names.get(row.requester_id, "")},
        "role": {"slug": role.slug if role else "", "name": role.name if role else ""},
        "scope": {
            "type": scope_type, "id": str(scope_id) if scope_id is not None else None,
            "name": scope.name if (scope is not None and show_name) else None,
        },
        "note": row.note,
        "state": row.state,
        "created_at": row.created_at,
        "decided_at": row.decided_at,
        "decided_by": (
            {"id": row.decided_by_id, "name": names.get(row.decided_by_id, "")}
            if row.decided_by_id is not None else None
        ),
        "binding_id": row.binding_id,
    }


def readable_scope(session: Session, principal, row: AccessRequest) -> tuple[access.Scope | None, bool]:
    """The row's scope (None if it no longer resolves) and whether `principal` may see its name."""
    try:
        scope = scope_of(session, row)
    except HTTPException:
        return None, False
    return scope, access.may_read_scope(principal, scope) or may_decide(session, principal, row, scope)
