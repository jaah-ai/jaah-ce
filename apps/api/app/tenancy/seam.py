"""The READ SEAM: one org predicate, applied once, at the session — plus the scope it reads and the
escape that lets identity resolve at all.

An opt-out default: the predicate attaches to every ORM select, so a forgotten call site fails closed
(specification §3). Non-ORM `text(...)` reads are not covered; the bypass census polices them. `_MODE`
is `ENFORCING` (unbound reads nothing); recovery is reverting that constant (AD4), so `SHADOW` stays
tested. Pitfalls are documented where decided: `criteria_for` (unbound is `false()`), `read_seam` (no
lambda criteria), `stamp_scope` (identity-map hits), `identity_scope` (a scope, not a one-row read).
Registered on `SessionLocal` in `app/db.py`, so migration and test-harness sessions are left alone.
"""

from __future__ import annotations

import logging
import os
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import Iterator, Optional, Union
from uuid import UUID

from sqlalchemy import Table, false, select
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ColumnElement

from app.models.access import Permission, UserGroupMembership
from app.models.base import Base
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.tenancy import exclusion_log
from app.tenancy.manifest import TENANT_OWNED

log = logging.getLogger("tenancy-seam")


# ── The scope vocabulary ────────────────────────────────────────────────────────────────────────────


class _ScopeSentinel:
    """A scope that is not an org id. Compared by IDENTITY (`is`), never by value.

    Two plain instances, not an enum or `str` subclass, so `==` against a `UUID` can never answer True
    by accident.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return self._name


#: The fleet-wide plane: the sweeps, the crons and the control plane's own loopback caller. No criteria
#: is applied (§3 allowlist). Binding it is a decision a call site makes, never a default or fallthrough.
OPERATOR = _ScopeSentinel("OPERATOR")

#: The identity escape (`identity_scope`). Never bindable: entered and left, suspending the bound scope.
IDENTITY = _ScopeSentinel("IDENTITY")

#: What `current_scope()` answers. `None` is UNBOUND, a real state: the predicate is `false()`.
Scope = Union[UUID, _ScopeSentinel, None]


_SCOPE: ContextVar[Scope] = ContextVar("tenancy_scope", default=None)


class TenancyScopeConflict(RuntimeError):
    """One session, two different filtering scopes. Raised at execute time; see `stamp_scope`."""


# ── The request plane ───────────────────────────────────────────────────────────────────────────────

_REQUEST_PLANE: ContextVar[bool] = ContextVar("tenancy_request_plane", default=False)


def mark_request_plane() -> None:
    """Record that THIS context is serving an inbound HTTP/WebSocket request. **`async def` ONLY.**

    Arms the write seam's unstamped-create refusal (`app/tenancy/write_seam.py`) only where a caller can
    be asked for an org; machinery under `operator_scope()` has nobody to ask. A mark, not a scope. Set,
    never restored, like `bind()`. Only the cookie plane marks, skipped when the decision resolved
    `seam.OPERATOR` (the node-token branch); a superadmin on a `Tenancy.OPERATOR` route still arms it,
    since their decision resolves their own org and the route's `OPERATOR` is bound after.

    Latent gap: `authz_guard` returns before `_bind_read_scope` for `Access.INFRA`/`Access.ALWAYS`
    (`authz._decides_without_io`), so a future `@always(Tenancy.OPERATOR)` create route must mark its own
    context (the tenancy scope-site census checks it). Defaults to `False`: a missed mark under-refuses
    (counted), never over-refuses."""
    _REQUEST_PLANE.set(True)


def on_request_plane() -> bool:
    """True when this context is serving an inbound request. See `mark_request_plane`."""
    return _REQUEST_PLANE.get()


@contextmanager
def request_plane() -> Iterator[None]:
    """`mark_request_plane()` as a REGION, for a test or a caller that must put the flag back.

    Production uses the bare setter (a request's context is discarded whole); this lets a test arm the
    refusal without leaking the flag into later tests on the same thread."""
    token = _REQUEST_PLANE.set(True)
    try:
        yield
    finally:
        _REQUEST_PLANE.reset(token)


def current_scope() -> Scope:
    """The org bound to THIS context, or `None` when nothing has bound one."""
    return _SCOPE.get()


def bound_org() -> Optional[UUID]:
    """The ONE org bound to this context, or `None` — for the `OPERATOR` sentinel as much as for an
    unbound context, because neither names a tenant.

    For stamping which org a side effect happened in (e.g. `app/events.py`'s collection sentinel), not
    for scoping; reads and writes consult `current_scope()`."""
    scope = _SCOPE.get()
    return scope if isinstance(scope, UUID) else None


# ── The operator org filter ─────────────────────────────────────────────────────────────────────────

#: The orgs an operator's GET narrowed the `OPERATOR` plane to (`X-Jaah-Org-Scope`), or `None` for All. A
#: FILTER, not a scope (D19): the bound scope stays `OPERATOR`, so no `scope is OPERATOR` consumer, session
#: stamp or `TenancyScopeConflict` can tell it is there. Set once per request by `authz._bind_read_scope`,
#: never outside a request, so background sweeps never see it.
_ORG_FILTER: ContextVar[Optional[frozenset[UUID]]] = ContextVar("tenancy_org_filter", default=None)

#: True inside an explicit escape (`operator_scope`, `detached`, `bound_to`, `identity_scope`,
#: `bind_from`): machinery and by-id resolution read past the filter and never 404 because of it.
_ORG_FILTER_SUSPENDED: ContextVar[bool] = ContextVar("tenancy_org_filter_suspended", default=False)


def set_org_filter(orgs: Optional[frozenset[UUID]]) -> None:
    """Narrow this request's `OPERATOR` reads to `orgs`. **`async def` ONLY**, like `bind()`.

    `None` or an empty set is All. Called from `authz._bind_read_scope` alone, after it bound `OPERATOR`
    for an operator's GET/HEAD; every other caller and method ignores the header (R5.7, R5.8)."""
    _ORG_FILTER.set(orgs or None)


def request_org_filter() -> Optional[frozenset[UUID]]:
    """The orgs this request's operator filter selected, or `None` for All — EVEN inside an escape.

    For residue sites (raw SQL, reads inside `operator_scope()`) that serve LIST data to an operator GET
    and must apply the filter themselves (the org-filter site census)."""
    return _ORG_FILTER.get()


def _active_org_filter() -> Optional[frozenset[UUID]]:
    """The filter `criteria_for` applies: the request's, unless an escape suspended it."""
    if _ORG_FILTER_SUSPENDED.get():
        return None
    return _ORG_FILTER.get()


#: The classes the operator filter never narrows at the seam. `admin_users` is how a request names people
#: — the operator's own row ("who is asking"), a task's creator, a comment's author — and hiding a person
#: because their membership sits in an unselected org would 401 the caller or blank a name. The users
#: directory narrows its own query by membership row instead (D20, `routes/users.py::list_users`).
ORG_FILTER_EXEMPT_CLASSES: frozenset[type[Base]] = frozenset({AdminUser})


@contextmanager
def request_org_filter_applied() -> Iterator[None]:
    """Re-apply this request's operator filter inside an escape, for a block. The ORM twin of
    `request_org_filter()`: a residue site whose LIST read runs inside `operator_scope()` enters this
    after it (`with seam.operator_scope(), seam.request_org_filter_applied(): ...`). With no filter set
    it changes nothing, so a mutation sharing the helper is unaffected."""
    token = _ORG_FILTER_SUSPENDED.set(False)
    try:
        yield
    finally:
        _ORG_FILTER_SUSPENDED.reset(token)


@contextmanager
def _org_filter_suspended() -> Iterator[None]:
    """Suspend the operator filter for a block. ONE home for every escape's suspension."""
    token = _ORG_FILTER_SUSPENDED.set(True)
    try:
        yield
    finally:
        _ORG_FILTER_SUSPENDED.reset(token)


def bind(scope: Union[UUID, _ScopeSentinel]) -> None:
    """Bind this context's org. **CALL IT ONLY FROM AN `async def`.**

    A `def` dependency (or anything via `run_in_threadpool`) runs on a copied context in a worker thread,
    so the set dies with the thread and the request silently runs unbound. Both bind sites
    (`authz.authz_guard`, `routes/agent/auth.py::_check_token`) await the blocking half and bind on the loop.
    Enforced by the tenancy scope-site census, which fails any call not lexically inside an
    `async def` (a runtime check cannot distinguish the two).

    `IDENTITY` is refused; only `identity_scope()` may enter it.
    """
    if scope is IDENTITY:
        raise ValueError("IDENTITY is entered through identity_scope(), never bound")
    if scope is not OPERATOR and not isinstance(scope, UUID):
        raise TypeError(f"a scope is an org UUID or OPERATOR, not {scope!r}")
    _SCOPE.set(scope)


# ── The identity escape ─────────────────────────────────────────────────────────────────────────────


#: The classes identity resolution reads: the escape's boundary (any other tenant-owned class gets
#: `false()` inside `identity_scope()`), asserted equal to the touched set by
#: the seam-read tests. Rule: escape reads
#: that resolve the caller, scope reads that describe someone else (`actors.py::actor_names` is scoped).
#: Known unescaped twin: `routes/auth.py::_scope_permissions` may silently lose implied triples.
IDENTITY_CLASSES: frozenset[type[Base]] = frozenset(
    {
        AdminUser,
        Permission,
        UserGroupMembership,
        Node,
    }
)


@contextmanager
def identity_scope() -> Iterator[None]:
    """SUSPEND the bound scope for the reads that RESOLVE an identity, and restore it on exit.

    Identity resolution is circular: the org comes from the principal read out of `admin_users`
    (`authz._resolve_principal`), and the agent plane finds its node by token hash
    (`node_credentials._node_for_token`). A region bounded by `IDENTITY_CLASSES`, not a one-row read,
    since `authz.principal_for_user` issues collection queries.

    Re-entry is a no-op (the Slack path re-enters via `principal_for_user`). What comes out must not
    lazy-load later (`expire_on_commit=False` would load under the caller's scope), so identity reads
    return frozen values (`authz.Principal`) or scalars.
    """
    if _SCOPE.get() is IDENTITY:
        yield
        return
    token = _SCOPE.set(IDENTITY)
    try:
        with _org_filter_suspended():
            yield
    finally:
        _SCOPE.reset(token)


# ── Leaving the caller's scope ──────────────────────────────────────────────────────────────────────


@contextmanager
def detached() -> Iterator[None]:
    """LEAVE whatever scope the caller decided, for a block, and put it back on exit. Binds NOTHING.

    For fleet-wide work started from an org-bound context (a `BackgroundTasks` pass runs in the request's
    context). Sanctioned spelling: `with seam.detached(), seam.operator_scope(): ...`. Alone it opens
    nothing, so it cannot widen; but it disarms `bind_from`'s re-point refusal, and `bind_from` has no
    entitlement check, so a `detached()` + `bind_from` pairing owes an entitlement argument at its call
    site (see `routes/agent/task_terminal.py::_announce_question_round_in_task_scope`).

    A region, correct on any thread. It does not touch the session stamp (a session stamped with org A
    still raises under `operator_scope()`; fleet-wide passes open their own session). It also leaves the
    request plane, so `write_seam._refuses_unstamped_create` stays unarmed for machinery like
    `routes/crons.py::_run`.
    """
    scope_token = _SCOPE.set(None)
    plane_token = _REQUEST_PLANE.set(False)
    try:
        with _org_filter_suspended():
            yield
    finally:
        _REQUEST_PLANE.reset(plane_token)
        _SCOPE.reset(scope_token)


# ── The operator escape ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def operator_scope() -> Iterator[None]:
    """ENTER the fleet-wide plane for a block, and restore the previous scope on exit.

    For sweeps that must see every org's rows (the drain, retention purges, node liveness). Every call
    site is enumerated by the tenancy scope-site census (§3 allowlist). A region, not `bind()`,
    because sweeps and DBOS steps run on worker threads. Re-entry is a no-op.

    Not the durable plane's answer: one-task work has the task's org (`bind_from()`).

    Refuses to widen a context that already decided an org: one `with` line would otherwise promote an
    org-bound block to every org, and the per-session stamp does not catch a fresh `SessionLocal()`.
    Leave the scope first (`detached()`). `None` and `IDENTITY` are not deciding scopes (`_filters`).
    """
    prior = _SCOPE.get()
    if prior is OPERATOR:
        # Re-entry binds nothing new, but still suspends an operator's org filter: the block is machinery.
        with _org_filter_suspended():
            yield
        return
    if _filters(prior):
        raise TenancyScopeConflict(
            f"operator_scope() entered under org {prior!r}: the fleet-wide plane is for a caller that "
            "has bound nothing, and widening a request that already belongs to one org is the failure "
            "this boundary exists to prevent (app/tenancy/seam.py::operator_scope)"
        )
    token = _SCOPE.set(OPERATOR)
    try:
        with _org_filter_suspended():
            yield
    finally:
        _SCOPE.reset(token)


@contextmanager
def bound_to(scope: Optional[UUID]) -> Iterator[None]:
    """ENTER an org already decided elsewhere, for a block, and restore on exit. `None` is a no-op.

    `bind()`'s region form, for a caller that already knows the org and runs on a worker thread. Its one
    caller is the guard's object probe (`authz._enforce_object_params` via `can_access`), which runs
    before `_bind_read_scope` and would otherwise read `false()` and 404 every owned row.

    Refuses to re-point a context already deciding a different org (entering the org in force is a no-op);
    `None`/`IDENTITY` are not deciding scopes. `None` runs the block unbound, consistent with
    `_bind_read_scope` for that principal. `OPERATOR` is excluded by type: use `operator_scope()`.
    """
    if scope is None:
        with _org_filter_suspended():
            yield
        return
    if not isinstance(scope, UUID):
        raise TypeError(f"bound_to() takes an org UUID or None, not {scope!r}")
    prior = _SCOPE.get()
    if prior == scope:
        with _org_filter_suspended():
            yield
        return
    if _filters(prior):
        raise TenancyScopeConflict(
            f"bound_to({scope!r}) entered under scope {prior!r}: one unit of work belongs to one org, "
            "and re-pointing a context that has already decided one is the failure this boundary "
            "exists to prevent (app/tenancy/seam.py::bound_to)"
        )
    token = _SCOPE.set(scope)
    try:
        with _org_filter_suspended():
            yield
    finally:
        _SCOPE.reset(token)


# ── The row resolver ────────────────────────────────────────────────────────────────────────────────


class TenancyRowMissing(LookupError):
    """`bind_from()` was pointed at a primary key that resolves to no row."""


@contextmanager
def bind_from(session: Session, cls: type[Base], pk: object) -> Iterator[Scope]:
    """DERIVE the org from one row and enter that org's scope for a block. Yields the scope entered.

    So one-task durable work is not tenancy-free (§3.8). A region, since DBOS steps run on worker threads.
    The resolving read uses `session.connection()` (the ORM path would read `false()`) and selects the
    mapped attribute, one column by primary key (pinned by the seam-read tests).

    `org_id IS NULL` runs unbound (fail closed); a missing row raises `TenancyRowMissing`; re-pointing a
    context deciding a different org is refused. The read begins the transaction, stamping the scope in
    force then (nesting in `operator_scope()` on one session fails via `check_session_stamp`), and holds a
    pooled connection: a block doing network I/O must start with `session.rollback()` (`app/db.py`).

    An existence oracle with no entitlement check: it binds any existing key's org, whoever asked.
    """
    # An opaque scalar key only: a `ColumnElement` would be inlined into the WHERE
    # (`literal_column("1 OR 1=1")`) and resolve an arbitrary org.
    if not isinstance(pk, (int, str, UUID)):
        raise TypeError(f"bind_from() takes an opaque scalar primary key, not {type(pk).__name__}")
    table = cls.__table__
    keys = list(table.primary_key.columns)
    if len(keys) != 1:
        raise TypeError(
            f"bind_from() resolves a single-column primary key; {cls.__name__} has {len(keys)}"
        )
    if table.c.get("org_id") is None or not hasattr(cls, "org_id"):
        raise TypeError(f"bind_from() needs an org_id column; {cls.__name__} has none")
    # `Connection.execute`, not `Session.execute` (see the docstring). The mapped attribute in the SELECT,
    # the Table column in the WHERE (a PK attribute is not always spelled like its column).
    row = session.connection().execute(
        select(cls.org_id).where(keys[0] == pk)
    ).first()
    if row is None:
        raise TenancyRowMissing(f"{cls.__name__} {pk!r} does not exist")
    org = row[0]
    if org is not None and not isinstance(org, UUID):
        raise TypeError(f"{cls.__name__}.org_id held {org!r}, which is not an org id")
    prior = _SCOPE.get()
    if _filters(prior) and prior != org:
        raise TenancyScopeConflict(
            f"bind_from() derived org {org!r} from {cls.__name__} {pk!r} while scope {prior!r} is in "
            "force; one unit of work belongs to one org (app/tenancy/seam.py::bind_from)"
        )
    # Fail here, naming the wrong composition, rather than at whichever ORM read comes first.
    check_session_stamp(session, org)
    if org is None:
        # Deduped via `_remember`: every legacy row takes this arm until the backfill lands.
        if _remember(("bind-from-unbound", cls.__name__)):
            log.warning(
                "tenancy seam: %s %r carries no org_id — running UNBOUND, which reads nothing once "
                "enforcing (app/tenancy/seam.py::bind_from)",
                cls.__name__,
                pk,
            )
    token = _SCOPE.set(org)
    try:
        with _org_filter_suspended():
            yield org
    finally:
        _SCOPE.reset(token)


# ── The mode ────────────────────────────────────────────────────────────────────────────────────────


class SeamMode(str, Enum):
    """SHADOW computes and logs; ENFORCING also applies. There is no third state."""

    SHADOW = "shadow"
    ENFORCING = "enforcing"


_MODE = SeamMode.ENFORCING


def seam_mode() -> SeamMode:
    """The seam's current mode. `ENFORCING` in every deployment today."""
    return _MODE


@contextmanager
def _mode_override(mode: SeamMode) -> Iterator[None]:
    """`enforcing()`/`shadow()`'s one body: `_MODE` is `mode` for the block, then what it was."""
    _require_test_process(mode.value)
    global _MODE
    prior = _MODE
    _MODE = mode
    try:
        yield
    finally:
        _MODE = prior


def enforcing() -> AbstractContextManager[None]:
    """Run this block with the criteria APPLIED. **A test affordance, not a runtime switch.**

    Redundant with the `ENFORCING` default but kept: after a revert to `SHADOW` (AD4), enforcing-path
    assertions must still exercise an applied predicate. A module global (a `TestClient` request runs in
    another thread), so it is refused outside a test process: a process-wide flip from a handler would
    change filtering for every concurrent caller. A deployment's mode is the constant above.
    """
    return _mode_override(SeamMode.ENFORCING)


def shadow() -> AbstractContextManager[None]:
    """Run this block with the criteria COMPUTED AND LOGGED BUT NOT APPLIED. **A test affordance.**

    `enforcing()`'s mirror, so shadow mode's claim ("computes and logs, changes no row set") stays tested
    for the revert path (by the seam-read tests). Refused outside a test process,
    since it would stop filtering for every concurrent caller.
    """
    return _mode_override(SeamMode.SHADOW)


def _require_test_process(name: str) -> None:
    """Refuse a mode override outside pytest. ONE home for both affordances' guard; keys on
    `PYTEST_CURRENT_TEST`, set per test item."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            f"{name}() is a test affordance; the shipped mode is changed by editing _MODE"
        )


# ── The predicate ───────────────────────────────────────────────────────────────────────────────────


def criteria_for(cls: type[Base], scope: Scope) -> Optional[ColumnElement[bool]]:
    """The org predicate for one mapped class under one scope. `None` means "no criteria at all".

    Explicit branches because `cls.org_id == current_scope()` renders `== None` as `IS NULL` when unbound,
    failing open onto exactly the unstamped rows. `false()` is the only safe unbound answer.

    No `lambda`: `with_loader_criteria`'s lambda form is cached by code object, baking the first org into
    the compiled SQL. A plain expression carries its value in the cache key
    (a seam-read test checks two orgs on one statement). `OPERATOR` applies
    the request's operator org filter (`set_org_filter`), so an operator's GET narrows here, at one place.
    """
    if scope is None:
        # UNBOUND. Not "no filter" — the read returns NOTHING, loudly (see `_log_shadow`).
        return false()
    if scope is OPERATOR:
        # An operator's org filter narrows the fleet plane (D19); unset or suspended, no criteria at all.
        orgs = _active_org_filter()
        if orgs is None or cls in ORG_FILTER_EXEMPT_CLASSES:
            return None
        return cls.org_id.in_(sorted(orgs))
    if scope is IDENTITY:
        # The escape's boundary: open for the identity classes, shut for everything else.
        return None if cls in IDENTITY_CLASSES else false()
    if isinstance(scope, UUID):
        return cls.org_id == scope
    # Unreachable through `bind` (type-checked), and still denies so a new scope kind without a branch fails closed.
    log.error("tenancy seam: unhandled scope %r — denying %s", scope, cls.__name__)
    return false()


# ── The session stamp ───────────────────────────────────────────────────────────────────────────────


#: ONE namespaced key on `Session.info`, alongside `app/actors.py`'s `actor_id`.
SESSION_SCOPE_KEY = "tenancy_scope"

_UNSTAMPED = object()


def _filters(scope: Scope) -> bool:
    """Is this scope one that DECIDES which rows a session sees? `None` and `IDENTITY` are not."""
    return scope is OPERATOR or isinstance(scope, UUID)


def stamp_scope(session: Session, transaction: object, connection: object) -> None:
    """Record the scope this session FIRST ran under, on `after_begin`.

    `with_loader_criteria` never sees an identity-map hit, so a session re-bound from org A to B could
    serve A's objects; the stamp makes that detectable. Not in `bind()`: the guard's session predates the
    bind, and the agent plane's `get_session` is a sub-dependency of `_check_token`.

    The first deciding scope wins, not the first scope: the agent plane's first transaction is the token
    lookup under `identity_scope()`, and the cookie plane's is often an unbound probe, both compatible with
    everything. A non-deciding scope leaves the session unstamped; once settled it is never rewritten.
    """
    stamp_if_deciding(session, current_scope())


def stamp_if_deciding(session: Session, scope: Scope) -> None:
    """Record `scope` as this session's, if it decides anything and nothing was recorded before.

    Also called from `read_seam`: `after_begin` fires only when a transaction begins, and a request that
    authenticates then binds on one transaction would otherwise only ever stamp the pre-bind scope."""
    if not _filters(scope):
        return
    session.info.setdefault(SESSION_SCOPE_KEY, scope)


def check_session_stamp(session: Session, scope: Scope) -> None:
    """RAISE when one session is used under two different filtering scopes.

    Called by `read_seam` on every ORM select and by `app/tenancy/write_seam.py::stamp_org` on every flush
    (a write-only unit would otherwise stamp the second scope's org onto the first's instances); hence public.

    `IDENTITY` is compatible with any stamp (`authz._resolve_principal` runs on every request), and so is
    an unbound side (it put no rows in the identity map). Only two different deciding scopes conflict,
    which no shape this app produces reaches: a scope is bound once per request and a session belongs to
    one request. If it fires, fix the call site that shared the session."""
    stamp = session.info.get(SESSION_SCOPE_KEY, _UNSTAMPED)
    if stamp is _UNSTAMPED or stamp == scope:
        return
    if scope is IDENTITY or stamp is IDENTITY:
        return
    if not _filters(stamp) or not _filters(scope):
        return
    raise TenancyScopeConflict(
        f"this session first ran under scope {stamp!r} and is now executing under {scope!r}; "
        "a session belongs to one org for its whole life (app/tenancy/seam.py::stamp_scope)"
    )


# ── The shadow log ──────────────────────────────────────────────────────────────────────────────────


#: First occurrence per (reason, class), bounded. Answers "which paths are unbound", never "how often" or
#: "still", so the durable exclusion log, not this, is the enforcement exit condition. It records that a
#: predicate was computed, not that it attached (proved by the seam-read tests).
_SHADOW_SEEN: set[tuple[str, str]] = set()
_SHADOW_SEEN_CAP = 512


def reset_shadow_log() -> None:
    """Forget what has already been logged. For tests, and for an operator draining the trail."""
    _SHADOW_SEEN.clear()


def _remember(key: tuple[str, str]) -> bool:
    """Is this the FIRST time this (reason, subject) pair has been seen? Records it if so.

    One home for the first-occurrence rule (two emitters). The cap stops the logging too, so a full key
    space never inverts into per-query logging."""
    if key in _SHADOW_SEEN:
        return False
    if len(_SHADOW_SEEN) >= _SHADOW_SEEN_CAP:
        return False
    _SHADOW_SEEN.add(key)
    return True


def _log_shadow(cls: type[Base], scope: Scope, *, restricted: bool) -> None:
    """One line per (scope_kind, class), the first time that pair is seen.

    `restricted` is passed in: inside the identity escape an `IDENTITY_CLASSES` member is open while
    everything else is `false()`, which the scope alone cannot tell. Branches on
    `exclusion_log.scope_kind_of`'s answer, the same string the durable log stores, so the console line and
    the row cannot disagree. A `SCOPE_KINDS` value missing from the table below still logs, at INFO."""
    kind = exclusion_log.scope_kind_of(scope, restricted=restricted)
    if not _remember((kind, cls.__name__)):
        return
    mode = seam_mode().value
    if kind == exclusion_log.SCOPE_KIND_UNBOUND:
        log.warning(
            "tenancy seam [%s]: UNBOUND read of %s — NO ORG IS BOUND on this context, so the predicate "
            "is false() and this read returns nothing once the seam enforces (mode=%s)",
            kind, cls.__name__, mode,
        )
    elif kind == exclusion_log.SCOPE_KIND_IDENTITY_DENIED:
        log.warning(
            "tenancy seam [%s]: %s read inside identity_scope() but it is not one of IDENTITY_CLASSES "
            "— the predicate is false() and this read returns nothing once the seam enforces (mode=%s)",
            kind, cls.__name__, mode,
        )
    elif kind == exclusion_log.SCOPE_KIND_IDENTITY_OPEN:
        log.info(
            "tenancy seam [%s]: %s read under the identity escape — NO predicate is applied, now or "
            "after the flip (mode=%s)",
            kind, cls.__name__, mode,
        )
    elif kind == exclusion_log.SCOPE_KIND_OPERATOR:
        log.info(
            "tenancy seam [%s]: %s read under OPERATOR scope — NO predicate is applied, now or after "
            "the flip (mode=%s)",
            kind, cls.__name__, mode,
        )
    elif kind == exclusion_log.SCOPE_KIND_ORG:
        log.info(
            "tenancy seam [%s]: %s restricted to org %s (mode=%s)", kind, cls.__name__, scope, mode
        )
    else:
        log.info(
            "tenancy seam [%s]: %s read under a scope this module has no line for (mode=%s)",
            kind, cls.__name__, mode,
        )


# ── The listener ────────────────────────────────────────────────────────────────────────────────────


_CLASS_BY_TABLE: dict[Table, type[Base]] = {}


def _class_by_table() -> dict[Table, type[Base]]:
    """`{Table: mapped class}`, built once, LAZILY — mappers configure on first use, so building this at
    import time would see an incomplete registry.

    Built into a local and rebound, never filled in place: another pool thread seeing a half-built dict
    would skip a class's predicate (fail open). The rebind is atomic under the GIL."""
    global _CLASS_BY_TABLE
    if not _CLASS_BY_TABLE:
        _CLASS_BY_TABLE = {mapper.local_table: mapper.class_ for mapper in Base.registry.mappers}
    return _CLASS_BY_TABLE


def mapped_classes_of(state: ORMExecuteState) -> tuple[type[Base], ...]:
    """Every mapped class this statement TOUCHES — selected, joined, or reached through a subquery.

    `all_mappers` is not enough: for `select(Permission.key).join(RolePermission, ...)` it reports only
    `Permission`, leaving join-only entities unfiltered. So tables are read off the expression tree
    (~25µs for a two-entity join; re-measure if a statement grows large) and mapped back to classes.
    Shared with the recording listener that derives `IDENTITY_CLASSES`."""
    classes: dict[type[Base], None] = {}
    for mapper in state.all_mappers:
        classes[mapper.class_] = None
    by_table = _class_by_table()
    for element in visitors.iterate(state.statement):
        if isinstance(element, Table):
            cls = by_table.get(element)
            if cls is not None:
                classes[cls] = None
    return tuple(classes)


def read_seam(state: ORMExecuteState) -> None:
    """The `do_orm_execute` listener: compute the org predicate for every tenant-owned class in this
    statement, log what it would exclude, and — only when ENFORCING — apply it.

    The three-term guard is the standard recipe: column and relationship loads re-query objects whose
    parent read was already filtered (criteria there only breaks attribute access), and `is_select` keeps
    DML to the write stamp. `include_aliases=True` covers `aliased(...)` subqueries and self-joins.
    """
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    scope = current_scope()
    # The stamp check raises in both modes: two deciding scopes on one session is a calling-code defect,
    # unreachable on this app's shapes (`check_session_stamp`). Deliberately not gated on `ENFORCING`, so
    # the check has run across deployments before it matters. If it fires, fix the call site.
    check_session_stamp(state.session, scope)
    stamp_if_deciding(state.session, scope)
    try:
        classes = mapped_classes_of(state)
    except Exception as exc:  # noqa: BLE001 - see below
        # Shadow mode must change nothing, so a walk failure is swallowed there; under ENFORCING the
        # predicate did not land, so it fails closed and propagates. Such a statement leaves no exclusion
        # line (the `return` precedes `_log_shadow`). Deduped: the first occurrence per exception type
        # carries the traceback.
        if _remember(("walk-failed", type(exc).__name__)):
            log.exception("tenancy seam: could not read the classes out of this statement")
        if _MODE is SeamMode.ENFORCING:
            raise
        return
    for cls in classes:
        if cls not in TENANT_OWNED:
            continue
        criteria = criteria_for(cls, scope)
        # Logged before the `None` test so the operator plane (crons, sweeps, loopback), which applies no
        # predicate, still leaves a trail.
        _log_shadow(cls, scope, restricted=criteria is not None)
        if criteria is None:
            continue
        if scope is OPERATOR:
            # The operator's own org filter (`set_org_filter`): a view narrowing, not a tenancy exclusion,
            # so it leaves no exclusion-log row.
            if _MODE is SeamMode.ENFORCING:
                state.statement = state.statement.options(
                    with_loader_criteria(cls, criteria, include_aliases=True)
                )
            continue
        # The durable count, below the `None` test (nothing excluded means no row). Not deduped; aggregated
        # in memory, since `exclusion_log.record` runs inside another statement. Guarded: a raising counter
        # on every ORM select would be an outage. Deduped logging, never re-raised; only the predicate
        # fails closed.
        try:
            exclusion_log.record(
                mapped_class=cls.__name__,
                scope_kind=exclusion_log.scope_kind_of(scope, restricted=True),
                bound_org=scope if isinstance(scope, UUID) else None,
                mode=exclusion_log.MODE_READ,
            )
        except Exception as exc:  # noqa: BLE001 - a counter must never break a read; see above
            if _remember(("exclusion-record-failed", type(exc).__name__)):
                log.exception("tenancy seam: the exclusion counter raised; its counts are lost")
        if _MODE is SeamMode.ENFORCING:
            state.statement = state.statement.options(
                with_loader_criteria(cls, criteria, include_aliases=True)
            )
