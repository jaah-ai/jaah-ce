"""Live Cast — an org publishes chosen projects' tasks to a public, read-only page.

Module map:
* `entitlement.py` — how many projects an org may publish and their backfill N (`entitlement()`, the PINNED
  contract every other Live Cast module reads).
* `billing.py` — what the plan editors are offered, and the plan subscription item a customer's own products
  override bills (through the product engine's plan lane).
* `evaluate.py` — the evaluation engine's rules: item rows, staleness, and one item's verdict through the support
  LLM; run durably by `app/durable/live_cast.py` (its queue, workflow, sweep cron and nudge).
* `tokens.py` — a public viewer's short-lived token; `serialize.py` — the allow-list a viewer's responses pass
  through. Both serve the public viewer API, `app/routes/live_public.py`.

The product is a product-engine kind (`app/products/kinds_impl/live_cast.py`): its catalogue product and price, and
the lines a plan, an org's override and a comp carry, are the engine's (`app/products/lines.py`); the legacy line
tables and price row are kept equal for the previous release until the legacy code goes.
"""
