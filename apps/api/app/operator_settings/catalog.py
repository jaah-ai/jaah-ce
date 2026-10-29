"""The assembled Settings catalogue: `CATALOG` in render order and the key sets derived from it."""
from __future__ import annotations

from app import edition_features
from app.operator_settings import (
    catalog_billing,
    catalog_integrations,
    catalog_ops,
    catalog_orgs,
    catalog_runners,
    catalog_tasks,
)
from app.operator_settings.knobs import Knob

STATE_KEY = "operator_settings"

# ── Sections render in declaration order inside each group; the Settings page orders the groups themselves
# (`apps/web/src/pages/Settings.tsx::CATEGORY_GROUPS`), a new group leading the listed ones, under
# "Other", in declaration order. A `_Section` marker names the card for every knob after it. A tenant-visible
# group name is scope-neutral (`tests/test_settings.py` censuses org-tier copy).
# Each group's knobs live in its `catalog_*` module; the order below is the render order.
CATALOG: tuple[Knob, ...] = (
    *catalog_tasks.KNOBS,
    *catalog_billing.KNOBS,
    *catalog_runners.KNOBS,
    *catalog_orgs.KNOBS,
    *catalog_integrations.KNOBS,
    *catalog_ops.KNOBS,
)

# `app_title` is deliberately not a knob: inert (`apps/web/index.html` hardcodes the tab title; only
# the OpenAPI title reads APP_TITLE), so it would save and change nothing. It stays a column on the
# `app_settings` singleton, returned by `GET /api/settings` for the public-login twin.

_BY_KEY: dict[str, Knob] = {knob.key: knob for knob in CATALOG}

#: Every knob's tenancy verdict, derived from the catalogue, never re-typed.
#: The operator-settings tests assert key-set equality both ways, every value in
#: `TIERS`, and that a `Knob` with no `tier` raises.
KNOB_TIERS: dict[str, str] = {knob.key: knob.tier for knob in CATALOG}

#: The runner-behaviour kill switches: the wire contract of `GET /api/agent/settings/runner-flags`.
#: A subset with no other home (not derivable from `group` or a prefix); keeps that route enumerable.
#: Pinned by the operator-settings tests against the route model and the runner-side mirror of these keys.
RUNNER_FLAG_KEYS: tuple[str, ...] = (
    "runner_no_poll_gate",
    "runner_read_once",
    "runner_batch_recon",
    "runner_hook_dedupe",
    "runner_project_memory",
)

#: The "Infrastructure" > "Daemon" knobs: the wire contract of `PoolTickOut.node_settings`, derived from the section.
#: The node-side mirror is `apps/jaahd/node_settings.py::SPECS`, pinned against this catalogue (keys,
#: defaults, ranges) by the daemon's node-settings test.
NODE_SETTING_KEYS: tuple[str, ...] = tuple(
    knob.key for knob in CATALOG if (knob.group, knob.section) == ("Infrastructure", "Daemon")
)

#: The org-tier subset, as a set: the writer and the resolver both test membership.
ORG_TIER_KEYS: frozenset[str] = frozenset(key for key, tier in KNOB_TIERS.items() if tier == "org")

#: The org-tier knobs a tenant's own Org Admin may override for its org (`Knob.tenant_editable`).
TENANT_EDITABLE_KEYS: frozenset[str] = frozenset(knob.key for knob in CATALOG if knob.tenant_editable)
#: Org-tier keys a tenant is never served (`Knob.tenant_visible`); `routes/settings.py::_settings_out` drops them.
TENANT_HIDDEN_KEYS: frozenset[str] = frozenset(knob.key for knob in CATALOG if not knob.tenant_visible)


def in_edition(knob: Knob) -> bool:
    """Does this edition carry the feature `knob` configures (`Knob.feature`)? Read at call time."""
    return knob.feature is None or edition_features.enabled(knob.feature)


def knob_label(key: str) -> str:
    """A knob's label as Settings shows it — for a reader naming a blank one to the operator."""
    return _knob(key).label


def _knob(key: str) -> Knob:
    knob = _BY_KEY.get(key)
    if knob is None:
        raise KeyError(f"unknown operator setting {key!r}")
    return knob
