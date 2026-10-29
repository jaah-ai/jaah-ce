"""The resolver: stored overrides per tier, the org scoping refusal, coercion and the typed `get_*` readers."""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

if TYPE_CHECKING:  # annotations only: importing this module needs no sqlalchemy (stdlib-only callers, e.g. app/console_direct.py)
    from sqlalchemy.orm import Session

from app.jaah_env import env_get
from app.operator_settings import cache, catalog
from app.operator_settings.errors import OperatorSettingsScopeError
from app.operator_settings.knobs import Knob

log = logging.getLogger("operator-settings")


def overrides(session: Optional[Session] = None) -> dict[str, Any]:
    """The raw override dict: only the keys an operator changed, as stored (no coercion, no fallback).
    Cached for `_CACHE_TTL_SECONDS`. With no `session`, opens and closes its own. Any DB failure is logged
    and resolves to "no overrides" (fail-closed: a store outage must never break dispatch)."""
    with cache._cache_lock:
        now = time.monotonic()
        if cache._cached_at is not None and (now - cache._cached_at) < cache._CACHE_TTL_SECONDS:
            return dict(cache._cached_overrides)
        started_at_generation = cache._generation

    fresh = _read_overrides(session)   # outside the lock: one slow DB read must not block every caller

    with cache._cache_lock:
        if cache._generation != started_at_generation:
            # Invalidated while reading: `fresh` predates that write, so serve it but never cache it.
            return dict(fresh)
        cache._cached_overrides = fresh
        cache._cached_at = time.monotonic()
        return dict(cache._cached_overrides)


def _read_overrides(session: Optional[Session]) -> dict[str, Any]:
    try:
        if session is not None:
            return _load_row(session)
        from app.db import SessionLocal

        with SessionLocal() as own_session:
            return _load_row(own_session)
    except Exception:
        log.warning("operator_settings: could not read the %r row — proceeding with no overrides",
                    catalog.STATE_KEY, exc_info=True)
        return {}


def _load_row(session: Session) -> dict[str, Any]:
    from app.models.control_plane import SystemState

    row = session.get(SystemState, catalog.STATE_KEY)
    if row is None or not isinstance(row.value, dict):
        return {}
    return dict(row.value)


# ── The org tier ─────────────────────────────────────────────────────────────────────────────────────
# Precedence: org override > operator override > env var > code default (org-tier knobs only).
# The org is an argument at every door, never an ambient scope: the tier's consumers are unbound
# (pool tick, drain, cron threads), where a seam-denied read would silently fall to the operator value.
# A read passing no org is refused once any org overrides that knob (`_refuse_unscoped_read`).

# Every org-tier knob is threaded; the operator-settings tests keep it so (their census of
# unthreaded readers is empty). `git_app_credential_id` is operator-tier. The census is a cross-tenant
# control: `orgs_overriding()` is fleet-wide, so one tenant's value would make another's unthreaded
# read raise. The operator writes org rows (`routes/orgs.py::put_org_settings`), so every org-tier reader
# threads its org and the census is empty.

# Reads run on the fleet-wide plane on their own session, filtered `WHERE org_id = :org` by the
# caller's argument. `detached()` then `operator_scope()`: the caller is often bound to another org.


def org_overrides(org: UUID) -> dict[str, Any]:
    """`{key: value}` for one org: only the knobs that org overrode, as stored.

    Cached per org on `overrides()`' pattern; a DB failure resolves to `{}` (the operator's values). The
    one place "no rows" may mean "no override", because the read either succeeded or raised.

    No `session` parameter: this runs fleet-wide, and `seam.check_session_stamp` raises when one session
    runs under two deciding scopes, so borrowing a request's session would conflict."""
    with cache._cache_lock:
        now = time.monotonic()
        if cache._cached_org_at is not None and (now - cache._cached_org_at) < cache._CACHE_TTL_SECONDS:
            if org in cache._cached_org:
                return dict(cache._cached_org[org])
        started_at_generation = cache._generation
        # A TTL expiry empties the whole map: one timestamp covers every org.
        if cache._cached_org_at is not None and (now - cache._cached_org_at) >= cache._CACHE_TTL_SECONDS:
            cache._cached_org = {}

    fresh = _read_org_overrides(org)

    with cache._cache_lock:
        if cache._generation != started_at_generation:
            return dict(fresh)
        if cache._cached_org_at is None or (time.monotonic() - cache._cached_org_at) >= cache._CACHE_TTL_SECONDS:
            cache._cached_org = {}
            cache._cached_org_at = time.monotonic()
        # One timestamp for the map: a read straddling a window restart can be served for TTL + its own
        # latency. Bounded and never cross-org (keyed by `org`, filtered by `org_id`); writes use the generation.
        cache._cached_org[org] = fresh
        return dict(fresh)


def orgs_overriding() -> frozenset[str]:
    """Every knob key some org has a stored value for, whichever org that is.

    The refusal's input only: refusing on the tier declaration alone would break every operator-plane
    reader the day a knob became org-tier; refusing on data fails exactly when a per-org value exists.
    Fleet-wide, returning keys only (never a value or an org)."""
    with cache._cache_lock:
        now = time.monotonic()
        if cache._cached_overridden_at is not None and (now - cache._cached_overridden_at) < cache._CACHE_TTL_SECONDS:
            return cache._cached_overridden
        started_at_generation = cache._generation

    fresh = _read_overridden_keys()

    with cache._cache_lock:
        if cache._generation != started_at_generation:
            return fresh
        cache._cached_overridden = fresh
        cache._cached_overridden_at = time.monotonic()
        return fresh


def _org_plane():
    """The scope every read in this section runs under (see the section header). A helper so the escape
    has one call site per primitive in the tenancy scope-site census."""
    from app.tenancy import seam

    return (seam.detached(), seam.operator_scope())


def _read_org_overrides(org: UUID) -> dict[str, Any]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.orgs import OrgSetting

    try:
        detached, operator = _org_plane()
        with detached, operator, SessionLocal() as session:
            rows = session.scalars(select(OrgSetting).where(OrgSetting.org_id == org)).all()
            # A row whose key is not an org-tier knob is dropped (written before a tier change, or by hand).
            return {row.key: row.value for row in rows if row.key in catalog.ORG_TIER_KEYS}
    except Exception:
        log.warning(
            "operator_settings: could not read the org_settings rows for org %s — proceeding with no "
            "org overrides", org, exc_info=True,
        )
        return {}


def _read_overridden_keys() -> frozenset[str]:
    from sqlalchemy import distinct, select

    from app.db import SessionLocal
    from app.models.orgs import Org, OrgSetting

    try:
        detached, operator = _org_plane()
        with detached, operator, SessionLocal() as session:
            # Joined to `orgs`: `org_settings.org_id` has no foreign key (see `app/models/base.py::OrgScoped`), so a
            # deleted org's rows would otherwise refuse every unscoped read of that knob forever.
            keys = set(
                session.scalars(
                    select(distinct(OrgSetting.key)).where(
                        OrgSetting.org_id.in_(select(Org.id))
                    )
                ).all()
            )
    except Exception:
        # Swallowed to "nobody has overridden anything", so a store outage never makes every org-tier read
        # raise. The weaker direction for the guard, accepted: it catches a developer's missing argument.
        log.warning(
            "operator_settings: could not read the org_settings key census — treating the org tier as "
            "empty for this TTL", exc_info=True,
        )
        return frozenset()
    return frozenset(keys)


def _refuse_unscoped_read(knob: Knob) -> None:
    """Raise if `knob` is org-tier, some org has overridden it, and the caller passed no org.

    The error is the point: an operator-tier answer here would be a plausible value belonging to nobody,
    served to a caller meant to serve one tenant."""
    if knob.tier != "org":
        return
    if knob.key not in orgs_overriding():
        return
    raise OperatorSettingsScopeError(
        f"{knob.key} is a per-org knob and at least one org has overridden it, but this read passed no "
        "org — answering it from the operator tier would serve one tenant a value belonging to nobody. "
        "Pass the org in: get_*(key, org=<the org this work is for>). "
        "app/operator_settings/reads.py, the org tier."
    )


# ── Coercion + clamping ──────────────────────────────────────────────────────────────────────────────

def _env_text(knob: Knob) -> Optional[str]:
    if not knob.env:
        return None
    # The new spelling, else the legacy one the deployed task definitions still set (`app/jaah_env.py`).
    raw = env_get(knob.env)
    return raw if raw not in (None, "") else None


#: Each `Knob.kind`'s Python type: what a default is cast to and a numeric bound is read as.
_PY_TYPE: dict[str, type] = {"int": int, "float": float, "bool": bool, "str": str}


def _coerce_number(value: Any, knob: Knob) -> Optional[float]:
    """`value` as the knob's number, or None when it is not one (a bool, non-numeric or non-finite)."""
    if isinstance(value, bool):
        return None
    if knob.kind == "int" and isinstance(value, int):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric):
        return None
    return int(numeric) if knob.kind == "int" else numeric


def _coerce_str(value: Any, knob: Knob) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if knob.minimum is not None and len(value) < knob.minimum:
        return None
    if knob.maximum is not None and len(value) > knob.maximum:
        return None
    return value


def _means_unset(value: float, knob: Knob) -> bool:
    """Whether `value` should be read as "not configured" rather than as a number to honour.

    A non-positive value for a knob whose minimum is positive means "unset", a convention load-bearing
    in `console_direct._positive_finite` and `pool_state`: `FLEET_MAX_OBSERVATION_AGE_SECS=0` means the
    catalogue default and `DIRECT_CONSOLE_TOKEN_TTL_SECONDS=0` means 24h, not "clamp to the floor" (a
    1-second freshness bound, 5-minute console credentials).

    A minimum of zero is honoured literally: `fleet_dispatch_cap`'s floor is 1, so its 0 reads as unset, while a
    knob whose minimum is 0 keeps a saved 0."""
    return knob.minimum is not None and knob.minimum > 0 and value <= 0


def _clamp(value: float, knob: Knob) -> float:
    """A number brought into the knob's declared range (int bounds for an int knob).

    Clamped, not rejected: the second stage after `_means_unset`. A well-typed misplaced number is a typo
    to correct toward safety (`console_direct.clamp_ttl_seconds`); only a meaningless value falls through."""
    bound = _PY_TYPE[knob.kind]
    if knob.minimum is not None:
        value = max(bound(knob.minimum), value)
    if knob.maximum is not None:
        value = min(bound(knob.maximum), value)
    return value


def _coerce(knob: Knob, raw: Any) -> tuple[Any, str]:
    """`(value, "")` with `raw` read as the knob's type and clamped, or `(None, why)` when it means nothing:
    the fail-closed chain every stored tier and the env var pass, so no two tiers can disagree on what a
    value says. `why` is `_reject_stored`'s wording."""
    if knob.kind == "bool":
        return (raw, "") if isinstance(raw, bool) else (None, "not a boolean")
    if knob.kind == "str":
        value = _coerce_str(raw, knob)
        return value, ("" if value is not None else "not a string of the accepted length")
    numeric = _coerce_number(raw, knob)
    if numeric is None:
        return None, "unusable"
    if _means_unset(numeric, knob):
        return None, "not a positive value"
    return _clamp(numeric, knob), ""


def _fallback(knob: Knob) -> Any:
    """The env-or-default value for `knob` (no DB override), used by each typed accessor and by
    `effective_settings`'s `fallback` field so the two never disagree."""
    text = _env_text(knob)
    if text is not None:
        if knob.kind == "bool":
            # The house convention (`task_notify._notify_enabled`): on unless the literal string "0".
            return text != "0"
        value, _ = _coerce(knob, text)
        if value is not None:
            return value
    value = _PY_TYPE[knob.kind](knob.default)
    return _clamp(value, knob) if knob.kind in ("int", "float") else value


# ── Typed accessors ──────────────────────────────────────────────────────────────────────────────────

def _reject_stored(key: str, raw: Any, why: str, tier: str) -> None:
    """Log a stored override that will not be honoured, and why (out of range or uncoercible; both matter,
    the range case most since a human typed it). `tier` names whose value was dropped."""
    log.warning(
        "operator_settings: stored %s override for %r is %s (%r) — falling back", tier, key, why, raw,
    )


def _stored_candidates(knob: Knob, org: Optional[UUID]) -> list[tuple[str, Any]]:
    """The stored tiers holding a value for `knob`, highest first: `[("org", …), ("operator", …)]`.

    A list, not the winner: a meaningless value (unparseable, wrong type, non-positive where the minimum
    is positive) falls through to the next tier, then to the env var, never served or crashing.

    Both call-site refusals live here, the one door both tiers pass: an org-tier knob read without an org
    while some org overrides it, and an operator-tier knob read with one."""
    out: list[tuple[str, Any]] = []
    if org is None:
        _refuse_unscoped_read(knob)
    else:
        if knob.tier != "org":
            raise OperatorSettingsScopeError(
                f"{knob.key} is an operator-global knob and was read for org {org}: there is one value "
                "and the operator owns it, so answering per-org would promise a separation this knob "
                "does not have. Read it with no org, or change its tier deliberately in CATALOG."
            )
        values = org_overrides(org)
        if knob.key in values:
            out.append(("org", values[knob.key]))
    ov = overrides()
    if knob.key in ov:
        out.append(("operator", ov[knob.key]))
    return out


def _operator_candidates(knob: Knob) -> list[tuple[str, Any]]:
    """The operator tier alone, with no refusal: what `effective_settings` renders.

    Not a back door around `_refuse_unscoped_read`: `GET /api/settings` asks "what has the operator set",
    well-defined however many orgs override it; refusing would take the page down at the first org value."""
    ov = overrides()
    return [("operator", ov[knob.key])] if knob.key in ov else []


def _stored_value(knob: Knob, candidates: list[tuple[str, Any]]) -> Optional[Any]:
    """The first candidate (highest tier first) that `_coerce` honours, or None when none is; each one
    passed over is logged with why."""
    for tier, raw in candidates:
        value, why = _coerce(knob, raw)
        if value is not None:
            return value
        _reject_stored(knob.key, raw, why, tier)
    return None


def _resolve_typed(knob: Knob, candidates: list[tuple[str, Any]]) -> Any:
    """`candidates` resolved through the typed, fail-closed, clamping chain, then the env var, then the
    default: the one path the four accessors, `node_settings` and the Settings view all read through, so
    none can coerce differently."""
    value = _stored_value(knob, candidates)
    return _fallback(knob) if value is None else value


# The four accessors. `org` is keyword-only, defaulting to None, so operator-tier call sites stay
# unchanged and an org-tier call that forgot it raises (`_refuse_unscoped_read`).


def get_int(key: str, *, org: Optional[UUID] = None) -> int:
    knob = catalog._knob(key)
    return _resolve_typed(knob, _stored_candidates(knob, org))


def get_float(key: str, *, org: Optional[UUID] = None) -> float:
    knob = catalog._knob(key)
    return _resolve_typed(knob, _stored_candidates(knob, org))


def get_bool(key: str, *, org: Optional[UUID] = None) -> bool:
    knob = catalog._knob(key)
    return _resolve_typed(knob, _stored_candidates(knob, org))


def get_str(key: str, *, org: Optional[UUID] = None) -> str:
    knob = catalog._knob(key)
    return _resolve_typed(knob, _stored_candidates(knob, org))


def get_text(key: str) -> str:
    """`get_str`, stripped: the one reader for an operator-plane knob whose value is a token (an id, a
    version, an address list) where surrounding whitespace is never part of the answer."""
    return get_str(key).strip()


def _typed_value(knob: Knob) -> Any:
    """The operator tier's resolved value: what the operator reads and every org inherits."""
    return _resolve_typed(knob, _operator_candidates(knob))

def node_settings() -> dict[str, Any]:
    """Every `NODE_SETTING_KEYS` knob, resolved now (operator tier, so no org): `PoolTickOut.node_settings`."""
    knobs = [catalog._knob(key) for key in catalog.NODE_SETTING_KEYS]
    return {knob.key: _resolve_typed(knob, _stored_candidates(knob, None)) for knob in knobs}
