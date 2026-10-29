"""The fleet account-selection policy: the single home for the gate, the tier and the defaults.

An account's 5h/7d quota is global, so "may this account run work" is decided here only. Pipeline:
enabled/auth_rejected filter → runner cap → hard 5h/7d gates → fewest-active-runners tier →
cold-start fallback. Pure; the live caller is `app/routes/fleet.py::_best_account`, which takes the
first tier member (ties go to discovery order). The node holds no copy.

API-key accounts have no quota to read (`app/account_auth.py`), so their readings are never defaulted
(a missing percentage read as 0 would win every tier). Which lane an API-key account rides is declared per
provider (`agent_providers.AgentProvider.api_key_lane`, carried on the candidate): a SPILLOVER account
(a key beside a provider's subscription) is consulted only when every other account yields nobody
(per-token billing = overflow lane); a PEER account (a provider whose only kind is a key) is an ordinary pick beside the OAuth
ones, ranked by the same fewest-runners tier (`is_spillover`).

The DEFAULT_* constants are the single home for the numbers."""
from __future__ import annotations

from typing import Optional

from app import account_auth

# The 5h plan-quota exclusion ceiling (80%).
DEFAULT_HIGH_PCT = 80
# 7-day plan-quota exclusion ceiling. Gated at exhaustion (100), not 80: a 7d-exhausted account walls
# the runner into the legacy-runtime CLI's usage-limit prompt immediately.
DEFAULT_WEEKLY_HIGH_PCT = 100

#: How many fleet runners one account may carry at once. An account at the cap is excluded
#: outright like a disabled one; the operator override is the `max_runners_per_account` knob, resolved
#: by callers (this module does no I/O). A hard cap by owner decision: with every account at it,
#: selection answers no account and the task waits (fleet ceiling = cap × accounts). A soft fallback
#: was rejected because the limit would never bind. It binds both account kinds (it counts runners,
#: not quota), so it is not in `QUOTA_DERIVED_EXCLUSION_KEYS`. An account's own `max_runners` (a provider
#: declaring `per_account_runner_cap`) replaces this number for that account, above or below it.
DEFAULT_MAX_RUNNERS_PER_ACCOUNT = 2


def is_spillover(candidate: dict) -> bool:
    """Whether this candidate rides the API-key SPILLOVER lane: an API-key account whose provider does not
    declare itself a peer (`agent_providers.AgentProvider.api_key_lane`). Anything but an explicit "peer"
    is spillover, so a candidate missing the key, or carrying an unknown value, keeps the old position
    (fail-safe: a lane is never widened by accident). OAuth candidates are never spillover."""
    return account_auth.is_api_key(candidate.get("auth_kind")) and candidate.get("api_key_lane") != "peer"


def runner_cap(candidate: dict, max_runners_per_account: Optional[int]) -> Optional[int]:
    """The concurrent-runner cap that binds this candidate: its own `max_runners` when set, else the fleet's
    `max_runners_per_account`. `max_runners_per_account=None` means "count no cap" (consoles, and the
    cold-start question) and disables BOTH, because a human session is not a fleet runner either way."""
    if max_runners_per_account is None:
        return None
    own = _own_cap(candidate)
    return own if own is not None else max_runners_per_account


def _own_cap(candidate: dict) -> Optional[int]:
    """The candidate's own `max_runners`, or None when it has none (a non-positive or non-int value is
    none: the column's check refuses one, and a fairness input should not depend on that)."""
    own = candidate.get("max_runners")
    if isinstance(own, int) and not isinstance(own, bool) and own > 0:
        return own
    return None


def runner_count(candidate: dict) -> int:
    """A candidate's active-runner count, defaulting to 0 on a missing/non-int/negative value.

    `bool` is rejected because it subclasses `int`; a negative would sort ahead of an idle account. The
    producer is a SQL `COUNT`, but a fairness input should not depend on that."""
    runners = candidate.get("runners")
    if not isinstance(runners, int) or isinstance(runners, bool) or runners < 0:
        return 0
    return runners


# ── Why a candidate is out of the running — one enumeration, and it is the filter ──────────────────────
#
# Each entry is (candidate key, polarity, operator-facing reason). `exclusion_reason` walks them and
# `gate_and_tier` filters on it, so one expression decides and one vocabulary explains (each absence
# has a different fix). Order is the reporting order, most specific first.
EXCLUSION_REASONS: tuple[tuple[str, bool, str], ...] = (
    ("enabled", False, "the account is disabled in the roster"),
    # `account_fold.BLOCKING_QUOTA_STATUSES`: a usage limit or a refused sign-in that has not yet cleared.
    ("auth_rejected", True, "it hit a usage limit or its sign-in was refused, and that hasn't cleared yet"),
    ("auth_pair_disallowed", True, "its provider and auth type are not on its organization's plan"),
    ("account_unobserved", True, "no node has attested this account inside the freshness window"),
    ("node_reports_no_credentials", True, "the node holding it reports no usable credential for it"),
    ("quota_stale", True, "its reading is too old, or undatable, to license dispatch"),
    # A node fact, binding every kind: the asking node is a Multi-Node Host (`app/vnodes.py`). Last, because
    # every reason above is about an account an operator can fix. A co-located node over its disk cap is NOT
    # refused here: its cap is a warning (the console's "Over cap" chip), never a reason. Whether a full disk
    # stops a launch is decided in one place, `app/launch_disk.py` (the disk gate and the GB floor), not here.
    ("vnode_host", True, "the asking node is a host, which runs no work itself"),
)

#: The quota-derived subset: absences in the observation chain feeding the 5h/7d gate, meaningless
#: (not merely unset) for an API-key account, which has no quota window or per-node credential dir.
#: Skipping them for that kind declines a test whose subject does not exist. `enabled`,
#: `auth_rejected` and `auth_pair_disallowed` are roster facts and bind every kind.
QUOTA_DERIVED_EXCLUSION_KEYS: frozenset[str] = frozenset(
    {"account_unobserved", "node_reports_no_credentials", "quota_stale"}
)


def exclusion_reason(
    candidate: dict, *, max_runners_per_account: Optional[int] = DEFAULT_MAX_RUNNERS_PER_ACCOUNT
) -> Optional[str]:
    """Why this candidate is out of the running, or None when it is in it.

    The filter and the explanation are the same function, so the operator-facing reason cannot drift
    from the gate. A candidate omitting an excludes-when-true key is not excluded by it (tests build
    partial dicts); `enabled` has the opposite polarity, so omitting it excludes.

    An API-key candidate skips only `QUOTA_DERIVED_EXCLUSION_KEYS`; `is_api_key` is False when
    `auth_kind` is absent, so the OAuth path is unchanged.

    The concurrency cap is checked here, last (reporting order only), because the reason must name both
    numbers; it binds both kinds. The number is `runner_cap`'s: the account's own cap where it has one,
    else the fleet's. `max_runners_per_account=None` applies no cap and is only for `gate_and_tier`'s
    "is this fleet measured?" question and for consoles."""
    api_key = account_auth.is_api_key(candidate.get("auth_kind"))
    for key, excludes_when_true, reason in EXCLUSION_REASONS:
        if api_key and key in QUOTA_DERIVED_EXCLUSION_KEYS:
            continue
        if bool(candidate.get(key)) is excludes_when_true:
            return reason
    runners = runner_count(candidate)
    cap = runner_cap(candidate, max_runners_per_account)
    if cap is not None and runners >= cap:
        whose = "this account's own" if _own_cap(candidate) is not None else "fleet's"
        return f"it is already carrying {runners} fleet runner(s), at or over the {whose} {cap}-runner limit"
    return None


def account_exclusion_reason(candidate: dict) -> Optional[str]:
    """Why this account may not carry its own run on, or None: `exclusion_reason` narrowed to the account's
    roster and verdict facts, for re-issuing a lease to the account already running there (a hand-off).

    Skips `QUOTA_DERIVED_EXCLUSION_KEYS` (the account is live on this node; freshness is the dispatcher's
    question) and `vnode_host` (a node fact), and counts no runner cap: the run replaces its own runner."""
    for key, excludes_when_true, reason in EXCLUSION_REASONS:
        if key in QUOTA_DERIVED_EXCLUSION_KEYS or key == "vnode_host":
            continue
        if bool(candidate.get(key)) is excludes_when_true:
            return reason
    return None


def account_ref(candidate: dict) -> str:
    """The string that names this candidate on the wire: its `config_dir` for OAuth, `apikey:<slug>` for an
    API key. Derived from `auth_kind` + `slug` so this stays a pure function of hand-built dicts."""
    if account_auth.is_api_key(candidate.get("auth_kind")):
        return account_auth.api_key_account_ref(candidate.get("slug"))
    return candidate["config_dir"]


def _reads_a_window(candidate: dict) -> bool:
    """Does this candidate carry a quota reading — a 5h OR a weekly figure? Some plans publish only a weekly
    window (`agent_providers.quota_windows` names each provider's), and that is a reading like any other."""
    return candidate.get("five_hour_percent") is not None or candidate.get("weekly_percent") is not None


def gate_and_tier(
    candidates: list[dict],
    *,
    high_pct: int = DEFAULT_HIGH_PCT,
    weekly_high_pct: int = DEFAULT_WEEKLY_HIGH_PCT,
    max_runners_per_account: Optional[int] = DEFAULT_MAX_RUNNERS_PER_ACCOUNT,
    spend_gate: bool = True,
) -> tuple[str, list[dict], list[dict]]:
    """The account-selection policy: roster filter, runner cap, 5h/7d (optionally spend) gates and the
    fewest-runners tier. Returns `(status, enabled, tier)`:
      * `("empty", [], [])`                      — nothing dispatchable.
      * `("no_qualifying_account", enabled, [])` — live 5h data but no headroom (escalate, never round-robin).
      * `("tier", enabled, tier)`                — the fewest-active-runners tier.
      * `("cold_start", enabled, [])`            — no candidate reports 5h data ⇒ round-robin `enabled`.
    `enabled` is the surviving set of the arm that answered (the arms are disjoint).

    `max_runners_per_account` can empty the answer (the task waits); `None` applies no cap, for console
    and `commands` sessions the cap does not count. `spend_gate`: the fleet path does not gate on spend.
    Absences exclude at the filter; blanking a percentage would fail open into `cold_start`.

    Spillover: the spillover arm (`is_spillover`) runs only when the primary arm yields
    `"empty"`/`"no_qualifying_account"`, skipping the quota gates. A PEER API-key account is in the
    primary arm: it skips the quota-derived exclusions (`exclusion_reason`) and qualifies through
    `has_quota_model=False` like a no-quota OAuth account, so its absent reading is never 0% used nor
    "exhausted", and it never makes a fleet "measured". With both arms empty the primary verdict is
    returned."""
    # The partition. A missing `auth_kind` reads as OAuth; a missing lane as spillover.
    api_key_candidates = [c for c in candidates if is_spillover(c)]
    oauth_candidates = (
        # The same list object when there is nothing to partition off.
        candidates if not api_key_candidates
        else [c for c in candidates if not is_spillover(c)]
    )

    # ── The primary arm: OAuth candidates plus peer API-key ones ─────────
    enabled = [
        c for c in oauth_candidates
        if exclusion_reason(c, max_runners_per_account=max_runners_per_account) is None
    ]
    if enabled:
        # The cold-start question is asked of the uncapped set (a fail-open fix): cold start is ungated, and
        # asked of the capped set "every measured account is full" would look like "nothing is measured",
        # round-robining onto an account of unknown quota. The cap decides who may be picked, never whether the
        # fleet is measured; a full fleet falls through to `no_qualifying_account`/`"empty"`. Computed inside
        # this branch so the extra pass runs only when there is a candidate.
        measured = [c for c in oauth_candidates if exclusion_reason(c, max_runners_per_account=None) is None]
        if not any(_reads_a_window(c) for c in measured):
            # `enabled`, never `candidates`: the filter above is the single exclusion point and must cover
            # cold start too (a rejected account has NULL quota, like an unprobed one). No spillover:
            # cold start is a pick. It admits a broken probe when nobody reports, and a fleet of only no-quota
            # accounts sits here permanently, at no cost. The capped `enabled` is what is round-robined.
            return "cold_start", enabled, []
        qualified = [
            c
            for c in enabled
            # No quota to gate on ⇒ nothing to gate: an account whose vendor publishes no usage reading
            # (`app/agent_providers.py`) would otherwise be dropped from the tier once any other account had a
            # reading. The key defaults True when absent, so existing candidates are unchanged. Not "any null
            # reading qualifies": that would fail open for an account whose probe is transiently failing.
            # A reading is ANY dated window: a plan with only a weekly window (no 5h one) is measured, and
            # gated on the window it has.
            if (not c.get("has_quota_model", True))
            or (_reads_a_window(c)
                and (c.get("five_hour_percent") is None or c["five_hour_percent"] < high_pct)
                and (not spend_gate or c.get("over_quota_spend_percent") is None
                     or c["over_quota_spend_percent"] < high_pct)
                and (c.get("weekly_percent") is None or c["weekly_percent"] < weekly_high_pct))
        ]
        if qualified:
            best_rc = min(runner_count(c) for c in qualified)
            return "tier", enabled, [c for c in qualified if runner_count(c) == best_rc]

    # The OAuth arm yielded nobody; composed here so the spillover gets a chance first. Non-empty
    # `enabled` here means live 5h data with no headroom.
    oauth_verdict = ("no_qualifying_account" if enabled else "empty", enabled, [])
    if not api_key_candidates:
        return oauth_verdict

    # ── The API-key arm (spillover): roster facts only, then the same tier rule ────────────────────────
    api_key_enabled = [
        c for c in api_key_candidates
        if exclusion_reason(c, max_runners_per_account=max_runners_per_account) is None
    ]
    if not api_key_enabled:
        # Nothing anywhere: return the OAuth verdict, which still distinguishes no-headroom from nothing.
        return oauth_verdict
    best_rc = min(runner_count(c) for c in api_key_enabled)
    # No `cold_start` arm: an API-key account is never measured, only dispatchable or not.
    return "tier", api_key_enabled, [c for c in api_key_enabled if runner_count(c) == best_rc]
