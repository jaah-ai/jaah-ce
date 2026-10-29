"""Operator-editable runtime knobs: the single home for the "Settings" catalogue and resolver.

Operator overrides live in one `system_state` KV row (`catalog.STATE_KEY`), holding only changed keys;
org overrides in `app/models/orgs.py::OrgSetting`. A knob qualifies only if its consumer re-reads it per call
(a control that saves and changes nothing is worse than none; see the note under `catalog.CATALOG`).

Precedence: org override (org-tier knobs only) > operator override > env var > code default. The org
is always an argument, never an ambient scope: readers are unbound, and an unscoped org-tier read is
refused once any org overrides it. Cached `cache._CACHE_TTL_SECONDS` per process; a DB failure reads as
"no overrides". Fail-closed accessors: a meaningless value (`reads._means_unset`) falls through a tier
and a well-typed out-of-range value is clamped, so neither can widen a safety bound.

Modules (import the one that owns a name; this package re-exports nothing):
- `knobs` — the `Knob` declaration, its tiers and the `_Section` card markers.
- `errors` — `OperatorSettingsError` (a refused write) and `OperatorSettingsScopeError` (a mis-scoped read).
- `catalog_tasks`, `catalog_billing`, `catalog_runners`, `catalog_orgs`, `catalog_integrations`,
  `catalog_ops` — each Settings group's knobs, in render order.
- `catalog` — `CATALOG` assembled in render order, `STATE_KEY`, and the key sets derived from it.
- `cache` — the process-wide override cache and `invalidate_cache`.
- `reads` — stored overrides per tier, coercion and the typed `get_*` readers.
- `defaults` — resolved views: create-form prefills, the Settings page rows, live picker choices.
- `writes` — validation and the operator-tier and org-tier writers.
"""
