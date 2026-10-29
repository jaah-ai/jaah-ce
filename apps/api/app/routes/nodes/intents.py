"""The slot-intent queue read: `GET /api/nodes/intents`."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, distinct, false, func, or_, select
from sqlalchemy.orm import Session

from app import fleet_verdicts, node_identity, project_identity
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import node_reach_clause
from app.db import get_session
from app.entity_shape import RefOut, iso_or_none
from app.models.identity import AdminUser
from app.models.worktrees import SlotIntent
from app.node_identity import is_uuid_shaped

router = APIRouter(tags=["nodes"])


class IntentRow(BaseModel):
    """One `slot_intents` row as a human reads it: the queue entry, not the node's executable copy.

    Not `PoolTickIntent` (routes/agent/pool_tick.py), which carries `preconditions`/`payload` (including prompt
    bundles). This carries the diagnosis and no payload, so a console read cannot leak a bundle."""

    id: int
    verb: str
    # The node's name, for display: the queue is read fleet-wide, so each row must name its host. Resolved in
    # one bulk read per page.
    node: str
    # The node's uuid, the identity (`node` is unique per org only).
    node_id: str
    # The project's name, resolved in bulk. None when no project row matches (unreachable via the FK),
    # rendered as unresolved.
    project: Optional[str] = None
    number: int
    slot: str
    reason: Optional[str] = None
    created_at: str
    age_seconds: float
    acked_at: Optional[str] = None
    outcome: Optional[str] = None
    refusals: int
    # Why a row is not moving, as one word: `due`, `withheld` (refused, in backoff) or `retired` (kept
    # until purge). Derived from three columns; the DB never stores the word.
    state: str
    next_attempt_at: Optional[str] = None
    # Seconds until this row may run again; positive only while `withheld`.
    withheld_seconds: Optional[float] = None
    # ── What the refusal tally is counting: both halves of `refusal_key`
    # (`fleet_verdicts.split_refusal_key`) ──────────────────────────────────────────────────────────────
    # `refusal_outcome`: a parked row's `outcome` is rewritten to `parked`, erasing what it refused with.
    # `refusal_detail`: `refusals`/`refusal_key` carry forward onto a re-minted row
    # (`fleet_verdicts.issue_intents`), whose `reason` is the mint's own sentence; the
    # refusing detail survives only in the key. Both None on a row that has never refused.
    refusal_outcome: Optional[str] = None
    refusal_detail: Optional[str] = None


class IntentQueue(BaseModel):
    """The intent queue in view, summarised: the wedge detector.

    Healthy is empty or nearly (60s decision pass, ~5s pool tick). `refusing` is the sharpest number: a
    row refusing identically is parked after `fleet_verdicts.REFUSAL_PARK_AFTER` attempts.

    Counts only; the rows are the page this rides on (`IntentPageOut.counts`), so there is no second
    truncated list. Fleet-wide, counted under the page's own WHERE."""

    pending: int
    # Pending and due now: what the next pool tick serves. `pending - due` is `withheld`.
    due: int
    withheld: int
    # Pending rows that have refused at least once; a subset of `pending`.
    refusing: int
    oldest_pending_age_seconds: Optional[float] = None
    # `{verb: count}` over pending rows only.
    by_verb: dict[str, int] = {}
    # `{outcome: count}` over retired rows still on the table; `parked`/`failed` means failing quietly.
    recent_outcomes: dict[str, int] = {}
    # How far back a retired failure can be seen: `fleet_verdicts.purge_unexecutable_intents` deletes
    # refusing and `superseded`/`parked` rows past this age, but never `done`, `accepted` or
    # `unsupported_verb`. So no `parked` row means none in the last hour, not none ever.
    # Deferred: those three outcomes have no purge arm, so the retired set grows with fleet lifetime and
    # its `ORDER BY created_at` is not index-covered; needs a purge arm or a `(node_id, created_at)` index.
    retention_seconds: float


# ── The intent queue, paged and fleet-wide (`GET /api/nodes/intents`) ──────────────────────────────
# Included before `reads`' `GET /api/nodes/{node_id}` on purpose (`app/main.py`): Starlette matches in
# registration order, so otherwise `intents` would be read as a node id and 404. Fleet-wide, with the node as a `?node=` filter
# over a grant-scoped read, since a wedge is on the node you have not opened. The pagination dialect is
# `GET /api/sessions`'s (1-based `page`, `per`, `total`, `total_unfiltered`).
_INTENTS_PER_PAGE = 25
_INTENTS_PER_MAX = 200
# An unbounded page overflows Postgres' int8 in the offset and 500s (as `routes/credentials/`).
_INTENTS_PAGE_MAX = 1_000_000

# The two sets: one parameter, since they are the same rows under one predicate (`_intent_set_clause`).
_SET_PENDING = "pending"
_SET_RETIRED = "retired"
_INTENT_SETS = (_SET_PENDING, _SET_RETIRED)

# The derived states a caller may filter on: the pending half of `IntentRow.state` only. `retired` is
# the other value of `set`, so offering it here too would allow contradictions (`_intent_filters`).
_INTENT_STATES = ("due", "withheld")

# The length bound on every open-text filter (`routes/sessions.py::_Q_MAX_LEN`): an unbounded needle is
# an unbounded comparison. Safe as a pydantic constraint: a query parameter carries no secret.
_INTENT_FILTER_MAX_LEN = 200


def _intent_row(
    row: SlotIntent, project: Optional[str], node: str, now: datetime, *, retired: bool
) -> IntentRow:
    """One queue entry, diagnosed. `state` is derived here and stored nowhere (see `IntentRow.state`).

    Both names are passed in, resolved in bulk by the caller, since a lookup here is an N+1. `project` is
    Optional (renders as `project#<id>`); `node` is required and the caller decides its fallback."""
    withheld_for: Optional[float] = None
    state = "retired"
    if not retired:
        # A NULL `next_attempt_at` is due now, as the drain reads it; no sentinel date stands in for "no backoff".
        remaining = (
            (row.next_attempt_at - now).total_seconds() if row.next_attempt_at is not None else 0.0
        )
        if remaining > 0:
            state, withheld_for = "withheld", remaining
        else:
            state = "due"
    refusal_outcome, refusal_detail = fleet_verdicts.split_refusal_key(row.refusal_key)
    return IntentRow(
        id=int(row.id),
        verb=row.verb,
        node=node,
        node_id=str(row.node_id),
        project=project,
        number=int(row.number),
        # One readable address for the slot; an unresolved project shows its id.
        slot=f"{project or f'project#{row.project_id}'}/{row.number}",
        reason=row.reason,
        created_at=iso_or_none(row.created_at) or "",
        age_seconds=(now - row.created_at).total_seconds(),
        acked_at=iso_or_none(row.acked_at),
        outcome=row.outcome,
        refusals=int(row.refusals or 0),
        state=state,
        next_attempt_at=iso_or_none(row.next_attempt_at),
        withheld_seconds=withheld_for,
        # Both halves; see `IntentRow.refusal_outcome` (the re-minted row case).
        refusal_outcome=refusal_outcome,
        refusal_detail=refusal_detail,
    )


def _intent_queue(session: Session, node_clauses: list[Any], now: datetime) -> IntentQueue:
    """The slot-intent queue under `node_clauses`, counted (see `IntentQueue` for why no rows).

    Takes a clause list rather than a `Node`: the scope is "every node the principal may see, optionally
    one", and re-deriving it would be a second home for the access rule (`authz.node_reach_clause`). An
    empty list means every row and is reachable only for an unrestricted principal with no filter; zero
    grants is a clause matching nothing, never an absent clause.

    One grouped read keyed on `(verb, outcome)`, split into pending/retired in Python
    (`fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES`, plus the NULL case `NOT IN` gets wrong): both are
    closed vocabularies, so the result is a few dozen rows however large the queue, and one statement
    cannot yield inconsistent halves. Only tallies cross the wire: the retired set never purges successes
    (`purge_unexecutable_intents` has no arm for `done`/`accepted`/`unsupported_verb`), so a per-row fetch
    would grow with fleet lifetime, twice per page (`SlotIntentsTab` mounts two tables)."""
    empty = IntentQueue(
        pending=0, due=0, withheld=0, refusing=0,
        retention_seconds=fleet_verdicts.UNEXECUTABLE_INTENT_MAX_AGE_SECS,
    )
    groups = list(
        session.execute(
            select(
                SlotIntent.verb,
                SlotIntent.outcome,
                func.count().label("tally"),
                # Withheld is `_intent_row`'s own predicate: NULL is due, only a future stamp withholds (`>` against
                # NULL is UNKNOWN, falling to `else_`). They must agree or the stat strip and `state` filter diverge.
                func.sum(case((SlotIntent.next_attempt_at > now, 1), else_=0)).label("withheld"),
                # `refusals > 0` against NULL likewise falls to `else_`, matching `(refusals or 0) > 0`.
                func.sum(case((SlotIntent.refusals > 0, 1), else_=0)).label("refusing"),
                func.min(SlotIntent.created_at).label("oldest"),
            )
            # `select_from` spelled out: a scope clause may be correlated, and without an enclosing FROM of
            # its table a correlated one silently allows all.
            .select_from(SlotIntent)
            .where(*node_clauses)
            .group_by(SlotIntent.verb, SlotIntent.outcome)
        ).all()
    )
    if not groups:
        return empty

    def is_pending(outcome: Optional[str]) -> bool:
        # `outcome IS NULL` spelled separately (as `fleet_verdicts.still_pending`): a NULL never compares out
        # of `not in`, so the never-acked rows a stuck queue is made of would drop.
        return outcome is None or outcome not in fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES

    pending_groups = [g for g in groups if is_pending(g.outcome)]
    retired_groups = [g for g in groups if not is_pending(g.outcome)]
    outcomes: dict[str, int] = {}
    for g in retired_groups:
        key = g.outcome or "unknown"
        outcomes[key] = outcomes.get(key, 0) + int(g.tally)
    # Accumulated: one verb can appear in several pending groups.
    by_verb: dict[str, int] = {}
    for g in pending_groups:
        by_verb[g.verb] = by_verb.get(g.verb, 0) + int(g.tally)
    pending = sum(int(g.tally) for g in pending_groups)
    withheld = sum(int(g.withheld) for g in pending_groups)
    # A min of per-group minima: still the single earliest pending row.
    oldest = min((g.oldest for g in pending_groups), default=None)
    return IntentQueue(
        pending=pending,
        due=pending - withheld,
        withheld=withheld,
        refusing=sum(int(g.refusing) for g in pending_groups),
        oldest_pending_age_seconds=(
            (now - oldest).total_seconds() if oldest is not None else None
        ),
        by_verb=dict(sorted(by_verb.items())),
        recent_outcomes=dict(sorted(outcomes.items())),
        retention_seconds=fleet_verdicts.UNEXECUTABLE_INTENT_MAX_AGE_SECS,
    )


def _intent_set_clause(set_: str) -> Any:
    """`pending` or `retired`, as one SQL predicate over `outcome`.

    The pending arm calls `fleet_verdicts.still_pending()`, the single home of the expression and its NULL
    arm. Retired is the strict complement, spelled as `outcome.in_(...)` (sargable) rather than
    `not_(still_pending())` (a NOT over an OR)."""
    if set_ == _SET_RETIRED:
        return SlotIntent.outcome.in_(list(fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES))
    return fleet_verdicts.still_pending()


def _intent_filters(
    *,
    node_clauses: list[Any],
    set_: str,
    verb: Optional[str],
    outcome: Optional[str],
    state: Optional[str],
    refusing: bool,
    now: datetime,
) -> list[Any]:
    """The narrowing, as clauses applied to the page and the count alike (as
    `routes/sessions.py::_list_filters`), so `total` describes the rows.

    An unrecognised `outcome` or `state` is a 422, never an empty page: both vocabularies are closed, and
    zero rows would falsely say "the fleet has no refusals". `verb` is the exception: rows outlive their
    verb's retirement from `SLOT_INTENT_VERBS` until arm 2 of `fleet_verdicts.purge_unexecutable_intents`
    deletes them, so it is length-bounded and matched as given; the envelope's `verbs` list guides the UI."""
    if outcome is not None:
        if outcome not in fleet_verdicts.SLOT_INTENT_OUTCOMES:
            raise HTTPException(status_code=422, detail=f"unknown outcome '{outcome}'")
        # The outcome must belong to the set asked for: `?set=pending&outcome=parked` selects the empty set by
        # construction, and a 200 would falsely report no parked intents. The UI cannot reach this.
        retiring = outcome in fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES
        if retiring != (set_ == _SET_RETIRED):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"outcome '{outcome}' cannot appear in the '{set_}' set: it is "
                    f"{'a retiring' if retiring else 'a non-retiring'} outcome"
                ),
            )
    if state is not None:
        if state not in _INTENT_STATES:
            raise HTTPException(status_code=422, detail=f"unknown state '{state}'")
        if set_ != _SET_PENDING:
            # Not silently ignored: a filter that quietly does nothing reads as evidence about the rows.
            raise HTTPException(
                status_code=422,
                detail=f"state filters the '{_SET_PENDING}' set only; this set is '{set_}'",
            )
    # The node scope leads, unchanged from the handler, so no query can forget it.
    filters: list[Any] = [*node_clauses, _intent_set_clause(set_)]
    if verb is not None:
        filters.append(SlotIntent.verb == verb)
    if outcome is not None:
        filters.append(SlotIntent.outcome == outcome)
    if state == "withheld":
        filters.append(SlotIntent.next_attempt_at.is_not(None))
        filters.append(SlotIntent.next_attempt_at > now)
    elif state == "due":
        filters.append(
            or_(SlotIntent.next_attempt_at.is_(None), SlotIntent.next_attempt_at <= now)
        )
    if refusing:
        filters.append(SlotIntent.refusals > 0)
    return filters


def _intent_ordering(set_: str) -> list[Any]:
    """The ORDER BY, total in both directions.

    Pending oldest first (the drain's order), retired newest first. Both close on `id`, since OFFSET
    pagination over a partial order can repeat or skip rows."""
    if set_ == _SET_RETIRED:
        return [SlotIntent.created_at.desc(), SlotIntent.id.desc()]
    return [SlotIntent.created_at.asc(), SlotIntent.id.asc()]


class IntentNodeOption(RefOut):
    """One option of the intent queue's node filter: the id `?node=` takes, and the name shown."""


class IntentPageOut(BaseModel):
    """One page of the fleet's intent queue, plus what a pager needs to be honest about it.

    The envelope is `SessionListOut`'s (one pagination dialect). `counts` rides here, since numbers and
    rows are two halves of one answer; two endpoints would be two WHEREs to keep in step."""

    items: list[IntentRow]
    # What the filters select: the pager's divisor, so the last page is known.
    total: int
    page: int
    per: int
    # The set's size under the node scope and no other filter, so an empty page can say "clear your
    # filters" vs "nothing here". `?node=` is scope, not a filter, so a quiet node does not report the
    # fleet's size.
    total_unfiltered: int
    # Echoed so the page need not remember which set it asked for.
    set: str
    # The vocabulary actually present in this set (not `fleet_verdicts`' full one), computed with the set
    # clause and node scope but not the other filters, so picking one option never empties the others.
    verbs: list[str] = []
    outcomes: list[str] = []
    # The node filter's options: distinct node names in this set, sorted, computed without `?node=` so the
    # dropdown does not eat its own options (`verbs`/`outcomes` do honour it). Built under the grant clause,
    # so it is never a roster oracle.
    nodes: list[str] = []
    # The same options as `{id, name}`, sorted by name: `?node=` takes the id, since a name is unique per org
    # only. `nodes` stays for readers built before it.
    node_options: list[IntentNodeOption] = []
    # Counts over the grant scope and `?node=`, not `set`/`verb`/`outcome`/`state`/`refusing`: `IntentQueue`
    # reports both halves itself, and row filters would just restate `total`.
    counts: IntentQueue


@router.get("/api/nodes/intents", response_model=IntentPageOut)
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def list_intents(
    page: int = Query(default=1, ge=1, le=_INTENTS_PAGE_MAX),
    per: int = Query(default=_INTENTS_PER_PAGE, ge=1, le=_INTENTS_PER_MAX),
    set_: str = Query(default=_SET_PENDING, alias="set", max_length=_INTENT_FILTER_MAX_LEN),
    # The node's uuid (`NodeOut.id`, `IntentNodeOption.id`), never its name. `min_length=1`: a blank could only
    # select nothing.
    node: Optional[str] = Query(default=None, min_length=1, max_length=_INTENT_FILTER_MAX_LEN),
    # `min_length=1` too: `?verb=` would match `''` and guarantee an empty page; `verb` has no vocabulary
    # check to catch it.
    verb: Optional[str] = Query(default=None, min_length=1, max_length=_INTENT_FILTER_MAX_LEN),
    outcome: Optional[str] = Query(default=None, max_length=_INTENT_FILTER_MAX_LEN),
    state: Optional[str] = Query(default=None, max_length=_INTENT_FILTER_MAX_LEN),
    refusing: bool = Query(default=False),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> IntentPageOut:
    """One page of the fleet's slot-intent queue, with its counts: the Audit surface. The rows are paged in
    full and narrowed by node, verb, outcome and whether the row has refused. Declared early (see the
    section header).

    Node-reach scoped with `authz.node_reach_clause` on the NOT NULL `slot_intents.node_id`, not
    `visible_node_ref_clause` (whose NULL arm is for nullable references). No `sees_all_nodes` branch: every
    row has a real node, and the primitive already answers `true()` for unrestricted principals. Zero
    grants yields an empty page (tested: the fail-open direction).

    An unknown, non-uuid or ungranted `?node=` is an empty page, not a 404: a filter addresses nothing, and both cases
    share one code path, so no oracle.

    Read-only: no `nodes`, `slot_intents` or console event moves (the `activity_events` trail is
    `UserActivityMiddleware`'s). The clock is the DB's (see `HealthClock`); here it decides the row set.
    No payload travels (`IntentRow`).

    Cost: seven statements read `slot_intents` (a top-N page, two totals, three `DISTINCT` scans, one
    grouped aggregate), none fetching a row per intent; keep it so. The retired set's ordering is not
    index-covered (see `IntentQueue.retention_seconds`)."""
    if set_ not in _INTENT_SETS:
        raise HTTPException(status_code=422, detail=f"unknown set '{set_}'")
    # The grant scope, built once and applied to every statement, so none of the seven can forget it.
    scope: list[Any] = [node_reach_clause(principal, SlotIntent.node_id)]
    node_clauses = list(scope)
    if node is not None:
        # A non-uuid yields `false()`, not `node_id == None` (NULL compares UNKNOWN and could admit rows under a
        # future `NOT`). No existence read: the grant clause already scopes the rows.
        node_clauses.append(SlotIntent.node_id == UUID(node) if is_uuid_shaped(node) else false())

    now = session.scalar(select(func.now()))
    filters = _intent_filters(
        node_clauses=node_clauses, set_=set_, verb=verb, outcome=outcome, state=state,
        refusing=refusing, now=now,
    )
    # Set clause and node scope only (see `IntentPageOut.total_unfiltered`).
    unfiltered = [*node_clauses, _intent_set_clause(set_)]
    total = session.scalar(select(func.count()).select_from(SlotIntent).where(*filters)) or 0
    total_unfiltered = (
        session.scalar(select(func.count()).select_from(SlotIntent).where(*unfiltered)) or 0
    )
    rows = list(
        session.scalars(
            select(SlotIntent)
            .where(*filters)
            .order_by(*_intent_ordering(set_))
            .offset((page - 1) * per)
            .limit(per)
        ).all()
    )
    # Two bulk name reads per page, never per row.
    names = project_identity.names_by_id(session, [r.project_id for r in rows])
    node_names = node_identity.names_by_id(session, [r.node_id for r in rows])
    verbs = sorted(
        v for v in session.scalars(
            select(distinct(SlotIntent.verb)).select_from(SlotIntent).where(*unfiltered)
        ).all() if v
    )
    outcomes = sorted(
        o for o in session.scalars(
            select(distinct(SlotIntent.outcome)).select_from(SlotIntent).where(*unfiltered)
        ).all() if o
    )
    # Node options over the set and grant scope only (`scope`, not `node_clauses`): distinct ids, then one
    # bulk resolve.
    listed = session.scalars(
        select(distinct(SlotIntent.node_id))
        .select_from(SlotIntent)
        .where(*scope, _intent_set_clause(set_))
    ).all()
    offered = sorted(node_identity.names_by_id(session, listed).items(), key=lambda item: (item[1], str(item[0])))
    return IntentPageOut(
        items=[
            _intent_row(
                r,
                names.get(r.project_id),
                # Unreachable through the FK (NOT NULL, CASCADE); degrades to a visible id instead of a 500.
                node_names.get(r.node_id) or f"node#{r.node_id}",
                now,
                retired=(set_ == _SET_RETIRED),
            )
            for r in rows
        ],
        total=int(total),
        page=page,
        per=per,
        total_unfiltered=int(total_unfiltered),
        set=set_,
        verbs=verbs,
        outcomes=outcomes,
        nodes=[name for _id, name in offered],
        node_options=[IntentNodeOption(id=str(node_id), name=name) for node_id, name in offered],
        # The same node scope as the rows, `?node=` included, so the banners describe what is on screen.
        counts=_intent_queue(session, node_clauses, now),
    )
