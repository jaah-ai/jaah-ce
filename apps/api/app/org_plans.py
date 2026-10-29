"""The plan catalogue an org's owner sees: the operator's `billing_plans` rows (Products › Plans,
`app/routes/billing_plans.py`), in `sort_order`. Retired rows (`active = false`) are left out — except the org's own
plan, which it keeps (retiring stops new assignment only).

The org's own plan is `selectable` while it has no price (choosing it stamps `orgs.plan_chosen_at`, which ticks the
setup card; moving between free plans is the operator's, buying a paid plan is the Upgrade flow).

A plan's PRICE is the mirror of its Stripe Price (`billing_plans.price_unit_amount`, copied from the Price
Stripe created, never typed into the column — C4: Stripe owns money). It is shown only while billing is
connected AND the row was priced on the pinned account (`app/billing/state.py::pinned`); otherwise it is
None. `offer` is the one word for what a tenant may do about a plan, single-homed here:

* `selectable` — the org's own plan, while it has no price;
* `price` — a self-serve plan with a price on the connected account (`$X/month`);
* `contact` — a sales-assigned plan (`self_serve = false`, Enterprise), whatever its price;
* `none` — a Free plan (an explicit $0, `is_free`) that is not the org's own: it is assigned, never bought, so there is
  nothing to offer;
* `coming_soon` — anything else: billing not connected, or a self-serve plan with no price yet (D-04).

The Change dialog lists the org's own plan and every `selectable`, `price` or `contact` plan; `hidden_reason` says
why an active plan is not listed (`coming_soon`, `none`), for the operator's Products › Plans. `(default)`
(`plan_terms.DEFAULTS_KEY`) is every plan's fallback, never a plan of the catalogue.

PER-AGENT PRICING. A `per_agent` plan's Price is per agent per interval: the org buys a count of agents
in [`min_agents` (NULL = 1), `max_agents` (NULL = no upper bound)], the plan item's quantity, and that count is its
Agents cap (`app/plan_terms.py::caps_of`). The range is the org's own on its own plan when an operator overrode it
(`bounds`, `plan_terms.min_agents_of`/`max_agents_of`); it bounds what the org may buy; on a plan not priced per agent the max is the Agents cap. `yearly` is the optional yearly Price, mirrored like the monthly one;
an org buys Monthly or Yearly only where it is set. `agent_refusal` is THE range check every purchase and change
passes, and its sentence names the range and the next plan that sells that count.

Reads the cache only; never calls Stripe.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app import edition_features, plan_terms
from app.operator_settings import reads as settings_reads
from app.models.billing import BillingPlan
from app.models.orgs import Org

if TYPE_CHECKING:
    from app.billing import state

#: The default billing interval (a plan may also sell yearly, `Plan.yearly`) and the one currency we sell in (PRD §6).
INTERVAL: Final = "month"
YEARLY: Final = "year"
INTERVALS: Final = (INTERVAL, YEARLY)
#: Annual billing off (`yearly_on`): no purchase is billed yearly and no subscription switches to yearly; a yearly
#: subscription keeps its interval and may still switch to monthly.
YEARLY_OFF_TEXT: Final = "Annual billing is off. Choose Monthly."
CURRENCY: Final = "usd"
#: Stripe's smallest USD charge. A price is 0 (Free: kept here, no Stripe Price — `app/billing/plans.py::make_free`) or
#: at least this.
MIN_CHARGE: Final = 50


def yearly_on() -> bool:
    """Annual billing is sold: the operator's `billing_annual` switch. Off, a tenant sees no yearly price at all."""
    return settings_reads.get_bool("billing_annual")


def free_or_chargeable(cents: int) -> int:
    """A price validator: refuses an amount Stripe could never charge (1–49 cents)."""
    if 0 < cents < MIN_CHARGE:
        raise ValueError("Free (0) or at least $0.50")
    return cents

Offer = Literal["selectable", "price", "contact", "coming_soon", "none"]


@dataclass(frozen=True)
class Price:
    """A plan's Stripe Price as mirrored: `amount` in cents, exactly the Price's `unit_amount`."""

    amount: int
    currency: str
    interval: str


@dataclass(frozen=True)
class Plan:
    """One catalogue entry as a tenant sees it."""

    key: str
    name: str
    blurb: str
    selectable: bool
    offer: Offer
    price: Optional[Price]
    #: The yearly Price; None = the plan sells monthly only.
    yearly: Optional[Price] = None
    per_agent: bool = False
    #: The agent range a purchase must fall in (`per_agent` plans); `max_agents` None = no upper bound.
    min_agents: int = 1
    max_agents: Optional[int] = None


def priced_on(row: BillingPlan, account: Optional[state.Account]) -> Optional[Price]:
    """`row`'s mirrored Price when it was made on `account`; None when unpriced, or priced elsewhere."""
    if (
        account is None
        or row.stripe_price_id is None
        or row.price_unit_amount is None
        or row.stripe_account_id != account.account_id
        or row.livemode is not account.livemode
    ):
        return None
    return Price(amount=row.price_unit_amount, currency=row.currency or CURRENCY, interval=INTERVAL)


def yearly_priced_on(row: BillingPlan, account: Optional[state.Account]) -> Optional[Price]:
    """`row`'s mirrored yearly Price when it was made on `account` (the row's own account: a yearly Price is pushed
    only beside a monthly one there, `app/billing/plans.py::push_price`)."""
    if (
        account is None
        or row.yearly_stripe_price_id is None
        or row.yearly_price_unit_amount is None
        or row.stripe_account_id != account.account_id
        or row.livemode is not account.livemode
    ):
        return None
    return Price(amount=row.yearly_price_unit_amount, currency=row.currency or CURRENCY, interval=YEARLY)


def kept_price(row: BillingPlan, defaults: bool, interval: str = INTERVAL) -> Optional[Price]:
    """`row`'s price kept on the row with no Stripe Price: Free ($0) on a plan, any amount on `(default)`
    (`defaults`), which is never sold. None otherwise."""
    if row.stripe_price_id is not None:
        return None
    amount = row.yearly_price_unit_amount if interval == YEARLY else row.price_unit_amount
    if amount is None or not (defaults or amount == 0):
        return None
    return Price(amount=amount, currency=row.currency or CURRENCY, interval=interval)


#: An agent range: `(low, high)`, high None = no upper bound.
Bounds = tuple[int, Optional[int]]


def _agents(row: BillingPlan) -> Bounds:
    """`row`'s own agent range, a bound it inherits read from `(default)` (`plan_terms.field`)."""
    session = object_session(row)
    dflt = plan_terms.defaults(session) if session is not None and row.inherits else None
    low: Optional[int] = plan_terms.field(row, dflt, "min_agents")  # type: ignore[assignment]
    high: Optional[int] = plan_terms.field(row, dflt, "max_agents")  # type: ignore[assignment]
    return low or 1, high


def min_agents(row: BillingPlan) -> int:
    return _agents(row)[0]


def bounds(session: Session, row: BillingPlan, org_id: Optional[UUID] = None) -> Bounds:
    """The agent range `org_id` may buy on `row`: the org's own (`plan_terms.min_agents_of`/`max_agents_of`) when
    `row` is its plan, else the plan's."""
    org = session.get(Org, org_id) if org_id is not None else None
    if org is not None:
        org = plan_terms.terms_org(session, org)
        if org.plan_key == row.key:
            low, high = plan_terms.min_agents_of(org, row).value, plan_terms.max_agents_of(org, row).value
            # The plan's own max lowered under the org's min empties the range: the plan's stands.
            if high is None or high >= low:
                return low, high
    return _agents(row)


def agent_range(row: BillingPlan, limits: Optional[Bounds] = None) -> str:
    """The plan's agent range (`limits`, else the plan's own) as a tenant reads it: "3–9 agents", "3 or more agents",
    "1 agent"."""
    low, high = limits or _agents(row)
    if high is None:
        return f"{low} or more agents"
    if high == low:
        return f"{low} agent" if low == 1 else f"{low} agents"
    return f"{low}–{high} agents"


def in_range(row: BillingPlan, agents: int, limits: Optional[Bounds] = None) -> bool:
    low, high = limits or _agents(row)
    return agents >= low and (high is None or agents <= high)


def agent_refusal(session: Session, row: BillingPlan, agents: int, org_id: Optional[UUID] = None) -> Optional[str]:
    """None when `row` sells `agents` agents (to `org_id`, `bounds`); else the sentence refusing it, naming the range
    and what to do next: the next active plan by `sort_order` whose range contains the count, or the range to pick
    from when there is none."""
    limits = bounds(session, row, org_id)
    low, high = limits
    if in_range(row, agents, limits):
        return None
    following = session.scalars(
        select(BillingPlan).where(
            BillingPlan.active.is_(True), BillingPlan.self_serve.is_(True), BillingPlan.per_agent.is_(True),
            BillingPlan.key != row.key,
        ).order_by(BillingPlan.sort_order, BillingPlan.key)
    ).all()
    above = high is not None and agents > high
    candidates = [plan for plan in following if plan.sort_order >= row.sort_order] if above else list(following)
    target = next((plan for plan in candidates if in_range(plan, agents)), None)
    head = f"{row.name} is for {agent_range(row, limits)}."
    if target is None:
        if high is None:
            return f"{head} Choose at least {low}."
        return f"{head} Choose between {low} and {high}."
    if above:
        assert high is not None
        return f"{head} For {high + 1} or more, choose {target.name}."
    return f"{head} For fewer than {low}, choose {target.name}."


def is_free(row: BillingPlan, defaults_row: Optional[BillingPlan]) -> bool:
    """`row`'s price, its own or `(default)`'s (`plan_terms.field`), is an explicit $0."""
    return plan_terms.field(row, defaults_row, "price") == 0


def _offer(row: BillingPlan, price: Optional[Price], connected: bool, free: bool) -> Offer:
    # Not self-serve (Enterprise) is contact-sales whatever its price says: never "Free: assigned only".
    if not row.self_serve:
        return "contact" if connected else "coming_soon"
    if free:
        return "none"
    if not connected:
        return "coming_soon"
    return "price" if price is not None else "coming_soon"


def hidden_reason(row: BillingPlan, account: Optional[state.Account], defaults_row: Optional[BillingPlan]) -> Optional[str]:
    """Why an org not on `row` cannot see it in Change, in a few words; None when it can, `row` is retired (its
    Retired chip says so), or billing is not connected (the list says that once, not on every plan)."""
    if not row.active:
        return None
    offer = _offer(row, priced_on(row, account), account is not None, is_free(row, defaults_row))
    if offer == "none":
        return "Free: assigned only"
    if offer != "coming_soon" or account is None:
        return None
    return "No price: Coming soon"


def entry(
    row: BillingPlan, account: Optional[state.Account], current: Optional[str] = None,
    defaults_row: Optional[BillingPlan] = None, limits: Optional[Bounds] = None,
) -> Plan:
    """`row` as a catalogue entry, against the pinned `account` (None = billing not connected), for an org on
    the plan `current`; `defaults_row` is `(default)` (`is_free`), `limits` the agent range the org may buy (`bounds`)."""
    price = priced_on(row, account)
    low, high = limits or _agents(row)
    selectable = row.key == current and price is None
    return Plan(
        key=row.key,
        name=row.name,
        blurb=row.blurb or "",
        selectable=selectable,
        offer="selectable" if selectable else _offer(
            row, price, account is not None, row.key != current and is_free(row, defaults_row),
        ),
        price=price,
        yearly=yearly_priced_on(row, account) if price is not None else None,
        per_agent=row.per_agent,
        min_agents=low,
        max_agents=high,
    )


def pinned(session: Session) -> Optional[state.Account]:
    """`app/billing/state.py::pinned`; None in the community edition, which has no billing."""
    if not edition_features.enabled("billing"):
        return None
    from app.billing import state

    return state.pinned(session)


def catalogue(session: Session, current: Optional[str] = None, org_id: Optional[UUID] = None) -> list[Plan]:
    """Every active `billing_plans` row in `sort_order`, plus `current` (the org's own plan) even when retired; with
    `org_id`, its own plan carries the org's agent range (`bounds`). Never `(default)`."""
    account = pinned(session)
    offered = BillingPlan.active.is_(True)
    rows = session.scalars(
        select(BillingPlan)
        .where(offered if current is None else offered | (BillingPlan.key == current))
        .where(BillingPlan.key != plan_terms.DEFAULTS_KEY)
        .order_by(BillingPlan.sort_order, BillingPlan.key)
    ).all()
    dflt = plan_terms.defaults(session)
    return [
        entry(row, account, current, dflt, bounds(session, row, org_id) if org_id is not None and row.key == current else None)
        for row in rows
    ]
