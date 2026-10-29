"""Connection testers (`POST /api/credentials/test`): the only place `apps/api` reaches an
operator-supplied third-party host. Six properties are security, not style:
1. Every test is an authenticated identity call, not a reachability ping. Three exceptions, each with a
   sentence stating its limit: `generic_api_key` (no known identity route; discriminates on nothing, so
   no vendor tester may be relaxed to it), `github_app` (recognition: 404 vs 200 for the client id) and
   `oidc_client` (the issuer's discovery document; the client pair is only checkable by a sign-in).
2. `follow_redirects=False`: following would bounce our Authorization header to a chosen host.
3. `CREDENTIAL_TEST_TIMEOUT_SECONDS` is one budget per request, not per call; no retries (`_request`).
4. No `str(exc)` or third-party error body reaches the operator; `detail` is a status or a fixed token. One
   exception: `test_azure` shows Microsoft Entra's own refusal sentence (`AADSTS…`, what the admin searches for),
   one line, bounded and with the secret cut out (`app/azure/wire.py::readable`); and `test_gcp` quotes Google's own
   refusal sentence through `_identity_text`.
5. The secret travels in a header or Basic auth, never in a URL or query string.
6. Read-only at the third party, except `test_github_app`'s unclaimed 15-minute device grant (nothing
   echoed; Connect uses a different endpoint). A mutating tester must extend this line and argue its rate.
"""
from __future__ import annotations

import json
import logging
import re
import string
import threading
import unicodedata
from datetime import datetime, timezone
from queue import Empty, Queue
from time import monotonic
from typing import Any, Callable, Iterator, Literal, Mapping, NamedTuple, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from app.credentials import sigv4
from app import edition_features, github_api, http_bounded, oauth2_wire
from app.credentials.oauth_wizards import connect_for_type
from app.credentials.registry import CREDENTIAL_TYPES, FieldSpec, CredentialTypeSpec, host_field
from app.entity_shape import is_blank

# The community edition has no cloud providers: `_not_in_ce` answers their testers before these names are read.
if edition_features.enabled("cloud_nodes"):
    from app.azure import wire as azure_wire
    from app.gcp import auth as gcp_auth, wire as gcp_wire

_log = logging.getLogger(__name__)

# The per-test time budget, this module's own constant on purpose (property 3).
CREDENTIAL_TEST_TIMEOUT_SECONDS = 10.0

# A tester needs one small JSON object at most; why a streamed capped read: `app/http_bounded.py`.
_MAX_RESPONSE_BYTES = 64 * 1024

# How much of a third-party identity string is quoted back to the operator. See `_identity_text`.
_MAX_IDENTITY_CHARS = 120

# Caps how many hostile hosts can hold a worker; `_request` cancels on abandonment, so permits return
# at the deadline. The extra thread buys the ability to walk away.
_MAX_CONCURRENT_EXCHANGES = 4
_EXCHANGE_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_EXCHANGES)


class TestResult(NamedTuple):
    """One completed connection test. `ok` is True only for `status == "success"`.

    `unsupported` is a third state (nothing to fix), but `ok` stays False so it never renders green.
    """

    # Not a pytest test class (pytest collects `Test*` names in a test module's namespace).
    __test__ = False

    ok: bool
    status: Literal["success", "failure", "unsupported"]
    message: str
    detail: Optional[str] = None
    #: The forge's non-2xx answer behind a failure, for `git_access`'s revocation check alone
    #: (`forges.revoked`). Never serialized: `routes/credentials/connection_test.py::CredentialTestOut` names its four
    #: fields and drops this one.
    http: Optional["_HttpOutcome"] = None
    #: The account a green test named (`_named`), for `git_connections` to stamp as a git credential's
    #: `account`. Never serialized either.
    identity: Optional[str] = None


# The deadline rides the signature, so a tester cannot call outside its budget.
Tester = Callable[[dict[str, Any], float], TestResult]


def new_deadline() -> float:
    """The one time budget for one `POST /api/credentials/test`, created by the route.

    Shared by every call a tester makes, so the bound is on the request. Created after the row lookup,
    secret fill and audit commit so DB latency is not charged to it (a convention enforced by the `Tester`
    signature and tests). Keeps the timeout constant single-homed.
    """
    return monotonic() + CREDENTIAL_TEST_TIMEOUT_SECONDS


def _client() -> httpx.Client:
    """The one client every tester uses: this module's own 10.0s budget, `http_bounded`'s policy
    (redirects off, `Accept-Encoding: identity` — it says why)."""
    return http_bounded.client(CREDENTIAL_TEST_TIMEOUT_SECONDS)


# ── Shared result construction ────────────────────────────────────────────────

def _failure(message: str, detail: Optional[str] = None) -> TestResult:
    return TestResult(ok=False, status="failure", message=message, detail=detail)


def _success(message: str, detail: Optional[str] = None, *, identity: Optional[str] = None) -> TestResult:
    return TestResult(ok=True, status="success", message=message, detail=detail, identity=identity)


# For an untestable type with no reason: unreachable from the route (the import guard requires a
# reason, and unknown slugs 400 first); only a monkeypatched test synthetic reaches it.
_UNSUPPORTED_WITHOUT_REASON = "Connection test not available for this credential type yet."


def unsupported_result(slug: str) -> TestResult:
    """The honest neutral state for a registry type no connection test could make a true claim about.

    Carries the type's own `untestable_reason` (single-homed in the registry; `CredentialTypeOut` also
    serves it). Total over any string: an unknown slug falls back rather than raising a 500.
    """
    spec = CREDENTIAL_TYPES.get(slug)
    reason = spec.untestable_reason if spec is not None else None
    return TestResult(
        ok=False,
        status="unsupported",
        message=reason or _UNSUPPORTED_WITHOUT_REASON,
    )


#: Why a NodeVault credential has no Test (D12, AC-5.9): exact text, pinned by its test.
NODEVAULT_UNTESTABLE = "Testing runs on Jaah's servers, which can't open NodeVault credentials."


def nodevault_untestable_result() -> TestResult:
    """The neutral `unsupported` result for a NodeVault credential: the test would run on the control plane,
    which holds no NodeVault key (`routes/credentials/connection_test.py::test_credential` returns it before any open)."""
    return TestResult(ok=False, status="unsupported", message=NODEVAULT_UNTESTABLE)


def withheld_secret_result(label: str, host_label: str) -> TestResult:
    """The result for a secret the server declined to substitute because the host moved.

    `host_label` is the type's own host field label (`host_field`: "Base URL", OIDC's "Issuer", OAuth2's
    "Token URL"), so the sentence names the field the operator changed.
    `routes/credentials/connection_test.py::_with_stored_secrets` decides that; the SENTENCE lives here with every
    other test-result sentence, so operator-facing test phrasing has one home."""
    return _failure(f"Re-enter the {label} to test against a different {host_label}.")


# Split out for `_header_name_clause`'s reason: the write path spells it against the config key.
_UNTRANSMITTABLE_CLAUSE = "contains characters that cannot be sent in a request."


def untransmittable_field_result(label: str) -> TestResult:
    """The result for a field value no request can carry, shared by `_field_problem` (unencodable header)
    and `test_aws` (an access key id the SigV4 signer refuses).
    """
    return _failure(f"{label} {_UNTRANSMITTABLE_CLAUSE}")


def _field_problem(spec: CredentialTypeSpec, config: dict[str, Any]) -> Optional[TestResult]:
    """A failure naming the first field the operator has to fix, or None when the config is usable.

    Keyed on the field label, in registry order. The transmittability check is here because httpx's
    `UnicodeEncodeError` would escape the mapping as a 500. `require_https_base_url` gets its own message;
    this is the test-time half of that flag and `routes/credentials/credential_wire.py::_validated_value` the write-time
    half, which must not drift. No message echoes the value (a `base_url` may carry userinfo).
    """
    # A blank optional header field is never sent (`_declared_tester`), so its edge whitespace is no problem.
    unsent_if_blank = {key for _header, key in spec.key_test.field_headers} if spec.key_test else set()
    for field in spec.fields:
        value = config.get(field.key)
        if field.required and is_blank(value):
            return _failure(f"{field.label} is required before testing.")
        if field.key in unsent_if_blank and is_blank(value):
            continue
        if isinstance(value, str) and not _is_transmittable(value):
            return untransmittable_field_result(field.label)
        if field.kind == "url" and not is_blank(value):
            base, _host = _split_base_url(value)
            if base is None:
                return _failure(f"{field.label} is not a valid http(s) URL.")
            if spec.require_https_base_url and not base.startswith("https://"):
                return _failure(
                    f"{field.label} must use https:// - {spec.label} credentials must never be "
                    "sent over a plaintext connection."
                )
            if field is host_field(spec):
                clause = _allowed_host_clause(spec, value)
                if clause is not None:
                    return _failure(f"{field.label} {clause}.")
    return None


def _allowed_host_clause(spec: CredentialTypeSpec, value: Any) -> Optional[str]:
    """What is wrong with `value` as the host field of a type that pins `allowed_hosts`, as a clause that
    follows a field name, or None (also for a blank value or a type with no `allowed_hosts`).

    Exactly an allowed origin: `https`, a listed hostname, no userinfo, no port, and an empty or `/` path,
    query and fragment. One clause for both halves: `_field_problem` prefixes the label, and
    `routes/credentials/credential_wire.py::_validated_value` the config key. Never echoes the value (it may carry userinfo).
    """
    if not spec.allowed_hosts or is_blank(value):
        return None
    cleaned = value.strip()
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        parts = None
    # `netloc` must equal a bare hostname, which refuses userinfo and any port (even an empty `host:`).
    # `urlsplit` silently drops tab/CR/LF, and `lower()` folds e.g. U+212A to `k`, so a value carrying either
    # would pass yet be stored in a spelling that is not the allowed origin.
    if (
        parts is None
        or not cleaned.isascii()
        or any(char in cleaned for char in "\t\r\n")
        or parts.scheme != "https"
        or parts.netloc.lower() not in spec.allowed_hosts
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        return "must be one of " + ", ".join(f"https://{host}" for host in spec.allowed_hosts)
    return None


def _is_transmittable(value: str) -> bool:
    """Whether `value` can go into an HTTP header or Basic auth at all.

    ASCII, not latin-1 (httpx encodes headers as ASCII, and a failure would blame the base URL). Control
    characters and edge whitespace are rejected too, since h11 refuses them.
    """
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    if value != value.strip():
        return False
    return all(char.isprintable() or char == "\t" for char in value)


def request_origin(base_url: Any) -> Optional[tuple[str, str, int]]:
    """`(scheme, host, port)` as the client will resolve them, or None when there is no usable origin.

    The single home for "same place", used by the stored-secret host pin
    (`routes/credentials/connection_test.py::_with_stored_secrets`) and `_request`'s pin; see `http_bounded.origin`.
    Edge whitespace is rejected, not stripped: httpx would encode a trailing space into the host.
    """
    if not isinstance(base_url, str) or not base_url.strip() or base_url != base_url.strip():
        return None
    return http_bounded.origin(base_url)


def _same_origin(a: Optional[tuple[str, str, int]], b: Optional[tuple[str, str, int]]) -> bool:
    """Whether two `request_origin(...)` results name the same place. `None` equals nothing, not even
    `None`: an unparseable destination must be refused."""
    return a is not None and a == b


def _split_base_url(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """`(normalized base URL, host)`, or `(None, None)` when it is not a usable http(s) URL.

    Query, fragment and trailing slash are dropped so a tester can append its path unconditionally.
    `host` is `urlsplit(...).hostname` (no userinfo, no port), since it is echoed to the operator.
    """
    if not isinstance(raw, str):
        return None, None
    cleaned = raw.strip()
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return None, None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None, None
    # netloc kept verbatim so the request goes where pointed; only `hostname` is narrowed for display.
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return base.rstrip("/"), parts.hostname


# Kept for each tester's defensive branch; `_field_problem` composes the real sentence.
_BAD_BASE_URL = "Base URL is not a valid http(s) URL."


# ── Where a test goes: one string, two consumers ──────────────────────────────

def _operator_host_field(spec: CredentialTypeSpec) -> Optional[FieldSpec]:
    """The field through which the operator chooses the host, or None for a fixed-host type — the
    registry's `host_field`, which is also the sealing destination's derivation (`sealing_destination`),
    so the tester's destination and the AAD a secret is bound to cannot disagree."""
    return host_field(spec)


def _effective_base_url(spec: CredentialTypeSpec, value: Any) -> Optional[str]:
    """The base URL a test of `spec` will actually use, given `value` for its operator host field.

    One derivation, two consumers: `_destination` and the router's stored-secret host pin; any divergence
    (even about `"   "`) is a hole in the pin. A type with no url field returns its default outright,
    ignoring a stale stored `base_url`.
    """
    field = _operator_host_field(spec)
    if field is None:
        return spec.default_base_url
    return spec.default_base_url if is_blank(value) else value


def _destination(spec: CredentialTypeSpec, config: dict[str, Any]) -> _Destination | TestResult:
    """Where a test of `spec` under `config` is going, or the failure for an unusable base URL.

    `operator_supplied` is True only when the operator filled the host field, so `_host_advice` never
    sends them to a URL they never typed; `host_label` is that field's own label for it to name.
    """
    field = _operator_host_field(spec)
    raw = config.get(field.key) if field is not None else None
    base, host = _split_base_url(_effective_base_url(spec, raw))
    if base is None or host is None:
        # Operator-supplied only (reported first by `_field_problem`); fixed defaults are proved at import.
        return _failure(_BAD_BASE_URL)
    return _Destination(
        base=base,
        host=host,
        operator_supplied=field is not None and not is_blank(raw),
        host_label=field.label if field is not None else "URL",
    )


# ── The shape gate, and the one sanitiser for third-party text ────────────────
# The false-green control: portals, LB error pages and SPA shells answer 200 with HTML, so a 2xx not
# parsing into the provider's container fails. Gated testers send `Accept: application/json` unless the
# provider pins one or sends none; Jira and the workflow-automation type are ungated.

# THE WIRE RULE: a third-party value put into a REQUEST (Xero's tenant id) must be a `str` passing
# `_is_transmittable`, or it is the unexpected-body failure — never a green.

def _json_body(body: bytes) -> Any:
    """The response body parsed as JSON, or None when it is not readable as JSON at all.

    A body that filled `_MAX_RESPONSE_BYTES` is unreadable (a parseable prefix is not the document);
    conservative, since a complete body >= 64 KiB is also refused (a follow-up). `RecursionError` is
    caught: deep nesting under the cap would otherwise 500 a completed test. A repeated key is
    unparsable, not last-wins, so `{"ok":false,"ok":true}` cannot green.
    """
    if len(body) >= _MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def _reject_duplicate_keys(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
    """`object_pairs_hook` for `_json_body`: builds `dict(pairs)` but raises `ValueError` on a repeated
    key, at any nesting level.
    """
    seen: dict[Any, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key in JSON object: {key!r}")
        seen[key] = value
    return seen


def _json_field(parsed: Any, *path: str | int) -> Any:
    """`parsed` walked down `path` (str keys, int indices), or None. Never raises on any body shape:
    a 2xx from an operator-named host is hostile input. An unhashable step can still raise `TypeError`,
    a programming error unreachable from a response.
    """
    current = parsed
    for step in path:
        if isinstance(step, int) and not isinstance(step, bool):
            if not isinstance(current, list) or not -len(current) <= step < len(current):
                return None
            current = current[step]
        elif isinstance(current, dict):
            current = current.get(step)
        else:
            return None
    return current


def _identity_text(*candidates: Any) -> Optional[str]:
    """The first candidate that survives sanitising, or None when none does.

    The one place third-party text is made safe to display: non-printables and strong-RTL characters
    (`R`, `AL`, which reorder surrounding text) are stripped, and the result is capped at
    `_MAX_IDENTITY_CHARS`. Markup escaping is the renderer's job. Every candidate is sanitised; an `int`
    (not `bool`) is coerced for HubSpot's `portalId`; any other non-`str` is skipped.
    """
    for candidate in candidates:
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            candidate = str(candidate)
        if not isinstance(candidate, str):
            continue
        cleaned = "".join(
            char
            for char in candidate
            if char.isprintable() and unicodedata.bidirectional(char) not in ("R", "AL")
        ).strip()
        if cleaned:
            return cleaned[:_MAX_IDENTITY_CHARS]
    return None


def _credentials_accepted(spec: CredentialTypeSpec) -> TestResult:
    """The success for a green with no identity to name, the single home for that sentence.
    Partial identities are all-or-nothing: "Connected to Xero as ." is worse.
    """
    return _success(f"Connected to {spec.label} - credentials accepted.")


def _unexpected_body(spec: CredentialTypeSpec, dest: _Destination) -> TestResult:
    """A 2xx that is not the provider's API answering. See the shape-gate note above."""
    return _failure(
        f"{spec.label} answered, but not with an API response - {_host_advice(dest)}",
        detail="unexpected_body",
    )


class _HttpOutcome(NamedTuple):
    """A completed exchange: the status, however much of the body was worth reading, and the headers."""

    status_code: int
    body: bytes
    headers: Mapping[str, str] = {}


class _Destination(NamedTuple):
    """Where one test is going, and who chose to send it there.

    `operator_supplied` keys on whether the operator typed this host (see `_destination`).
    """

    base: str  # normalised, no trailing slash — a tester appends its own path unconditionally
    host: str  # what the messages name; userinfo and port already dropped by `_split_base_url`
    operator_supplied: bool
    host_label: str = "URL"  # the type's own host field label ("Zone URL", "API URL"); "URL" with no host field


def _host_advice(dest: _Destination) -> str:
    """The one home for "who should the operator go fix" when a host does not answer usefully, shared by
    the transport failure and the shape gate so they name the same culprit — by the form's own label.
    """
    return f"check the {dest.host_label}." if dest.operator_supplied else "check this server's network access."


def _timed_out(dest: _Destination) -> TestResult:
    return _failure(
        f"Timed out after {int(CREDENTIAL_TEST_TIMEOUT_SECONDS)}s connecting to {dest.host}.",
        detail="timeout",
    )


def _unreachable(dest: _Destination) -> TestResult:
    return _failure(f"Could not reach {dest.host} - {_host_advice(dest)}", detail="transport_error")


def _gated_body(
    spec: CredentialTypeSpec,
    dest: _Destination,
    outcome: _HttpOutcome,
    container: type | tuple[type, ...] = dict,
) -> dict[str, Any] | list[Any] | TestResult:
    """The shape gate itself: the decision a shape-gated tester calls rather than reimplements.

    Available, not mandatory (`jira` and the workflow-automation type are deliberately ungated). `container`
    is a parameter because some providers answer a JSON array. Callers must check `isinstance(result, TestResult)`: an
    empty `{}`/`[]` is a legitimate body, so `if not gated:` is wrong.
    """
    parsed = _json_body(outcome.body)
    if not isinstance(parsed, container):
        return _unexpected_body(spec, dest)
    return parsed


def _request(
    method: Literal["GET", "POST"],
    url: str,
    *,
    dest: _Destination,
    deadline: float,
    auth: Optional[httpx.Auth] = None,
    headers: Optional[dict[str, str]] = None,
    content: Optional[bytes] = None,
    allowed_origin: Optional[tuple[str, str, int]] = None,
) -> _HttpOutcome | TestResult:
    """One outbound call, or the mapped failure for it, inside the caller's remaining `deadline`.

    The deadline is created once per request (`new_deadline`); a `0.0` budget is legal. The exchange runs
    on its own daemon thread under a bounded permit pool and the caller stops waiting at the deadline (see
    `_Abandonment`), because httpx timeouts are per-operation and a dripping host is never timed out
    (measured 27s on a 10s "bound").

    The read is streamed and capped at `_MAX_RESPONSE_BYTES` raw bytes (a buffered huge body would OOM
    the single API worker). `TimeoutException` is caught before `ConnectError` (`ConnectTimeout` is both).
    `url`'s origin is pinned to `allowed_origin` (default `request_origin(dest.base)`; a second fixed host
    passes a module constant, never one from `config`).
    """
    expected_origin = allowed_origin if allowed_origin is not None else request_origin(dest.base)
    actual_origin = request_origin(url)
    if not _same_origin(actual_origin, expected_origin):
        # Refused, not mistrusted: a splice would send the credential to a host the stored-secret pin never
        # compared. Reachable from input (`urlsplit` accepts bases `request_origin` rejects, giving `None`).
        # Logs origins only: `dest.base` may carry userinfo.
        _log.error(
            "credential test refused: url origin does not match the pinned destination "
            "(url_origin=%s expected_origin=%s dest_host=%s)", actual_origin, expected_origin, dest.host,
        )
        return _unreachable(dest)
    outcome: Queue = Queue(maxsize=1)  # a fresh queue per call, so no result can reach another caller
    handle = _Abandonment()

    def run() -> None:
        # Nothing may raise past `_exchange`, or the caller waits the whole deadline for an instant error.
        try:
            result = _exchange(method, url, dest, auth, headers, content, deadline, handle)
        except BaseException:  # noqa: BLE001 - a worker thread has nowhere to raise TO
            result = _unreachable(dest)
        # One put, outside the guard: a failing put inside it could put again and block on the full queue.
        outcome.put(result)

    # Daemon, so a hostile host cannot block interpreter shutdown (a pooled worker is joined at exit).
    threading.Thread(target=run, name="credential-test", daemon=True).start()
    try:
        return outcome.get(timeout=max(0.0, deadline - monotonic()))
    except Empty:
        handle.abandon()
        return _timed_out(dest)


class _Abandonment:
    """The handle a caller uses to cancel its own exchange, and the exchange uses to notice.

    Without it an abandoned worker stayed blocked in a dripping header read, holding its permit; four such
    requests exhausted the pool. Closing the client from the caller's thread ends the read at its next
    boundary (measured worst case: abandon + 10s read timeout). All ordering is under one lock: a client
    adopted after abandonment is never used, one adopted before is closed immediately.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: Optional[httpx.Client] = None
        self._abandoned = False

    def adopt(self, client: httpx.Client) -> bool:
        """Register the client to be closed on abandonment. False when the caller already gave up, in which
        case the exchange must not make its request."""
        with self._lock:
            if self._abandoned:
                return False
            self._client = client
            return True

    def done(self) -> None:
        """The exchange is finished with its client and closing it is now the exchange's own business."""
        with self._lock:
            self._client = None

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing is best-effort; the worker maps whatever it sees
                pass


def _exchange(
    method: Literal["GET", "POST"],
    url: str,
    dest: _Destination,
    auth: Optional[httpx.Auth],
    headers: Optional[dict[str, str]],
    content: Optional[bytes],
    deadline: float,
    handle: _Abandonment,
) -> _HttpOutcome | TestResult:
    """The actual request, on its own daemon thread. Never raises.

    An exchange nobody waits for must not send a credential, so the deadline is rechecked after the
    permit and the handle decides right before the request. Residual: httpcore's pool has no closed flag,
    so a narrowly-timed abandonment can still let one request through to the same host.
    """
    if not _EXCHANGE_SLOTS.acquire(timeout=max(0.0, deadline - monotonic())):
        return _timed_out(dest)
    try:
        if monotonic() >= deadline:
            return _timed_out(dest)
        return _exchange_locked(method, url, dest, auth, headers, content, deadline, handle)
    finally:
        _EXCHANGE_SLOTS.release()


def _exchange_locked(
    method: Literal["GET", "POST"],
    url: str,
    dest: _Destination,
    auth: Optional[httpx.Auth],
    headers: Optional[dict[str, str]],
    content: Optional[bytes],
    deadline: float,
    handle: _Abandonment,
) -> _HttpOutcome | TestResult:
    """The one place a request is put on the wire: no second transport, and no path around the deadline,
    the permit or the abandonment handle.

    `content` is raw bytes, never `data=`/`json=`: a SigV4 request hashes the exact bytes it sends.
    """
    client = _client()
    if not handle.adopt(client):
        # The caller gave up between the permit and here. Nothing is sent.
        client.close()
        return _timed_out(dest)
    try:
        with client, client.stream(
            method, url, auth=auth, headers=headers, content=content
        ) as response:
            # Truncating is safe because `_json_body` treats a capped body as unreadable. This deadline stops an
            # abandoned worker reading a trickle forever.
            body, _ = http_bounded.read_capped(
                response, _MAX_RESPONSE_BYTES, lambda: monotonic() >= deadline
            )
            return _HttpOutcome(response.status_code, body, dict(response.headers))
    except httpx.TimeoutException:
        return _timed_out(dest)
    except httpx.ConnectError:
        return _unreachable(dest)
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, UnicodeError, RuntimeError):
        # One fixed `detail`, not the exception class, which would fingerprint services the CP can reach.
        # `UnicodeError` is a belt behind `_field_problem`. `RuntimeError` is the reproduced abandonment race
        # (re-entering a client closed between `adopt()` and `with client`).
        return _unreachable(dest)
    finally:
        # Drop the reference so a late `abandon()` is usually a no-op (and `close()` is idempotent).
        handle.done()


def _status_failure(outcome: _HttpOutcome, type_label: str) -> Optional[TestResult]:
    """The failure for a non-2xx response, or None when it is a 2xx.

    A 3xx fails too (redirects are disabled, so the base URL is not the API). `type_label` comes from
    the registry, never a literal here.
    """
    code = outcome.status_code
    if 200 <= code < 300:
        return None
    if code in (401, 403):
        return _failure("Authentication rejected - check the credentials.", detail=f"HTTP {code}")
    return _failure(f"{type_label} returned HTTP {code}.", detail=f"HTTP {code}")


# ── The three shapes every tester is built from ───────────────────────────────
# One home for each ordering: a blank required field before an unparseable URL, a non-2xx before any
# body read. A tester copied from the wrong template would report the wrong fault.

def _prepare(
    slug: str, config: dict[str, Any]
) -> tuple[CredentialTypeSpec, _Destination] | TestResult:
    """The preamble every tester runs: its registry spec, the operator's fields checked, the destination
    resolved, or the failure to return instead of calling anything.

    `_field_problem` first: a blank required field is the better sentence than `_BAD_BASE_URL`.
    """
    spec = CREDENTIAL_TYPES[slug]
    problem = _field_problem(spec, config)
    if problem is not None:
        return problem
    dest = _destination(spec, config)
    if isinstance(dest, TestResult):
        # Already reported by `_field_problem` for an operator URL, and unreachable for fixed hosts by the
        # import-time guard at the foot of this module. A belt.
        return dest
    return spec, dest


def _checked_request(
    spec: CredentialTypeSpec,
    dest: _Destination,
    deadline: float,
    method: Literal["GET", "POST"],
    url: str,
    *,
    auth: Optional[httpx.Auth] = None,
    headers: Optional[dict[str, str]] = None,
    content: Optional[bytes] = None,
    allowed_origin: Optional[tuple[str, str, int]] = None,
    rejected_message: Optional[str] = None,
) -> _HttpOutcome | TestResult:
    """One outbound call whose non-2xx is already the operator's answer: `_request` then
    `_status_failure`. Only a 2xx comes back as an outcome.

    `rejected_message` replaces the shared 401/403 sentence only, for types where it would misdirect
    (the generic type's header name). Callers needing their own sentence
    for another status (Xero's 400, AWS's clock skew, Slack's 200 `ok:false`) call `_request` directly.
    """
    outcome = _request(
        method, url, dest=dest, deadline=deadline, auth=auth, headers=headers,
        content=content, allowed_origin=allowed_origin,
    )
    if isinstance(outcome, TestResult):
        return outcome
    if rejected_message is not None and outcome.status_code in (401, 403):
        return _failure(rejected_message, detail=f"HTTP {outcome.status_code}")._replace(http=outcome)
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure._replace(http=outcome)
    return outcome


# When the operator names none; see the `generic_api_key` registry entry.
_DEFAULT_API_KEY_HEADER = "Authorization"


class _HeaderAuth(httpx.Auth):
    """One header, set through httpx's auth flow rather than the header map; required for `Authorization`.

    Measured (httpx 0.27.2, pinned): a URL's `user:pass@` makes httpx synthesise `Authorization: Basic`,
    which overwrites a plain `Authorization` header but loses to an `auth` object. `_split_base_url` keeps
    userinfo, so an operator's key would otherwise be silently replaced.

    Used for `Authorization` only (`_api_key_request` decides): an `auth` object also suppresses the
    synthesis, so routing another header name here would stop sending the Basic header a Basic-auth
    proxy setup relies on (`test_a_userinfo_bearing_base_url_is_a_deliberate_non_secret`).
    """

    def __init__(self, name: str, value: str) -> None:
        self._name = name
        self._value = value

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers[self._name] = self._value
        yield request


def _api_key_request(
    spec: CredentialTypeSpec,
    dest: _Destination,
    deadline: float,
    *,
    path: str,
    key: str,
    header_name: str = _DEFAULT_API_KEY_HEADER,
    value_prefix: str = "",
    accept: Optional[str] = "application/json",
    extra_headers: Optional[dict[str, str]] = None,
    rejected_message: Optional[str] = None,
) -> _HttpOutcome | TestResult:
    """`GET {dest.base}{path}` carrying `key` in one header: the shape most of these types share.

    Every declarative type (`_declared_tester`, from its registry `KeyTestSpec`), Headscale and the generic type
    differ only in header name, scheme word, path and `Accept`, so those are arguments. Jira/Xero (BasicAuth),
    GitHub (host-dependent prefix), AWS (signed) and Slack (two POSTs) stay separate.

    `value_prefix` is a scheme word; the space is supplied here. The key never reaches a query string;
    `path` is the caller's literal. `Authorization` rides
    an `httpx.Auth` flow, every other name the header map (see `_HeaderAuth`).
    """
    headers: dict[str, str] = {}
    if accept is not None:
        headers["Accept"] = accept
    if extra_headers:
        headers.update(extra_headers)
    value = f"{value_prefix} {key}" if value_prefix else key
    displaceable = header_name.lower() == _DEFAULT_API_KEY_HEADER.lower()
    if not displaceable:
        headers[header_name] = value
    return _checked_request(
        spec, dest, deadline, "GET", f"{dest.base}{path}",
        auth=_HeaderAuth(header_name, value) if displaceable else None,
        headers=headers,
        rejected_message=rejected_message,
    )


def _declared_tester(slug: str) -> Tester:
    """The tester for a type whose registry entry declares `key_test`: `_prepare`, then one `_api_key_request`,
    then the verdict the spec names. The single home for every single-GET key check, so no such type has a
    function of its own; `_check_tester_registry` proves each spec sendable at import.
    """
    if CREDENTIAL_TYPES[slug].key_test is None:
        raise ValueError(f"{slug}: declares no key_test")

    def test(config: dict[str, Any], deadline: float) -> TestResult:
        prepared = _prepare(slug, config)
        if isinstance(prepared, TestResult):
            return prepared
        spec, dest = prepared
        # Read with `spec`, never captured at build time, so the request and the verdict share one spec.
        key_test = spec.key_test
        if key_test is None:
            raise ValueError(f"{slug}: declares no key_test")
        key = config.get(key_test.key_field)
        if key_test.blank_key_reason is not None and is_blank(key):
            return TestResult(ok=False, status="unsupported", message=key_test.blank_key_reason)
        key_label = _field_label(spec, key_test.key_field)
        bearer = key_test.bearer_when is not None and not is_blank(config.get(key_test.bearer_when))
        outcome = _api_key_request(
            spec, dest, deadline,
            path=key_test.path,
            key=str(key),
            header_name="Authorization" if bearer else key_test.header_name,
            value_prefix="Bearer" if bearer else key_test.value_prefix,
            accept=key_test.accept,
            extra_headers=dict(key_test.extra_headers) | {
                header: str(config[field]) for header, field in key_test.field_headers
                if not is_blank(config.get(field))
            },
            rejected_message=_vendor_rejected_message(spec, dest, key_label) if key_test.reject_names_vendor else None,
        )
        if isinstance(outcome, TestResult):
            return outcome
        if key_test.accept is None:
            # Not shape-gated: the body is bounded and never inspected.
            return _success(f"Connected to {spec.label} - {key_label} accepted.")
        if key_test.list_field is not None:
            parsed = _gated_body(spec, dest, outcome)
            if isinstance(parsed, TestResult) or not isinstance(_json_field(parsed, key_test.list_field), list):
                return _unexpected_body(spec, dest)
            return _credentials_accepted(spec)
        return _named(spec, dest, outcome, *key_test.identity, phrase=key_test.identity_phrase)

    return test


def _vendor_rejected_message(spec: CredentialTypeSpec, dest: _Destination, key_label: str) -> str:
    """A `reject_names_vendor` type's 401/403 sentence. With an operator host field (Make's zone) it names the
    host that refused, since a key valid on another host (an EU account left on `us1`) is rejected too."""
    field = _operator_host_field(spec)
    if field is None:
        return f"{spec.label} rejected the {key_label} - check it."
    return f"The {key_label} was rejected by {dest.host} - check the {key_label} and {field.label}."


def _named(
    spec: CredentialTypeSpec,
    dest: _Destination,
    outcome: _HttpOutcome,
    *paths: tuple[str | int, ...],
    phrase: str = "as",
) -> TestResult:
    """The identity verdict on a shape-gated JSON object: the first of `paths` that `_identity_text` keeps,
    as "Connected to {label} {phrase} {identity}.", or `_credentials_accepted` when none survives."""
    parsed = _gated_body(spec, dest, outcome)
    if isinstance(parsed, TestResult):
        return parsed
    identity = _identity_text(*(_json_field(parsed, *path) for path in paths))
    if identity is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} {phrase} {identity}.", identity=identity)


# ── Jira ──────────────────────────────────────────────────────────────────────

def test_jira(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET {base_url}/rest/api/3/myself` with Basic auth (email + API token).

    Names who the credentials belong to. Not shape-gated: an unreadable 2xx body still proves the
    credential worked, and gating would change a shipped sentence for no new signal.
    """
    prepared = _prepare("jira", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    email = str(config["email"]).strip()
    outcome = _checked_request(
        spec, dest, deadline, "GET", f"{dest.base}/rest/api/3/myself",
        auth=httpx.BasicAuth(email, str(config["api_token"])),
    )
    if isinstance(outcome, TestResult):
        return outcome
    return _success(f"Connected to {spec.label} as {_jira_identity(outcome.body, email)}.")


def _jira_identity(body: bytes, email: str) -> str:
    """`displayName` from the response, falling back to the account email.

    An unparsable, wrong-shape or truncated 2xx body is still a successful authenticated call. A
    non-string `displayName` is narrowed away (so `_identity_text`'s number coercion does not apply); the
    fallback is sanitised and capped. `or email` only keeps the return type `str`.
    """
    name = _json_field(_json_body(body), "displayName")
    return _identity_text(name if isinstance(name, str) else None, email) or email


# ── Xero ──────────────────────────────────────────────────────────────────────

# Call 2's address, a tester-local constant rather than a second `default_base_url`:
# `identity.xero.com` receives the operator's credentials (the host the stored-secret pin compares);
# `api.xero.com` only gets a token derived a moment earlier, and no operator field can move it.
_XERO_CONNECTIONS_URL = "https://api.xero.com/connections"

# Call 2's pin, computed once from the constant, never from `config`. Passed as `allowed_origin`
# because the default pin is `dest.base` (`identity.xero.com`).
_XERO_CONNECTIONS_ORIGIN = request_origin(_XERO_CONNECTIONS_URL)


def test_xero(config: dict[str, Any], deadline: float) -> TestResult:
    """`POST {base}/connect/token`, then `GET api.xero.com/connections` with the token and Tenant ID.

    Two calls across two hosts, spending one deadline sequentially (halving it would false-negative a
    slow valid credential). Call 2 is the identity call; a client-credentials token names no org.

    Call 2 carries `Xero-Tenant-Id` (hence Tenant ID is required). Observed live: no or empty header ->
    400; a tenant not on this connection -> 200 `[]`. So an empty list is a failure naming the Tenant ID.
    Custom Connections is a paid add-on; a plan without it cannot green.
    """
    prepared = _prepare("xero", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    access_token = _xero_access_token(spec, dest, config, deadline)
    if isinstance(access_token, TestResult):
        return access_token

    # `allowed_origin` is what the request is pinned to; `dest` only names failures, and every failure
    # from this call is replaced below.
    outcome = _request(
        "GET",
        _XERO_CONNECTIONS_URL,
        dest=dest,
        deadline=deadline,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            # Proved present and transmittable by `_field_problem`; `str()` because values are untyped.
            "Xero-Tenant-Id": str(config["tenant_id"]),
        },
        allowed_origin=_XERO_CONNECTIONS_ORIGIN,
    )
    if isinstance(outcome, TestResult):
        # One sentence for every way call 2 fails to complete, naming Xero (not a host the operator never
        # typed). `detail` passes through, keeping the timeout/transport distinction.
        return _failure(
            f"{spec.label} accepted the credentials but could not complete the organisation lookup"
            " - check this server's network access.",
            detail=outcome.detail,
        )
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure
    parsed = _json_body(outcome.body)
    if not isinstance(parsed, list):  # the shape gate: `/connections` documents a LIST of tenants
        return _unexpected_body(spec, dest)
    if not parsed:
        return _failure(
            f"{spec.label} accepted the credentials but no organisation matches this Tenant ID"
            " - check the Tenant ID."
        )
    tenant = _identity_text(_json_field(parsed, 0, "tenantName"))
    if tenant is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} as {tenant}.")


def _xero_access_token(
    spec: CredentialTypeSpec, dest: _Destination, config: dict[str, Any], deadline: float
) -> str | TestResult:
    """Call 1: the client-credentials access token, or the failure to report instead of calling again.

    `scope` is omitted when blank: a Custom Connection's scope must subset what the Xero UI granted, so
    absent (Xero's default, everything granted) is the only universal value. A 400 (`invalid_client`,
    `invalid_scope`) gets its own sentence, chosen by our branch and never copied from the vendor body;
    other statuses use the shared table.
    """
    form = {"grant_type": "client_credentials"}
    scope = config.get("scope")
    if not is_blank(scope):
        form["scope"] = str(scope)
    outcome = _request(
        "POST",
        f"{dest.base}/connect/token",
        dest=dest,
        deadline=deadline,
        # BasicAuth, not a body field: the credential travels in a header, never in a URL.
        auth=httpx.BasicAuth(str(config["client_id"]), str(config["client_secret"])),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        content=urlencode(form).encode("ascii"),
    )
    if isinstance(outcome, TestResult):
        return outcome
    if outcome.status_code == 400:
        return _failure(
            f"{spec.label} rejected the client credentials or the requested scope - check the "
            "Client ID, Client secret and Scope.",
            detail=f"HTTP {outcome.status_code}",
        )
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure
    parsed = _json_body(outcome.body)
    if not isinstance(parsed, dict):
        return _unexpected_body(spec, dest)
    access_token = _json_field(parsed, "access_token")
    if (
        not isinstance(access_token, str)
        or is_blank(access_token)
        or not _is_transmittable(access_token)
    ):
        # The wire rule on the one third-party value put into a request: a CRLF or unencodable byte would
        # surface as a transport error. `is_blank` is needed too: `_is_transmittable("")` is vacuously true.
        return _unexpected_body(spec, dest)
    return access_token


# ── Slack ─────────────────────────────────────────────────────────────────────

def test_slack(config: dict[str, Any], deadline: float) -> TestResult:
    """`POST https://slack.com/api/auth.test` with the bot token, then `POST .../apps.connections.open`
    with the app-level token: two credentials, one shared deadline, both required for a green.

    Slack answers HTTP 200 `{"ok": false, ...}` for a bad token, so `ok is True` is required on each
    call (`is not True` fails closed). Each rejection names that call's field; the vendor `error` is
    never read into a message. The `wss://` `url` from `apps.connections.open` is never read. POST with
    `content=b""`. Success names both workspace and user, or neither.
    """
    prepared = _prepare("slack", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    auth_test = _slack_call(spec, dest, deadline, config, "/api/auth.test", "token")
    if isinstance(auth_test, TestResult):
        return auth_test
    team = _identity_text(_json_field(auth_test, "team"))
    user = _identity_text(_json_field(auth_test, "user"))

    # Call 2: the app-level token, sharing the same deadline (one budget). Only `ok` is read.
    connections_open = _slack_call(spec, dest, deadline, config, "/api/apps.connections.open", "app_token")
    if isinstance(connections_open, TestResult):
        return connections_open

    if team is None or user is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} workspace {team} as {user}.")


def _slack_call(
    spec: CredentialTypeSpec,
    dest: _Destination,
    deadline: float,
    config: dict[str, Any],
    path: str,
    token_field: str,
) -> dict[str, Any] | TestResult:
    """One empty-body Slack POST with `config[token_field]` as the Bearer token: the `ok: true` object, or
    the failure naming that token's field."""
    token_label = _field_label(spec, token_field)
    outcome = _request(
        "POST",
        f"{dest.base}{path}",
        dest=dest,
        deadline=deadline,
        headers={
            "Authorization": f"Bearer {str(config[token_field])}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "application/json",
        },
        content=b"",
    )
    if isinstance(outcome, TestResult):
        return outcome
    return _ok_envelope(spec, dest, outcome, token_label=token_label, rejected=(401, 403))


def _ok_envelope(
    spec: CredentialTypeSpec,
    dest: _Destination,
    outcome: _HttpOutcome,
    *,
    token_label: str,
    rejected: tuple[int, ...],
) -> dict[str, Any] | TestResult:
    """A Slack/Telegram-style `{"ok": true, ...}` response read: a status in `rejected` or an `ok` that is
    not `true` is the failure naming `token_label`; otherwise the gated body."""
    if outcome.status_code in rejected:
        return _failure(
            f"Authentication rejected - check the {token_label}.", detail=f"HTTP {outcome.status_code}"
        )
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure
    parsed = _gated_body(spec, dest, outcome)
    if isinstance(parsed, TestResult):
        return parsed
    if _json_field(parsed, "ok") is not True:
        return _failure(f"Authentication rejected - check the {token_label}.", detail="rejected")
    return parsed


# ── GitHub ────────────────────────────────────────────────────────────────────

# The path differs by host: github.com has no prefix, GHES serves `/api/v3` (appending it on
# api.github.com is a probed 404). Keyed on the host, not on who chose it, matching the stored-secret pin.
_GITHUB_ENTERPRISE_PREFIX = "/api/v3"

# Computed from the registry default so it equals what `_destination` resolves a blank box to. Safe
# before the foot-of-module guard proves it parses, because that guard raises.
_GITHUB_COM_ORIGIN = request_origin(CREDENTIAL_TYPES["github"].default_base_url)


def test_github(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET {base}/user` on github.com, `GET {base}/api/v3/user` on a GitHub Enterprise Server.

    Names the token's account. Probed: an invalid PAT answers `401 {"message":"Bad credentials", ...}`.
    PAT-only: App installation and OAuth user tokens come from flows this module lacks. The host is
    operator-suppliable, so the shape gate matters (an SSO portal can answer 200 with HTML).
    """
    prepared = _prepare("github", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    on_public_api = _same_origin(request_origin(dest.base), _GITHUB_COM_ORIGIN)
    prefix = "" if on_public_api else _GITHUB_ENTERPRISE_PREFIX
    # Not `_api_key_request`: the prefix is host-dependent and the vendor pins `Accept`. The PAT goes via
    # `auth` (see `_HeaderAuth`): a `user:pass@` in the Base URL overwrites a plain header.
    outcome = _checked_request(
        spec, dest, deadline, "GET", f"{dest.base}{prefix}/user",
        auth=_HeaderAuth("Authorization", f"Bearer {config['token']}"),
        headers=github_api.headers(None),
    )
    if isinstance(outcome, TestResult):
        return outcome
    return _named(spec, dest, outcome, ("login",))


# ── GitHub OAuth App and GitHub App (connect and sign-in) ────────────────────────────────────────────────

# On `github.com`, not `api.github.com`, hence a tester beside `test_github`.
_DEVICE_CODE_PATH = "/login/device/code"

# The one error string that is a success: answering it proves GitHub resolved the client id. A red
# for "a setting is off" would cost the operator a needless rotation.
_DEVICE_FLOW_DISABLED = "device_flow_disabled"
# GitHub's device-flow error for a client id it does not hold ("you must pass your app's client ID"): a red,
# like the 404 an unknown id has been observed to answer.
_DEVICE_CLIENT_UNKNOWN = "incorrect_client_credentials"


def test_github_app(config: dict[str, Any], deadline: float) -> TestResult:
    """The OAuth App's recognition test (`_test_github_client_id`)."""
    return _test_github_client_id("github_app", config, deadline)


def test_github_install_app(config: dict[str, Any], deadline: float) -> TestResult:
    """The GitHub App's recognition test (`_test_github_client_id`); its `slug` is checked only as present."""
    return _test_github_client_id("github_install_app", config, deadline)


def _test_github_client_id(slug: str, config: dict[str, Any], deadline: float) -> TestResult:
    """`POST https://github.com/login/device/code` with the client id: a recognition test, and the
    sentence says so.

    The pair authenticates nothing alone (every endpoint needs the Connect flow's user grant,
    `git_connect.py`), so the secret cannot be exercised. A classic OAuth App's client id (`Ov23li...`)
    answers like a GitHub App's; its device flow is off unless enabled in the app's settings, which GitHub
    answers `device_flow_disabled` — still recognition, so green. Telling an OAuth App from a GitHub App
    needs a user's grant, which Connect's `wrong_app_kind` refusal does. Probed both ways: an unknown client id answers
    HTTP 404 `{"error":"Not Found"}`, a real one HTTP 200 with `device_code`/`user_code`. The success
    sentence states the secret is unchecked; keep that second sentence, it is the control.

    Nothing from the body is echoed: `device_code`/`user_code` are a bearer-equivalent pair, so the gate
    reads only presence; the unclaimed authorization expires. The client id travels in the POST body.
    A blank client secret still refuses the test (the registry's generic `required` check); left alone,
    as `routes/credentials/connection_test.py::_audit_test` notes.
    """
    prepared = _prepare(slug, config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    client_id_label = _field_label(spec, "client_id")
    outcome = _request(
        "POST",
        f"{dest.base}{_DEVICE_CODE_PATH}",
        dest=dest,
        deadline=deadline,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        content=urlencode({"client_id": str(config["client_id"])}).encode("ascii"),
    )
    if isinstance(outcome, TestResult):
        return outcome

    # Read before the status, for this type only: GitHub's status for `device_flow_disabled` is not
    # documented, so the body decides. Bounded to 2xx/4xx: a 3xx (redirects disabled) or 5xx says nothing
    # about the client id.
    parsed = _json_body(outcome.body)
    _plausible_setting_status = 200 <= outcome.status_code < 300 or 400 <= outcome.status_code < 500
    if _plausible_setting_status and _json_field(parsed, "error") == _DEVICE_FLOW_DISABLED:
        return _success(
            f"GitHub recognises this App's {client_id_label}, which has device flow turned off. The "
            f"{_field_label(spec, 'client_secret')} cannot be checked until a user completes the "
            "Connect flow."
        )
    if _plausible_setting_status and _json_field(parsed, "error") == _DEVICE_CLIENT_UNKNOWN:
        return _failure(
            f"GitHub does not recognise this {client_id_label} at its device-authorization endpoint.",
            detail=_DEVICE_CLIENT_UNKNOWN,
        )
    if outcome.status_code == 404:
        # Names the endpoint: host and path are constants, so if GitHub moves the route the operator can
        # tell "unknown App" from "asking the wrong place".
        return _failure(
            f"GitHub does not recognise this {client_id_label} at its device-authorization endpoint.",
            detail="HTTP 404",
        )
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure

    # The shape gate: a non-empty string, value never read past here. Hand-rolled because the body is
    # already parsed above; `_gated_body` would parse it twice.
    device_code = _json_field(parsed, "device_code")
    if not isinstance(parsed, dict) or not isinstance(device_code, str) or not device_code:
        return _unexpected_body(spec, dest)
    return _success(
        f"GitHub recognises this App's {client_id_label}. The "
        f"{_field_label(spec, 'client_secret')} cannot be checked until a user completes the Connect "
        "flow."
    )


# ── Bitbucket Cloud ───────────────────────────────────────────────────────────

#: The token kinds each forge's credential accepts, single-homed for the testers, the write path and
#: `git_access.git_login_for`. The first is the default for a row that names none (a pasted token).
BITBUCKET_KINDS: tuple[str, ...] = ("api_token", "oauth")
AZURE_DEVOPS_KINDS: tuple[str, ...] = ("pat", "oauth")
# An Azure DevOps organization name as it appears in `dev.azure.com/<organization>`: it becomes a path
# segment of the test URL, so nothing that could leave that segment is admitted.
_AZURE_ORG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,49}")


def _kind_clause(kinds: tuple[str, ...]) -> Callable[[str], Optional[str]]:
    def clause(value: str) -> Optional[str]:
        return None if value in kinds else f"must be one of {', '.join(kinds)}."
    return clause


def _azure_org_clause(value: str) -> Optional[str]:
    if _AZURE_ORG.fullmatch(value):
        return None
    return "must be an organization name as in dev.azure.com/<organization> - letters, digits and hyphens."


def test_bitbucket(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET https://api.bitbucket.org/2.0/user`: Basic (account email + API token) for an `api_token`, a
    Bearer token for `oauth`. Names the account. Fixed host, shape-gated all the same."""
    prepared = _prepare("bitbucket", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared
    kind = str(config.get("kind") or BITBUCKET_KINDS[0])
    if kind not in BITBUCKET_KINDS:
        return _failure(f"{_field_label(spec, 'kind')} {_kind_clause(BITBUCKET_KINDS)(kind)}")
    token = str(config["token"])
    if kind == "api_token":
        username = config.get("username")
        if is_blank(username):
            return _failure(f"{_field_label(spec, 'username')} is required to test an API token.")
        auth: httpx.Auth = httpx.BasicAuth(str(username), token)
    else:
        auth = _HeaderAuth("Authorization", f"Bearer {token}")
    outcome = _checked_request(
        spec, dest, deadline, "GET", f"{dest.base}/2.0/user",
        auth=auth, headers={"Accept": "application/json"},
    )
    if isinstance(outcome, TestResult):
        return outcome
    return _named(spec, dest, outcome, ("username",), ("display_name",))


# ── Azure DevOps Services ─────────────────────────────────────────────────────

def test_azure_devops(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET https://dev.azure.com/{test_org}/_apis/connectionData`: Basic (any username + PAT) for a
    `pat`, a Bearer token for `oauth`. Names the authenticated user. Probed: a bad PAT answers 401 (or a
    203 sign-in page, which the shape gate refuses)."""
    prepared = _prepare("azure_devops", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared
    kind = str(config.get("kind") or AZURE_DEVOPS_KINDS[0])
    if kind not in AZURE_DEVOPS_KINDS:
        return _failure(f"{_field_label(spec, 'kind')} {_kind_clause(AZURE_DEVOPS_KINDS)(kind)}")
    org = config.get("test_org")
    if is_blank(org):
        return _failure(f"{_field_label(spec, 'test_org')} is required before testing.")
    problem = _azure_org_clause(str(org))
    if problem is not None:
        return _failure(f"{_field_label(spec, 'test_org')} {problem}")
    token = str(config["token"])
    auth = (
        httpx.BasicAuth("jaah", token) if kind == "pat"
        else _HeaderAuth("Authorization", f"Bearer {token}")
    )
    outcome = _checked_request(
        spec, dest, deadline, "GET", f"{dest.base}/{org}/_apis/connectionData",
        auth=auth, headers={"Accept": "application/json"},
    )
    if isinstance(outcome, TestResult):
        return outcome
    if outcome.status_code != 200:
        # A 203 is Azure's sign-in page for an unaccepted credential: a 2xx that is not the API.
        return _failure("Authentication rejected - check the credentials.", detail=f"HTTP {outcome.status_code}")
    return _named(spec, dest, outcome, ("authenticatedUser", "providerDisplayName"))


# ── Azure (Your Cloud) ────────────────────────────────────────────────────────
# Two calls across two fixed hosts, as Xero: the client-credentials grant at Entra (the destination, which
# receives the secret), then `GET management.azure.com/subscriptions/{id}` with the token only.

if edition_features.enabled("cloud_nodes"):
    _AZURE_ARM_ORIGIN = request_origin(azure_wire.ARM_BASE)


def _not_in_ce() -> TestResult:
    """A cloud-only type's Test in the community edition (no cloud providers, no billing)."""
    from app.ce_caps import NOT_IN_CE

    return _failure(NOT_IN_CE)


def test_azure(config: dict[str, Any], deadline: float) -> TestResult:
    """A token for Azure Resource Manager, then the subscription read with it: names the subscription.

    A refused grant shows Entra's own sentence (`AADSTS7000215: Invalid client secret provided…`, property 4's
    exception); a 403 on the subscription says the app holds no role there."""
    if not edition_features.enabled("cloud_nodes"):
        return _not_in_ce()
    prepared = _prepare("azure", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared
    for key, clause in (("tenant_id", azure_wire.tenant_clause), ("client_id", azure_wire.guid_clause),
                        ("subscription_id", azure_wire.guid_clause)):
        problem = clause(str(config[key]))
        if problem is not None:
            return _failure(f"{_field_label(spec, key)} {problem}")
    secret = str(config["client_secret"])
    form = urlencode({
        "grant_type": "client_credentials", "client_id": str(config["client_id"]).strip(),
        "client_secret": secret, "scope": azure_wire.ARM_SCOPE,
    }).encode("ascii")
    outcome = _request(
        "POST", azure_wire.token_url(str(config["tenant_id"])), dest=dest, deadline=deadline,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, content=form,
    )
    if isinstance(outcome, TestResult):
        return outcome
    parsed = _json_body(outcome.body)
    if not 200 <= outcome.status_code < 300:
        refusal = azure_wire.token_refusal(parsed, secret)
        if refusal is not None and outcome.status_code < 500:
            return _failure(f"{spec.label} refused the credentials: {refusal}", detail=f"HTTP {outcome.status_code}")
        return _status_failure(outcome, spec.label) or _unexpected_body(spec, dest)
    access_token = _json_field(parsed, "access_token") if isinstance(parsed, dict) else None
    if not isinstance(access_token, str) or is_blank(access_token) or not _is_transmittable(access_token):
        return _unexpected_body(spec, dest)
    subscription = str(config["subscription_id"]).strip()
    read = _request(
        "GET", f"{azure_wire.ARM_BASE}/subscriptions/{subscription}?api-version=2022-12-01", dest=dest,
        deadline=deadline, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        allowed_origin=_AZURE_ARM_ORIGIN,
    )
    if isinstance(read, TestResult):
        return _failure(
            f"{spec.label} accepted the credentials but the subscription could not be read"
            " - check this server's network access.", detail=read.detail,
        )
    if read.status_code in (401, 403, 404):
        code, _message = azure_wire.arm_refusal(_json_body(read.body))
        return _failure(
            f"{spec.label} accepted the credentials, but the app cannot read subscription {subscription}"
            f"{f' ({code})' if code else ''} - grant it Contributor on that subscription.",
            detail=f"HTTP {read.status_code}",
        )
    failure = _status_failure(read, spec.label)
    if failure is not None:
        return failure
    body = _json_body(read.body)
    if not isinstance(body, dict) or _json_field(body, "subscriptionId") is None:
        return _unexpected_body(spec, dest)
    name = _identity_text(_json_field(body, "displayName"))
    if name is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} subscription {name}.")


# ── AWS ───────────────────────────────────────────────────────────────────────
# The one non-bearer type: the key pair signs the request (`sigv4.py`) and the secret never travels.
# Host, path, body, region and service are fixed, which makes signing safe (the signer signs for any
# host); `aws` has no url field, so `_destination` returns `default_base_url`.

_AWS_REGION = "us-east-1"
_AWS_SERVICE = "sts"
_AWS_BODY = b"Action=GetCallerIdentity&Version=2011-06-15"
_AWS_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"

# Bounded regexes, not an XML parser: `xml.etree` on network input is a billion-laughs surface.
_AWS_ACCOUNT_PATTERN = re.compile(rb"<Account>(\d{1,20})</Account>")
_AWS_ARN_PATTERN = re.compile(rb"<Arn>([^<]{1,2048})</Arn>")

# And the container: leaf tags alone would green a fabricated identity.
_AWS_RESULT_PATTERN = re.compile(rb"<GetCallerIdentityResult>")

# The one error name worth reading: a wrong clock would otherwise send the operator to rotate working
# keys. Two names, not keyed on status: `RequestTimeTooSkewed` (S3, 403) and STS's `RequestExpired`
# (400). Scoped to `<Code>`.
_AWS_CLOCK_PATTERN = re.compile(
    rb"<Code>\s{0,16}(?:RequestTimeTooSkewed|RequestExpired)\s{0,16}</Code>"
)


def test_aws(config: dict[str, Any], deadline: float) -> TestResult:
    """`POST https://sts.amazonaws.com/` with `Action=GetCallerIdentity`, signed with SigV4.

    Names the account and ARN, and is rejected (403) for a wrong key id or secret. The signer's header
    mapping is passed through verbatim and `_AWS_BODY` unchanged: the signature covers them, and any
    difference would be a 403 misreported as bad credentials.
    """
    prepared = _prepare("aws", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    url = f"{dest.base}/"
    # Temporary credentials only; blank is a long-lived key. Checked here so a refusal names the field
    # without reading the signer's message.
    session_token = str(config.get("session_token") or "").strip() or None
    if session_token is not None and (not _is_transmittable(session_token) or " " in session_token):
        return untransmittable_field_result(_field_label(spec, "session_token"))
    try:
        headers = sigv4.signed_headers(
            method="POST",
            url=url,
            region=_AWS_REGION,
            service=_AWS_SERVICE,
            access_key_id=str(config["access_key_id"]).strip(),
            secret_access_key=str(config["secret_access_key"]),
            session_token=session_token,
            payload=_AWS_BODY,
            content_type=_AWS_CONTENT_TYPE,
            # Read here, never in the signer, so its known-answer test stays byte-exact.
            amz_date=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        )
    except ValueError as exc:
        # The only reachable cause is the access key id (all else is constant or checked above, and
        # `_field_problem` proved the operator values printable ASCII). An AKID with `,` or `/` could
        # restructure the `Authorization` header, a proof a future edit could break, so only the exception
        # type is logged.
        _log.error("aws test: the signer refused an argument (%s)", type(exc).__name__)
        return untransmittable_field_result(_field_label(spec, "access_key_id"))

    outcome = _request(
        "POST", url, dest=dest, deadline=deadline, headers=headers, content=_AWS_BODY
    )
    if isinstance(outcome, TestResult):
        return outcome

    # A body that filled the cap is unreadable, decided once before either reader: no branch may key on
    # a tag that survived the cut (a padded skew code would misdirect a bad-key operator to the clock).
    unreadable = len(outcome.body) >= _MAX_RESPONSE_BYTES

    if (
        not 200 <= outcome.status_code < 300
        and not unreadable
        and _AWS_CLOCK_PATTERN.search(outcome.body) is not None
    ):
        return _failure(
            "AWS rejected the request as time-skewed - this server's clock is wrong.",
            detail=f"HTTP {outcome.status_code}",
        )
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure

    # The shape gate spelled for XML (`_gated_body` is JSON-only): the same decision in the same order,
    # requiring `<GetCallerIdentityResult>` and both leaf tags.
    if unreadable:
        return _unexpected_body(spec, dest)
    account_match = _AWS_ACCOUNT_PATTERN.search(outcome.body)
    arn_match = _AWS_ARN_PATTERN.search(outcome.body)
    if (
        _AWS_RESULT_PATTERN.search(outcome.body) is None
        or account_match is None
        or arn_match is None
    ):
        return _unexpected_body(spec, dest)

    arn = _identity_text(arn_match.group(1).decode("utf-8", "replace"))
    account = _identity_text(account_match.group(1).decode("ascii"))
    if arn is None or account is None:
        # All-or-nothing: "Connected to AWS as  (account 1234)." is worse than saying the keys worked.
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} as {arn} (account {account}).")


# ── GCP ───────────────────────────────────────────────────────────────────────

_GCP_COMPUTE = _Destination(base="https://compute.googleapis.com", host="compute.googleapis.com",
                            operator_supplied=False)
# A project id (or a domain-scoped `example.com:project`): the one key-supplied value put into a URL.
_GCP_PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9.:-]{0,62}")


def _google_says(outcome: _HttpOutcome) -> Optional[str]:
    """Google's refusal sentence: the token endpoint's `error_description`/`error`, an API's `error.message`."""
    parsed = _json_body(outcome.body)
    return _identity_text(
        _json_field(parsed, "error_description"), _json_field(parsed, "error", "message"), _json_field(parsed, "error"),
    )


def _google_refusal(outcome: _HttpOutcome, label: str) -> Optional[TestResult]:
    if 200 <= outcome.status_code < 300:
        return None
    said = _google_says(outcome)
    if said is None:
        return _status_failure(outcome, label)
    return _failure(f"Google refused: {said}", detail=f"HTTP {outcome.status_code}")


def test_gcp(config: dict[str, Any], deadline: float) -> TestResult:
    """The key exchanged for a token at `oauth2.googleapis.com/token` (a signed JWT bearer grant, the key itself
    sent nowhere), then `GET compute/v1/projects/{project}` with it — the project the key's nodes would launch in.

    A refused key or project reads Google's own sentence ("Invalid JWT Signature.", "Required
    'compute.projects.get' permission …"). The pasted key is re-serialised compactly before `_prepare`: its line
    breaks are formatting, and it travels in no header for `_field_problem`'s transmittability rule to guard."""
    if not edition_features.enabled("cloud_nodes"):
        return _not_in_ce()
    raw = config.get(gcp_auth.KEY_FIELD)
    key: Optional[gcp_auth.ServiceAccountKey] = None
    if isinstance(raw, str) and not is_blank(raw):
        try:
            key = gcp_auth.parse_key(raw)
        except ValueError as exc:
            return _failure(str(exc))  # fixed sentences naming a field, never a value
        config = {**config, gcp_auth.KEY_FIELD: json.dumps(json.loads(raw))}
    prepared = _prepare("gcp", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared
    if key is None:  # a non-text value `_prepare` let through (a blank one it refused)
        return _failure(f"{_field_label(spec, gcp_auth.KEY_FIELD)} must be the JSON key file's text.")
    project = str(config.get(gcp_auth.PROJECT_FIELD) or "").strip() or key.project_id
    if not project:
        return _failure(f"{_field_label(spec, gcp_auth.PROJECT_FIELD)} is required: the key names no project.")
    if not _GCP_PROJECT_PATTERN.fullmatch(project):
        return _failure(f"{_field_label(spec, gcp_auth.PROJECT_FIELD)} is not a GCP project id.")
    try:
        signed = gcp_auth.assertion(key, [gcp_wire.SCOPE])
    except ValueError as exc:
        return _failure(str(exc))
    outcome = _request(
        "POST", gcp_auth.TOKEN_URL, dest=dest, deadline=deadline,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        content=urlencode(gcp_auth.grant_body(signed)).encode(),
    )
    if isinstance(outcome, TestResult):
        return outcome
    refused = _google_refusal(outcome, spec.label)
    if refused is not None:
        return refused
    access = _json_field(_json_body(outcome.body), "access_token")
    if not isinstance(access, str) or not access or not _is_transmittable(access):
        return _unexpected_body(spec, dest)
    outcome = _request(
        "GET", f"{_GCP_COMPUTE.base}/compute/v1/projects/{project}", dest=_GCP_COMPUTE, deadline=deadline,
        headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
    )
    if isinstance(outcome, TestResult):
        return outcome
    refused = _google_refusal(outcome, spec.label)
    if refused is not None:
        return refused
    gated = _gated_body(spec, _GCP_COMPUTE, outcome)
    if isinstance(gated, TestResult):
        return gated
    named = _identity_text(_json_field(gated, "name"))
    who = _identity_text(key.client_email)
    if named is None or who is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} as {who} (project {named}).")


def _field_label(spec: CredentialTypeSpec, key: str) -> str:
    """The operator-facing label for one field, read from the registry.

    Defaults to the key rather than raising `StopIteration`, so a registry rename cannot turn a test
    result into a 500.
    """
    return next((field.label for field in spec.fields if field.key == key), key)


# ── Headscale ─────────────────────────────────────────────────────────────────

def test_headscale(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET {api_url}/api/v1/apikey` with a Bearer API key (headscale REST v1).

    Green names the calling key's own expiry, found by prefix: headscale lists every key as its public
    prefix plus `***` (`hscontrol/types/api_key.go::Proto`), and a silently lapsed key stops every node
    join. `login_server` is only format-checked (https, by `_field_problem`); nodes dial it, the CP does not.
    """
    prepared = _prepare("headscale", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    key = str(config["api_key"])
    outcome = _api_key_request(
        spec, dest, deadline,
        path="/api/v1/apikey",
        key=key,
        value_prefix="Bearer",
        rejected_message=(
            f"{spec.label} rejected the API key - check it has not expired or been revoked "
            "(`headscale apikeys list`)."
        ),
    )
    if isinstance(outcome, TestResult):
        return outcome
    parsed = _gated_body(spec, dest, outcome)
    if isinstance(parsed, TestResult):
        return parsed
    listed = _json_field(parsed, "apiKeys")
    if not isinstance(listed, list):
        return _unexpected_body(spec, dest)
    expiry = _headscale_key_expiry(listed, key)
    if expiry is None:
        return _success(
            f"Connected to {spec.label} - API key accepted, but its expiry could not be found."
        )
    return _success(f"Connected to {spec.label} - this API key expires {expiry}.")


def _headscale_key_expiry(listed: list[Any], key: str) -> Optional[str]:
    """The listed `expiration` of the key whose masked prefix `key` starts with, sanitised, or None."""
    for entry in listed:
        prefix = _json_field(entry, "prefix")
        if not isinstance(prefix, str):
            continue
        visible = prefix.removesuffix("***").removesuffix("-")
        if visible and key.startswith(visible):
            return _identity_text(_json_field(entry, "expiration"))
    return None


# ── OAuth2: the refresh exchange, which is the strongest check in this module ──
# The one tester family whose green proves client, secret and refresh token are live and consistent.
# It spends nothing only because its callers are pinned providers whose refresh tokens do not rotate;
# the minted access token is discarded (caching needs a DB write). At an unknown vendor the exchange is
# destructive (a rotating provider retires the token), which is why `oauth2_client` has no tester
# (`registry.py`'s `untestable_reason`). A second caller must establish its rotation behaviour first.

# A row without a client cannot be checked (only the minting client can refresh), and a red would
# misreport a possibly working credential, so it is `unsupported`, a neutral banner in
# `CredentialEditPage.tsx`.
_NO_CLIENT_MESSAGE = (
    "No client ID and secret on this credential, so there is nothing to exchange the refresh token "
    "with - only the client that minted a token can refresh it."
)
_NOT_CONNECTED_MESSAGE = "No refresh token yet - press Connect, or paste one you already hold."

# `invalid_grant` (RFC 6749 §5.2, imported from `oauth2_wire`) is the one refusal a retry cannot fix,
# and gets its own sentence because the fix is to reauthorize.


def _refresh_exchange(slug: str, config: dict[str, Any], deadline: float) -> TestResult:
    """`POST <token endpoint>` with a `refresh_token` grant: the whole of both testers below.

    The endpoint comes from the pinned provider and nowhere else; a type whose provider pins none has
    no tester (see the assertion below). No `scope` is sent: on a refresh it can only narrow (RFC 6749
    §6), and the check should verify everything the grant carries.
    """
    prepared = _prepare(slug, config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    refresh_token = config.get("refresh_token")
    # The client is reported first: "press Connect" would 400 on a row with no client
    # (`start_credential_connect`).
    if is_blank(client_id) or is_blank(client_secret):
        return TestResult(ok=False, status="unsupported", message=_NO_CLIENT_MESSAGE)
    if is_blank(refresh_token):
        return TestResult(ok=False, status="unsupported", message=_NOT_CONNECTED_MESSAGE)

    # The provider's endpoint, with no fallback: a refresh against an unidentified endpoint is the
    # destructive case. Unreachable (`tests/test_oauth2_connect_types.py` pins a `token_url` for every
    # testable connect type), so a `RuntimeError` is a registry bug. `_request` still pins the origin.
    provider = connect_for_type(slug)
    if provider is None or not provider.token_url:
        raise RuntimeError(
            f"{slug}: a refresh-grant test needs a provider with a pinned token_url; refusing to "
            "spend a refresh token against an endpoint read from the credential row"
        )
    endpoint = provider.token_url
    form = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token),
            "client_id": str(client_id),
            "client_secret": str(client_secret),
        }
    ).encode("ascii")

    outcome = _request(
        "POST",
        endpoint,
        dest=dest,
        deadline=deadline,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        content=form,
    )
    if isinstance(outcome, TestResult):
        return outcome

    # Body before status: RFC 6749 §5.2 puts the reason in a 400 body, which separates "reauthorize"
    # from "fix the secret".
    parsed = _json_body(outcome.body)
    if isinstance(parsed, dict):
        error = _json_field(parsed, "error")
        if isinstance(error, str) and error:
            if error == oauth2_wire.INVALID_GRANT:
                return _failure(
                    "The vendor no longer honours this refresh token - it was revoked, expired or "
                    "already replaced. Press Connect to reauthorize.",
                    detail=error,
                )
            return _failure(
                f"{spec.label} refused the refresh - check the client ID and secret.", detail=error
            )
        if isinstance(_json_field(parsed, "access_token"), str) and _json_field(
            parsed, "access_token"
        ):
            # Nothing from the grant is echoed: the tokens are the only identifying strings.
            return _success(
                f"Connected to {spec.label} - the refresh token was exchanged for an access token."
            )

    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure
    # A 2xx with neither `access_token` nor `error` is not the token endpoint (the shape gate).
    return _unexpected_body(spec, dest)


def test_google(config: dict[str, Any], deadline: float) -> TestResult:
    """A refresh grant against `https://oauth2.googleapis.com/token`.

    A row with the operator's own OAuth client is exchanged for real; a CLI-minted row (no client; see
    `registry.py`'s `client_id` field) gets the neutral `_NO_CLIENT_MESSAGE`.
    """
    return _refresh_exchange("google", config, deadline)


# ── Generic API key ───────────────────────────────────────────────────────────

# RFC 7230's token alphabet for a header name. Checked because h11 would refuse a bad name as a
# transport failure, sending the operator to the base URL.
_HEADER_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~")

# Headers this module sets itself: `Accept-Encoding: identity` bounds decompression (`_client`) and
# `Host` decides which vhost a pinned origin reaches; an operator overwrite would silently lose them.
_RESERVED_HEADER_NAMES = frozenset(
    {"host", "accept-encoding", "content-length", "connection", "transfer-encoding", "expect"}
)


def _value_prefix_clause(prefix: str) -> Optional[str]:
    """What is wrong with `prefix` as a scheme word, as a clause that follows a field name, or None
    (a clause for `_header_name_clause`'s reason).
    """
    if " " in prefix or "\t" in prefix:
        return "must be a single word - it is the scheme in front of the key, not the key."
    return None


def _header_name_clause(name: str) -> Optional[str]:
    """What is wrong with `name` as an HTTP header name, as a clause that follows a field name, or None.

    A clause because the tester prefixes the field label while the write path prefixes the config key,
    which `CredentialEditPage.tsx::inlineServerError` uses to place the error beside its field.

    Echoing the name in the reserved case is safe: it is reached only after the alphabet check, for one
    of the six `_RESERVED_HEADER_NAMES` literals.
    """
    if not set(name) <= _HEADER_NAME_CHARS:
        return "is not a valid HTTP header name."
    if name.lower() in _RESERVED_HEADER_NAMES:
        return f"cannot be {name} - this server sets that header itself."
    return None


def _value_prefix_problem(spec: CredentialTypeSpec, prefix: str) -> Optional[TestResult]:
    """A failure naming the Value prefix field, or None when `prefix` is one scheme word.

    A secret-classification guard, not a wire one: `Bearer sk-...` pasted whole would be stored as
    `text`, served to every reader and absent from `secrets_set`. A tab is checked too, since
    `_is_transmittable` admits one.
    """
    clause = _value_prefix_clause(prefix)
    return None if clause is None else _failure(f"{_field_label(spec, 'value_prefix')} {clause}")


def _header_name_problem(spec: CredentialTypeSpec, name: str) -> Optional[TestResult]:
    """A failure naming the Header name field (by its registry label), or None when `name` is one a
    request can carry.
    """
    clause = _header_name_clause(name)
    return None if clause is None else _failure(f"{_field_label(spec, 'header_name')} {clause}")


def _stripe_publishable_clause(value: str) -> Optional[str]:
    """A publishable key is the one value the browser receives, so a secret pasted here would be served in
    plaintext: anything that is not a `pk_` key is refused before it is stored."""
    return None if value.strip().startswith(("pk_live_", "pk_test_")) else "must be a Stripe publishable key (pk_...)"


# Keyed by type and field, so `write_time_problem` and the tester read the same check.
_WRITE_TIME_CHECKS = {
    ("stripe", "publishable_key"): _stripe_publishable_clause,
    ("generic_api_key", "header_name"): _header_name_clause,
    ("generic_api_key", "value_prefix"): _value_prefix_clause,
    ("bitbucket", "kind"): _kind_clause(BITBUCKET_KINDS),
    ("azure_devops", "kind"): _kind_clause(AZURE_DEVOPS_KINDS),
    ("azure_devops", "test_org"): _azure_org_clause,
}
if edition_features.enabled("cloud_nodes"):
    _WRITE_TIME_CHECKS.update({
        ("azure", "tenant_id"): azure_wire.tenant_clause,
        ("azure", "client_id"): azure_wire.guid_clause,
        ("azure", "subscription_id"): azure_wire.guid_clause,
    })


def write_time_problem(spec: CredentialTypeSpec, field: FieldSpec, value: str) -> Optional[str]:
    """The sentence for a value that must not be stored at all, or None: the write-time half of the
    rules whose test-time half is `_header_name_problem` and `_value_prefix_problem`.

    Double-homed like `require_https_base_url`, so a row is never savable in a state it can never be
    tested in, and because a runner fetches the config verbatim (`credential_get`) and builds its own
    request. Called by `routes/credentials/credential_wire.py::_validated_value`; the sentence opens with the config key.

    Checks the raw value (after `_is_transmittable`), so this half is no weaker than the test-time half:
    a stripping check would save `"Bearer "` forever untestable. Only an empty value is skipped. For
    non-`url` fields only (a `url` field is stripped before storing).
    """
    check = _WRITE_TIME_CHECKS.get((spec.slug, field.key))
    if check is None or not value:
        return None
    if not _is_transmittable(value):
        return f"{field.key} {_UNTRANSMITTABLE_CLAUSE}"
    clause = check(value)
    return None if clause is None else f"{field.key} {clause}"


def test_generic_api_key(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET {base_url}` carrying the key in the operator's own header, assuming nothing about the answer.

    The one tester with no provider behind it, so:
      - no path is appended (a guessed `/me` would 404 working credentials);
      - no shape gate, a stated limit: a captive portal's HTML 200 passes, so the success sentence says
        the host answered, not that the key is valid. That wording is the control; keep it;
      - no identity echoed; the only value shown is the host, userinfo already stripped;
      - a sharper reachability oracle than its siblings, accepted: no body is echoed, and it sits inside
        the route's documented egress posture behind a superadmin-granted `credentials:edit`;
      - 401/403 names all three fields, since the header name or scheme word is the likelier fault.

    A blank header name means `Authorization`; a blank prefix sends the key alone (the registry's rule).
    The Base URL is optional with no default (the registry's rule), so a blank one has nowhere to send it.
    """
    if is_blank(config.get("base_url")):
        return _failure("Base URL is required before testing.")
    prepared = _prepare("generic_api_key", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared

    header_name = str(config.get("header_name") or "").strip() or _DEFAULT_API_KEY_HEADER
    problem = _header_name_problem(spec, header_name)
    if problem is not None:
        return problem
    value_prefix = str(config.get("value_prefix") or "").strip()
    problem = _value_prefix_problem(spec, value_prefix)
    if problem is not None:
        return problem

    outcome = _api_key_request(
        spec, dest, deadline,
        # The base URL itself; `_split_base_url` already dropped the query and trailing slash.
        path="",
        key=str(config["api_key"]),
        header_name=header_name,
        value_prefix=value_prefix,
        # No `Accept`: there is no container to ask for.
        accept=None,
        rejected_message=(
            f"{dest.host} rejected the request - check the {_field_label(spec, 'api_key')}, the "
            f"{_field_label(spec, 'header_name')} and the {_field_label(spec, 'value_prefix')}."
        ),
    )
    if isinstance(outcome, TestResult):
        return outcome
    # No `detail` on a green (`CredentialEditPage.tsx` renders it only on failure).
    return _success(
        f"{dest.host} answered HTTP {outcome.status_code} to a request carrying the "
        f"{_field_label(spec, 'api_key')}. A generic endpoint has no documented success shape, so "
        "this is not proof the key is valid."
    )


# ── Stripe ────────────────────────────────────────────────────────────────────


def test_stripe(config: dict[str, Any], deadline: float) -> TestResult:
    """The Stripe keys' connection test (`app/billing/stripe_tester.py`); the community edition has no billing."""
    if not edition_features.enabled("billing"):
        return _not_in_ce()
    from app.billing import stripe_tester

    return stripe_tester.connection_test(config, deadline)


# ── OIDC ──────────────────────────────────────────────────────────────────────

_OIDC_DISCOVERY_PATH = "/.well-known/openid-configuration"


def test_oidc_client(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET {issuer}/.well-known/openid-configuration`, green only when the document's `issuer` is exactly the
    operator's (OIDC Discovery 1.0 §4.3: a sign-in refuses any other spelling, a trailing slash included).

    Proves the issuer, not the client: an unauthenticated call (property 1's limit), since the only check of the
    client pair is a real sign-in. Sends no secret. Each failure names the Issuer field, the one thing to fix.
    """
    prepared = _prepare("oidc_client", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared
    issuer_label = _field_label(spec, "issuer")

    outcome = _request(
        "GET", f"{dest.base}{_OIDC_DISCOVERY_PATH}", dest=dest, deadline=deadline,
        headers={"Accept": "application/json"},
    )
    if isinstance(outcome, TestResult):
        if outcome.detail == "transport_error":
            return _failure(f"Could not reach {dest.host} - check the {issuer_label}.", detail="transport_error")
        return outcome
    # Not `_status_failure`: the call carries no credential, so a 401/403 is no "Authentication rejected".
    if not 200 <= outcome.status_code < 300:
        return _failure(
            f"No OpenID configuration at {dest.host} - check the {issuer_label}.",
            detail=f"HTTP {outcome.status_code}",
        )
    found = _json_field(_json_body(outcome.body), "issuer")
    if not isinstance(found, str) or _identity_text(found) is None:
        return _failure(
            f"{dest.host} answered, but not with an OpenID configuration - check the {issuer_label}.",
            detail="unexpected_body",
        )
    if found != str(config["issuer"]).strip():
        # No `detail`: the console prints it beside the sentence, and this one already says everything.
        if not _same_origin(request_origin(found), request_origin(dest.base)):
            return _failure(f"{dest.host} names an issuer on another host - check the {issuer_label}.")
        shown = _identity_text(found)
        if shown != found:
            # Sanitising changed it, so quoting it as "exactly that" would send the operator round in circles.
            return _failure(f"{issuer_label} does not match the provider's - copy it from your provider's settings.")
        return _failure(f"The provider names its issuer {shown} - set {issuer_label} to exactly that.")
    return _success(f"{issuer_label} verified at {dest.host} - the client ID and secret are checked at sign-in.")


# ── Telegram ──────────────────────────────────────────────────────────────────

def test_telegram_bot(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET https://api.telegram.org/bot<token>/getMe`: bespoke because the token rides the URL PATH, which no
    `KeyTestSpec` can place. Telegram answers 401 for a bad token and `{"ok": true, "result": {...}}` for a good
    one; `ok is True` is required. No message or log line carries the URL, which carries the token. Success
    names the bot's @username."""
    prepared = _prepare("telegram_bot", config)
    if isinstance(prepared, TestResult):
        return prepared
    spec, dest = prepared
    token_label = _field_label(spec, "bot_token")
    outcome = _request(
        "GET",
        f"{dest.base}/bot{str(config['bot_token']).strip()}/getMe",
        dest=dest,
        deadline=deadline,
        headers={"Accept": "application/json"},
    )
    if isinstance(outcome, TestResult):
        return outcome
    # 404 is Telegram's answer to a token that is not even token-shaped.
    parsed = _ok_envelope(spec, dest, outcome, token_label=token_label, rejected=(401, 404))
    if isinstance(parsed, TestResult):
        return parsed
    username = _identity_text(_json_field(parsed, "result", "username"))
    if username is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} @{username}.")


# The hand-written testers: every type whose Test is more than the one GET a `KeyTestSpec` declares.
_BESPOKE_TESTERS: dict[str, Tester] = {
    "jira": test_jira,
    "xero": test_xero,
    "slack": test_slack,
    "telegram_bot": test_telegram_bot,
    "github": test_github,
    "github_app": test_github_app,
    "github_install_app": test_github_install_app,
    "bitbucket": test_bitbucket,
    "azure_devops": test_azure_devops,
    "aws": test_aws,
    "azure": test_azure,
    "gcp": test_gcp,
    "headscale": test_headscale,
    "stripe": test_stripe,
    "google": test_google,
    "generic_api_key": test_generic_api_key,
    "oidc_client": test_oidc_client,
}


def _check_tester_registry(
    types: Mapping[str, CredentialTypeSpec], bespoke: Mapping[str, Tester]
) -> None:
    """The structural guards over how each type is tested, run at import over the real registry (and by tests
    over a synthetic one). `RuntimeError`, not `assert` (`python -O`). Each message names the slug.

    `testable` (registry) and tester registration are one fact in two files that cannot derive from each
    other: a testable type has exactly one of a bespoke tester or a `key_test`. `testable=False` iff
    `untestable_reason`, both directions (a stale reason on a testable type would contradict a working Test
    button). A `key_test` must be sendable as declared, since nothing else reads it before a request.
    """
    both = sorted(slug for slug, spec in types.items() if spec.key_test is not None and slug in bespoke)
    if both:
        raise RuntimeError(f"key_test and a bespoke tester disagree - both declared for: {both}")
    tested = set(bespoke) | {slug for slug, spec in types.items() if spec.key_test is not None}
    testable = {slug for slug, spec in types.items() if spec.testable}
    if testable != tested:
        raise RuntimeError(
            f"CREDENTIAL_TYPES[*].testable and TESTERS disagree: {sorted(testable ^ tested)}"
        )
    if any((not spec.testable) != bool(spec.untestable_reason) for spec in types.values()):
        raise RuntimeError(
            "CREDENTIAL_TYPES[*].testable and untestable_reason disagree: "
            "untestable with no reason="
            f"{sorted(s for s, t in types.items() if not t.testable and not t.untestable_reason)}"
            ", testable carrying a reason="
            f"{sorted(s for s, t in types.items() if t.testable and t.untestable_reason)}"
        )
    for slug, spec in types.items():
        key_test = spec.key_test
        if key_test is None:
            continue
        fields = {field.key: field for field in spec.fields}
        key_field = fields.get(key_test.key_field)
        problems = []
        if not key_test.path.startswith("/") or any(char.isspace() for char in key_test.path):
            problems.append(f"path {key_test.path!r} must start with / and hold no whitespace")
        if key_field is None or key_field.kind != "secret":
            problems.append(f"key_field {key_test.key_field!r} is not a declared secret field")
        unknown = [field for _header, field in key_test.field_headers if field not in fields]
        if unknown:
            problems.append(f"field_headers name undeclared fields {unknown}")
        headers = [key_test.header_name, *(name for name, _ in key_test.extra_headers),
                   *(name for name, _ in key_test.field_headers)]
        problems += [f"header {name!r} {clause}" for name in headers if (clause := _header_name_clause(name))]
        lowered = [name.lower() for name in headers]
        if len(set(lowered)) != len(lowered):
            problems.append("header names repeat (case-insensitive)")
        if _DEFAULT_API_KEY_HEADER.lower() in lowered[1:]:
            # Only `header_name` rides `_HeaderAuth`; in the header map a URL's userinfo would displace it.
            problems.append(f"only header_name may be {_DEFAULT_API_KEY_HEADER}")
        if (clause := _value_prefix_clause(key_test.value_prefix)) is not None:
            problems.append(f"value_prefix {clause}")
        if key_field is not None and (key_test.blank_key_reason is not None) == key_field.required:
            problems.append("blank_key_reason must be set exactly when key_field is optional")
        if key_test.list_field is not None and key_test.identity:
            problems.append("list_field and identity are exclusive")
        if problems:
            raise RuntimeError(f"{slug}: key_test and its registry entry disagree - {'; '.join(problems)}")


_check_tester_registry(CREDENTIAL_TYPES, _BESPOKE_TESTERS)

#: Every tester by slug: the bespoke functions plus one `_declared_tester` per `key_test`.
TESTERS: dict[str, Tester] = {
    **_BESPOKE_TESTERS,
    **{slug: _declared_tester(slug) for slug, spec in CREDENTIAL_TYPES.items() if spec.key_test is not None},
}

# Second and third guards, one loop over the `url`-field classification. Second: more than one url
# field requires `base_url_field`, naming one of them (checked first; the third trusts it). Third:
# a required url field has no `default_base_url`, a type with no url field has one, and when set it must
# parse to an origin (checked here: the registry cannot import `request_origin`). An OPTIONAL url field may
# go without a default (`generic_api_key`): its tester refuses a blank one. Whole registry, untestable too.
for _slug in CREDENTIAL_TYPES:
    _spec = CREDENTIAL_TYPES[_slug]
    _url_fields = [field for field in _spec.fields if field.kind == "url"]
    if len(_url_fields) > 1 and _spec.base_url_field is None:
        raise RuntimeError(
            f"{_slug}: declares {len(_url_fields)} kind==\"url\" fields "
            f"({[field.key for field in _url_fields]}) and names none in base_url_field — only the "
            "first would ever be pinned"
        )
    if _spec.base_url_field is not None and _spec.base_url_field not in {
        field.key for field in _url_fields
    }:
        raise RuntimeError(
            f"{_slug}: base_url_field={_spec.base_url_field!r} is not one of its kind==\"url\" "
            f"fields ({[field.key for field in _url_fields]})"
        )
    _host_field = _operator_host_field(_spec)
    _url_required = _host_field is not None and _host_field.required
    if (_url_required and _spec.default_base_url is not None) or (
        _host_field is None and _spec.default_base_url is None
    ):
        raise RuntimeError(
            f"{_slug}: default_base_url must be set unless the type declares a url field, and never "
            f"with a REQUIRED one (default_base_url={_spec.default_base_url!r}, "
            f"required url field={_url_required})"
        )
    if _spec.default_base_url is not None and request_origin(_spec.default_base_url) is None:
        raise RuntimeError(
            f"{_slug}: default_base_url {_spec.default_base_url!r} has no comparable origin — the "
            "stored-secret host pin would refuse every fill for this type"
        )
    # Fifth: `require_https_base_url` forbids a plaintext default. The scheme is read through
    # `request_origin` so a good `HTTPS://` literal is not rejected.
    if _spec.require_https_base_url and _spec.default_base_url is not None:
        if request_origin(_spec.default_base_url)[0] != "https":
            raise RuntimeError(
                f"{_slug}: require_https_base_url=True but default_base_url "
                f"{_spec.default_base_url!r} is not https://"
            )


def _check_save_rules(types: Mapping[str, CredentialTypeSpec]) -> None:
    """G7: the save-time rules (`require_one_of`, `allowed_hosts`) must be satisfiable as declared.

    Every `require_one_of` group is non-empty and names only declared, OPTIONAL keys (a required key would
    make the group redundant with the required check). An `allowed_hosts` type has a host field, and that
    field's pre-fill `default` and the type's `default_base_url` (its fallback destination) are allowed.
    `RuntimeError`, not `assert` (`python -O`); takes `types` so a test can pass a synthetic registry.
    """
    for slug, spec in types.items():
        declared = {field.key: field for field in spec.fields}
        for group in spec.require_one_of:
            if not group:
                raise RuntimeError(f"{slug}: require_one_of has an empty group, which every row satisfies")
            for key in group:
                if key not in declared:
                    raise RuntimeError(f"{slug}: require_one_of names {key!r}, which the type does not declare")
                if declared[key].required:
                    raise RuntimeError(f"{slug}: require_one_of names {key!r}, which is already required")
        if not spec.allowed_hosts:
            continue
        if any(not host or host != host.lower() for host in spec.allowed_hosts):
            # `_allowed_host_clause` compares a lowercased netloc, so a mixed-case entry could never match.
            raise RuntimeError(f"{slug}: allowed_hosts must be non-empty lowercase hostnames")
        field = host_field(spec)
        if field is None:
            raise RuntimeError(f"{slug}: allowed_hosts is set but the type has no host (url) field")
        for name, default in (("default", field.default), ("default_base_url", spec.default_base_url)):
            if default is not None and _allowed_host_clause(spec, default) is not None:
                raise RuntimeError(f"{slug}: {name} {default!r} is not one of its allowed_hosts")


_check_save_rules(CREDENTIAL_TYPES)

# Fourth guard, `test_aws`'s alone: the URL it signs must be one `sigv4` accepts, since it blames a
# signing `ValueError` on the access key id. `request_origin` is weaker than `sigv4._signing_url`, so
# the signer's own parse runs once at import over the exact string.
_aws_signing_url = f"{CREDENTIAL_TYPES['aws'].default_base_url}/"
try:
    sigv4.host_header(_aws_signing_url)
except ValueError as _exc:
    raise RuntimeError(
        f"aws: default_base_url yields {_aws_signing_url!r}, which the SigV4 signer refuses "
        f"({_exc}) — every AWS test would report this as an access key id problem"
    ) from _exc
