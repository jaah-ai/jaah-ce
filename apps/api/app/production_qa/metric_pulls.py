"""The post-deploy regression check's reads (`regression.py`): one window's error count, request count and p95 latency
from the monitor a trigger's `source_config.metrics` names (`trigger_config.SentryMetrics` and its twins), signed with
the org credential it references by id. Jaah reads two windows per deploy and stores the result; it keeps no metric
history (no APM, no metric store).

`pull(session, trigger, start, end)` → `Sample`, or raises:
* `ConfigError` — nothing to retry: no `metrics`, the credential missing, another org's, of the wrong type, or not
  openable here (a NodeVault row), a Sentry base URL that is not a public https host, an unknown Datadog site;
* `PullError` — the monitor was unreachable, refused, or answered something unreadable: retried by the caller.
Neither message ever carries a secret or a monitor's own error prose (it can echo the request).

PER VENDOR (contracts read 2026-11-01; each test fixture is the vendor's documented shape):

| kind | request | errors / requests / p95 |
|---|---|---|
| `sentry` | `GET {base_url}/api/0/organizations/{org_slug}/events-stats/` twice, Bearer `auth_token`: `dataset=errors`, `yAxis=count()`, `query=event.type:error`; then `dataset=transactions`, `yAxis=count()` and `p95(transaction.duration)` — `projectSlug`, `environment`, ISO `start`/`end`, `interval=5m` | sum of each bucket's `count`; sum; bucket p95s weighted by that bucket's transactions |
| `datadog` | `POST https://api.{site}/api/v2/query/timeseries`, `DD-API-KEY` + `DD-APPLICATION-KEY`; queries `a` errors, `b` hits, `c` p95, one formula each, `interval` = the window | sum of `a`'s points; sum of `b`'s; the mean of `c`'s points (one point per window) |
| `cloudwatch` | `GetMetricData` (AWS JSON 1.0, target `GraniteServiceVersion20100801`, SigV4 service `monitoring`) on the `aws` credential, one `MetricStat` each, `Period` = the window rounded up to a minute | sum of `errors`' values; sum of `requests`'; the mean of `latency`'s (its `stat`, e.g. `p95`) |

Doc-unverified, pinned by the fixtures until a live run: Sentry's `events-stats` is EXPERIMENTAL and absent from its
public docs (shape read from `getsentry/sentry`'s serializer: `[ts, [{"count": n}]]`, an empty bucket `[ts, []]`, a
multi-`yAxis` answer keyed by axis); Datadog documents a bearer token for this endpoint — the API/application key
pair it accepts on every other v2 route is used here.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features, http_bounded
from app.clock import utcnow
from app.models.automations import TaskTrigger
from app.triggers import SOURCE_CLOUDWATCH, SOURCE_DATADOG, SOURCE_SENTRY

log = logging.getLogger("cockpit-production-qa")

TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 1024 * 1024

# Which credential type each monitor's `metrics.credential_id` must name.
CREDENTIAL_TYPES: dict[str, str] = {SOURCE_SENTRY: "sentry", SOURCE_DATADOG: "datadog", SOURCE_CLOUDWATCH: "aws"}

# Datadog's sites (https://docs.datadoghq.com/getting_started/site/); the API host is `api.<site>`.
DATADOG_SITES = frozenset({
    "datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu", "ap1.datadoghq.com",
    "ap2.datadoghq.com", "uk1.datadoghq.com", "ddog-gov.com", "us2.ddog-gov.com",
})

_CLOUDWATCH_TARGET = "GraniteServiceVersion20100801.GetMetricData"


class ConfigError(Exception):
    """The trigger's metrics cannot be read as configured; retrying changes nothing."""


class PullError(Exception):
    """The monitor did not answer usably; a later attempt may."""


@dataclass(frozen=True)
class Sample:
    """One window's numbers; None where the monitor offers no value."""

    errors: Optional[float]
    requests: Optional[float]
    p95_ms: Optional[float]


def _client(timeout: float) -> httpx.Client:
    """The seam tests swap for a mock transport; `http_bounded`'s policy (no redirects, capped reads)."""
    return http_bounded.client(timeout)


# ── The credential ──────────────────────────────────────────────────────────────────────────────────

def credential_problem(session: Session, org_id: Any, kind: str, metrics: Optional[dict[str, Any]]) -> Optional[str]:
    """Why a trigger's `metrics` cannot name its credential (missing, another org's, the wrong type, a NodeVault row
    the control plane cannot open), or None. Shared by the trigger write door and the pull."""
    from app.models.credentials import Credential

    if not metrics:
        return None
    expected = CREDENTIAL_TYPES.get(kind)
    if expected is None:
        return f"a {kind} trigger has no metrics"
    row = session.scalar(select(Credential).where(Credential.id == metrics.get("credential_id")))
    if row is None or row.org_id != org_id:
        return "metrics: its credential no longer exists"
    if row.type != expected:
        return f"metrics: credential '{row.name}' is a {row.type} credential, not {expected}"
    if getattr(row, "nodevault", False):
        return f"metrics: credential '{row.name}' is a NodeVault credential, which Jaah cannot open"
    return None


def _credential(session: Session, trigger: TaskTrigger) -> Any:
    from app.models.credentials import Credential

    metrics = (trigger.source_config or {}).get("metrics")
    if not metrics:
        raise ConfigError("no metrics configured")
    problem = credential_problem(session, trigger.org_id, trigger.source_kind, metrics)
    if problem is not None:
        raise ConfigError(problem.removeprefix("metrics: "))
    # Spent on the owner's behalf: re-asked on every pull, since the owner may have lost the credential since save.
    from app.authz.principal import principal_for_user
    from app.authz.visibility import usable_credential_clause
    from app.trigger_intake import trigger_owner

    owner = trigger_owner(session, trigger)
    row = None if owner is None else session.scalar(select(Credential).where(
        Credential.id == metrics["credential_id"],
        usable_credential_clause(principal_for_user(session, owner), trigger.project_id),
    ))
    if row is None:
        raise ConfigError("the trigger's owner may no longer use its credential")
    return row


def _field(row: Any, key: str, required: bool = True) -> str:
    """One field of the credential, a secret opened on its way to the request; an error names the field, never the
    value. A blank optional field is ""."""
    from app.credential_crypto import SealedSecret, open_secret
    from app.credential_keys import NodeVaultUnavailable

    value = (row.config or {}).get(key)
    if isinstance(value, SealedSecret):
        try:
            value = open_secret(row, key)
        except ValueError:
            raise ConfigError(f"credential '{row.name}' holds a {key} that cannot be opened — re-enter it") from None
        except NodeVaultUnavailable as exc:
            raise ConfigError(str(exc)) from None
    value = str(value or "").strip()
    if not value and required:
        raise ConfigError(f"credential '{row.name}' has no {key}")
    return value


# ── One exchange ────────────────────────────────────────────────────────────────────────────────────

# The monotonic time by which the current `pull` must be done (set by `pull`, read by every exchange): one deploy's
# pulls never outlive the cron pass that holds its row.
_deadline: float = float("inf")


def _timeout(vendor: str) -> float:
    remaining = _deadline - time.monotonic()
    if remaining < 1:
        raise PullError(f"{vendor} did not answer in time")
    return min(TIMEOUT_SECONDS, remaining)


def _exchange(vendor: str, method: str, url: str, headers: dict[str, str], body: bytes = b"") -> Any:
    try:
        with _client(_timeout(vendor)) as http, http.stream(method, url, headers=headers, content=body) as response:
            status, raw = response.status_code, http_bounded.read_capped(response, MAX_RESPONSE_BYTES)[0]
    except httpx.HTTPError as exc:
        raise PullError(f"{vendor} could not be reached ({type(exc).__name__})") from None
    if not 200 <= status < 300:
        raise PullError(f"{vendor} answered HTTP {status}")
    parsed = http_bounded.json_or_none(raw) if len(raw) < MAX_RESPONSE_BYTES else None
    if not isinstance(parsed, dict):
        raise PullError(f"{vendor} answered something that is not a JSON document")
    return parsed


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


# ── Sentry ──────────────────────────────────────────────────────────────────────────────────────────

def _sentry_buckets(series: Any) -> list[float]:
    """Each bucket's value in one `events-stats` series; an empty bucket (`[ts, []]`) is 0."""
    if not isinstance(series, dict) or not isinstance(series.get("data"), list):
        raise PullError("Sentry answered events-stats without a data series")
    out: list[float] = []
    for row in series["data"]:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], list):
            raise PullError("Sentry answered an events-stats bucket of an unknown shape")
        value = _number(row[1][0].get("count")) if row[1] and isinstance(row[1][0], dict) else 0.0
        out.append(value or 0.0)
    return out


def _sentry(session: Session, trigger: TaskTrigger, start: datetime, end: datetime) -> Sample:
    row = _credential(session, trigger)
    base = _field(row, "base_url").rstrip("/")
    refusal = http_bounded.public_destination_refusal(base)
    if refusal is not None:
        raise ConfigError(f"credential '{row.name}': {refusal}")
    target = trigger.source_config["metrics"]["sentry"]
    url = f"{base}/api/0/organizations/{quote(_field(row, 'org_slug'), safe='')}/events-stats/"
    headers = {"Authorization": f"Bearer {_field(row, 'auth_token')}", "Accept": "application/json"}
    common = [("projectSlug", target["project_slug"]), ("environment", target["environment"]),
              ("start", start.isoformat()), ("end", end.isoformat()), ("interval", "5m")]

    def get(params: list[tuple[str, str]]) -> Any:
        return _exchange("Sentry", "GET", f"{url}?{urlencode(common + params)}", headers)

    errors = get([("dataset", "errors"), ("yAxis", "count()"), ("query", "event.type:error")])
    both = get([("dataset", "transactions"), ("yAxis", "count()"), ("yAxis", "p95(transaction.duration)")])
    counts = _sentry_buckets(both.get("count()"))
    p95s = _sentry_buckets(both.get("p95(transaction.duration)"))
    requests = sum(counts)
    p95 = None
    if requests and len(p95s) == len(counts):
        p95 = sum(c * p for c, p in zip(counts, p95s, strict=True)) / requests
    return Sample(errors=sum(_sentry_buckets(errors)), requests=requests, p95_ms=p95)


# ── Datadog ─────────────────────────────────────────────────────────────────────────────────────────

def _datadog(session: Session, trigger: TaskTrigger, start: datetime, end: datetime) -> Sample:
    row = _credential(session, trigger)
    site = _field(row, "site").strip().lower()
    if site not in DATADOG_SITES:
        raise ConfigError(f"credential '{row.name}': {site!r} is not a Datadog site")
    target = trigger.source_config["metrics"]["datadog"]
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    names = ("a", "b", "c")
    queries = [target["errors_query"], target["hits_query"], target["p95_query"]]
    body = json.dumps({"data": {"type": "timeseries_request", "attributes": {
        "from": start_ms, "to": end_ms, "interval": end_ms - start_ms,
        "queries": [{"data_source": "metrics", "query": q, "name": n} for n, q in zip(names, queries, strict=True)],
        "formulas": [{"formula": n} for n in names],
    }}}).encode()
    headers = {"DD-API-KEY": _field(row, "api_key"), "DD-APPLICATION-KEY": _field(row, "app_key"),
               "Content-Type": "application/json", "Accept": "application/json"}
    answer = _exchange("Datadog", "POST", f"https://api.{site}/api/v2/query/timeseries", headers, body)
    attributes = ((answer.get("data") or {}).get("attributes") or {}) if isinstance(answer.get("data"), dict) else {}
    series, values = attributes.get("series"), attributes.get("values")
    if not isinstance(series, list) or not isinstance(values, list) or len(series) != len(values):
        raise PullError("Datadog answered a timeseries of an unknown shape")
    points: dict[int, list[float]] = {0: [], 1: [], 2: []}
    for position, (meta, row_values) in enumerate(zip(series, values, strict=True)):
        index = meta.get("query_index", position) if isinstance(meta, dict) else position
        if index in points and isinstance(row_values, list):
            points[index] += [v for v in (_number(x) for x in row_values) if v is not None]
    return Sample(errors=sum(points[0]) if series else None, requests=sum(points[1]) if series else None,
                  p95_ms=(sum(points[2]) / len(points[2])) if points[2] else None)


# ── CloudWatch ──────────────────────────────────────────────────────────────────────────────────────

def _metric_stat(ref: dict[str, Any], period: int) -> dict[str, Any]:
    return {"Metric": {"Namespace": ref["namespace"], "MetricName": ref["metric_name"],
                       "Dimensions": [{"Name": d["name"], "Value": d["value"]} for d in ref.get("dimensions") or []]},
            "Period": period, "Stat": ref["stat"]}


def _cloudwatch(session: Session, trigger: TaskTrigger, start: datetime, end: datetime) -> Sample:
    if not edition_features.enabled("aws"):
        raise ConfigError("CloudWatch metrics are not available in this edition")
    from app.aws import json11
    from app.aws.container_credentials import AwsCredentials

    row = _credential(session, trigger)
    credentials = AwsCredentials(_field(row, "access_key_id"), _field(row, "secret_access_key"),
                                 _field(row, "session_token", required=False) or None)
    target = trigger.source_config["metrics"]["cloudwatch"]
    region = target["region"]
    period = max(60, math.ceil((end - start).total_seconds() / 60) * 60)
    refs = {"e": target["errors"], "r": target["requests"], "l": target.get("latency")}
    payload: dict[str, object] = {
        "StartTime": int(start.timestamp()), "EndTime": int(end.timestamp()), "ScanBy": "TimestampAscending",
        "MetricDataQueries": [{"Id": i, "MetricStat": _metric_stat(ref, period), "ReturnData": True}
                              for i, ref in refs.items() if ref],
    }

    def unreachable(transport_error: str) -> Exception:
        return PullError(f"CloudWatch could not be reached ({transport_error})")

    def refused(message: str, *, status_code: Optional[int] = None, code: Optional[str] = None,
                aws_detail: Optional[str] = None) -> Exception:
        return PullError(f"CloudWatch refused GetMetricData (HTTP {status_code}" + (f", {code})" if code else ")"))

    answer = json11.call(
        label="CloudWatch", log=log, action="GetMetricData", target=_CLOUDWATCH_TARGET, payload=payload,
        url=f"https://monitoring.{region}.amazonaws.com/", region=region, service="monitoring",
        credentials=credentials, client=_client, now=lambda: utcnow(), timeout=_timeout("CloudWatch"),
        max_bytes=MAX_RESPONSE_BYTES, error=refused, unreachable=unreachable, content_type=json11.CONTENT_TYPE_1_0,
    )
    results = answer.get("MetricDataResults")
    if not isinstance(results, list):
        raise PullError("CloudWatch answered GetMetricData without MetricDataResults")
    values: dict[str, list[float]] = {}
    for result in results:
        if isinstance(result, dict) and isinstance(result.get("Values"), list):
            values[str(result.get("Id"))] = [v for v in (_number(x) for x in result["Values"]) if v is not None]
    latency = values.get("l") or []
    return Sample(errors=sum(values.get("e", [])), requests=sum(values.get("r", [])),
                  p95_ms=(sum(latency) / len(latency)) if latency else None)


_PULLS: dict[str, Callable[[Session, TaskTrigger, datetime, datetime], Sample]] = {
    SOURCE_SENTRY: _sentry, SOURCE_DATADOG: _datadog, SOURCE_CLOUDWATCH: _cloudwatch,
}


def pull(session: Session, trigger: TaskTrigger, start: datetime, end: datetime,
         deadline: float = float("inf")) -> Sample:
    """One window's numbers from the trigger's monitor (module docstring), done by the monotonic `deadline` (else a
    `PullError`); raises `ConfigError` or `PullError`."""
    global _deadline
    reader = _PULLS.get(trigger.source_kind)
    if reader is None:
        raise ConfigError(f"a {trigger.source_kind} trigger has no metrics")
    _deadline = deadline
    try:
        return reader(session, trigger, start, end)
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"the metrics config is incomplete ({type(exc).__name__})") from None
    finally:
        _deadline = float("inf")
