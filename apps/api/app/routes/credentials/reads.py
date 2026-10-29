"""Credential read routes — the roster, its `/types` and `/owners` options, the seal key, NodeVault state and one row."""
from __future__ import annotations

import base64
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, literal, literal_column, or_, select
from sqlalchemy.orm import Session

from app.actors import actor_names as resolve_actor_names
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.credentials.registry import CREDENTIAL_TYPES
from app import credential_consumers, credential_keys, nodevault_policy
from app.credential_keys import WRAP_ALGORITHM
from app.credentials.testers import NODEVAULT_UNTESTABLE
from app.models.credentials import (
    Credential,
    NV_KEY_CURRENT,
    NodeVaultKey,
)
from app.models.identity import AdminUser, UserIdentity
from app.models.orgs import Org
from app.tenancy.labels import org_refs
from app.tenancy.seam import bound_org
from app.routes.credentials.guards import _VIEW_KEY, _may_edit, _org_of, _scoped_row_or_404
from app.list_page import like_pattern
from app.routes.credentials import roster
from app.routes.credentials.roster import (
    ROSTER_OWNER,
    RosterHealth,
    RosterSecrets,
    RosterFilters,
    RosterSort,
    _uuid_array,
    capped_count,
    keyset_page,
    reads_as_operator,
    roster_filters_from_query,
    roster_scope,
    roster_where,
)
from app.routes.credentials.credential_wire import (
    in_edition,
    CredentialListOut,
    CredentialOut,
    CredentialTypeOut,
    FieldSpecOut,
    SealKeyOut,
    _NAME_MAX_LEN,
    _actor_names,
    _connect_out,
    _credential_out,
    _home_labels,
    _host_field_key,
    _untestable_reason_out,
)


router = APIRouter(tags=["credentials"])


_PAGE_MAX = 1_000_000


#: The OFFSET path's ORDER BY per `sort` (`roster.RosterSort`), each ending in `id` so OFFSET pages over a TOTAL
#: order (`test_list_pagination_is_a_total_order`). `org` orders by the tenant's display name, joined outer so a
#: dangling `org_id` (no FK) sorts last rather than dropping out of the page while `total` counts it. The keyset
#: path's keys are `roster._KEYS`.
_ROSTER_ORDER: dict[str, tuple[Any, ...]] = {
    "newest": (Credential.created_at.desc(), Credential.id.desc()),
    "oldest": (Credential.created_at.asc(), Credential.id.asc()),
    "name": (func.lower(Credential.name), Credential.id),
    "type": (Credential.type, func.lower(Credential.name), Credential.id),
    # `Org.id` after the name, as the keyset walk: two orgs may share a name but for case.
    "org": (func.lower(Org.name).asc().nulls_last(), Org.id, func.lower(Credential.name), Credential.id),
}


def _ordered(page_select: Select, sort: str) -> Select:
    """`page_select` in `sort`'s order; only `org` needs a join (it reads `orgs.name`)."""
    if sort == "org":
        page_select = page_select.outerjoin(Org, Org.id == Credential.org_id)
    return page_select.order_by(*_ROSTER_ORDER[sort])


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/credentials", response_model=CredentialListOut)
@feature("credentials", "view", own_credential=True)
def list_credentials(
    page: int = Query(default=1, ge=1, le=_PAGE_MAX),
    per: int = Query(default=25, ge=1, le=200),
    # `type_` avoids shadowing the builtin. `q` is bounded (an unbounded `LIKE '%…%'`); pydantic constraints are
    # safe on query parameters, which never carry a secret.
    type_: Optional[str] = Query(default=None, alias="type"),
    q: Optional[str] = Query(default=None, max_length=_NAME_MAX_LEN),
    # Which connections cannot authenticate: the stored `has_stored_secret`. A `Literal`, so an unknown value is a
    # 422 rather than an unfiltered page.
    secrets: Optional[RosterSecrets] = Query(default=None),
    # Only the credentials I brought, for every principal including a superadmin (authorship is not a
    # capability). Server-side because `CredentialOut.created_by` is a display name, not an id.
    mine: bool = Query(default=False),
    # The owner narrowing by user id (from `GET /api/credentials/owners`), which `mine` cannot express; repeatable,
    # the owners a union. `created_by_id` is its former single-valued name, read only when `owner_id` is absent.
    owner_id: list[UUID] = Query(default=[]),
    created_by_id: Optional[UUID] = Query(default=None),
    # A UUID or the literal `none`, so `str`; parsed by `roster_filters_from_query`, which 422s anything else.
    org_id: Optional[str] = Query(default=None),
    # Expired, expiring within 7 days, never used, last test failed (`roster._health`).
    health: Optional[RosterHealth] = Query(default=None),
    # The page's order (`_ROSTER_ORDER`, `roster._KEYS`); a `Literal`, so an unknown value is a 422.
    sort: RosterSort = Query(default="newest"),
    # Keyset paging (`roster.keyset_page`): `cursor` from a previous page's `next_cursor`, or `paging=keyset` for
    # the first page. Neither: the OFFSET path with its exact `total`, for `page`-based callers.
    paging: Optional[Literal["keyset"]] = Query(default=None),
    cursor: Optional[str] = Query(default=None, max_length=2048),
    _user: AdminUser = Depends(require_user),
    # No `require_superadmin`: the gate is `credentials:view` plus the scoped select, and
    # `own_credential=True` admits a keyless session, which `roster_scope` narrows to rows it brought.
    # Scoping reads this frozen principal.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CredentialListOut:
    """One page of the roster in `sort`'s order (newest first by default), plus its `total`.

    Keyset (`cursor`, or `paging=keyset`): `per + 1` rows after the cursor, `next_cursor` null on the last page,
    and a `total` capped at `roster.ROSTER_COUNT_CAP` (`total_capped`). OFFSET (neither): `page` over the same
    order with an exact `total`; every order ends in `id`, a total order, and `page` is bounded above, since
    `(page - 1) * per` could overflow int8.

    Every filter is `roster.roster_where`'s, applied to the page and the `total` alike."""
    if (cursor is not None or paging == "keyset") and page != 1:
        raise HTTPException(status_code=422, detail="cursor and page cannot be combined")
    filters = roster_filters_from_query(
        type_slug=type_, q=q, owner_ids=owner_id or ([created_by_id] if created_by_id is not None else []),
        org_id=org_id, secrets=secrets, health=health, mine=mine,
    )
    # Both statements are scoped: an unscoped count leaks how many credentials exist and strands Next.
    where = roster_where(session, principal, filters)
    next_cursor: Optional[str] = None
    total_capped = False
    if cursor is not None or paging == "keyset":
        rows, next_cursor = keyset_page(session, where, filters, sort, per, cursor)
        total, total_capped = capped_count(session, where)
    else:
        total = session.scalar(select(func.count()).select_from(Credential).where(*where)) or 0
        rows = session.scalars(
            _ordered(select(Credential).where(*where), sort).offset((page - 1) * per).limit(per)
        ).all()
    # One query for all actor names on the page, never per row.
    actors = _actor_names(session, rows)
    # One query for all org labels (`app/tenancy/labels.py::org_refs`); unknown ids are simply absent.
    orgs = org_refs(session, [row.org_id for row in rows])
    # And one each for the home prefixes' project names and owner emails.
    homes = _home_labels(session, rows)
    return CredentialListOut(
        items=[
            # The server's per-row edit verdict (`_may_edit` is pure).
            _credential_out(
                row, actor_names=actors, org_labels=orgs, home_labels=homes,
                editable=_may_edit(principal, row),
            )
            for row in rows
        ],
        total=total, total_capped=total_capped, next_cursor=next_cursor, page=page, per=per,
    )


# Declared before `/api/credentials/{credential_id}`: routes match in order, and the typed parameter
# would swallow `/seal-key` with a 422. Same admission as `/types`: whoever may render the form (a
# keyless member saving their own credential included) must be able to seal what they type. The key is
# PUBLIC; the private half never leaves KMS (or the local provider), so serving it is not a disclosure.
@router.get("/api/credentials/seal-key", response_model=SealKeyOut)
@feature("credentials", "view", own_credential=True)
def get_credential_seal_key(user: AdminUser = Depends(require_user)) -> SealKeyOut:
    """The deployment's secret-wrapping public key (`app/credential_keys.py`), base64 SPKI DER, plus the
    key id every envelope stamps. Cached in-process after the first `GetPublicKey`; a 503 (never a 500
    with a body) when KMS cannot be reached, so the form withholds Save rather than posting plaintext."""
    provider = credential_keys.provider()
    try:
        spki = provider.public_key_der()
    except Exception as exc:  # noqa: BLE001 - the class name is the whole diagnosis wanted here
        # `KmsError`/`KmsUnreachableError` (transport class name only) or a local-key defect. Nothing of
        # a key reaches the message.
        raise HTTPException(
            status_code=503,
            detail=f"the secret-wrapping key is unavailable ({type(exc).__name__})",
        ) from None
    return SealKeyOut(
        key_id=provider.key_id,
        algorithm=WRAP_ALGORITHM,
        public_key=base64.b64encode(spki).decode("ascii"),
    )


# Same ordering rule for `/types`.
@router.get("/api/credentials/types", response_model=list[CredentialTypeOut])
@feature("credentials", "view", own_credential=True)
def list_credential_types(
    user: AdminUser = Depends(require_user),
    # Relaxed from `require_superadmin`: serves the in-process catalog, needed by the roster
    # and by a keyless user bringing their own credential. Narrowed by the caller's plane, hence `user`.
) -> list[CredentialTypeOut]:
    """The type/field schema, in dropdown order — server-owned and fetched, never mirrored in
    TypeScript. The form renders its fields from this, so a new type is one edit to
    `app/credentials/registry.py`.

    Every org is served every type: no credential type is operator-only, so a row of any type can be
    created and edited wherever its org may hold credentials. An edition omits the types of a feature it lacks (`credential_wire.in_edition`)."""
    return [
        CredentialTypeOut(
            slug=spec.slug,
            label=spec.label,
            testable=spec.testable,
            host_field=_host_field_key(spec),
            default_base_url=spec.default_base_url,
            untestable_reason=_untestable_reason_out(
                spec, connect_served=_connect_out(spec.slug) is not None
            ),
            oauth_connect=_connect_out(spec.slug),
            require_one_of=[list(group) for group in spec.require_one_of],
            fields=[FieldSpecOut.model_validate(field) for field in spec.fields],
        )
        for spec in CREDENTIAL_TYPES.values()
        if in_edition(spec.slug)
    ]


# ── The two filter rosters ────────────────────────────────────────────────────
# Declared before `/api/credentials/{credential_id}` (see `/types`). Neither carries `own_credential=True`, deliberately: a keyless session may see its own rows, but not
# a roster of people and their actor ids. Such a caller gets `mine` instead. Both are also scoped via
# `roster_scope`, so the key never widens which rows are counted.


class CredentialOwnerOut(BaseModel):
    """One entry of the roster's Owner filter: who to offer, what to draw, and how many rows they own.

    `id` is the filter value for `?owner_id=`; this is the only place a client can get an owner's
    actor id (list rows carry names). `credential_count` counts the caller's unfiltered roster, not the
    current page, so an option is never removed by the filter that hid it (as `routes/tasks.py::
    task_creators`). On the search path it is capped at `roster.ROSTER_COUNT_CAP`, and `count_capped` says so
    (the full list's counts are exact, `count_capped` always false)."""

    id: UUID
    name: str
    credential_count: int
    count_capped: bool = False


#: The search path's page: `limit` defaults to this, and neither `limit` nor `ids` may exceed the max.
_OWNER_SEARCH_DEFAULT = 20
_OWNER_SEARCH_MAX = 50

#: An owner's "First Last", lowercased: the expression `ix_identities_name_trgm` indexes (revision 202610240400),
#: spelled with literal constants (not bound parameters) so the planner matches it to the index.
_OWNER_NAME = func.lower(
    func.coalesce(UserIdentity.first_name, literal_column("''"))
    .op("||")(literal_column("' '"))
    .op("||")(func.coalesce(UserIdentity.last_name, literal_column("''")))
)


def owner_search_statement(
    principal: Principal | None, q: Optional[str], ids: list[UUID], limit: int,
) -> Select:
    """The Owner filter's search: up to `limit` owner ids, each owning at least one credential in the caller's
    roster scope (`roster.roster_scope`, the full list's own clause, so no scope widens). `q` (stripped, non-empty)
    matches the owner's "First Last" or email by substring through their trigram indexes; `ids` narrows to those
    owners. The email arm is the operator's (`roster.reads_as_operator`, as the roster's own search): a tenant
    caller matches an email only where it IS the owner's label (no name), so no member's email is probed beyond
    what the full list already shows. Prefix matches first, then the display name ("First Last", else the email), then `id`. Selected
    through `AdminUser`, so a tenant caller's seam filter applies as on the full list."""
    owns = (
        select(literal(1)).select_from(Credential)
        .where(roster_scope(principal, mine=False), ROSTER_OWNER == AdminUser.id)
        .exists()
    )
    email = func.lower(UserIdentity.email)
    unnamed = func.btrim(_OWNER_NAME) == ""
    stmt = select(AdminUser.id).join(UserIdentity, UserIdentity.id == AdminUser.identity_id).where(owns)
    order: list[Any] = []
    if q:
        pattern = like_pattern(q.lower())
        # `like_pattern`'s pattern without its leading `%`: "starts with q".
        prefix = pattern[1:]
        operator = reads_as_operator(principal, RosterFilters())

        def email_like(p: str) -> Any:
            hit = email.like(p, escape="\\")
            return hit if operator else and_(unnamed, hit)

        stmt = stmt.where(or_(email_like(pattern), _OWNER_NAME.like(pattern, escape="\\")))
        order.append(case((or_(
            email_like(prefix),
            func.lower(UserIdentity.first_name).like(prefix, escape="\\"),
            func.lower(UserIdentity.last_name).like(prefix, escape="\\"),
            _OWNER_NAME.like(prefix, escape="\\"),
        ), 0), else_=1))
    if ids:
        stmt = stmt.where(AdminUser.id == _uuid_array(ids))
    display = case((unnamed, email), else_=func.btrim(_OWNER_NAME))
    return stmt.order_by(*order, display, AdminUser.id).limit(limit)


def owner_counts_statement(principal: Principal | None, ids: list[UUID], cap: int) -> Select:
    """`(owner id, count)` for each of `ids`: that owner's credentials in the caller's scope, counted over at most
    `cap + 1` rows each (`ix_credentials_owner_expr`), so one prolific owner never costs a full count."""
    capped = (
        select(literal(1)).select_from(Credential)
        .where(roster_scope(principal, mine=False), ROSTER_OWNER == AdminUser.id)
        .limit(cap + 1).correlate(AdminUser).subquery()
    )
    counted = select(func.count()).select_from(capped).scalar_subquery()
    return select(AdminUser.id, counted).where(AdminUser.id == _uuid_array(ids))


@router.get("/api/credentials/owners", response_model=list[CredentialOwnerOut])
@feature("credentials", "view")
def list_credential_owners(
    # `q` is bounded like the roster's (an unbounded `LIKE '%…%'`); NUL is a 422 (`roster_filters_from_query`).
    q: Optional[str] = Query(default=None, max_length=_NAME_MAX_LEN),
    limit: Optional[int] = Query(default=None, ge=1, le=_OWNER_SEARCH_MAX),
    # A stored selection's owners, resolved to names; repeatable.
    ids: list[UUID] = Query(default=[]),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[CredentialOwnerOut]:
    """Who owns the credentials this caller can see, with each one's count — the Owner filter's options.

    "Who owns a credential I can reach", not "on this page". No zero-count self entry (unlike
    `routes/tasks.py::task_creators`): `?mine=true` covers that.

    No parameters: every owner, ordered by count DESC then name, never by recency, so the visible entries do not
    reshuffle. Any of `q`, `ids`, `limit`: the search path (`owner_search_statement`) — at most `limit` owners
    (default 20, max 50; with `ids` alone, `len(ids)`), prefix matches first then by name, NOT by count (A13): a
    GROUP BY over 1M rows on every keystroke is unaffordable. Counts are then taken for the shown owners only,
    each capped at `roster.ROSTER_COUNT_CAP` (`count_capped`).

    Scoped with `roster_scope`'s count clause around `Credential` (not `page_select.subquery()`), the
    module's own idiom, which also keeps `config` out of the statement.
    `test_the_owner_roster_aggregate_is_reached_by_the_read_seam` holds the read seam's coverage.
    """
    count_clause = roster_scope(principal, mine=False)
    needle = roster_filters_from_query(q=q).q
    if len(ids) > _OWNER_SEARCH_MAX:
        raise HTTPException(status_code=422, detail=f"ids takes at most {_OWNER_SEARCH_MAX} values")
    if needle is not None or ids or limit is not None:
        if limit is None:
            limit = len(ids) if ids and needle is None else _OWNER_SEARCH_DEFAULT
        found = list(session.scalars(owner_search_statement(principal, needle, ids, limit)).all())
        if not found:
            return []
        cap = roster.ROSTER_COUNT_CAP
        counts = dict(session.execute(owner_counts_statement(principal, found, cap)).all())
        labels = resolve_actor_names(session, found)
        return [
            CredentialOwnerOut(
                id=owner_id, name=labels.get(owner_id, ""),
                credential_count=min(counts.get(owner_id, 0), cap), count_capped=counts.get(owner_id, 0) > cap,
            )
            for owner_id in found
        ]
    rows = session.execute(
        select(AdminUser.id, func.count())
        .select_from(Credential)
        .where(count_clause)
        # An inner join drops nothing: `ROSTER_OWNER` falls back to `created_by_id`, NOT NULL with a
        # restricting FK. Machine-written rows carry the system actor, which is a real entry here.
        .join(AdminUser, AdminUser.id == ROSTER_OWNER)
        # Grouped by the id alone: two people may share a label (`identities.person_label`),
        # so the label is resolved per owner afterwards, never a grouping key.
        .group_by(AdminUser.id)
    ).all()
    labels = resolve_actor_names(session, [owner_id for owner_id, _count in rows])
    # `id` last, so the order is total (display names are not unique).
    ordered = sorted(rows, key=lambda r: (-r[1], labels.get(r[0], ""), str(r[0])))
    return [
        CredentialOwnerOut(id=owner_id, name=labels.get(owner_id, ""), credential_count=count)
        for owner_id, count in ordered
    ]


class NodeVaultTypeOut(BaseModel):
    eligible: bool
    reason: Optional[str] = None


class NodeVaultStateOut(BaseModel):
    """What the credential form needs to render the NodeVault checkbox (arch §4.1)."""

    state: Literal["not_setup", "active", "frozen", "not_in_plan"]
    current_key_hash: Optional[str] = None
    types: dict[str, NodeVaultTypeOut]
    # Why Test is disabled on a NodeVault credential: the credential form shows it, served so it never copies
    # the sentence (`credentials/testers.py::NODEVAULT_UNTESTABLE`).
    untestable_reason: str


class NodeVaultEligibilityOut(BaseModel):
    eligible: bool
    reason: Optional[str] = None
    consumers: list[str]


# Declared before `/api/credentials/{credential_id}`: a literal segment must win over the UUID parameter.
@router.get("/api/credentials/nodevault-state", response_model=NodeVaultStateOut)
@feature("credentials", "view", own_credential=True)
def get_nodevault_state(
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> NodeVaultStateOut:
    """The caller's org's NodeVault state, its current key hash, and which types may be NodeVault. An
    operator's read binds the fleet plane (no bound org), so their own org answers."""
    org = _org_of(session, bound_org() or _user.org_id)
    policy = nodevault_policy.status(session, org) if org is not None else None
    if policy is None or (not policy.allowed and not policy.setup):
        state = "not_in_plan"
    elif policy.frozen:
        state = "frozen"
    else:
        state = "active" if policy.setup else "not_setup"
    current = (
        session.scalar(select(NodeVaultKey.key_hash).where(
            NodeVaultKey.org_id == org.id, NodeVaultKey.state == NV_KEY_CURRENT
        ))
        if org is not None and policy is not None and policy.setup else None
    )
    types = {}
    for slug in CREDENTIAL_TYPES:
        reason = credential_consumers.type_ineligible_reason(slug)
        types[slug] = NodeVaultTypeOut(eligible=reason is None, reason=reason)
    return NodeVaultStateOut(
        state=state, current_key_hash=current, types=types, untestable_reason=NODEVAULT_UNTESTABLE,
    )


@router.get("/api/credentials/{credential_id}/nodevault-eligibility", response_model=NodeVaultEligibilityOut)
@feature("credentials", "view", own_credential=True)
def get_nodevault_eligibility(
    credential_id: UUID,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> NodeVaultEligibilityOut:
    """Whether this credential may be NodeVault: its type is eligible AND no control-plane consumer opens it
    (D1/D7). `consumers` names them either way, so the form can say what to unhook."""
    row = _scoped_row_or_404(session, principal, credential_id, _VIEW_KEY)
    reason, consumers = credential_consumers.eligibility(session, row)
    return NodeVaultEligibilityOut(eligible=reason is None, reason=reason, consumers=consumers)


@router.get("/api/credentials/{credential_id}", response_model=CredentialOut)
@feature("credentials", "view", own_credential=True)
def get_credential(
    credential_id: UUID,
    _user: AdminUser = Depends(require_user),
    # Relaxed from `require_superadmin`; `own_credential=True` narrows keyless callers to
    # rows they brought.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CredentialOut:
    # Scoped (D13) with the route's own key, like every write (`_scoped_row_or_404`).
    row = _scoped_row_or_404(session, principal, credential_id, _VIEW_KEY)
    return _credential_out(
        row,
        actor_names=_actor_names(session, [row]),
        org_labels=org_refs(session, [row.org_id]),
        home_labels=_home_labels(session, [row]),
        editable=_may_edit(principal, row),
    )
