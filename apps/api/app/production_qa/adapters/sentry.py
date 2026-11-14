"""The `sentry` monitor adapter (contract: `app/production_qa/adapters/__init__.py`).

Vendor docs: https://docs.sentry.io/organization/integrations/integration-platform/webhooks/ (headers, signature),
…/webhooks/issues/ and …/webhooks/issue-alerts/ (the two resources read here; the test fixtures are those pages'
samples).

VERIFY: `Sentry-Hook-Signature` is the hex HMAC-SHA256 of the raw body under the integration's Client Secret (the
trigger's sealed secret, pasted by its owner), compared in constant time.

FIELD MAP — both resources fold onto one fingerprint, `sentry:{issue id}`:

| Observation    | `issue` (`Sentry-Hook-Resource: issue`)  | `event_alert` (an alert rule's `triggered`)        |
|----------------|------------------------------------------|----------------------------------------------------|
| fingerprint    | `data.issue.id`                          | `data.event.issue_id`                              |
| title          | `title`                                  | `title`                                            |
| severity       | `level` (below)                          | `level` (below)                                    |
| environment    | not sent (NULL)                          | `environment`, else the `environment` tag          |
| occurred_at    | `lastSeen` (resolve: `Sentry-Hook-Timestamp`, else now; unresolve: the later of the two) | `datetime`, else `timestamp` |
| first_seen     | `firstSeen`                              | — (the occurrence time)                            |
| count          | `count`, `absolute`                      | none, `increment`                                  |
| release        | not sent (NULL)                          | `release`                                          |
| commit_sha     | not sent (NULL) — Sentry sends no commit on either resource                                   |
| stack_frames   | not sent (NULL)                          | `exception.values[*].stacktrace.frames`, innermost first: `{path, function, lineno, in_app}` |
| vendor_url     | `web_url`, else `permalink`              | `web_url`                                          |
| vendor_ref     | `shortId`                                | `event_id`                                         |
| action         | `resolved` → resolve; `created`/`unresolved` → occurrence; others record nothing |  occurrence      |

Severity: fatal → critical; error, warning, info, debug as named; anything else → error. `source_config.environments`
(when set) drops an `event_alert` from another environment; an `issue` names none and is always recorded. Any other
resource (`installation`, `metric_alert`, `comment`, …) records nothing.
"""
from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any, Optional

from app import trigger_secrets
from app.clock import utcnow
from app.models.automations import TaskTrigger
from app.production_qa import fingerprints
from app.production_qa.adapters.common import frame, json_object, lowered, observation, parse_time, web_url, with_secret
from app.production_qa.adapters.common import text as _text
from app.production_qa.incidents import Observation
from app.triggers import SENTRY_REQUEST_ID_HEADER, SENTRY_RESOURCE_HEADER, SENTRY_SIGNATURE_HEADER, SOURCE_SENTRY

_SEVERITY = {"fatal": "critical", "error": "error", "warning": "warning", "info": "info", "debug": "debug"}
_OCCURRENCE_ACTIONS = ("created", "unresolved")
_RESOLVE_ACTION = "resolved"


def verify(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> bool:
    """Whether `Sentry-Hook-Signature` signs `body` under the trigger's secret. No secret: never."""
    presented = lowered(headers).get(SENTRY_SIGNATURE_HEADER, "")
    return with_secret(trigger, partial(trigger_secrets.verify_signature, prefixed=False), body, presented)


def delivery_id(headers: Mapping[str, str], body: bytes) -> Optional[str]:
    """Sentry's `Request-ID`: a retry resends it."""
    return _text(lowered(headers).get(SENTRY_REQUEST_ID_HEADER))


def _severity(level: Any) -> str:
    return _SEVERITY.get(str(level or "").strip().lower(), "error")


def _observation(trigger: TaskTrigger, **fields: Any) -> Observation:
    return observation(SOURCE_SENTRY, trigger, **fields)


def _issue(trigger: TaskTrigger, headers: Mapping[str, str], payload: Mapping[str, Any]) -> list[Observation]:
    action = payload.get("action")
    issue = (payload.get("data") or {}).get("issue") or {}
    if action not in _OCCURRENCE_ACTIONS and action != _RESOLVE_ACTION:
        return []
    fingerprint = fingerprints.monitor(SOURCE_SENTRY, issue.get("id"))
    facts = {k: issue[k] for k in ("culprit", "status", "substatus", "userCount") if issue.get(k) is not None}
    common = dict(fingerprint=fingerprint, title=_text(issue.get("title")) or fingerprint,
                  severity=_severity(issue.get("level")), vendor_url=web_url(issue.get("web_url") or issue.get("permalink")),
                  vendor_ref=_text(issue.get("shortId")), facts={"resource": "issue", **facts})
    if action == _RESOLVE_ACTION:
        stamp = lowered(headers).get("sentry-hook-timestamp")
        at = parse_time(stamp) or utcnow()
        return [_observation(trigger, action="resolve", occurred_at=at, **common)]
    last_seen = parse_time(issue.get("lastSeen")) or utcnow()
    if action == "unresolved":
        # Unresolved by hand, with no new event: `lastSeen` predates the resolve, so the hook's own time is the
        # moment (otherwise the reopen would read as stale).
        last_seen = max(last_seen, parse_time(lowered(headers).get("sentry-hook-timestamp")) or last_seen)
    count = issue.get("count")
    return [_observation(
        trigger, occurred_at=last_seen, first_seen=parse_time(issue.get("firstSeen")),
        count=int(count) if count is not None and str(count).strip().isdigit() else None, count_mode="absolute",
        **common,
    )]


def _environment_of(event: Mapping[str, Any]) -> Optional[str]:
    if _text(event.get("environment")):
        return _text(event.get("environment"))
    for tag in event.get("tags") or ():
        if isinstance(tag, (list, tuple)) and len(tag) == 2 and tag[0] == "environment":
            return _text(tag[1])
    return None


def _frames(event: Mapping[str, Any]) -> Optional[list[dict[str, Any]]]:
    """Every exception's frames, innermost first (Sentry lists them outermost first; the last exception is the one
    raised)."""
    out: list[dict[str, Any]] = []
    for value in reversed((event.get("exception") or {}).get("values") or []):
        for raw in reversed(((value or {}).get("stacktrace") or {}).get("frames") or []):
            out.append(frame(raw.get("filename") or raw.get("abs_path"), raw.get("function"), raw.get("lineno"),
                             raw.get("in_app")))
    return out or None


def _event_alert(trigger: TaskTrigger, payload: Mapping[str, Any]) -> list[Observation]:
    data = payload.get("data") or {}
    event = data.get("event") or {}
    environment = _environment_of(event)
    wanted = (trigger.source_config or {}).get("environments") or []
    if wanted and environment not in wanted:
        return []
    fingerprint = fingerprints.monitor(SOURCE_SENTRY, event.get("issue_id"))
    at = parse_time(event.get("datetime")) or parse_time(event.get("timestamp")) or utcnow()
    facts: dict[str, Any] = {"resource": "event_alert"}
    if _text(data.get("triggered_rule")):
        facts["triggered_rule"] = _text(data.get("triggered_rule"))
    if _text(event.get("culprit")):
        facts["culprit"] = _text(event.get("culprit"))
    return [_observation(
        trigger, fingerprint=fingerprint, title=_text(event.get("title")) or fingerprint,
        severity=_severity(event.get("level")), environment=environment, occurred_at=at,
        release=_text(event.get("release")), stack_frames=_frames(event), vendor_url=web_url(event.get("web_url")),
        vendor_ref=_text(event.get("event_id")), facts=facts,
    )]


def normalise(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> list[Observation]:
    """The body's observation, by `Sentry-Hook-Resource`; [] for a resource or action that is no incident fact.
    A malformed body of a resource read here raises ValueError (the delivery settles `record_error`)."""
    resource = lowered(headers).get(SENTRY_RESOURCE_HEADER, "")
    if resource not in ("issue", "event_alert"):
        return []
    payload = json_object(body, "Sentry")
    return _issue(trigger, headers, payload) if resource == "issue" else _event_alert(trigger, payload)
