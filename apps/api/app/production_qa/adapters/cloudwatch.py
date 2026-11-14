"""The `cloudwatch` monitor adapter: an HTTPS subscription to an Amazon SNS topic that CloudWatch alarms publish to
(contract: `app/production_qa/adapters/__init__.py`). `app/alarm_relay.py` (Jaah's own alarms → Slack) is unrelated.

Vendor docs (read 2026-11-01):
https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html (signature),
https://docs.aws.amazon.com/sns/latest/dg/http-header.html (headers),
https://docs.aws.amazon.com/sns/latest/dg/http-subscription-confirmation-json.html and
…/http-notification-json.html (the two envelopes — the test fixtures are those samples), and
https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html (the alarm `Message`
schema; the fixture's `Message` is AWS's own sample from aws-lambda-go's test data).

VERIFY, in order — any failure refuses the request:
1. the body is an SNS envelope (`Notification`, `SubscriptionConfirmation` or `UnsubscribeConfirmation`);
2. its `TopicArn` is one of the trigger's `topic_arns` (a validly signed message of any other topic is refused);
3. `SigningCertURL` (and a confirmation's `SubscribeURL`) is `https://sns.<the topic's region>.amazonaws.com/…`
   (`.amazonaws.com.cn` in the China partition) on port 443 — host-pinned, so a forged message can never make Jaah
   fetch from anywhere else (SSRF); the certificate is fetched over verified TLS without redirects, size-capped and
   cached per URL;
4. `Signature` (base64) is the certificate key's RSA PKCS#1 v1.5 signature over the documented string to sign —
   SHA1 for `SignatureVersion` 1, SHA256 for 2.

CONFIRMATION: a `SubscriptionConfirmation` is confirmed by a GET of its (already verified) `SubscribeURL` when the
delivery is recorded; it records nothing. An `UnsubscribeConfirmation` records nothing.

FIELD MAP — a `Notification`'s `Message` is the alarm's JSON; one fingerprint per alarm:

| Observation  | CloudWatch alarm `Message`                                                    |
|--------------|-------------------------------------------------------------------------------|
| fingerprint  | `AlarmName|Region|AWSAccountId` (`cloudwatch:…`, `fingerprints.cloudwatch_alarm`) |
| title        | `AlarmName`, then `: AlarmDescription` when there is one                       |
| severity     | the trigger's `severity` (CloudWatch sends none; default `error`)             |
| action       | `NewStateValue` `OK` → resolve; `ALARM` → occurrence; `INSUFFICIENT_DATA` records nothing |
| environment  | not sent (NULL)                                                               |
| occurred_at  | `StateChangeTime`, else the envelope's `Timestamp`                            |
| count        | none, `increment`                                                             |
| release      | not sent (NULL)                                                               |
| commit_sha   | not sent (NULL)                                                               |
| stack_frames | not sent (NULL)                                                               |
| vendor_url   | not sent (NULL) — the alarm carries no console link                           |
| vendor_ref   | `AlarmArn`                                                                    |
| facts        | `reason` (`NewStateReason`), `old_state`, `metric` (`Trigger.Namespace/MetricName`) |

A `Message` that is not an alarm's JSON (a plain SNS publish to the same topic) records nothing. Dedup: the signed
`MessageId` (`x-amz-sns-message-id` repeats it) — SNS's retry resends it.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import threading
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import urlsplit

from app import http_bounded
from app.clock import utcnow
from app.models.automations import TaskTrigger
from app.production_qa import fingerprints
from app.production_qa.adapters.common import json_id, json_object, observation, parse_time, text
from app.production_qa.incidents import Observation
from app.triggers import SOURCE_CLOUDWATCH

NOTIFICATION = "Notification"
SUBSCRIPTION_CONFIRMATION = "SubscriptionConfirmation"
UNSUBSCRIBE_CONFIRMATION = "UnsubscribeConfirmation"
_TYPES = (NOTIFICATION, SUBSCRIPTION_CONFIRMATION, UNSUBSCRIBE_CONFIRMATION)
# The documented string to sign, per message type, in its byte-sorted key order (`Subject` only when present).
_SIGNED_NOTIFICATION = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SIGNED_CONFIRMATION = ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type")
# SNS's signing certificates are served at this path; any other path, query or fragment is refused (an attacker could
# otherwise mint endless distinct URLs, each a live fetch and a cache entry).
_CERT_PATH = re.compile(r"/SimpleNotificationService-[0-9a-f]{32}\.pem")
_TOPIC_REGION = re.compile(r"arn:aws(?P<partition>-cn|-us-gov)?:sns:(?P<region>[a-z0-9-]+):\d{12}:.+")
_RESOLVE_STATE = "OK"
_ALARM_STATE = "ALARM"

_TIMEOUT = 5.0
_MAX_CERT_BYTES = 16 * 1024
_MAX_CACHED_CERTS = 32
_certs: dict[str, Any] = {}
_certs_lock = threading.Lock()


def _region_of(topic_arn: str) -> Optional[tuple[str, str]]:
    """(region, host suffix) of an SNS topic ARN; None when it is no SNS topic ARN."""
    match = _TOPIC_REGION.fullmatch(topic_arn or "")
    if match is None:
        return None
    suffix = "amazonaws.com.cn" if match["partition"] == "-cn" else "amazonaws.com"
    return match["region"], suffix


def pinned_url(url: Any, topic_arn: str, *, certificate: bool = False) -> bool:
    """Whether `url` is an https URL on `sns.<the topic's region>.<amazonaws suffix>`, port 443, no credentials — and,
    for a `certificate`, exactly SNS's certificate path. A control character or space refuses it (the parser would
    strip what the fetch then sends)."""
    where = _region_of(topic_arn)
    if where is None or not isinstance(url, str) or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    region, suffix = where
    if certificate and (parts.query or parts.fragment or not _CERT_PATH.fullmatch(parts.path or "")):
        return False
    return (parts.scheme == "https" and parts.username is None and parts.password is None and port in (None, 443)
            and (parts.hostname or "") == f"sns.{region}.{suffix}")


def _fetch(url: str) -> bytes:
    """GET `url` (verified TLS, no redirects, capped); raises ValueError on any failure. Tests replace this seam."""
    try:
        with http_bounded.client(_TIMEOUT) as client, client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"SNS answered {response.status_code}")
            raw, truncated = http_bounded.read_capped(response, _MAX_CERT_BYTES)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — any transport fault is a failed fetch
        raise ValueError(f"SNS fetch failed: {type(exc).__name__}") from None
    if truncated:
        raise ValueError("SNS answer over the size cap")
    return raw


def _public_key(cert_url: str) -> Any:
    """The signing certificate's public key, fetched once per URL."""
    with _certs_lock:
        key = _certs.get(cert_url)
    if key is not None:
        return key
    from cryptography import x509

    try:
        key = x509.load_pem_x509_certificate(_fetch(cert_url)).public_key()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — an unreadable certificate refuses the message
        raise ValueError(f"unreadable SNS certificate: {type(exc).__name__}") from None
    with _certs_lock:
        if len(_certs) >= _MAX_CACHED_CERTS:
            # The oldest goes: SNS rotates its certificate rarely, so the live one stays.
            _certs.pop(next(iter(_certs)))
        _certs[cert_url] = key
    return key


def string_to_sign(envelope: Mapping[str, Any]) -> Optional[bytes]:
    """The documented string to sign (`Key\\nValue\\n` per signed field), or None when a required field is missing."""
    keys = _SIGNED_NOTIFICATION if envelope.get("Type") == NOTIFICATION else _SIGNED_CONFIRMATION
    out: list[str] = []
    for key in keys:
        value = envelope.get(key)
        if value is None and key == "Subject":
            continue
        if not isinstance(value, str):
            return None
        out.append(f"{key}\n{value}\n")
    return "".join(out).encode()


def _signature_valid(envelope: Mapping[str, Any]) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    algorithm = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(str(envelope.get("SignatureVersion")))
    payload = string_to_sign(envelope)
    if algorithm is None or payload is None:
        return False
    try:
        signature = base64.b64decode(str(envelope.get("Signature") or ""), validate=True)
        key = _public_key(str(envelope["SigningCertURL"]))
    except (binascii.Error, ValueError):
        return False
    if not isinstance(key, rsa.RSAPublicKey):
        return False
    try:
        key.verify(signature, payload, padding.PKCS1v15(), algorithm)
    except InvalidSignature:
        return False
    return True


def verify(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> bool:
    """Whether `body` is an SNS message of one of the trigger's topics, signed by SNS (the module's VERIFY steps)."""
    try:
        envelope = json_object(body, "SNS")
    except ValueError:
        return False
    topic = envelope.get("TopicArn")
    allowed = (trigger.source_config or {}).get("topic_arns") or []
    if envelope.get("Type") not in _TYPES or not isinstance(topic, str) or topic not in allowed:
        return False
    if not pinned_url(envelope.get("SigningCertURL"), topic, certificate=True):
        return False
    if envelope.get("Type") == SUBSCRIPTION_CONFIRMATION and not pinned_url(envelope.get("SubscribeURL"), topic):
        return False
    return _signature_valid(envelope)


def delivery_id(headers: Mapping[str, str], body: bytes) -> Optional[str]:
    """The envelope's signed `MessageId` (what `x-amz-sns-message-id` repeats unsigned): a replay of a genuine message
    under a fresh header is still a duplicate."""
    return json_id(body, "SNS", "MessageId")


def _confirm(envelope: Mapping[str, Any]) -> None:
    """Confirm the subscription: GET its `SubscribeURL`, pinned again (the stored body is re-read here)."""
    url, topic = envelope.get("SubscribeURL"), str(envelope.get("TopicArn") or "")
    if not pinned_url(url, topic):
        raise ValueError("SubscribeURL is not the topic's SNS endpoint")
    _fetch(str(url))


def normalise(trigger: TaskTrigger, headers: Mapping[str, str], body: bytes) -> list[Observation]:
    """A `Notification`'s alarm as one observation; a confirmation is confirmed and records nothing."""
    envelope = json_object(body, "SNS")
    kind = envelope.get("Type")
    if kind == SUBSCRIPTION_CONFIRMATION:
        _confirm(envelope)
        return []
    if kind != NOTIFICATION:
        return []
    try:
        alarm = json.loads(str(envelope.get("Message") or ""))
    except ValueError:
        return []
    if not isinstance(alarm, dict) or not text(alarm.get("AlarmName")):
        return []
    state = text(alarm.get("NewStateValue"))
    if state not in (_RESOLVE_STATE, _ALARM_STATE):
        return []
    name = text(alarm.get("AlarmName")) or ""
    fingerprint = fingerprints.cloudwatch_alarm(name, text(alarm.get("Region")) or "", text(alarm.get("AWSAccountId"))
                                                or "")
    description = text(alarm.get("AlarmDescription"))
    trigger_spec = alarm.get("Trigger")
    metric: dict[str, Any] = trigger_spec if isinstance(trigger_spec, dict) else {}
    facts = {key: value for key, value in (
        ("reason", text(alarm.get("NewStateReason"))), ("old_state", text(alarm.get("OldStateValue"))),
        ("metric", "/".join(p for p in (text(metric.get("Namespace")), text(metric.get("MetricName"))) if p) or None),
    ) if value is not None}
    return [observation(
        SOURCE_CLOUDWATCH, trigger, fingerprint=fingerprint,
        title=f"{name}: {description}" if description else name,
        severity=(trigger.source_config or {}).get("severity") or "error",
        action="resolve" if state == _RESOLVE_STATE else "occurrence",
        occurred_at=(parse_time(alarm.get("StateChangeTime")) or parse_time(envelope.get("Timestamp"))
                     or utcnow()),
        vendor_ref=text(alarm.get("AlarmArn")), facts=facts,
    )]
