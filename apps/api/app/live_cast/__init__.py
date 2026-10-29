"""Live Cast — an org publishes chosen projects' tasks to a public, read-only page.

Module map:
* `entitlement.py` — how many projects an org may publish and their backfill N (`entitlement()`, the PINNED
  contract every other Live Cast module reads).
* `billing.py` — Live Cast's catalogue price (`rental_prices` `live_cast:project`), what the plan editors are
  offered, and the subscription item a customer's own products override bills.
* `evaluate.py` — the evaluation engine's rules: item rows, staleness, and one item's verdict through the support
  LLM; run durably by `app/durable/live_cast.py` (its queue, workflow, sweep cron and nudge).
* `tokens.py` — a public viewer's short-lived token; `serialize.py` — the allow-list a viewer's responses pass
  through. Both serve the public viewer API, `app/routes/live_public.py`.

The product is a plan-product kind (`app/models/billing.py::PLAN_PRODUCT_KINDS`), so a plan, an org's override and a comp
carry it through `app/plan_terms.py` and `app/plan_bundles.py` like any other line.
"""
