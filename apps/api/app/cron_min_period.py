"""THE SINGLE HOME of the minimum cron period: how often any one org's cron may fire, and who sets it.

Four tiers, each bounded below by the one above it:

1. `operator_floor` — the operator's own minimum (`operator_settings` knob `OPERATOR_KNOB`, 10s). It
   governs the operator plane, org zero (Operator), whose system crons it bounds too, and
   it is the absolute floor: no other tier may go under it.
2. `tenant_default` — what a tenant org gets with no override: a plan term (`billing_plans.cron_min_period_seconds`),
   the org's plan's own (`plan`), else the `(default)` plan row's (`default`, `plan_terms.defaults`), else
   `TENANT_DEFAULT` — read as at least the operator floor.
3. `orgs.cron_min_period_seconds` — the operator's per-org override, lower or higher than the default but
   never under the operator floor (refused on write, raised to it on read).
4. `orgs.cron_min_period_own_seconds` — the org administrator's own value, which can only RAISE the
   minimum: a value under the upstream (tiers 1-3) is refused on write and ignored on read.

`effective` = the largest of the upstream and the org's own value.

Two enforcements, both reading `resolve`: a create/update that sets a schedule faster than the effective
minimum is refused (`app/routes/crons.py`), and a firing of a row saved before the minimum was raised is
skipped while less than the minimum has passed since its last firing (`app/cron_runtime.py`). A row whose
schedule complies is never skipped, so an early wake or a Run now of it is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import plan_terms
from app.operator_settings import reads as settings_reads
from app.cron_schedule import CronScheduleError, min_gap_over_next_occurrences
from app.models.orgs import Org
from app.tenancy.ids import is_tenant_org

#: The operator's own minimum — a key of `app/operator_settings/catalog.py::CATALOG`.
OPERATOR_KNOB = "cron_min_period_operator_seconds"
#: The tenant default with no `(default)` row (or a NULL there): an hour.
TENANT_DEFAULT = 3600

#: The largest minimum any tier may set: a day. A minimum above it would forbid a daily cron.
MAX_SECONDS = 86400

FallbackSource = Literal["operator", "plan", "default"]
UpstreamSource = Literal["operator", "plan", "default", "org_override"]


@dataclass(frozen=True)
class CronMinPeriod:
    """One org's minimum, every tier visible so a form can say where the number came from."""

    operator_floor: int
    tenant_default: int
    #: What the org follows with no override: the tenant default for a tenant (its plan's, else `(default)`'s), else
    #: the operator minimum.
    fallback: int
    fallback_source: FallbackSource
    #: The operator's per-org override as stored (None = follows the floor or the tenant default).
    override: Optional[int]
    #: The minimum the org administrator may not go under, and which tier set it.
    upstream: int
    upstream_source: UpstreamSource
    #: The org administrator's own value as stored (None = none).
    own: Optional[int]
    effective: int


def operator_floor() -> int:
    return settings_reads.get_int(OPERATOR_KNOB)


def _tenant_default(session: Session, org: Optional[Org]) -> tuple[int, Literal["plan", "default"]]:
    """The tenant default before the floor: the org's plan's own value (`plan`), else `(default)`'s, else
    `TENANT_DEFAULT` (`default`). An org on `(default)` itself, or no org, reads `default`."""
    plan = plan_terms.plan_row(session, plan_terms.terms_org(session, org).plan_key) if org is not None else None
    dflt = plan_terms.defaults(session)
    row = plan_terms.field_row(plan, dflt, "cron_min_period_seconds")
    if row is not None and row is not dflt and row.cron_min_period_seconds is not None:
        return row.cron_min_period_seconds, "plan"
    value = dflt.cron_min_period_seconds if dflt is not None else None
    return (TENANT_DEFAULT if value is None else value), "default"


def resolve(session: Session, org_id: UUID) -> CronMinPeriod:
    """`org_id`'s minimum. An org with no row reads as a tenant with no overrides (fail-closed: the
    tenant default is the stricter answer)."""
    floor = operator_floor()
    org = session.get(Org, org_id)
    raw_default, default_source = _tenant_default(session, org)
    tenant_default = max(floor, raw_default)
    override = org.cron_min_period_seconds if org is not None else None
    own = org.cron_min_period_own_seconds if org is not None else None
    fallback: int
    fallback_source: FallbackSource
    if is_tenant_org(org_id):
        fallback, fallback_source = tenant_default, default_source
    else:
        fallback, fallback_source = floor, "operator"
    upstream: int
    source: UpstreamSource
    if override is not None:
        upstream, source = max(floor, override), "org_override"
    else:
        upstream, source = fallback, fallback_source
    effective = max(upstream, own) if own is not None else upstream
    return CronMinPeriod(
        operator_floor=floor,
        tenant_default=tenant_default,
        fallback=fallback,
        fallback_source=fallback_source,
        override=override,
        upstream=upstream,
        upstream_source=source,
        own=own,
        effective=effective,
    )


class MinPeriodError(ValueError):
    """A value a tier may not set; the message is the sentence a form shows."""


def check_override(value: Optional[int]) -> Optional[int]:
    """The operator's per-org override: None clears it; otherwise between the operator floor and a day."""
    if value is None:
        return None
    floor = operator_floor()
    if isinstance(value, bool) or not isinstance(value, int) or not floor <= value <= MAX_SECONDS:
        raise MinPeriodError(
            f"The minimum period is between your own minimum ({floor}s) and {MAX_SECONDS}s."
        )
    return value


def check_own(value: Optional[int], period: CronMinPeriod) -> Optional[int]:
    """The org administrator's value: None clears it; otherwise between the upstream and a day."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not period.upstream <= value <= MAX_SECONDS:
        raise MinPeriodError(
            f"The minimum period is between {period.upstream}s, the lowest your organization may set, "
            f"and {MAX_SECONDS}s."
        )
    return value


def schedule_gap(
    schedule_kind: str, cron_expression: Optional[str], interval_seconds: Optional[int]
) -> float:
    """The smallest gap, in seconds, between a schedule's firings: its interval, or a cron expression's
    smallest gap over its next firings (`app/cron_schedule.py::min_gap_over_next_occurrences`).

    MEASURED IN UTC, NOT THE CONTROL PLANE'S ZONE. A schedule's cadence does not depend on the zone, but a
    local-time walk across a DST change reads a daily, weekly or monthly expression as firing an hour
    apart, which would refuse every such cron under a minimum above an hour.

    Raises `CronScheduleError` when the expression cannot be measured."""
    return min_gap_over_next_occurrences(
        schedule_kind, cron_expression, interval_seconds, datetime.now(timezone.utc), timezone.utc
    )


def below_minimum_detail(gap: float, period: CronMinPeriod) -> str:
    """The refusal a save below the minimum gets."""
    return (
        f"This schedule fires every {gap:g}s at its fastest; your organization's minimum period is "
        f"{period.effective}s. Choose a slower schedule."
    )


__all__ = [
    "CronMinPeriod",
    "CronScheduleError",
    "MAX_SECONDS",
    "MinPeriodError",
    "OPERATOR_KNOB",
    "TENANT_DEFAULT",
    "below_minimum_detail",
    "check_override",
    "check_own",
    "operator_floor",
    "resolve",
    "schedule_gap",
]
