"""The tenancy seam's durable exclusion log — one browsable, filtered, counted view of
`tenancy_exclusions`.

`GET /api/tenancy-exclusions`: one page of the buckets the seam recorded when a scoped read returned
less than asked (or a write carried no org), plus counts, filter vocabularies and the retention window.

Why a table beside the seam's console log: that log is deduped per (reason, class) per process, so it
answers "which paths", never "how often" or "still". With the seam enforcing, a rising bucket here is
a defect report — a real caller reading less than it asked for.

Rows are aggregated per (class, scope_kind, org, route, minute) under `uq_tenancy_exclusions_bucket`
(`NULLS NOT DISTINCT`), so write volume tracks distinct excluding paths, not traffic. The trade: no
per-request row (that trail is `activity_events`, `GET /api/audit`).

`scope_kind` separates a working boundary (`org`) from broken callers (`unbound`, `identity_denied`,
…); `mode` separates excluded reads from unattributable writes (strictly worse: silent and permanent).

The table is `Shared` in the census and carries no `org_id`: the operator needs the whole population,
and the most important rows have no org. Flat disclosure: org names and route/cron identifiers of other
tenants, never tenant data.

Gate: `@superadmin_only(Tenancy.OPERATOR)`. A grantable `system:view` would be a cross-org read the
tenancy axis cannot narrow (no `org_id` to filter). Verdict per route:
`test_authz_policy.py::_OPERATOR_PLANE_ALLOWLIST`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_superadmin
from app.authz.catalog import Tenancy
from app.authz.declarations import superadmin_only
from app.cron_retention import activity_events_retention_days
from app.db import get_session
from app.models.identity import AdminUser
from app.models.orgs import Org, TenancyExclusion
from app.routes.audit import _require_aware
from app.tenancy import exclusion_log
from app.tenancy.exclusion_log import ALL_RECORDED_SCOPE_KINDS, MODES
from app.tenancy.manifest import TENANT_OWNED
from app.tenancy.seam import seam_mode
from app.entity_shape import UuidRefOut, from_row, optional_uuid


router = APIRouter(prefix="/api/tenancy-exclusions", tags=["tenancy"])

#: `bound_org=unbound` on the wire means `bound_org IS NULL`: a sentinel option in the org picker (as
#: `routes/audit.py`'s `SYSTEM_ACTOR`), never a valid uuid. Deliberately not `SCOPE_KIND_UNBOUND`: a
#: wire value and a stored value must not share a constant.
UNBOUND_ORG = "unbound"

#: The label an item carries when `bound_org IS NULL` — the same word the filter uses.
UNBOUND_ORG_LABEL = "unbound"

_PER_MAX = 200
_OFFSET_MAX = 100_000


# ── Wire shapes ───────────────────────────────────────────────────────────────────────────────────


class TenancyExclusionOrgOut(UuidRefOut):
    """One option in the org filter. The page adds its own "unbound" choice (`UNBOUND_ORG`); no
    synthetic row here, which would be indistinguishable from a real org named "unbound"."""


class TenancyExclusionOut(BaseModel):
    """One bucket — a (class, scope_kind, org, route, minute) tuple and how many rows it excluded.

    `occurred_at` is the start of the bucket's minute, never the instant of an exclusion.
    `bound_org_label` is served so a human reads a name, not a uuid (see `_org_label`)."""

    id: int
    occurred_at: datetime
    mapped_class: str
    scope_kind: str
    bound_org: Optional[UUID] = None
    bound_org_label: str
    route_or_job: str
    mode: str
    excluded_count: int


class TenancyExclusionsPageOut(BaseModel):
    """One page, both counts, the filter vocabularies and the window the log covers.

    `total` is the filtered count; `total_unfiltered` distinguishes "nothing matches these filters"
    from "nothing recorded" and is guaranteed only when `total == 0` (a cost contract: the table is
    large and unindexed for `count(*)`; the endpoint body holds the arms).

    `covering_since` is never null and is `max(retention floor, log_started_at)`, minute-floored: the
    empty state prints "no exclusions since <covering_since>", which must not claim pruned or
    never-gathered evidence (`app/tenancy/exclusion_log.py::LOG_STARTED_AT_KEY`).

    `writer_ok`/`writer_detail` tell an empty log from a failing writer (process-local;
    `exclusion_log.WriterHealth`). `classes`, `orgs`, `scope_kinds`, `modes` come from the closed sets
    that define them, so options never drift or vanish with retention. `seam_mode` says whether a row is
    a prediction (`shadow`) or a report of rows withheld (`enforcing`)."""

    items: list[TenancyExclusionOut] = Field(default_factory=list)
    total: int
    total_unfiltered: int
    covering_since: datetime
    retention_days: int
    log_started_at: Optional[datetime] = None
    latest_occurred_at: Optional[datetime] = None
    writer_ok: bool = True
    writer_detail: Optional[str] = None
    classes: list[str] = Field(default_factory=list)
    orgs: list[TenancyExclusionOrgOut] = Field(default_factory=list)
    scope_kinds: list[str] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)
    seam_mode: str


# ── Filters ───────────────────────────────────────────────────────────────────────────────────────


def _org_label(bound_org: Optional[UUID], names: dict[UUID, str]) -> str:
    """What a human reads where the uuid is: `unbound` for NULL, the org's name, or the bare uuid for a
    deleted org (the log outlives it; never blank). Collapsed here so page and filter agree.
    """
    if bound_org is None:
        return UNBOUND_ORG_LABEL
    return names.get(bound_org) or str(bound_org)


def _floor_to_minute(moment: datetime) -> datetime:
    """`moment` with seconds and microseconds dropped — the resolution `occurred_at` is stored at.

    Mirrors `exclusion_log._minute` on the write side, minus its zone normalisation and now-default."""
    return moment.replace(second=0, microsecond=0)


def _writer_detail(health) -> Optional[str]:
    """The writer's state as one sentence an operator can act on, or None when it is healthy.

    Scoped to the API process serving this request (replicas flush independently). Carries only what
    `exclusion_log._first_line` let through — this string reaches a browser."""
    if health.ok:
        return None
    last_ok = health.last_ok_at.isoformat() if health.last_ok_at else "never in this process"
    detail = (
        f"{health.consecutive_failures} consecutive flush failures in the API process serving this "
        f"page (last success: {last_ok}). Counts recorded since then are lost, so an empty result "
        f"here is NOT evidence that the seam excluded nothing."
    )
    return f"{detail} Last error — {health.last_error}" if health.last_error else detail


def _filters(
    *,
    mapped_class: Optional[str],
    bound_org: Optional[UUID],
    unbound_only: bool,
    scope_kind: Optional[str],
    mode: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
) -> list[Any]:
    """The operator's filters as a clause list, shared by the page query and `total` so the two agree.

    `bound_org` and `unbound_only` are mutually exclusive; two clauses because `= NULL` does not work."""
    clauses: list[Any] = []
    if mapped_class:
        clauses.append(TenancyExclusion.mapped_class == mapped_class)
    if unbound_only:
        clauses.append(TenancyExclusion.bound_org.is_(None))
    elif bound_org is not None:
        clauses.append(TenancyExclusion.bound_org == bound_org)
    if scope_kind:
        clauses.append(TenancyExclusion.scope_kind == scope_kind)
    if mode:
        clauses.append(TenancyExclusion.mode == mode)
    # Half-open window: minute buckets tile only if one bound is exclusive.
    if start is not None:
        clauses.append(TenancyExclusion.occurred_at >= start)
    if end is not None:
        clauses.append(TenancyExclusion.occurred_at < end)
    return clauses


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=TenancyExclusionsPageOut)
@superadmin_only(Tenancy.OPERATOR)
def browse_tenancy_exclusions(
    mapped_class: Optional[str] = Query(
        default=None,
        max_length=64,
        description=(
            "Exact match on the ORM class name, e.g. 'Task'. One of the values `classes` returns; a "
            "value not in that list matches nothing rather than 422ing, because the list is derived "
            "from the table's live contents and a class can age out of it between two page loads. "
            "CLEARED MEANS OMITTED — an empty value is read as no filter."
        ),
    ),
    bound_org: Optional[str] = Query(
        default=None,
        # Bounded so an arbitrary string is not echoed back in the 422 detail.
        max_length=64,
        description=(
            "An org uuid, or the literal 'unbound' for the buckets where NO org was bound "
            "(bound_org IS NULL) — the value the org picker's 'unbound' option carries. Anything else "
            "that is not a uuid is a 422. CLEARED MEANS OMITTED."
        ),
    ),
    scope_kind: Optional[str] = Query(
        default=None,
        max_length=64,
        description=(
            "One of the values `scope_kinds` returns: 'unbound' (nothing bound an org — for a read the "
            "predicate is false() and it returns nothing once the seam enforces; for a write there was "
            "no org to stamp), 'identity_denied' (inside the identity escape but not one of its "
            "classes), 'derived_unstamped' (a WRITE ONLY: an org was bound, but the row derives its "
            "org from a parent that has none, so there was nothing to inherit), 'operator' (the "
            "fleet-wide plane — a read is not narrowed and never appears here, a WRITE is left "
            "unstamped and does), or 'org' (a bounded read narrowed to its own "
            "tenant, which is the boundary working; a write under an org bind is stamped, so it never "
            "appears here). Which values a row can carry therefore depends on its `mode`. Anything else "
            "is a 422, because unlike `mapped_class` this is a CLOSED vocabulary the server owns. "
            "CLEARED MEANS OMITTED."
        ),
    ),
    mode: Optional[str] = Query(
        default=None,
        max_length=64,
        description=(
            "One of the values `modes` returns: 'read' (a query returned fewer rows) or "
            "'unbound_write' (a write carried no org and cannot be attributed to one). A closed "
            "vocabulary, so anything else is a 422. CLEARED MEANS OMITTED."
        ),
    ),
    start: Optional[datetime] = Query(
        default=None,
        description=(
            "INCLUSIVE lower bound on occurred_at. ISO-8601 WITH AN EXPLICIT OFFSET (…+02:00 or …Z); a "
            "zoneless value is refused with 422 rather than read in a guessed zone. CLEARED MEANS "
            "OMITTED: drop the key, do not send it empty — '?start=' is a 422 pydantic raises before "
            "this handler is entered."
        ),
    ),
    end: Optional[datetime] = Query(
        default=None,
        description=(
            "EXCLUSIVE upper bound on occurred_at — half-open so two adjacent windows cannot both "
            "claim the bucket on the boundary. Same explicit-offset and cleared-means-omitted "
            "requirements as start."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=_PER_MAX),
    # Bounded: Postgres reads and discards every skipped row.
    offset: int = Query(default=0, ge=0, le=_OFFSET_MAX),
    _actor: AdminUser = Depends(require_superadmin),
    session: Session = Depends(get_session),
) -> TenancyExclusionsPageOut:
    """One page of the durable exclusion log, with everything the page needs to be honest about it.

    Flat: the caller reads every org's buckets (see the module docstring).

    Validation is asymmetric: `scope_kind` and `mode` are closed server vocabularies, so an unknown
    value is a 422 (silently matching nothing would read as "the seam is clean"); `mapped_class` is
    open, since a stale-but-valid filter must not break the page.

    `covering_since` is `max(now - retention_days, log_started_at)` — deliberately not the oldest bucket
    present, which would shrink the claimed window as the log went quiet.

    Ordered `occurred_at DESC, id DESC`: minute ties are the normal case, and `id` makes the order total
    so paging neither duplicates nor skips.
    """
    # An empty string is a cleared control (`scope_kind=`), not a filter. Typed params cannot arrive
    # empty (pydantic refuses them), so the client owes omission there.
    mapped_class = mapped_class or None
    bound_org = bound_org or None
    scope_kind = scope_kind or None
    mode = mode or None

    # Validated against the union across both modes (`ALL_RECORDED_SCOPE_KINDS`).
    if scope_kind is not None and scope_kind not in ALL_RECORDED_SCOPE_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown scope_kind '{scope_kind}' — expected one of "
                f"{', '.join(ALL_RECORDED_SCOPE_KINDS)}"
            ),
        )
    if mode is not None and mode not in MODES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown mode '{mode}' — expected one of {', '.join(MODES)}",
        )

    unbound_only = bound_org == UNBOUND_ORG
    org_filter: Optional[UUID] = None
    if bound_org is not None and not unbound_only:
        org_filter = optional_uuid(bound_org)
        if org_filter is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'bound_org' must be an org uuid or the literal '{UNBOUND_ORG}' for the buckets "
                    f"where no org was bound, got '{bound_org}'"
                ),
            )

    # One contract for zoneless wire timestamps, imported from `routes/audit.py`.
    start = _require_aware(start, "start")
    end = _require_aware(end, "end")

    clauses = _filters(
        mapped_class=mapped_class,
        bound_org=org_filter,
        unbound_only=unbound_only,
        scope_kind=scope_kind,
        mode=mode,
        start=start,
        end=end,
    )

    # Resolved first: the windowed count reads `covering_since`.
    retention_days = activity_events_retention_days()
    # The later of the two terms (see the response model).
    retention_floor = datetime.now(timezone.utc) - timedelta(days=retention_days)
    started_at = exclusion_log.log_started_at()
    # Floored to the minute, or the first bucket of the log's starting minute is excluded.
    covering_since = _floor_to_minute(
        max(retention_floor, started_at) if started_at else retention_floor
    )

    total = session.scalar(
        select(func.count()).select_from(TenancyExclusion).where(*clauses)
    ) or 0
    # Unfiltered answers "has anything been recorded". Computed only in the one arm that reads it (a
    # filter narrowed the set to nothing); with no filter it is `total` exactly, and with a matching
    # filter the page never reads it.
    if not clauses:
        total_unfiltered = total
    elif total == 0:
        # Windowed, matching the page's "the retained window holds N" and covering sweep arrears; also a
        # range scan on the bucket index's leading `occurred_at`.
        total_unfiltered = session.scalar(
            select(func.count())
            .select_from(TenancyExclusion)
            .where(TenancyExclusion.occurred_at >= covering_since)
        ) or 0
    else:
        total_unfiltered = total

    rows = session.scalars(
        select(TenancyExclusion)
        .where(*clauses)
        .order_by(TenancyExclusion.occurred_at.desc(), TenancyExclusion.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()

    # Over the unfiltered set, so it answers "is the seam still excluding anything". A backwards scan
    # on the leading `occurred_at` index column.
    latest_occurred_at = session.scalar(select(func.max(TenancyExclusion.occurred_at)))

    # Option lists from the closed sets that define them (`TENANT_OWNED`, the `orgs` axis table), not
    # `SELECT DISTINCT` over the log: no whole-index passes, stable pickers, and aged-out options stay
    # selectable.
    classes = sorted(cls.__name__ for cls in TENANT_OWNED)
    all_orgs = session.execute(select(Org.id, Org.name, Org.deleted_at).order_by(Org.name)).all()
    # A soft-deleted org is offered as no option, but still names its own past rows.
    option_orgs = [(org_id, name) for org_id, name, deleted_at in all_orgs if deleted_at is None]
    names: dict[UUID, str] = {org_id: name for org_id, name, _ in all_orgs}

    orgs = [
        TenancyExclusionOrgOut(id=org_id, name=_org_label(org_id, names))
        for org_id, _name in option_orgs
    ]

    health = exclusion_log.writer_health()
    return TenancyExclusionsPageOut(
        items=[
            from_row(TenancyExclusionOut, row, bound_org_label=_org_label(row.bound_org, names))
            for row in rows
        ],
        total=total,
        total_unfiltered=total_unfiltered,
        covering_since=covering_since,
        retention_days=retention_days,
        log_started_at=started_at,
        latest_occurred_at=latest_occurred_at,
        writer_ok=health.ok,
        writer_detail=_writer_detail(health),
        classes=classes,
        orgs=orgs,
        scope_kinds=list(ALL_RECORDED_SCOPE_KINDS),
        modes=list(MODES),
        seam_mode=seam_mode().value,
    )
