"""The cross-node credential-account fold: the `account_fold` VIEW (migration 0065) plus this reader.

The view keeps the composition rules (e.g. `bool_or(rejected)`); this module keeps the values (e.g.
`AUTH_REJECTED_QUOTA`). A constant crosses into SQL only as a bound parameter, never as a literal in a
view body, so it cannot drift.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app import edition_features
from app.clock import as_utc
from app.entity_shape import iso_or_none

if TYPE_CHECKING:
    from uuid import UUID

    from app import vnodes

log = logging.getLogger("account-fold")

# The canonical auth-rejected quota (the collector's 401/402/403 shape): every percentage explicitly None,
# so no consumer finds a number under a dead token. It does not erase another node's figure (`effective_quota`).
AUTH_REJECTED_QUOTA: dict[str, Any] = {
    "status": "auth_rejected",
    "fiveHourPercent": None, "fiveHourResetsAt": None,
    "weeklyPercent": None, "weeklyResetsAt": None,
    "sonnetWeeklyPercent": None, "sonnetWeeklyResetsAt": None,
    "overQuotaSpendPercent": None,
}

# The keys that make a quota block a reading rather than a bare verdict — exactly the view's `has_percent`
# set. `sonnetWeeklyPercent` is deliberately excluded there as here.
QUOTA_PERCENT_KEYS = ("fiveHourPercent", "weeklyPercent", "overQuotaSpendPercent")


def asserts_a_percentage(block: Any) -> bool:
    """True when a quota block CLAIMS A NUMBER — any of `QUOTA_PERCENT_KEYS` present and not null.

    A block asserting a percentage replaces what is stored and must be dated; one asserting none merges.
    Deliberately looser than `as_quota_int` because it must match the SQL spelling
    `(quota->>'<key>') IS NOT NULL` (upsert arm, migration `0098`); otherwise `{"fiveHourPercent": true}`
    could be accepted undated and then replace a dated block."""
    if not isinstance(block, dict):
        return False
    return any(block.get(key) is not None for key in QUOTA_PERCENT_KEYS)


# ── The verdicts that take an account OUT of the dispatch rotation ─────────────────────────────────────

# Percentages lag by the ~60s quota-mirror refresh, so an account that just walled still reads healthy and
# would be re-dispatched into the wall. A runner's break therefore becomes a verdict (`record_account_verdict`);
# this is the exclusion vocabulary `routes/fleet.py::_account_candidates` tests for `gate_and_tier`.

# Each member clears itself when the next successful probe publishes a real reading (`quota_reading` arm 1
# emits a `None` status).
BLOCKING_QUOTA_STATUSES = (
    # The token is dead and a human must re-login.
    "auth_rejected",
    # The 5h/7d rolling window closed mid-run; clears when it rolls or the mirror shows it.
    "usage_limit",
    # The org's spend cap / credit balance ran out; handled like `usage_limit`.
    "credit_exhausted",
)

# Not in this set: `refresh_blocked` (a percent-less, undatable block already excluded by `quota_stale`) and
# `no_credentials` (a node `status` in `extra`, excluded via `NODE_STATUS_NO_CREDENTIALS`). Neither is a dead
# token, and `auth_rejected` drives the roster's re-login affordance.

_FOLD_SELECT = text(
    """
    SELECT slug, config_dir, enabled, email, display_name, extra,
           auth_rejected, quota, observed_at
      FROM account_fold
     ORDER BY slug
    """
)


def reconstitute(row: Any) -> dict[str, Any]:
    """One `account_fold` row -> the full per-slug record `/api/resources` and the quota readers use.

    Where the view's `auth_rejected` flag becomes `AUTH_REJECTED_QUOTA`. `extra` is merged first so a typed
    column wins over a stale same-named key. `email`/`display_name` are omitted, not None, when unreported;
    `observed_at` is not included (read the view directly)."""
    record: dict[str, Any] = dict(row.extra or {})
    record["slug"] = row.slug
    record["config_dir"] = row.config_dir or row.slug
    record["enabled"] = True if row.enabled is None else row.enabled
    if row.email is not None:
        record["email"] = row.email
    if row.display_name is not None:
        record["display_name"] = row.display_name
    record["quota"] = effective_quota(row)
    return record


def effective_quota(row: Any) -> Optional[dict[str, Any]]:
    """One `account_fold` row's quota block as a reader must see it; one home for dead-token handling.

    Discriminated by the block's own `status`: its own `auth_rejected` collapses the percentages beside it
    (leftovers from before the token died), while a sibling node's rejection leaves the reading standing.
    `authRejectedReason` (and `credentialPath`) survives every collapse."""
    if not row.auth_rejected:
        return row.quota
    block = row.quota if isinstance(row.quota, dict) else {}
    sibling_rejection = block.get("status") != "auth_rejected"
    if sibling_rejection and any(as_quota_int(block.get(key)) is not None for key in QUOTA_PERCENT_KEYS):
        return {**block, "status": "auth_rejected"}
    # Why, and — for a credential the node could not read — where it looked (jaahd `collector.CREDENTIAL_PATH_KEY`).
    said = {key: block[key] for key in ("authRejectedReason", "credentialPath") if block.get(key)}
    return dict(AUTH_REJECTED_QUOTA, **said) if said else AUTH_REJECTED_QUOTA


# How recently a node must have reported for its word to count: 600s = 60s cadence ×
# `node_liveness.STALE_REPORT_MULTIPLE` (10). Shared by `nodes.last_seen` and
# `account_observations.attested_at` checks, and public so `routes/nodes/reads.py::_counts_map` answers the same
# window as the dispatch gate.

# Applied to `attested_at` (CP-stamped), never to the node-forwarded `observed_at`: a window on the node's
# clock would let a node decide its own accounts' dispatchability. Not `pool_state.max_observation_age_secs()`
# either, which is sized against a pool pass.
RECENT_REPORT_SECONDS = 600

# The uuid as text, not the name (unique per org only): these become `account_node_matrix` keys, matched in
# Python against `account_node_records`' node field; name order keeps the columns stable.
_RECENT_NODES_SELECT = text(
    "SELECT CAST(id AS text) FROM nodes"
    " WHERE last_seen IS NOT NULL AND last_seen >= now() - make_interval(secs => :secs)"
    " ORDER BY name, id"
)


def fleet_node_ids(session, *, recent_only: bool = False) -> list[str]:
    """Node uuids (as text) from the roster, name-ordered. `recent_only` narrows to those that have reported
    inside `RECENT_REPORT_SECONDS` — the environment-membership test used by `account_node_matrix`."""
    if recent_only:
        return list(session.scalars(_RECENT_NODES_SELECT, {"secs": RECENT_REPORT_SECONDS}).all())
    return list(session.scalars(text("SELECT CAST(id AS text) FROM nodes ORDER BY name, id")).all())


def account_node_matrix(
    session, slugs: Iterable[str], *, allowed_node_ids: Optional[set[str]] = None,
    tenant_reader: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Per-account state across this environment's nodes: `{slug: [one entry per node]}`, node-ordered.
    Each entry's `node_id` is the node uuid (as text), `node_name` its display name.

    Absence becomes a stated row (`present: False`), since `account_node_records` only speaks about nodes
    that observed an account. Nodes counted: those reporting inside `RECENT_REPORT_SECONDS`, union those
    observing the account (not the ghost-laden roster, not the per-process presence registry).

    `allowed_node_ids` (uuids as text) narrows to the caller's granted nodes (`None` = unrestricted),
    computed by the caller so the grant clause stays in `app/authz/`; both the recent roster and the
    observed `extra` are filtered, since either discloses a node.

    Each entry also carries the NODE's facts that decide whether it can serve the account at all
    (`_serving_facts`): `nodelink_enabled` (the node reports a mounted token share, `nodes.shared_accounts_root`), and `hosting`/`hosted_on` for a Multi-Node Host and the nodes
    it carries. `tenant_reader` withholds a co-located node's host when the host is in another org."""
    observed = account_node_records(session)
    roster = fleet_node_ids(session, recent_only=True)
    if allowed_node_ids is not None:
        roster = [node_id for node_id in roster if node_id in allowed_node_ids]
    matrix: dict[str, list[dict[str, Any]]] = {}
    for slug in slugs:
        by_node = {entry["node_id"]: entry for entry in observed.get(slug, [])}
        extra = [
            n for n in sorted(by_node, key=lambda n: (by_node[n]["node_name"], n))
            if n not in set(roster) and (allowed_node_ids is None or n in allowed_node_ids)
        ]
        matrix[slug] = [
            {**by_node[node_id], "present": True} if node_id in by_node
            else {"node_id": node_id, "node_name": None, "config_dir": None, "quota": None, "status": None,
                  "no_reading_reason": None, "refresh_expires_at": None, "token_source": None,
                  "observed_at": None, "present": False}
            for node_id in [*roster, *extra]
        ]
    facts = _serving_facts(
        session, {entry["node_id"] for entries in matrix.values() for entry in entries},
        tenant_reader=tenant_reader,
    )
    for entries in matrix.values():
        for entry in entries:
            entry.update(facts.get(entry["node_id"], _NO_SERVING_FACTS))
    return matrix


#: A node the roster no longer names: nothing known, so nothing claimed.
_NO_SERVING_FACTS: dict[str, Any] = {
    "nodelink_enabled": False, "hosting": None, "hosted_on": None, "provisioning": False,
}

#: How long after it first appears a never-probed node may read `provisioning`. Bounded, so a node that never comes
#: up (its NodeLink share never mounts) goes back to saying what it waits on rather than "provisioning" forever.
PROVISIONING_WINDOW_SECONDS = 3600

# `probed`: has the node EVER published a successful probe of any account — a `connected` status, a quota verdict,
# or a reading (`QUOTA_PERCENT_KEYS`). A new node publishes `no_credentials` and NodeLink waits until then, which is
# not a fault of any account. `young`: inside `PROVISIONING_WINDOW_SECONDS` of its first appearance.
_SERVING_NODES_SELECT = text(
    """
    SELECT n.id, n.name, n.org_id,
           COALESCE(n.shared_accounts_root, '') <> '' AS has_token_share,
           n.first_seen > now() - make_interval(secs => :window) AS young,
           EXISTS (
             SELECT 1 FROM account_observations o
              WHERE o.node_id = n.id
                AND ((o.extra->>'status') = 'connected'
                     OR (o.quota->>'status') IS NOT NULL
                     OR (o.quota->>'fiveHourPercent') IS NOT NULL
                     OR (o.quota->>'weeklyPercent') IS NOT NULL
                     OR (o.quota->>'overQuotaSpendPercent') IS NOT NULL)
           ) AS probed
      FROM nodes n
     WHERE CAST(n.id AS text) = ANY(:ids)
    """
)


def _serving_facts(session, node_ids: set[str], *, tenant_reader: bool) -> dict[str, dict[str, Any]]:
    """`{node uuid (text): {node_name, nodelink_enabled, hosting, hosted_on, provisioning}}` — whether a node can
    serve an account at all, and its display name.

    `provisioning`: a node that runs work, appeared inside `PROVISIONING_WINDOW_SECONDS`, and has not yet published
    one successful probe of any account — a new node still coming up, whose waiting cells say nothing about the
    account. `hosting` is `"host"` (a Multi-Node Host runs no work, `app/vnodes.py`), `"co_located"` (with
    `hosted_on` naming its host) or None. A tenant reader is never told a host in another org (an
    operator host, `routes/nodes/node_wire.py::_narrow_for_tenant`'s rule): both fields blank, as for any node."""
    if not node_ids:
        return {}
    rows = session.execute(
        _SERVING_NODES_SELECT, {"ids": sorted(node_ids), "window": PROVISIONING_WINDOW_SECONDS}
    ).all()
    hosting: dict[UUID, vnodes.Hosting] = {}
    if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
        from app import vnodes  # Local: `vnodes` reaches the ORM, which this reader otherwise never loads.

        hosting = vnodes.hosting_facts(session, [row.id for row in rows])
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        fact = hosting.get(row.id)
        kind, host = None, None
        if fact is not None and fact.is_host:
            kind = "host"
        elif fact is not None and not (tenant_reader and fact.host_org_id != row.org_id):
            kind, host = "co_located", fact.host_name
        out[str(row.id)] = {
            "node_name": row.name, "nodelink_enabled": bool(row.has_token_share), "hosting": kind, "hosted_on": host,
            "provisioning": kind != "host" and not row.probed and bool(row.young),
        }
    return out


def read_fold(session) -> list[dict[str, Any]]:
    """Every folded account record, slug-ordered — THE source. Order is contractual (cold-start round-robin)."""
    return [reconstitute(row) for row in session.execute(_FOLD_SELECT).all()]


def emails_for_slugs(session, slugs: Iterable[str]) -> dict[str, str]:
    """`{slug: email}` for the slugs that have one folded — the narrow read for "who is this account?".

    Read at read time rather than copied, since email is sensed per-node data. One `= ANY(:slugs)` query for
    the set; a slug with no email is absent (never None); an empty input short-circuits.
    """
    wanted = list(dict.fromkeys(slugs))
    if not wanted:
        return {}
    rows = session.execute(
        text("SELECT slug, email FROM account_fold WHERE slug = ANY(:slugs)"), {"slugs": wanted}
    ).all()
    return {row.slug: row.email for row in rows if row.email}


def account_records(session) -> list[dict[str, Any]]:
    """Every folded account record — the single home for "where do account records come from".

    Always the SQL fold, with no fallback: the retired `claude_resources` blob would yield
    permanently-frozen records rather than a visibly empty list."""
    return read_fold(session)


#: What a node may say about its token's source (`account_node_records`); anything else reads as unsaid.
#: The pair of jaahd `shared_storage.TOKEN_SOURCE_*`, which writes them.
TOKEN_SOURCE_SHARED = "shared"
TOKEN_SOURCES = (TOKEN_SOURCE_SHARED, "local")

# The join is load-bearing: `account_observations.node_id` is a uuid, but this projection's `node_id` is
# matched in Python against `fleet_node_ids()` names; the raw column would make every cell `present: False`
# silently. INNER JOIN is safe (NOT NULL). The projected key keeps the wire name `node_id`.
_ACCOUNT_NODES_SELECT = text(
    """
    SELECT CAST(n.id AS text) AS node_id, n.name AS node_name, o.slug, o.config_dir,
           COALESCE((o.quota->>'status') = 'auth_rejected', false) AS auth_rejected,
           o.quota,
           o.extra->>'status' AS status,
           o.extra->>'no_reading_reason' AS no_reading_reason,
           o.extra->>'refresh_expires_at' AS refresh_expires_at,
           o.extra->>'token_source' AS token_source,
           o.observed_at
      FROM account_observations o
      JOIN nodes n ON n.id = o.node_id
     ORDER BY o.slug, n.name, n.id
    """
)


def account_node_records(session) -> dict[str, list[dict[str, Any]]]:
    """Per-node account state, `{slug: [{node_id, node_name, config_dir, quota, status, no_reading_reason,
    refresh_expires_at, observed_at}, ...]}`, node-ordered — the UNCOLLAPSED view. `node_id` is the node uuid
    (as text), `node_name` display only.

    `no_reading_reason` is the node's word for why its last pass read no quota (jaahd
    `collector.NO_READING_*`), so a `no reading` cell can say which one it is; null when it gave none.

    `token_source` is where the node reads the token: `"shared"` (a mounted token-folder NodeLink) or `"local"`
    (jaahd `shared_storage.token_source`); null from a node that does not say (older), never guessed.

    `refresh_expires_at` is when this node's Claude login dies outright (the refresh token's hard lifetime,
    jaahd `collector._refresh_expires_iso`), so the page can warn before it passes.

    `status` is projected so a bare cell is not rendered as usable. `no_credentials` is not folded into
    `auth_rejected` (different fixes). `auth_rejected` comes from the observation's own `quota.status`,
    never the view's OR'd column, which keeps this per-node. A slug with no observations is absent."""
    records: dict[str, list[dict[str, Any]]] = {}
    for row in session.execute(_ACCOUNT_NODES_SELECT).all():
        records.setdefault(row.slug, []).append({
            "node_id": row.node_id,
            "node_name": row.node_name,
            "config_dir": row.config_dir,
            "quota": effective_quota(row),
            "status": row.status,
            "no_reading_reason": row.no_reading_reason,
            "refresh_expires_at": row.refresh_expires_at,
            "token_source": row.token_source if row.token_source in TOKEN_SOURCES else None,
            "observed_at": iso_or_none(row.observed_at),
        })
    return records


# ══ THE FOLD IS THE READING ════════════════════════════════════════════════════════════════════════════

# The composition rule lives on the read path, off `account_observations`, so the dispatch gate
# (`routes/fleet.py::_account_candidates` -> `jaah_scheduler.gate_and_tier`) and the roster page
# (`routes/accounts.py::_account_out`) cannot disagree. The frozen `accounts` percentage columns are not read
# (`models/credentials.py::Account`).

# `accounts.quota_status` / `quota_status_until` stay: `record_account_verdict` writes them with a 900s TTL.
# `resolved_quota_status` is where the two stores meet.

# Writers of `account_observations`: `fleet_observe.account_upsert_statement` (`POST /api/agent/fleet/observe`)
# and `record_account_verdict`'s merge below.


def as_quota_int(value: Any) -> Optional[int]:
    """A quota percentage as a non-negative int, or None (a bool/float/None/missing reads as no data).

    The single home for "what counts as a reading"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def reading_observed_at(quota: dict[str, Any]) -> Optional[datetime]:
    """The collector's `observedAt` (stamped when it actually read the usage API) as an aware UTC datetime,
    or None when absent/unparseable. It dates the numbers, since the collector re-publishes cached readings."""
    raw = quota.get("observedAt")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamped = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("account-fold: dropping malformed quota observedAt %r", raw)
        return None
    return as_utc(stamped)


# ══ THE QUOTA FRESHNESS BOUND ══════════════════════════════════════════════════════════════════════════

# `gate_and_tier` has no freshness check of its own, and the collector re-publishes cached readings, so a
# stale healthy value would keep licensing dispatch. A NULL stamp reads as stale.

# The exclusion happens at the `enabled` filter, not by blanking `five_hour_percent`: if every candidate
# were blanked, `cold_start` would round-robin the whole set with no quota requirement. Fail closed:
# everything stale dispatches nothing.

# Guarded by `speaks_about_quota(reading)`: arm 1 (percentages + date) is judged by the date; arm 2 (bare
# verdict, undatable) excludes; arm 3 (`{}`) is unguarded so an unprobed account can still reach `cold_start`.

# Known and open: (a) the fleet-wide fold stamps `max(observedAt)` while its percentages come from the argmax
# node (migration 0092), display-only since gate callers read `quota_readings_for_node`; (b) a `no_credentials`
# node status lands in `extra` and reads as arm 3, bounded by the node-scoped candidate read.


_MAX_QUOTA_AGE_DEFAULT = 1800.0


def max_quota_age_secs() -> float:
    """How old a quota reading may be and still license dispatch. One home.

    1800s = 3× the node's default 600s `node_account_probe_interval_secs` (2× its 900s ceiling); it must be
    a multiple, since a reading is legitimately one interval old before the next probe. Three intervals
    tolerate a missed probe."""
    return _MAX_QUOTA_AGE_DEFAULT


def quota_is_stale(
    quota_updated_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
    max_age_secs: Optional[float] = None,
) -> bool:
    """True when a quota reading is too old to license dispatch — including when it has no stamp at all
    (`quota_reading` arm 2)."""
    if quota_updated_at is None:
        return True
    bound = max_quota_age_secs() if max_age_secs is None else max_age_secs
    # `now` omitted uses this process's clock (unlike `standing_verdict`): the operand is the collector's
    # node-clock stamp, bounded by the 120s skew clamp (`fleet_observe._MAX_STAMP_SKEW_SECS`). Production
    # passes `now=db_clock(session)`; kept I/O-free for testing.
    reference = now or datetime.now(timezone.utc)
    stamped = (
        quota_updated_at
        if quota_updated_at.tzinfo
        else quota_updated_at.replace(tzinfo=timezone.utc)
    )
    return (reference - stamped).total_seconds() > bound


def quota_reading(quota: Any, *, observed_at: Optional[datetime] = None) -> dict[str, Any]:
    """The quota facts ONE folded record is entitled to speak to, under the names every reader uses.

    Only keys the record knows, so `{}` means "nothing to say about quota", not "blank". Names are the
    keys `gate_and_tier` and `AccountOut` use. Arms: a reading -> percentages +
    `quota_updated_at` + verdict (including None, which clears a stale `auth_rejected`); a verdict only ->
    `quota_status` alone, undatable, so it excludes; neither -> `{}`, the only bootstrap arm. Dated by
    `observedAt`, else the view's `max(observed_at)`, else None."""
    block = quota if isinstance(quota, dict) else {}
    five = as_quota_int(block.get("fiveHourPercent"))
    weekly = as_quota_int(block.get("weeklyPercent"))
    over = as_quota_int(block.get("overQuotaSpendPercent"))
    status = block.get("status") if isinstance(block.get("status"), str) else None
    if any(value is not None for value in (five, weekly, over)):
        return {
            "five_hour_percent": five,
            "weekly_percent": weekly,
            "over_quota_spend_percent": over,
            "quota_updated_at": reading_observed_at(block) or observed_at,
            "quota_status": status,
        }
    if status is not None:
        return {"quota_status": status}
    return {}


# The keys `quota_reading` may emit: every quota fact. Read by `speaks_about_quota`.
QUOTA_READING_KEYS = (
    "five_hour_percent", "weekly_percent", "over_quota_spend_percent", "quota_updated_at", "quota_status",
)

# The node's own status for the slug (`extra->>'status'`): carried with a reading but not a quota fact, or
# every `connected` account would read as undatable and be excluded.
NODE_STATUS_KEY = "node_status"
# The node's word that an account has NO quota to read (a Shared account whose CLI may not run on the share,
# an API-key login, a login with no plan project) — structural, unlike a reading that failed this pass. Written by
# `apps/jaahd/provider_health.py` (`QUOTA_MODEL_*`, the same spellings); read by
# `routes/fleet.py::_account_candidates`, which then treats the account as having no quota model. Not a quota
# fact, so `speaks_about_quota` ignores it.
QUOTA_MODEL_KEY = "quota_model"
QUOTA_MODEL_NONE = "none"

# "I hold this credential and my probe produced no usable record this pass": no `oauthAccount`, a login in
# flight, or a `probe_account` soft-fail (credential-less accounts never reach observation). It excludes; it
# cannot break bootstrap, since the node stamps it only after one successful probe on that host.

# Self-clearing: a successful probe publishes `status: "connected"`, and a pass with no probe emits an explicit
# `status: None`, since the CP's `||` merge preserves an absent key (`jaahd/main.py::_compose_account_records`).
NODE_STATUS_NO_CREDENTIALS = "no_credentials"


def speaks_about_quota(reading: Optional[dict[str, Any]]) -> bool:
    """True when the fold said something about QUOTA for this slug — not merely that it saw the account.

    A key test, not `bool(reading)`: `NODE_STATUS_KEY` is not a quota fact, and counting it would exclude
    every healthy unprobed account."""
    if not reading:
        return False
    return any(key in reading for key in QUOTA_READING_KEYS)


def _compose_reading(row: Any) -> dict[str, Any]:
    """One fold/observation row -> its quota reading, plus the node's own status and quota-model word when
    it stated them.

    Shared by all reading composers. Each is attached only when non-NULL."""
    reading = quota_reading(effective_quota(row), observed_at=row.observed_at)
    status = getattr(row, "node_status", None)
    if status is not None:
        reading[NODE_STATUS_KEY] = status
    if getattr(row, "quota_model", None) == QUOTA_MODEL_NONE:
        reading[QUOTA_MODEL_KEY] = QUOTA_MODEL_NONE
    return reading


# ══ ABSENT MEANS EXCLUDED ══════════════════════════════════════════════════════════════════════════════

# A slug with no fresh observation is absent from these mappings, and absent excludes; otherwise it would
# survive `gate_and_tier` and land on `cold_start` with no quota requirement.

# The window (`RECENT_REPORT_SECONDS`) is applied in the SELECTs, so a reader cannot dispatch against what it
# cannot see. The reader side is `account_unobserved`, tested beside `auth_rejected` and `quota_stale`.

# Per slug, not a wholesale coverage gate: one freshly provisioned account without an observation must not
# halt every other account.

# The attestation join is in the reader (a reader's policy), not the view. `node_status` needs no join.

# The folded `extra` drops explicit nulls (migration 0092), so here a node's `status: null` yields to a
# sibling's `no_credentials`; the dispatch path reads one node's row, where a null is a null.
_READING_SELECT = text(
    """
    SELECT f.slug, f.auth_rejected, f.quota, f.observed_at,
           f.extra->>'status' AS node_status,
           f.extra->>'quota_model' AS quota_model
      FROM account_fold f
      JOIN (
            SELECT slug, max(attested_at) AS attested_at
              FROM account_observations
             GROUP BY slug
           ) a ON a.slug = f.slug
     WHERE a.attested_at > now() - make_interval(secs => :secs)
     ORDER BY f.slug
    """
)


def quota_readings(session) -> dict[str, dict[str, Any]]:
    """Every FRESHLY-OBSERVED slug's quota reading, keyed by slug — the fleet-wide fold's word on quota.

    Absent means exclude; present with `{}` means observed with nothing to say (bootstrap): keep
    `.get(slug) is None` and `== {}` apart. The window is in the SQL on the CP-stamped, NOT NULL
    `attested_at`."""
    return {
        row.slug: _compose_reading(row)
        for row in session.execute(_READING_SELECT, {"secs": RECENT_REPORT_SECONDS}).all()
    }


def quota_reading_for(session, slug: str) -> Optional[dict[str, Any]]:
    """One slug's quota reading, or **None** when no node has observed it inside the freshness window.

    `None` and `{}` are different answers: `None` is "nobody is attesting this account" (exclude it), `{}`
    is "observed, nothing to say about quota" (bootstrap)."""
    row = session.execute(
        text(
            """
            SELECT f.slug, f.auth_rejected, f.quota, f.observed_at,
                   f.extra->>'status' AS node_status,
           f.extra->>'quota_model' AS quota_model
              FROM account_fold f
              JOIN (
                    SELECT slug, max(attested_at) AS attested_at
                      FROM account_observations
                     WHERE slug = :slug
                     GROUP BY slug
                   ) a ON a.slug = f.slug
             WHERE f.slug = :slug
               AND a.attested_at > now() - make_interval(secs => :secs)
            """
        ),
        {"slug": slug, "secs": RECENT_REPORT_SECONDS},
    ).first()
    return None if row is None else _compose_reading(row)


_NODE_READING_SELECT = text(
    """
    SELECT slug,
           COALESCE((quota->>'status') = 'auth_rejected', false) AS auth_rejected,
           quota,
           observed_at,
           extra->>'status' AS node_status,
           extra->>'quota_model' AS quota_model
      FROM account_observations
       -- `:node_uuid` IS ALREADY RESOLVED by the caller through `node_identity.node_id_or_none` (uuid
       -- only: a name is not an identifier, and node names are not unique).
       -- FAIL-CLOSED in the direction this path needs: an unresolvable value binds NULL,
       -- `node_id = NULL` is UNKNOWN, so no row comes back and the gate sees nothing.
     WHERE node_id = :node_uuid
       AND attested_at > now() - make_interval(secs => :secs)
     ORDER BY slug
    """
)


def quota_readings_for_node(session, node_id: str) -> dict[str, dict[str, Any]]:
    """One NODE's quota readings, keyed by slug — the fold as that node reported it. THE DISPATCH PATH.

    `node_id` is the node uuid, resolved via `node_identity.node_id_or_none`. Per node
    because a login is per (account, node): the view's `bool_or(r.rejected)` would let one rejected node
    veto the account fleet-wide. Unobserved slugs are absent (exclude), and `auth_rejected` comes from the
    observation's own `quota.status`."""
    # Function-level import: nothing here imports `app.*` at module scope, so `fleet_observe` cannot cycle.
    from app.node_identity import node_id_or_none

    rows = session.execute(
        _NODE_READING_SELECT,
        {"node_uuid": node_id_or_none(session, node_id), "secs": RECENT_REPORT_SECONDS},
    ).all()
    return {row.slug: _compose_reading(row) for row in rows}


# ── Writing a verdict back: the runner's own break as evidence about its account ───────────────────────

# How long a runner-reported verdict is believed (migration 0093 stores the deadline per row).

# 900s = fifteen ~60s mirror refreshes: enough to cover the mirror catching up through missed probes, short
# enough to return re-login/top-up remedies within a quarter hour. Erring long is the dangerous direction.
ACCOUNT_VERDICT_TTL_SECONDS = 900

# A `usage_limit` break naming a plan-window wall (weekly / 7-day / monthly) with no parseable reset: such a
# wall lasts hours to days, so 24h. See `verdict_ttl_seconds`.
ACCOUNT_VERDICT_TTL_LONG_SECONDS = 86400

# Ceiling on a TTL read off a reset instant (a weekly window never resets later); bounds a misparse.
ACCOUNT_VERDICT_TTL_MAX_SECONDS = 7 * 86400

# Reset instants carried by `usage_limit` lines (causes: `apps/jaahd/hooks/break_cause.py::_PATTERNS`), passed
# through as the break's `detail`:
#   * `Claude AI usage limit reached|<epoch seconds>`;
#   * `… limit reached · resets Sep 8, 11am (America/New_York)`, or `resets 11pm (America/New_York)` meaning
#     the next such time in that zone. No year is printed.
# `continuing automatically at 3pm` names no zone and is deliberately not parsed.
_RESET_EPOCH = re.compile(r"limit reached\|(\d{10})(?!\d)", re.I)
_RESET_CLOCK = re.compile(
    r"resets\s+"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\s*"
    r"\((?P<tz>[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)*)\)",
    re.I,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# A wall that is a plan window rather than the 5h one, when the line says so but names no instant.
_PLAN_WINDOW = re.compile(r"weekly|7[- ]day|monthly", re.I)


def verdict_ttl_seconds(status: str, detail: str, *, now: Optional[datetime] = None) -> int:
    """How long a runner-reported verdict is believed, in seconds — PURE, and it never raises.

    A standing verdict excludes for its whole TTL (`resolved_quota_status` lets nothing non-blocking clear
    it), and against a weekly wall the mirror may never publish an excluding figure, so only `usage_limit`
    is scaled. `auth_rejected` stays short so a re-login is not exile
    (tests/test_runner_breaks.py::test_the_account_verdict_expires_so_a_re_login_is_not_permanent_exile).

    Ladder for `usage_limit`:
      1. a future reset instant parsed off `detail` → seconds until it, clamped to [default, max];
      2. else a plan-window word → `ACCOUNT_VERDICT_TTL_LONG_SECONDS`;
      3. else the default.
    Any parse failure falls down the ladder. `now` is injectable for tests."""
    if status != "usage_limit" or not detail:
        return ACCOUNT_VERDICT_TTL_SECONDS
    try:
        now = now or datetime.now(timezone.utc)
        instant = _reset_instant(detail, now)
        names_window = bool(_PLAN_WINDOW.search(detail))
        if instant is not None:
            seconds = int((instant - now).total_seconds())
            if seconds > 0:
                # A week needs corroboration: `detail` is runner-authored and a standing verdict cannot be
                # cleared from the console, so an instant is capped at the plan-window ceiling unless the line
                # also names the window.
                ceiling = ACCOUNT_VERDICT_TTL_MAX_SECONDS if names_window else ACCOUNT_VERDICT_TTL_LONG_SECONDS
                return max(ACCOUNT_VERDICT_TTL_SECONDS, min(seconds, ceiling))
        if names_window:
            return ACCOUNT_VERDICT_TTL_LONG_SECONDS
    except Exception:  # noqa: BLE001 — a TTL must never take down the verdict write; the default is safe
        # Truncated: the line is arbitrary session output; the log needs only enough to find it.
        log.warning("could not read a reset instant off break detail %r; using the default TTL",
                    detail[:120])
    return ACCOUNT_VERDICT_TTL_SECONDS


def _reset_instant(detail: str, now: datetime) -> Optional[datetime]:
    """The reset instant a break line names, or None when it names none this module can place in time."""
    epoch = _RESET_EPOCH.search(detail)
    if epoch:
        return datetime.fromtimestamp(int(epoch.group(1)), tz=timezone.utc)
    clock = _RESET_CLOCK.search(detail)
    if clock is None:
        return None
    try:
        zone = ZoneInfo(clock.group("tz"))
    except Exception:  # noqa: BLE001 — not a zone we know; the caller falls down the ladder
        return None
    hour, minute = int(clock.group("hour")), int(clock.group("minute") or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None  # the regex admits two digits; the clock does not admit "99pm"
    hour = hour % 12 + (12 if clock.group("ampm").lower() == "pm" else 0)
    local_now = now.astimezone(zone)
    if clock.group("mon"):
        month, day = _MONTHS.index(clock.group("mon").lower()[:3]) + 1, int(clock.group("day"))
        # No year is printed: take the year placing the date closest to now (handles New Year, never turns a
        # passed reset into one a year out). A nonexistent date is a torn line.
        candidates = []
        for year in (local_now.year - 1, local_now.year, local_now.year + 1):
            try:
                candidates.append(datetime(year, month, day, hour, minute, tzinfo=zone))
            except ValueError:
                pass
        return min(candidates, key=lambda c: abs(c - now), default=None)
    # No date: the next time that wall clock reads so in that zone — later today, else tomorrow.
    instant = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if instant <= now:
        instant += timedelta(days=1)
    return instant


def db_clock(session) -> Optional[datetime]:
    """The DATABASE's `now()`, read once per answer — the reader's clock for anything the DB stamped.

    `quota_status_until` is `now() + interval`, so only that clock may judge it; every row is judged against
    one instant. `None` when unreadable; callers fail safe (`standing_verdict` keeps the verdict applied)."""
    return session.scalar(text("SELECT now()"))


def standing_verdict(
    status: Optional[str], until: Optional[datetime], *, db_now: Optional[datetime]
) -> Optional[str]:
    """`accounts.quota_status` when it is still believed, else None — AN EXPIRED VERDICT IS NOT A VERDICT.

    Shared by the dispatch gate and the roster page. A NULL `until` is a fossil (the only writer always
    stamps one), not "forever"; coupled to `resolved_quota_status` branch 2. An unreadable `db_now` leaves
    the verdict standing."""
    if status is None or until is None:
        return None
    if db_now is not None and until <= db_now:
        return None
    return status


def resolved_quota_status(
    observed_status: Optional[str],
    *,
    stored: Optional[str],
    until: Optional[datetime],
    db_now: Optional[datetime],
) -> Optional[str]:
    """The ONE verdict a reader acts on, from the two stores that hold verdicts. Both readers call this.

    A blocking status from either store wins; nothing non-blocking clears one:
      1. the observed status when blocking (so `usage_limit` never masks an observed `auth_rejected`);
      2. else the standing column verdict when blocking;
      3. else the observed status, including None (how a probe clears a stale rejection).

    So a fresh healthy reading cannot clear a live break verdict early: comparing a node clock to the CP's
    with zero tolerance would let a fast node clear it with a pre-break reading."""
    if observed_status in BLOCKING_QUOTA_STATUSES:
        return observed_status
    standing = standing_verdict(stored, until, db_now=db_now)
    if standing in BLOCKING_QUOTA_STATUSES:
        return standing
    return observed_status


def record_account_verdict(
    session, *, config_dir: str, node_id: Optional[str], status: str, detail: str = ""
) -> bool:
    """Record a `quota_status` verdict against the account a runner was using when its session broke.
    Returns True when a verdict was actually written. `detail` is the break's matched transcript line; it
    sets how long the verdict stands (`verdict_ttl_seconds`) and is stored, redacted, as the verdict's reason.

    Writes a verdict only, never a percentage or date. Two places, because readers consult both: the
    `accounts.quota_status` column (with a deadline) and the reporting node's observation row, merged to
    keep its percentages. Does not commit; the caller owns the transaction."""
    if status not in BLOCKING_QUOTA_STATUSES:
        log.warning(
            "account verdict %r is not one of %s — refusing to write it; a verdict the selector does not "
            "read would exclude nothing while looking like it had",
            status, list(BLOCKING_QUOTA_STATUSES),
        )
        return False
    # An unregistered reporter may not condemn an account (`node_identity.is_registered_node`), gated here so
    # every caller inherits it: the route takes `node_id`/`account` as body fields, and a live verdict
    # outranks observations for its whole TTL.

    # An empty `node_id` skips the gate (the CP's reclaim paths report with no node). This does not prove the
    # reporter ran on the account.

    # "No node" is `None` or `""` (the route's `Field(default="")`); the `if node_id:` guards cover both and
    # keep a non-uuid out of the uuid column comparison. New callers should pass `None`.

    # Function-level import: nothing here imports `app.*` at module scope, so `fleet_observe` cannot cycle.
    from app.node_identity import is_registered_node, warn_unregistered_node

    # A wire resolver: only the node's uuid passes; a name-presenting node is warned about.
    if node_id and not is_registered_node(node_id, session):
        warn_unregistered_node(node_id, f"the {status!r} account verdict for {config_dir!r}")
        return False
    # One `try` around the whole write: `record_runner_break` calls this before parking the slot, so an
    # escaping exception would skip the park and expose the tree to the next dispatch. The cost is one missed
    # verdict.
    try:
        return _write_account_verdict(
            session, config_dir=config_dir, node_id=node_id, status=status,
            ttl=verdict_ttl_seconds(status, detail), detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — see above; a verdict must never take down the park
        log.error(
            "could not record the %r verdict for account %r from node %r (%s: %s). The account stays in "
            "the dispatch rotation until the quota mirror catches up (~60s); the break itself is still "
            "recorded and the slot is still parked.",
            status, config_dir, node_id, type(exc).__name__, exc,
        )
        return False


def _shown_detail(detail: str) -> Optional[str]:
    """A break's runner-authored sentence as the roster shows it: secret shapes masked, then the keyword pass and cap.
    Fails closed on the text, never on the verdict: no redactor means no sentence."""
    from app import secret_redact
    from app.agent_session_lifecycle import END_DETAIL_MAX, redacted_error

    try:
        shaped, _ = secret_redact.redact(detail or "")
    except secret_redact.RedactionUnavailable:
        return None
    return redacted_error(shaped, END_DETAIL_MAX).strip() or None


def _write_account_verdict(
    session, *, config_dir: str, node_id: Optional[str], status: str, ttl: int, detail: str = ""
) -> bool:
    """The verdict write itself, separated so the caller's `try` visibly covers every statement."""
    # Function-level import, repeated here: importing only in the caller once raised `NameError` inside its
    # blanket `try`, silently dropping every verdict.
    from app.entity_shape import SYSTEM_ACTOR_ID
    from app.account_auth import account_slug_from_ref
    from app.node_identity import node_id_or_none

    # One reverse resolution for both spellings (`account_auth.account_slug_from_ref`, handling
    # `apikey:<slug>` and per-node dirs via `node_clone.account_slug_from_dir`). API-key accounts must resolve:
    # `auth_rejected` is the only automatic brake on the spillover lane, and without it a revoked key loops
    # dispatch indefinitely.
    slug = session.scalar(
        text("SELECT slug FROM accounts WHERE slug = :slug LIMIT 1"),
        {"slug": account_slug_from_ref(config_dir)},
    )
    if not slug:
        log.warning(
            "runner break names account %r, which matches no row in `accounts` — no verdict recorded "
            "(the account will keep its current standing in the dispatch gate)",
            config_dir,
        )
        return False
    # The stored column carries an expiry (`ttl`, migration 0093), or no re-login or top-up could undo it.
    # `now()` is the DB clock every reader uses via `standing_verdict`; never `datetime.now()` here.
    #
    # `updated_by_id` names the system actor; `updated_at` is owned by `trg_accounts_set_updated_at` (0109).
    session.execute(
        text(
            "UPDATE accounts"
            "   SET quota_status = :status,"
            "       quota_status_until = now() + make_interval(secs => :ttl),"
            "       quota_status_detail = :detail,"
            "       updated_by_id = CAST(:actor AS uuid)"
            " WHERE slug = :slug"
        ),
        {
            "status": status, "ttl": ttl, "slug": slug,
            # Every verdict replaces the last one's reason, an empty one included.
            "detail": _shown_detail(detail),
            "actor": str(SYSTEM_ACTOR_ID),
        },
    )
    # The reporting node's own observation row, so its reading agrees with the column. `jsonb_set(...,
    # create_missing => true)` adds `status`; `COALESCE` handles a NULL block; `cast(... AS text)` since `::`
    # collides with SQLAlchemy binds. Verdict-only, so legal under `asserts_a_percentage` (hence `0098`
    # clears only undated percentage blocks). Timestamps are untouched, or a stale reading would look fresh.
    merged = 0
    if node_id:
        merged = session.execute(
            text(
                """
                UPDATE account_observations
                   SET quota = jsonb_set(
                           COALESCE(quota, '{}'::jsonb), '{status}', to_jsonb(cast(:status AS text)), true
                       )
                 -- `:node_uuid` is resolved by the caller through `node_identity.node_id_or_none` (uuid
                 -- only). Binding a non-uuid straight at this uuid column would raise `invalid input syntax for type uuid`
                 -- inside the caller's protective `try` — a SILENTLY SWALLOWED verdict, i.e. an account
                 -- that hit a wall staying in the dispatch rotation. Passing the resolved value is also
                 -- why the `""`/None fleet-wide sentinel is merely a no-op here rather than an error: it
                 -- resolves to NULL and `node_id = NULL` matches nothing.
                 WHERE node_id = :node_uuid AND slug = :slug
                """
            ),
            {
                "status": status,
                "node_uuid": node_id_or_none(session, node_id),
                "slug": slug,
            },
        ).rowcount
    log.warning(
        "account %s marked %r from a runner break on node %s (observation rows updated: %d) — it is out "
        "of the dispatch rotation for the next %.0f minutes, or until a fresh probe reads it healthy",
        slug, status, node_id or "<unknown>", merged, ttl / 60.0,
    )
    return True


def clear_auth_rejection(session, *, slug: str, org_id) -> tuple[int, int]:
    """Lift an `auth_rejected` verdict once a login of `slug` has succeeded: the login is that verdict's fix.

    Clears the standing column verdict (with its reason) and the `auth_rejected` status on every node's
    observation row, so the roster and the dispatch gate stop excluding the account at once instead of
    waiting out the TTL. Every node of `org_id`, not only the one logged in on: the login mints into the
    shared token folder. A node that still cannot use the account re-reports the rejection on its next
    probe. Only what a login fixes is touched: a node's `token`/`unreadable` rejection (`apps/jaahd/collector.py`'s
    `authRejectedReason`; absent on older nodes) and the column's `auth_rejected`, which only a runner's
    `auth_expired` break writes. A billing or seat rejection, or a usage wall, stays.

    Returns `(accounts cleared, observation rows cleared)`. Does not commit; the caller owns the transaction."""
    from app.entity_shape import SYSTEM_ACTOR_ID

    verdicts = session.execute(
        text(
            "UPDATE accounts"
            "   SET quota_status = NULL, quota_status_until = NULL, quota_status_detail = NULL,"
            "       updated_by_id = CAST(:actor AS uuid)"
            " WHERE slug = :slug AND org_id = :org AND quota_status = 'auth_rejected'"
        ),
        {"slug": slug, "org": str(org_id), "actor": str(SYSTEM_ACTOR_ID)},
    ).rowcount
    observations = session.execute(
        text(
            "UPDATE account_observations"
            "   SET quota = quota - 'status' - 'authRejectedReason' - 'credentialPath'"
            " WHERE slug = :slug AND org_id = :org AND quota->>'status' = 'auth_rejected'"
            "   AND COALESCE(quota->>'authRejectedReason', 'token') IN ('token', 'unreadable')"
        ),
        {"slug": slug, "org": str(org_id)},
    ).rowcount
    if verdicts or observations:
        log.info(
            "account %s: a login succeeded, so its auth_rejected verdict is lifted (verdict cleared: %d, "
            "observation rows cleared: %d)", slug, verdicts, observations,
        )
    return verdicts, observations
