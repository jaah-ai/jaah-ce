"""The ledger statistics the router's policy reads, per configuration and stratum (architecture §6).

`LedgerStats` is the seam the policy reads through `current()`; `OutcomeLedgerStats` answers it from the outcome
ledger (`app/outcome_ledger/metrics.py`, which owns every metric definition — nothing here defines one). Statistics
are the org's own first; where the org has fewer than `MIN_OUTCOMES` judged outcomes for a configuration, the
operator-plane prior (`source='prior'`) answers when the OTHER orgs' rows for it meet the ledger's cross-org floor
(`metrics.MIN_ORGS` orgs, `metrics.MIN_OUTCOMES` judged rows) and no one org holds more than `MAX_ORG_SHARE` of them,
so a reader can neither subtract its own rows nor read one dominant org through the aggregate. The prior is read on the
fleet plane only (the lease pass; an operator request with no org filter): a tenant-bound read never takes one. It is
an aggregate only: no org name, id or per-org figure ever enters a `ConfigStats`. `NullLedgerStats` (every configuration has no data) answers in an edition
without the ledger's read side (`[feature.exec_dashboard]` in `packages/editions/features.toml`), and in tests.

How a ledger row meets a router candidate (`decisions.config_key`): a built-in `builtin:<provider>:<tier>` takes the
provider's rows of the tier's model family where that family is the provider's own, else all its rows (a tier names
a family of one vendor's models; another vendor's tiered run is recorded under its own model id); an agent `agent:<id>:-` takes that SDK agent's
rows. The stratum `<persona>|<bucket>|<language>` takes the persona's rows of the bucket and language — and those whose
size or language the ledger does not know (an attempt that never merged has no diff), so a failure counts against its
configuration in every stratum rather than in none.
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Protocol, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from app import agent_providers, edition_features, model_catalog
from app.clock import utcnow
from app.tenancy import seam

if TYPE_CHECKING:
    from app.outcome_ledger import metrics as ledger

#: Whether this edition ships the ledger's read side (`outcome_ledger/metrics.py`). The import below spells the
#: guard out literally: the community-edition tree check honours only `if edition_features.enabled("<feature>"):`.
LEDGER_READS = edition_features.enabled("exec_dashboard")

if edition_features.enabled("exec_dashboard"):
    from app.outcome_ledger import metrics as ledger  # noqa: F811

    #: The fewest judged outcomes a configuration needs before the policy trusts its statistics — the ledger's own
    #: ranking threshold, read from its one home.
    MIN_OUTCOMES: int = ledger.MIN_RANKED
else:
    #: No ledger reads: no configuration's statistics are ever trusted.
    MIN_OUTCOMES = sys.maxsize

Source = Literal["org", "prior", "none"]


@dataclass(frozen=True)
class ConfigStats:
    """One configuration's outcomes in one stratum: `n` judged attempts, `survived` of them, the dollars per
    attempt and the median completion seconds (None when unknown), and whose data it is."""

    n: int
    survived: int
    cost_usd: Optional[Decimal]
    p50_secs: Optional[float]
    source: Source

    def as_record(self) -> dict[str, Any]:
        """The JSON a decision row's candidate carries."""
        return {"n": self.n, "survived": self.survived,
                "cost_usd": str(self.cost_usd) if self.cost_usd is not None else None,
                "p50_secs": self.p50_secs, "source": self.source}


NO_DATA = ConfigStats(n=0, survived=0, cost_usd=None, p50_secs=None, source="none")


class LedgerStats(Protocol):
    @property
    def version(self) -> str:
        """Recorded on every decision row (`routing_decisions.stats_version`) and part of its inputs."""
        ...

    def config_stats(self, session: Session, org_id: UUID, stratum: str,
                     configs: Sequence[str]) -> dict[str, ConfigStats]:
        """`{config_key: stats}` for every key in `configs`."""
        ...

    def agent_profile(self, session: Session, org_id: UUID, agent_ref: str) -> dict[str, Any]:
        """An agent's ledger profile for the marketplace (C8): `{n, metrics: [{key, value, low, high}]}`, `{}` when
        the ledger has judged none of its outcomes."""
        ...


class NullLedgerStats:
    """No ledger: every configuration is below the minimum."""

    version = "none"

    def config_stats(self, session: Session, org_id: UUID, stratum: str,
                     configs: Sequence[str]) -> dict[str, ConfigStats]:
        return {key: NO_DATA for key in configs}

    def agent_profile(self, session: Session, org_id: UUID, agent_ref: str) -> dict[str, Any]:
        return {}


#: The ledger statistics' generation: the daily date is appended, so a pending pick is recomputed once a day as the
#: ledger judges more outcomes (`version` is part of a decision's inputs).
LEDGER_VERSION = "ledger-1"
#: How long one org's (or the prior's) rows are reused across picks and marketplace entries.
CACHE_SECS = 300
#: The largest share of a prior's judged rows one org may hold.
MAX_ORG_SHARE = 0.5


def _matches(row: ledger.Row, key: str) -> bool:
    """Whether the ledger row ran the router candidate `key` (`decisions.config_key`)."""
    kind, who, tier = key.split(":", 2)
    if kind == "agent":
        return row.agent_kind == "sdk" and str(row.agent_id) == who
    if row.agent_kind != "builtin" or row.provider != who:
        return False
    family = model_catalog.TIER_FAMILY.get(tier)
    if tier == "-" or family is None or agent_providers.provider_of_model(family) != who:
        return True
    return row.model_family == family


def _in_stratum(row: ledger.Row, bucket: str, language: str) -> bool:
    from app.agent_router import features

    bucket_ok = row.complexity_bucket in (None, "na", bucket)
    language_ok = language == features.ANY_LANGUAGE or row.language in (None, language)
    return bucket_ok and language_ok


def _stats(rows: list[ledger.Row], source: Source) -> ConfigStats:
    """The ledger's own survival, cost per success and completion over `rows`, in the policy's shape."""
    m = ledger.metrics(rows, ledger.TaskIndex(rows))
    success, cps, completion = m["success"], m["cost_per_success"], m["completion"]
    n = int(success["n"])
    if n == 0:
        return NO_DATA
    rate = float(success["value"])
    # The policy scores survival per dollar of one attempt: cost per success × success rate.
    per_attempt = (Decimal(str(cps["value_usd"])) * Decimal(str(rate))).quantize(Decimal("0.0001")) \
        if cps["value_usd"] is not None else None
    median_h = completion["median_h"]
    return ConfigStats(n=n, survived=round(rate * n), cost_usd=per_attempt,
                       p50_secs=median_h * 3600 if median_h is not None else None, source=source)


def _prior_ok(pool: list[ledger.Row]) -> bool:
    """The ledger's cross-org floor holds over `pool`, and no one org dominates its judged rows."""
    final = [row for row in pool if row.final]
    by_org = Counter(row.org_id for row in final)
    return ledger._floor_met(pool) and max(by_org.values(), default=0) <= MAX_ORG_SHARE * len(final)


def _fleet_plane() -> bool:
    """Whether this read sees every org: the operator plane (or no binding) with no operator org filter."""
    scope = seam.current_scope()
    return (scope is None or scope is seam.OPERATOR) and seam.request_org_filter() is None


class OutcomeLedgerStats:
    """The router's statistics, read from the outcome ledger: org first, operator-plane prior second."""

    def __init__(self, cache_secs: float = CACHE_SECS, clock: Callable[[], float] = time.monotonic) -> None:
        self._cache: dict[Any, tuple[float, Any]] = {}
        self._cache_secs, self._clock = cache_secs, clock

    @property
    def version(self) -> str:
        return f"{LEDGER_VERSION}:{utcnow().date().isoformat()}"

    def _cached(self, key: Any, read: Callable[[], Any]) -> Any:
        """`read()`, reused for `cache_secs`. Keyed with the operator's org filter, so a narrowed read never answers an
        unnarrowed one; expired entries are dropped as new ones land."""
        now = self._clock()
        key = (key, seam.request_org_filter())
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = read()
        if self._cache_secs > 0:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + self._cache_secs, value)
        return value

    def _rows(self, session: Session, org_id: Optional[UUID], persona: str) -> list[ledger.Row]:
        """The countable rows of `persona` in `org_id` (None = every org: the prior's scope)."""
        return self._cached(("rows", org_id, persona),
                            lambda: ledger.rows(session, org_id, ledger.Filters(persona=persona)))

    def config_stats(self, session: Session, org_id: UUID, stratum: str,
                     configs: Sequence[str]) -> dict[str, ConfigStats]:
        fleet = _fleet_plane()
        return self._cached(("stats", org_id, stratum, tuple(configs), fleet),
                            lambda: self._config_stats(session, org_id, stratum, configs, fleet))

    def _config_stats(self, session: Session, org_id: UUID, stratum: str, configs: Sequence[str],
                      fleet: bool) -> dict[str, ConfigStats]:
        persona, bucket, language = stratum.split("|", 2)
        own = [row for row in self._rows(session, org_id, persona) if _in_stratum(row, bucket, language)]
        prior: Optional[list[ledger.Row]] = None
        out: dict[str, ConfigStats] = {}
        for key in configs:
            stats = _stats([row for row in own if _matches(row, key)], "org")
            # An org's own SDK agent has no prior: no other org ran it.
            if stats.n < MIN_OUTCOMES and key.startswith("builtin:") and fleet:
                if prior is None:
                    prior = [row for row in self._rows(session, None, persona)
                             if row.org_id != org_id and _in_stratum(row, bucket, language)]
                pool = [row for row in prior if _matches(row, key)]
                if _prior_ok(pool):
                    stats = _stats(pool, "prior")
            out[key] = stats
        return out

    def agent_profile(self, session: Session, org_id: UUID, agent_ref: str) -> dict[str, Any]:
        entries = self._cached(("profiles", org_id), lambda: {
            entry["key"]: entry for entry in ledger.profiles(session, org_id, "agent", ledger.Filters())})
        provider = agent_providers.by_value(agent_ref)
        entry = entries.get(f"builtin:{provider.value.upper()}" if provider is not None else f"sdk:{agent_ref}")
        if entry is None or not entry["n_final"]:
            return {}
        m = entry["metrics"]
        figures = [{"key": "survival", "value": m["success"]["value"], "low": m["success"]["lo"],
                    "high": m["success"]["hi"]},
                   {"key": "cost_per_success", "value": m["cost_per_success"]["value_usd"]}]
        return {"n": entry["n_final"], "metrics": [f for f in figures if f["value"] is not None]}


_current: LedgerStats = OutcomeLedgerStats() if LEDGER_READS else NullLedgerStats()


def current() -> LedgerStats:
    """The statistics every pick reads."""
    return _current


def use(stats: LedgerStats) -> LedgerStats:
    """Swap the implementation (tests); returns the previous one."""
    global _current
    previous, _current = _current, stats
    return previous
