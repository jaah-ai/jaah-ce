"""Incident fingerprints — the one spelling of each key an incident is folded on (`incidents.fingerprint`, unique per
project). An adapter or recorder builds its key here; nothing else formats one.

* monitor: `"{kind}:{vendor id}"` — Sentry issue id, PagerDuty `incident_key` else incident id, CloudWatch
  `AlarmName|Region|AWSAccountId` (`cloudwatch_alarm`), Datadog `$AGGREG_KEY`, the generic payload's `fingerprint`;
* `deploy:{env}:{workflow_path}:{failing_job or 'run'}`; `probe:{env}:{probe_id}`; `regression:{env}:{metric}`;
  `lane:{stage}`.

A key longer than `MAX_CHARS` keeps a readable head and ends in a digest of the whole, so two long vendor ids never
collide and a sender cannot grow the unique index without bound.
"""
from __future__ import annotations

import hashlib
from typing import Optional

MAX_CHARS = 200
_DIGEST_CHARS = 16


def _bounded(key: str) -> str:
    if len(key) <= MAX_CHARS:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()[:_DIGEST_CHARS]
    return f"{key[:MAX_CHARS - _DIGEST_CHARS - 1]}~{digest}"


def _part(value: object, what: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"a fingerprint needs a {what}")
    return text


def monitor(kind: str, vendor_id: object) -> str:
    return _bounded(f"{_part(kind, 'source kind')}:{_part(vendor_id, 'vendor id')}")


def cloudwatch_alarm(alarm_name: str, region: str, account_id: str) -> str:
    return monitor("cloudwatch", f"{_part(alarm_name, 'alarm name')}|{_part(region, 'region')}|"
                                 f"{_part(account_id, 'account id')}")


def deploy(environment: str, workflow_path: str, failing_job: Optional[str] = None) -> str:
    job = (failing_job or "").strip() or "run"
    return _bounded(f"deploy:{_part(environment, 'environment')}:{_part(workflow_path, 'workflow path')}:{job}")


def probe(environment: str, probe_id: object) -> str:
    return _bounded(f"probe:{_part(environment, 'environment')}:{_part(probe_id, 'probe id')}")


def regression(environment: str, metric: str) -> str:
    return _bounded(f"regression:{_part(environment, 'environment')}:{_part(metric, 'metric')}")


def lane(stage: str) -> str:
    return _bounded(f"lane:{_part(stage, 'stage')}")
