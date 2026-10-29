"""The write seam: one `before_flush` listener on `SessionLocal` that stamps `org_id`, refuses a
cross-org write, and counts writes it had no org to stamp (a listener, so a forgotten route fails closed).

  * Org bound: NULL `org_id` is stamped, a different org refused. `Derived` rows check the parent's
    committed org (`_committed_orgs`): unstamped → child left NULL and counted; other org → refused.
  * Nothing bound: `Derived` rows inherit; other NULL writes are counted (`mode='unbound_write'`),
    except operator-plane request creates, which are refused (`_refuses_unstamped_create`). No org
    floor on `Stored` writes. Deletes are checked only (`_refuse_cross_org_deletes`).

Blind to Core DML, `text()` and raw psycopg (each such site is listed in the tenancy-bypass
census). Parent agreement on derivation paths
only: `app/tenancy/keys.py` (whose unkeyed references this seam does not hold either).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.tenancy import exclusion_log
from app.tenancy.keys import ORG_COLUMN, PARENT_ID_COLUMN
from app.tenancy.seam import (
    OPERATOR,
    check_session_stamp,
    current_scope,
    on_request_plane,
    stamp_if_deciding,
)

log = logging.getLogger("tenancy-write-seam")


class TenancyWriteRefused(RuntimeError):
    """A write would have crossed the org boundary. Raised out of `flush()`/`commit()`.

    Three ways: an `org_id` that is not the bound org; a `Derived` parent in a different org; or an
    operator-plane request create carrying no org (`_refuses_unstamped_create`).

    A `RuntimeError`, not an HTTP exception: the listener serves every plane, and a cross-org write is
    a caller defect. On a DBOS step (e.g. `app/durable/task_done.py`) an uncaught raise fails the step
    permanently rather than 500ing a request."""


#: `route_or_job` label for the parent-org resolution's own reads, so operators can subtract them
#: (see `_committed_orgs`).
_PARENT_ORG_CALLER = "tenancy:write-seam-parent-org"

#: `{tenant-owned class: its `Derived` census entry, or `None` when `Stored`}`; see `_derivations`.
_DERIVATIONS: dict[type, Any] | None = None


def _derivations() -> dict[type, Any]:
    """The census, reduced to exactly what this listener asks of it, per tenant-owned class.

    `None` means `Stored`. Deferred import (`app/db.py` imports this module; the manifest imports
    `app.models`), memoised because this runs on every flush, and keyed off `TENANT_OWNED` so the
    manifest stays the single home for which verdicts are tenant-owned."""
    global _DERIVATIONS
    if _DERIVATIONS is None:
        from app.tenancy.manifest import TENANCY, Derived, TENANT_OWNED

        _DERIVATIONS = {
            cls: (TENANCY[cls] if isinstance(TENANCY[cls], Derived) else None) for cls in TENANT_OWNED
        }
    return _DERIVATIONS


def stamp_org(session: Session, _flush_context: Any, _instances: Any) -> None:
    """`before_flush` listener: stamp, refuse, or count every tenant-owned instance in this flush.

    Registered on `SessionLocal` after the actor stamp (`app/db.py`); order between the two does not
    matter. `session.is_modified` filters `session.dirty`, which includes unchanged objects.

    The `Stored`-then-`Derived` pass order buys nothing; the parent-org guard is what matters.

    Also checks and stamps the session stamp (`seam.check_session_stamp`, `stamp_if_deciding`) so one
    session used under two deciding scopes is refused on writes too; `after_begin` fires after
    `before_flush`, so the stamp must be set here as well."""
    scope = current_scope()
    derivations = _derivations()
    bound = scope if isinstance(scope, UUID) else None
    check_session_stamp(session, scope)
    stamp_if_deciding(session, scope)

    # Deletes are their own walk: `session.dirty` excludes `session.deleted`.
    _refuse_cross_org_deletes(session, derivations, bound)

    # One walk, two buckets; each object is classified once. Classes absent from `derivations` are left
    # alone.
    stored: list[Any] = []
    derived: list[tuple[Any, Any]] = []
    for objects, is_new in ((session.new, True), (session.dirty, False)):
        for obj in objects:
            cls = type(obj)
            if cls not in derivations:
                continue
            if not is_new and not session.is_modified(obj, include_collections=False):
                continue
            derivation = derivations[cls]
            if derivation is None:
                stored.append(obj)
            else:
                derived.append((obj, derivation))

    for obj in stored:
        _apply(session, obj, bound, scope, derivation=None, parent_orgs={})
    if derived:
        # Unconditional on every plane: fenced to a bind, cron flushes skipped inheritance and kept
        # writing NULL `org_id` rows that `SET NOT NULL` would turn into 500s. Cost: one Core SELECT
        # per distinct parent class per flush. The parent's org is the only value revision 0181's
        # composite key accepts.
        parent_orgs = _committed_orgs(session, derived)
        for obj, derivation in derived:
            _apply(session, obj, bound, scope, derivation=derivation, parent_orgs=parent_orgs)


def _refuses_unstamped_create(scope: object) -> bool:
    """Is a CREATE carrying no org refused in this context, or counted? Boundary B28's switch.

    True only on the operator plane's request side (spec §5.2): operator scope stamps nothing, so a
    request create must name its org. The machinery behind the same `OPERATOR` sentinel (cron
    activities, cron-dispatched command audits —
    listed in the tenancy-scope census) has no request to read an org from and is only counted;
    `seam.mark_request_plane()` is what separates the two. Live: every
    `@superadmin_only(Tenancy.OPERATOR)` request reaches the `True` arm, backstopping create surfaces
    that forget their `org_id` field."""
    return scope is OPERATOR and on_request_plane()


def _apply(
    session: Session,
    obj: Any,
    bound: UUID | None,
    scope: object,
    *,
    derivation: Any,
    parent_orgs: dict[tuple[type, Any], UUID | None] | None,
) -> None:
    """One instance's verdict. Split out so the two passes above cannot drift on it.

    `derivation` is the object's `Derived` census entry, or `None` for a `Stored` one — the only thing
    the two passes differ on."""
    current = getattr(obj, "org_id", None)

    if current is not None and bound is not None and current != bound:
        raise TenancyWriteRefused(
            f"{type(obj).__name__} was written with org_id={current} while org {bound} is bound to "
            "this context. A write may not cross the org boundary; leave `org_id` unset and the "
            "write seam stamps the bound org (app/tenancy/write_seam.py::stamp_org)"
        )

    if bound is None:
        # A `Derived` child inherits rather than naming: its org is one NOT NULL FK hop away. With
        # nothing bound the first resolved parent settles it (`_parent_org`). Not fenced to
        # `session.new` like the refusal below: filling a dirty backlog row is a repair, not a refusal.
        if derivation is not None:
            # `parent_orgs` `None` (read failed) → `_UNSTAMPED_PARENT`; `{}` → `None`. `_parent_org`
            # handles both; neither is a `UUID`, so both fall through correctly.
            inherited = _parent_org(obj, derivation, None, parent_orgs)
            if isinstance(inherited, UUID):
                obj.org_id = inherited
                return
            if inherited is _UNSTAMPED_PARENT:
                # No org to inherit; NULL is the only value the composite key accepts. Fenced to
                # `session.new`: a persistent row's org may be real on a table promoted to `Derived`
                # ahead of its keying revision.
                if obj in session.new:
                    obj.org_id = None
                if _refuses_unstamped_create(scope) and obj in session.new:
                    # The remedy is upstream: a `Derived` class has no `org_id` on its request body,
                    # so the parent must be stamped first.
                    raise TenancyWriteRefused(
                        f"{type(obj).__name__} was created on the operator plane and derives its org "
                        "from a parent that carries none, so there is nothing to inherit and nothing "
                        "to name — a `Derived` class has no `org_id` on its request body by design. "
                        "Stamp the PARENT row's org first; this child then inherits it "
                        "(app/tenancy/write_seam.py::_parent_org)"
                    )
                _count_unbound(obj, scope, derived_unstamped=True)
                return

        # An explicit `org_id` with nothing bound and no contradicting parent is left alone and not
        # counted (the operator plane writes other orgs' rows; B28's create surfaces take this arm).
        # Below the parent walk on purpose, so an explicit value cannot skip the parent check. Explicit
        # writers (`routes/agent/task_reads.py::create_system_task`, `runner_breaks.py::record`, `routes/crons.py`)
        # are safe: the refusal fires only on NULL, and their classes are `Stored`.
        if current is not None:
            return

        if _refuses_unstamped_create(scope) and obj in session.new:
            # Fenced to `session.new`: refusing an UPDATE of a backlog row would 500 its cleanup.
            raise TenancyWriteRefused(
                f"{type(obj).__name__} was created on the operator plane with no org_id, and operator "
                "scope stamps nothing — a create must NAME the org it belongs to. Set `org_id` on the "
                "request (its create surface carries the field); a row landing with none belongs to no "
                "tenant and cannot be repaired once the column is NOT NULL "
                "(app/tenancy/write_seam.py::_refuses_unstamped_create)"
            )
        _count_unbound(obj, scope)
        return

    # Org bound; `current` is NULL or equal. The parent check runs either way, so an explicitly-set
    # bound org on a `Derived` instance cannot skip the guard.
    if derivation is not None:
        parent_org = _parent_org(obj, derivation, bound, parent_orgs)
        if parent_org is _UNSTAMPED_PARENT:
            # No org to inherit: NULL keeps the composite key vacuous. Counted as `derived_unstamped`
            # (the parent needs stamping, not the caller's bind). An explicit value is cleared, fenced to
            # `session.new` so a real committed org on a newly-promoted `Derived` table is never blanked.
            if obj in session.new:
                obj.org_id = None
            _count_unbound(obj, scope, bound=bound, derived_unstamped=True)
            return

    obj.org_id = bound


def _refuse_cross_org_deletes(session: Session, derivations: dict[type, Any], bound: UUID | None) -> None:
    """Refuse a DELETE of another org's row. Checked, never stamped and never counted.

    A cascading delete is the most destructive cross-org write. Nothing is stamped (the row has no
    future) or counted (a delete shrinks the backlog). A NULL `org_id` is left alone, as with dirty
    rows; with nothing bound nothing is refused. Known residual: a NULL-org row may be another
    tenant's, and cascading through an unkeyed reference (`worktrees` off `projects`) destroys more
    than an UPDATE would mislabel; the enforcing read seam and the NOT NULL contraction closed the
    reachable population."""
    if bound is None:
        return
    for obj in session.deleted:
        if type(obj) not in derivations:
            continue
        current = getattr(obj, "org_id", None)
        if current is not None and current != bound:
            raise TenancyWriteRefused(
                f"{type(obj).__name__} with org_id={current} was deleted while org {bound} is bound to "
                "this context. A delete may not cross the org boundary — a cascading key takes the row's "
                "children with it (app/tenancy/write_seam.py::_refuse_cross_org_deletes)"
            )


def _committed_orgs(
    session: Session, derived: list[tuple[Any, Any]]
) -> dict[tuple[type, Any], UUID | None] | None:
    """Every derivation parent's `org_id` as the database holds it, one query per parent class, or
    `None` when the read failed.

    From the database, not the instance: the child's INSERT precedes the parent's UPDATE, and
    `committed_state` is cleared by a refresh of an expired instance. A column select disturbs no
    instance, cannot re-enter this listener (autoflush is suppressed) and runs before this flush's SQL.
    At most one query per distinct parent class (nine), however many instances.

    Absent from the mapping = no such row; present as `None` = row exists with no org.

    A failed read returns `None` instead of raising: `before_flush` runs outside the flush's
    rollback block, so a raise would leave the transaction aborted and unrolled-back. `None` is
    fail-safe — every child is left NULL and counted; `{}` would stamp and risk the FK violation."""
    # No `isinstance(derivation, Derived)` assert: `_derivations()` already guarantees it, and an assert
    # vanishes under `-O`.
    wanted: dict[type, set[Any]] = {}
    for obj, derivation in derived:
        for column, parent_cls in _pairs(obj, derivation):
            pk = getattr(obj, column, None)
            if pk is not None:
                # `_pk` on BOTH sides of this dict, or a `str` pk misses its own row — see `_pk`.
                wanted.setdefault(parent_cls, set()).add(_pk(parent_cls, pk))

    orgs: dict[tuple[type, Any], UUID | None] = {}
    # Attributed to this guard's own label so its read buckets stay subtractable from the `mode='read'`
    # count. `caller()` restores the previous label.
    try:
        with exclusion_log.caller(_PARENT_ORG_CALLER):
            for parent_cls, pks in wanted.items():
                parent_id = parent_cls.__table__.c[PARENT_ID_COLUMN]
                # Both columns come off `__table__`: a Core select carries no mapped entity, so the read
                # seam cannot attach its predicate. With instrumented attributes the caller's org would
                # hide exactly the unstamped and cross-org parents this guard judges, inverting it.
                # `operator_scope()` is unavailable: the session is already stamped with the bound org.
                rows = session.execute(
                    select(parent_id, parent_cls.__table__.c[ORG_COLUMN]).where(parent_id.in_(pks))
                ).all()
                for pk, org in rows:
                    orgs[(parent_cls, _pk(parent_cls, pk))] = _org_value(parent_cls, org)
    except Exception:  # noqa: BLE001 - fail SAFE, not silent; see the docstring's last two paragraphs
        if _remember(f"{_PARENT_ORG_CALLER}:resolution"):
            log.exception(
                "tenancy write seam: could not read the derivation parents' orgs, so every derived "
                "instance in this flush is left unstamped and counted rather than stamped"
            )
        return None
    return orgs


def _org_value(parent_cls: type, org: Any) -> UUID | None:
    """A parent row's `org_id`, as a `UUID` or `None`, refusing anything else loudly.

    A silent coercion of an unexpected type would read every parent as unstamped forever with no log;
    this logs once per class and takes the same fail-safe arm."""
    if org is None or isinstance(org, UUID):
        return org
    if _remember(f"{parent_cls.__name__}:org-type"):
        log.error(
            "tenancy write seam: %s.%s came back as %s (%r), not a UUID — every %s child is being left "
            "unstamped and counted. The column type or the driver changed under this guard",
            parent_cls.__name__,
            ORG_COLUMN,
            type(org).__name__,
            org,
            parent_cls.__name__,
        )
    return None


def _pairs(obj: Any, derivation: Any) -> list[tuple[str, type]]:
    """This entry's (local column, parent class) pairs — delegated to `Derived.pairs`, the single home
    for the rule (a local copy once diverged from `app/tenancy/keys.py`)."""
    return list(derivation.pairs)


def _pk(parent_cls: type, value: Any) -> Any:
    """A derivation parent's primary key, in the Python type of that parent's own `id` column.

    Both sides of the lookup must normalise identically: a `str` pk missing its row reads as "no such
    row" and stamps from the bound scope — the FK violation direction. The column's `python_type`
    decides (covering int-keyed `tasks`/`projects`). An unconvertible value is returned unchanged so the
    FK reports it rather than a `TypeError` out of the listener."""
    if value is None:
        return None
    try:
        target = parent_cls.__table__.c[PARENT_ID_COLUMN].type.python_type
    except (AttributeError, KeyError, NotImplementedError):
        return value
    if isinstance(value, target):
        return value
    try:
        return target(value)
    except (ValueError, TypeError, AttributeError):
        return value


#: Returned by `_parent_org` when a resolved parent carries no `org_id`; distinct from `None` ("nothing
#: resolvable"), which takes the opposite branch.
_UNSTAMPED_PARENT = object()


def _parent_org(
    obj: Any,
    derivation: Any,
    bound: UUID | None,
    parent_orgs: dict[tuple[type, Any], UUID | None] | None,
) -> object:
    """Read this instance's derivation parents out of `parent_orgs` and report what they say.
    Returns `_UNSTAMPED_PARENT` if any resolved parent carries no org, `None` if nothing was resolvable,
    and the org every resolved parent agrees on otherwise. Raises `TenancyWriteRefused` for a parent that
    disagrees.

    `bound=None` means the first resolved parent sets the expectation (the operator-plane inheritance
    arm). `parent_orgs is None` (failed read) answers `_UNSTAMPED_PARENT`. No I/O here.

    A `None` column is skipped (e.g. `role_bindings` derives through one of two principal columns); a
    column naming no row is the single-column FK's finding."""
    if parent_orgs is None:
        return _UNSTAMPED_PARENT

    resolved = None
    unstamped = False
    expected = bound
    for column, parent_cls in _pairs(obj, derivation):
        pk = _pk(parent_cls, getattr(obj, column, None))  # normalized BOTH sides — see `_pk`
        if pk is None:
            # Legitimately unset, or a pending parent with no issued pk yet: stamped from the bound scope.
            continue
        if (parent_cls, pk) not in parent_orgs:
            continue
        parent_org = parent_orgs[(parent_cls, pk)]
        if parent_org is None:
            # Does not end the walk: a cross-org refusal outranks an unstamped parent.
            unstamped = True
            continue
        if expected is None:
            # Nothing bound: this parent is the expectation from here on.
            expected = parent_org
        elif parent_org != expected:
            raise TenancyWriteRefused(
                f"{type(obj).__name__} derives its org from {parent_cls.__name__}.{column}, which is in "
                f"org {parent_org}, while org {expected} is "
                + ("bound to this context" if bound is not None else "what its other parent carries")
                + ". A child may not claim an org its parent does not have "
                "(app/tenancy/write_seam.py::_parent_org)"
            )
        resolved = parent_org
    return _UNSTAMPED_PARENT if unstamped else resolved


def _count_unbound(
    obj: Any, scope: object, *, bound: UUID | None = None, derived_unstamped: bool = False
) -> None:
    """Record one unstamped write into the exclusion log's buffer.

    `bound_org` is `None` except on the `derived_unstamped` arm, where a real org was bound; NULL there
    would falsely say nothing was bound (`scope_kind` says `derived_unstamped`).

    Guarded: this runs on every ORM flush, so a raising counter would 500 every write. Failures are
    logged once per exception type and counted in `dropped()`, so an empty log is distinguishable from
    a broken counter."""
    try:
        exclusion_log.record(
            mapped_class=type(obj).__name__,
            scope_kind=exclusion_log.write_scope_kind_of(
                scope, derived_unstamped=derived_unstamped
            ),
            bound_org=bound,
            mode=exclusion_log.MODE_UNBOUND_WRITE,
        )
    except Exception as exc:  # noqa: BLE001 - a counter must never break a write; see above
        global _DROPPED
        _DROPPED += 1
        if _remember(f"count-unbound:{type(exc).__name__}"):
            log.exception(
                "tenancy write seam: the unbound-write counter raised; its counts are lost "
                "(%d dropped in this process so far)",
                _DROPPED,
            )


#: Keys already logged once (see `_remember`); bounded by the distinct keys its callers produce.
_LOGGED_FAILURES: set[str] = set()

#: Unbound stamps this listener could not record. Read through `dropped()`; see `_count_unbound`.
_DROPPED = 0


def dropped() -> int:
    """How many unbound stamps this process failed to record.

    Non-zero means `tenancy_exclusions` is only a lower bound. Not locked: a lost increment under a
    race is cheaper than a second lock inside a flush."""
    return _DROPPED


def _remember(name: str) -> bool:
    """True the first time this key is seen — the once-per-process gate on every log line here (each
    sits on a per-flush path). Not locked: a duplicate line under a race is harmless.

    Every key carries its caller's prefix, so callers cannot silence one another."""
    if name in _LOGGED_FAILURES:
        return False
    _LOGGED_FAILURES.add(name)
    return True


def reset_failure_log() -> None:
    """Re-arm `_remember` so one test's induced failure cannot silence the next one's
    (`tests/test_tenancy_write_seam.py`)."""
    global _DROPPED
    _LOGGED_FAILURES.clear()
    _DROPPED = 0
