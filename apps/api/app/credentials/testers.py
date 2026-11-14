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

import functools
import importlib
import json
import logging
import re
import string
import threading
import unicodedata
from queue import Empty, Queue
from time import monotonic
from typing import Any, Callable, Iterator, Literal, Mapping, NamedTuple, Optional, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from app.clock import utcnow
from app.credentials import sigv4
from app import account_auth, edition_features, github_api, http_bounded, mcp_client, oauth2_wire
from app.credentials.oauth_wizards import connect_for_type
from app.integrations import catalog
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


class _Verdict(Exception):
    """A step helper's early verdict (`_prepare`, `_request`, `_gated_body`, …), raised so nothing after the
    step runs and the helper's return type is the plain value. `_verdict_guard` turns it back into the
    `TestResult` the `Tester` contract promises; it never leaves this module."""

    def __init__(self, result: TestResult) -> None:
        super().__init__(result.message)
        self.result = result


def _verdict_guard(tester: Tester) -> Tester:
    """The one place a `_Verdict` becomes a return value: on every function `TESTERS` serves."""

    @functools.wraps(tester)
    def guarded(config: dict[str, Any], deadline: float) -> TestResult:
        try:
            return tester(config, deadline)
        except _Verdict as verdict:
            return verdict.result

    return guarded


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
    catalog_spec = catalog.spec_for_type(spec.slug)
    if catalog_spec is not None and catalog_spec.http_test is not None:
        unsent_if_blank |= {key for _header, key in catalog_spec.http_test.field_headers}
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


def _destination(spec: CredentialTypeSpec, config: dict[str, Any]) -> _Destination:
    """Where a test of `spec` under `config` is going; an unusable base URL is the raised failure.

    `operator_supplied` is True only when the operator filled the host field, so `_host_advice` never
    sends them to a URL they never typed; `host_label` is that field's own label for it to name.
    """
    field = _operator_host_field(spec)
    raw = config.get(field.key) if field is not None else None
    base, host = _split_base_url(_effective_base_url(spec, raw))
    if base is None or host is None:
        # Operator-supplied only (reported first by `_field_problem`); fixed defaults are proved at import.
        raise _Verdict(_failure(_BAD_BASE_URL))
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
) -> dict[str, Any] | list[Any]:
    """The shape gate itself: the decision a shape-gated tester calls rather than reimplements.

    Available, not mandatory (`jira` and the workflow-automation type are deliberately ungated). `container`
    is a parameter because some providers answer a JSON array. The failure is raised, never signalled by the
    value: an empty `{}`/`[]` is a legitimate body.
    """
    parsed = _json_body(outcome.body)
    if not isinstance(parsed, container):
        raise _Verdict(_unexpected_body(spec, dest))
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
    addresses: tuple[str, ...] = (),
) -> _HttpOutcome:
    """One outbound call inside the caller's remaining `deadline`; the mapped failure for it is raised.

    `addresses`, when given, are the only addresses dialled (`http_bounded.pin`): a tenant-chosen host is vetted by
    the caller (`http_bounded.vetted_addresses`) and then never resolved again.

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
        raise _Verdict(_unreachable(dest))
    outcome: Queue = Queue(maxsize=1)  # a fresh queue per call, so no result can reach another caller
    handle = _Abandonment()

    def run() -> None:
        # Nothing may raise past `_exchange`, or the caller waits the whole deadline for an instant error.
        try:
            result = _exchange(method, url, dest, auth, headers, content, deadline, handle, addresses)
        except BaseException:  # noqa: BLE001 - a worker thread has nowhere to raise TO
            result = _unreachable(dest)
        # One put, outside the guard: a failing put inside it could put again and block on the full queue.
        outcome.put(result)

    # Daemon, so a hostile host cannot block interpreter shutdown (a pooled worker is joined at exit).
    threading.Thread(target=run, name="credential-test", daemon=True).start()
    try:
        result = outcome.get(timeout=max(0.0, deadline - monotonic()))
    except Empty:
        handle.abandon()
        raise _Verdict(_timed_out(dest)) from None
    # The worker returns its failure as a value (an exception has nowhere to go on that thread); it is
    # raised here, on the caller's.
    if isinstance(result, TestResult):
        raise _Verdict(result)
    return result


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
    addresses: tuple[str, ...] = (),
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
        return _exchange_locked(method, url, dest, auth, headers, content, deadline, handle, addresses)
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
    addresses: tuple[str, ...] = (),
) -> _HttpOutcome | TestResult:
    """The one place a request is put on the wire: no second transport, and no path around the deadline,
    the permit or the abandonment handle.

    `content` is raw bytes, never `data=`/`json=`: a SigV4 request hashes the exact bytes it sends.
    """
    client = _client()
    if addresses:
        try:
            http_bounded.pin(client, httpx.URL(url).raw_host.decode("ascii"), addresses)
        except (httpx.InvalidURL, ValueError, UnicodeError):
            client.close()
            return _unreachable(dest)
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
) -> tuple[CredentialTypeSpec, _Destination]:
    """The preamble every tester runs: its registry spec, the operator's fields checked, the destination
    resolved — or the failure raised instead of calling anything.

    `_field_problem` first: a blank required field is the better sentence than `_BAD_BASE_URL`.
    """
    spec = CREDENTIAL_TYPES[slug]
    problem = _field_problem(spec, config)
    if problem is not None:
        raise _Verdict(problem)
    return spec, _destination(spec, config)


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
    addresses: tuple[str, ...] = (),
) -> _HttpOutcome:
    """One outbound call whose non-2xx is already the operator's answer: `_request` then
    `_status_failure`. Only a 2xx comes back as an outcome; anything else is raised.

    `rejected_message` replaces the shared 401/403 sentence only, for types where it would misdirect
    (the generic type's header name). Callers needing their own sentence
    for another status (Xero's 400, AWS's clock skew, Telegram's 404) call `_request` directly.
    """
    outcome = _request(
        method, url, dest=dest, deadline=deadline, auth=auth, headers=headers,
        content=content, allowed_origin=allowed_origin, addresses=addresses,
    )
    if rejected_message is not None and outcome.status_code in (401, 403):
        raise _Verdict(_failure(rejected_message, detail=f"HTTP {outcome.status_code}")._replace(http=outcome))
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        raise _Verdict(failure._replace(http=outcome))
    return outcome


def _bearer_headers(token: str) -> dict[str, str]:
    """The header map of a JSON call carrying a token a first call minted. Those calls go to a fixed vendor host,
    so no userinfo can displace `Authorization` (the `_HeaderAuth` rule guards operator-typed hosts)."""
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _access_token(spec: CredentialTypeSpec, dest: _Destination, outcome: _HttpOutcome) -> str:
    """A 2xx token response's `access_token`, or the raised shape failure: the wire rule on the one third-party
    value put into a request (a CRLF or unencodable byte would surface as a transport error). `is_blank` is
    needed too: `_is_transmittable("")` is vacuously true."""
    token = _json_field(_json_body(outcome.body), "access_token")
    if not isinstance(token, str) or is_blank(token) or not _is_transmittable(token):
        raise _Verdict(_unexpected_body(spec, dest))
    return token


def _follow_up(
    url: str, *, dest: _Destination, deadline: float, headers: dict[str, str],
    allowed_origin: Optional[tuple[str, str, int]], could_not: str,
) -> _HttpOutcome:
    """A two-host test's second call, `GET url` with the token the first minted: every way it fails to complete is
    one sentence (`could_not`, naming the vendor rather than a host the operator never typed) and `detail` passes
    through, keeping the timeout/transport distinction. `allowed_origin` is the pin; `dest` only names failures."""
    try:
        return _request("GET", url, dest=dest, deadline=deadline, headers=headers, allowed_origin=allowed_origin)
    except _Verdict as verdict:
        raise _Verdict(_failure(
            f"{could_not} - check this server's network access.", detail=verdict.result.detail,
        )) from None


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
    basic_user: Optional[str] = None,
    addresses: tuple[str, ...] = (),
) -> _HttpOutcome:
    """`GET {dest.base}{path}` carrying `key` in one header: the shape most of these types share.

    Every declarative type (`_declared_tester`, from its registry `KeyTestSpec`) and the generic type
    differ only in header name, scheme word, path and `Accept`, so those are arguments. Xero (BasicAuth + a
    second call), GitHub (host-dependent prefix), AWS (signed) and Slack (two POSTs) stay separate.

    `value_prefix` is a scheme word; the space is supplied here. `basic_user` sends `Authorization: Basic`
    (user:key) instead of the header. The key never reaches a query string; `path` is the caller's literal.
    `Authorization` rides an `httpx.Auth` flow, every other name the header map (see `_HeaderAuth`).
    """
    headers: dict[str, str] = {}
    if accept is not None:
        headers["Accept"] = accept
    if extra_headers:
        headers.update(extra_headers)
    value = f"{value_prefix} {key}" if value_prefix else key
    auth: Optional[httpx.Auth] = None
    if basic_user is not None:
        auth = httpx.BasicAuth(basic_user, key)
    elif header_name.lower() == _DEFAULT_API_KEY_HEADER.lower():
        auth = _HeaderAuth(header_name, value)
    else:
        headers[header_name] = value
    return _checked_request(
        spec, dest, deadline, "GET", f"{dest.base}{path}",
        auth=auth, headers=headers, rejected_message=rejected_message, addresses=addresses,
    )


def _declared_tester(slug: str) -> Tester:
    """The tester for a type whose registry entry declares `key_test`: `_prepare`, then one `_api_key_request`,
    then the verdict the spec names. The single home for every single-GET key check, so no such type has a
    function of its own; `_check_tester_registry` proves each spec sendable at import.
    """
    if CREDENTIAL_TYPES[slug].key_test is None:
        raise ValueError(f"{slug}: declares no key_test")

    def test(config: dict[str, Any], deadline: float) -> TestResult:
        spec, dest = _prepare(slug, config)
        # Read with `spec`, never captured at build time, so the request and the verdict share one spec.
        key_test = spec.key_test
        if key_test is None:
            raise ValueError(f"{slug}: declares no key_test")
        key = config.get(key_test.key_field)
        key_label = _field_label(spec, key_test.key_field)
        bearer = key_test.bearer_when is not None and not is_blank(config.get(key_test.bearer_when))
        rejected_message = key_test.rejected_message or (
            _vendor_rejected_message(spec, dest, key_label) if key_test.reject_names_vendor else None
        )
        connect_token = config.get(key_test.connect_token_field) if key_test.connect_token_field else None
        if is_blank(key) and not is_blank(connect_token):
            # A Connect row: its cached OAuth access token, as Bearer. A lapsed one is renewed by the refresh
            # path before its next use, so the Test spends nothing on it; with no refresh token, as
            # `_catalog_http`, only the person can renew it.
            if _cached_token_expired(config):
                if is_blank(config.get("refresh_token")):
                    return _failure(f"The {spec.label} access token has expired - press Connect to reauthorize.",
                                    detail="expired")
                return _unsupported(_RENEWS_MESSAGE)
            key, bearer = connect_token, True
            key_label = _field_label(spec, str(key_test.connect_token_field))
            # Nobody pasted it, so "check it" is no remedy.
            rejected_message = f"{spec.label} rejected the {key_label} - press Connect to reauthorize."
        elif key_test.blank_key_reason is not None and is_blank(key):
            return _unsupported(key_test.blank_key_reason)
        addresses: tuple[str, ...] = ()
        if key_test.vet_destination and dest.operator_supplied:
            # No DNS oracle: one sentence whether the host is private, reserved or unresolvable.
            refusal, addresses = http_bounded.vetted_addresses(f"{dest.base}{key_test.path}")
            if refusal is not None or not addresses:
                return _failure(f"{spec.label} can't be reached at that address. Use the site's public https:// address.",
                                detail="refused_destination")
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
            rejected_message=rejected_message,
            basic_user=str(config[key_test.basic_user_field]).strip() if key_test.basic_user_field else None,
            addresses=addresses,
        )
        if key_test.identity_fallback_field is not None:
            # Ungated: an unreadable 2xx is still an authenticated call, so the fallback names it. A non-string
            # value is not a name while a fallback is at hand (`_identity_text` would coerce a number).
            found = (_json_field(_json_body(outcome.body), *path) for path in key_test.identity)
            fallback = str(config[key_test.identity_fallback_field]).strip()
            identity = _identity_text(*(value for value in found if isinstance(value, str)), fallback) or fallback
            return _success(f"Connected to {spec.label} {key_test.identity_phrase} {identity}.")
        if key_test.accept is None:
            # Not shape-gated: the body is bounded and never inspected.
            return _success(f"Connected to {spec.label} - {key_label} accepted.")
        if key_test.list_field is not None:
            listed = _json_field(_gated_body(spec, dest, outcome), key_test.list_field)
            if not isinstance(listed, list):
                return _unexpected_body(spec, dest)
            if key_test.list_verdict is not None:
                return _LIST_VERDICTS[key_test.list_verdict](spec, str(key), listed)
            return _credentials_accepted(spec)
        return _named(spec, dest, outcome, *key_test.identity, phrase=key_test.identity_phrase)

    return _verdict_guard(test)


def _vendor_rejected_message(spec: CredentialTypeSpec, dest: _Destination, key_label: str) -> str:
    """A `reject_names_vendor` type's 401/403 sentence. With an operator host field (Make's zone) it names the
    host that refused, since a key valid on another host (an EU account left on `us1`) is rejected too."""
    field = _operator_host_field(spec)
    key_label = _mid_sentence(key_label, spec.label)
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
    as "Connected to {label} {phrase} {identity}.", or `_credentials_accepted` when none survives. A path that starts
    with an index reads a JSON array (Xero's `/connections`)."""
    array = any(path and isinstance(path[0], int) and not isinstance(path[0], bool) for path in paths)
    parsed = _gated_body(spec, dest, outcome, (dict, list) if array else dict)
    identity = _identity_text(*(_json_field(parsed, *path) for path in paths))
    if identity is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} {phrase} {identity}.", identity=identity)


# ── Xero ──────────────────────────────────────────────────────────────────────

# Call 2's address, a tester-local constant rather than a second `default_base_url`:
# `identity.xero.com` receives the operator's credentials (the host the stored-secret pin compares);
# `api.xero.com` only gets a token derived a moment earlier, and no operator field can move it.
_XERO_CONNECTIONS_URL = "https://api.xero.com/connections"

# Call 2's pin, computed once from the constant, never from `config`. Passed as `allowed_origin`
# because the default pin is `dest.base` (`identity.xero.com`).
_XERO_CONNECTIONS_ORIGIN = request_origin(_XERO_CONNECTIONS_URL)


@_verdict_guard
def test_xero(config: dict[str, Any], deadline: float) -> TestResult:
    """`POST {base}/connect/token`, then `GET api.xero.com/connections` with the token and Tenant ID.

    Two calls across two hosts, spending one deadline sequentially (halving it would false-negative a
    slow valid credential). Call 2 is the identity call; a client-credentials token names no org.

    Call 2 carries `Xero-Tenant-Id` (hence Tenant ID is required). Observed live: no or empty header ->
    400; a tenant not on this connection -> 200 `[]`. So an empty list is a failure naming the Tenant ID.
    Custom Connections is a paid add-on; a plan without it cannot green.
    """
    spec, dest = _prepare("xero", config)

    access_token = _xero_access_token(spec, dest, config, deadline)

    outcome = _follow_up(
        _XERO_CONNECTIONS_URL, dest=dest, deadline=deadline, allowed_origin=_XERO_CONNECTIONS_ORIGIN,
        # Proved present and transmittable by `_field_problem`; `str()` because values are untyped.
        headers={**_bearer_headers(access_token), "Xero-Tenant-Id": str(config["tenant_id"])},
        could_not=f"{spec.label} accepted the credentials but could not complete the organisation lookup",
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
) -> str:
    """Call 1: the client-credentials access token; the failure to report instead of calling again is raised.

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
    if outcome.status_code == 400:
        raise _Verdict(_failure(
            f"{spec.label} rejected the client credentials or the requested scope - check the "
            "Client ID, Client secret and Scope.",
            detail=f"HTTP {outcome.status_code}",
        ))
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        raise _Verdict(failure)
    return _access_token(spec, dest, outcome)


# ── Slack ─────────────────────────────────────────────────────────────────────

def _slack_tester(slug: str, token_field: str, app_token_field: Optional[str] = None) -> Tester:
    """`POST https://slack.com/api/auth.test` with `config[token_field]`, then (`slack` only) `POST
    .../apps.connections.open` with the app-level token: two credentials, one shared deadline, both required for a
    green. A tenant's installed workspace (`slack_workspace`) holds the bot token alone.

    Slack answers HTTP 200 `{"ok": false, ...}` for a bad token, so `ok is True` is required on each
    call (`is not True` fails closed). Each rejection names that call's field; the vendor `error` is
    never read into a message. The `wss://` `url` from `apps.connections.open` is never read. POST with
    `content=b""`. Success names both workspace and user, or neither.
    """

    def test(config: dict[str, Any], deadline: float) -> TestResult:
        spec, dest = _prepare(slug, config)
        auth_test = _slack_call(spec, dest, deadline, config, "/api/auth.test", token_field)
        team = _identity_text(_json_field(auth_test, "team"))
        user = _identity_text(_json_field(auth_test, "user"))
        if app_token_field is not None:
            # Call 2, sharing the same deadline (one budget). Only `ok` is read (by `_ok_envelope`).
            _slack_call(spec, dest, deadline, config, "/api/apps.connections.open", app_token_field)
        if team is None or user is None:
            return _credentials_accepted(spec)
        return _success(f"Connected to Slack workspace {team} as {user}.")

    return _verdict_guard(test)


def _slack_call(
    spec: CredentialTypeSpec,
    dest: _Destination,
    deadline: float,
    config: dict[str, Any],
    path: str,
    token_field: str,
) -> dict[str, Any]:
    """One empty-body Slack POST with `config[token_field]` as the Bearer token: the `ok: true` object; the
    failure naming that token's field is raised."""
    rejected = f"Authentication rejected - check the {_field_label(spec, token_field)}."
    outcome = _checked_request(
        spec, dest, deadline, "POST", f"{dest.base}{path}", content=b"", rejected_message=rejected,
        headers={**_bearer_headers(str(config[token_field])),
                 "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    return _ok_envelope(spec, dest, outcome, rejected=rejected)


def _ok_envelope(
    spec: CredentialTypeSpec, dest: _Destination, outcome: _HttpOutcome, *, rejected: str
) -> dict[str, Any]:
    """A Slack/Telegram-style `{"ok": true, ...}` 2xx read: an `ok` that is not `true` raises `rejected`, the
    sentence naming the token's field; otherwise the gated body."""
    parsed = _gated_body(spec, dest, outcome)
    if _json_field(parsed, "ok") is not True:
        raise _Verdict(_failure(rejected, detail="rejected"))
    return parsed


# ── GitHub ────────────────────────────────────────────────────────────────────

# The path differs by host: github.com has no prefix, GHES serves `/api/v3` (appending it on
# api.github.com is a probed 404). Keyed on the host, not on who chose it, matching the stored-secret pin.
_GITHUB_ENTERPRISE_PREFIX = "/api/v3"

# Computed from the registry default so it equals what `_destination` resolves a blank box to. Safe
# before the foot-of-module guard proves it parses, because that guard raises.
_GITHUB_COM_ORIGIN = request_origin(CREDENTIAL_TYPES["github"].default_base_url)


@_verdict_guard
def test_github(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET {base}/user` on github.com, `GET {base}/api/v3/user` on a GitHub Enterprise Server.

    Names the token's account. Probed: an invalid PAT answers `401 {"message":"Bad credentials", ...}`.
    PAT-only: App installation and OAuth user tokens come from flows this module lacks. The host is
    operator-suppliable, so the shape gate matters (an SSO portal can answer 200 with HTML).
    """
    spec, dest = _prepare("github", config)

    on_public_api = _same_origin(request_origin(dest.base), _GITHUB_COM_ORIGIN)
    prefix = "" if on_public_api else _GITHUB_ENTERPRISE_PREFIX
    # Not `_api_key_request`: the prefix is host-dependent and the vendor pins `Accept`. The PAT goes via
    # `auth` (see `_HeaderAuth`): a `user:pass@` in the Base URL overwrites a plain header.
    outcome = _checked_request(
        spec, dest, deadline, "GET", f"{dest.base}{prefix}/user",
        auth=_HeaderAuth("Authorization", f"Bearer {config['token']}"),
        headers=github_api.headers(None),
    )
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
    spec, dest = _prepare(slug, config)

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


# The OAuth App's and the GitHub App's recognition tests; the GitHub App's `slug` is checked only as present.
test_github_app = _verdict_guard(functools.partial(_test_github_client_id, "github_app"))
test_github_install_app = _verdict_guard(functools.partial(_test_github_client_id, "github_install_app"))


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


@_verdict_guard
def test_bitbucket(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET https://api.bitbucket.org/2.0/user`: Basic (account email + API token) for an `api_token`, a
    Bearer token for `oauth`. Names the account. Fixed host, shape-gated all the same."""
    spec, dest = _prepare("bitbucket", config)
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
    return _named(spec, dest, outcome, ("username",), ("display_name",))


# ── Azure DevOps Services ─────────────────────────────────────────────────────

@_verdict_guard
def test_azure_devops(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET https://dev.azure.com/{test_org}/_apis/connectionData`: Basic (any username + PAT) for a
    `pat`, a Bearer token for `oauth`. Names the authenticated user. Probed: a bad PAT answers 401 (or a
    203 sign-in page, which the shape gate refuses)."""
    spec, dest = _prepare("azure_devops", config)
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


@_verdict_guard
def test_azure(config: dict[str, Any], deadline: float) -> TestResult:
    """A token for Azure Resource Manager, then the subscription read with it: names the subscription.

    A refused grant shows Entra's own sentence (`AADSTS7000215: Invalid client secret provided…`, property 4's
    exception); a 403 on the subscription says the app holds no role there."""
    if not edition_features.enabled("cloud_nodes"):
        return _not_in_ce()
    spec, dest = _prepare("azure", config)
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
    parsed = _json_body(outcome.body)
    if not 200 <= outcome.status_code < 300:
        refusal = azure_wire.token_refusal(parsed, secret)
        if refusal is not None and outcome.status_code < 500:
            return _failure(f"{spec.label} refused the credentials: {refusal}", detail=f"HTTP {outcome.status_code}")
        return _status_failure(outcome, spec.label) or _unexpected_body(spec, dest)
    access_token = _access_token(spec, dest, outcome)
    subscription = str(config["subscription_id"]).strip()
    read = _follow_up(
        f"{azure_wire.ARM_BASE}/subscriptions/{subscription}?api-version=2022-12-01", dest=dest, deadline=deadline,
        headers=_bearer_headers(access_token), allowed_origin=_AZURE_ARM_ORIGIN,
        could_not=f"{spec.label} accepted the credentials but the subscription could not be read",
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


@_verdict_guard
def test_aws(config: dict[str, Any], deadline: float) -> TestResult:
    """`POST https://sts.amazonaws.com/` with `Action=GetCallerIdentity`, signed with SigV4.

    Names the account and ARN, and is rejected (403) for a wrong key id or secret. The signer's header
    mapping is passed through verbatim and `_AWS_BODY` unchanged: the signature covers them, and any
    difference would be a 403 misreported as bad credentials.
    """
    spec, dest = _prepare("aws", config)

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
            amz_date=utcnow().strftime("%Y%m%dT%H%M%SZ"),
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


def _google_access_token(
    spec: CredentialTypeSpec, dest: _Destination, key: gcp_auth.ServiceAccountKey, deadline: float,
    scopes: Optional[list[str]] = None,
) -> str:
    """An access token for `key` (`_service_account_grant`), or the raised failure in Google's own words. `dest` is
    the token host's; `scopes` defaults to `cloud-platform`."""
    outcome = _service_account_grant(key, scopes or [gcp_wire.SCOPE], dest, deadline)
    refused = _google_refusal(outcome, spec.label)
    if refused is not None:
        raise _Verdict(refused)
    return _access_token(spec, dest, outcome)


def _google_get(
    spec: CredentialTypeSpec, api: _Destination, url: str, access: str, deadline: float,
    rejected: Optional[Callable[[int, Optional[str]], TestResult]] = None,
) -> dict[str, Any]:
    """`GET url` at a Google API with the minted token: the gated body, or the raised refusal in Google's own words.
    `rejected` builds a catalog hook's own 401/403 sentence from the status and Google's reason (AC2.6: the vendor,
    the field and the status by name)."""
    outcome = _request("GET", url, dest=api, deadline=deadline, headers=_bearer_headers(access))
    if rejected is not None and outcome.status_code in (401, 403):
        raise _Verdict(rejected(outcome.status_code, _google_says(outcome)))
    refused = _google_refusal(outcome, spec.label)
    if refused is not None:
        raise _Verdict(refused)
    body = _gated_body(spec, api, outcome)
    assert isinstance(body, dict)  # `_gated_body`'s default container
    return body


def _service_account_prepared(
    type_slug: str, config: dict[str, Any]
) -> tuple[CredentialTypeSpec, _Destination, "gcp_auth.ServiceAccountKey", dict[str, Any]]:
    """`_prepare` for a type holding a pasted Google service account key (`gcp_auth.KEY_FIELD`): the key parsed, and
    the config with the key re-serialised compactly — its line breaks are formatting, and it travels in no header
    for `_field_problem`'s transmittability rule to guard. A refusal is raised, naming a field, never a value."""
    raw = config.get(gcp_auth.KEY_FIELD)
    key: Optional[gcp_auth.ServiceAccountKey] = None
    if isinstance(raw, str) and not is_blank(raw):
        try:
            key = gcp_auth.parse_key(raw)
        except ValueError as exc:
            raise _Verdict(_failure(str(exc))) from None  # fixed sentences naming a field, never a value
        config = {**config, gcp_auth.KEY_FIELD: json.dumps(json.loads(raw))}
    spec, dest = _prepare(type_slug, config)
    if key is None:  # a non-text value `_prepare` let through (a blank one it refused)
        raise _Verdict(_failure(f"{_field_label(spec, gcp_auth.KEY_FIELD)} must be the JSON key file's text."))
    return spec, dest, key, config


def _service_account_grant(
    key: "gcp_auth.ServiceAccountKey", scopes: list[str], dest: _Destination, deadline: float
) -> _HttpOutcome:
    """The JWT bearer grant at `gcp_auth.TOKEN_URL` (only the signed assertion is sent); an unsignable key raised."""
    try:
        signed = gcp_auth.assertion(key, scopes)
    except ValueError as exc:
        raise _Verdict(_failure(str(exc))) from None
    return _request(
        "POST", gcp_auth.TOKEN_URL, dest=dest, deadline=deadline,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        content=urlencode(gcp_auth.grant_body(signed)).encode(),
    )


def _google_project_ready(
    type_slug: str, config: dict[str, Any]
) -> tuple[CredentialTypeSpec, _Destination, "gcp_auth.ServiceAccountKey", str]:
    """The prelude shared by the Google Cloud project testers, before any call leaves: the cloud-edition gate (the
    community edition ships no Google Cloud signer), the pasted key (`_service_account_prepared`) and the project —
    the config's `gcp_auth.PROJECT_FIELD`, else the key's own, refused unless it is a project id. A refusal is
    raised as the verdict; the token is the caller's (`_google_access_token`, its scopes vary)."""
    if not edition_features.enabled("cloud_nodes"):
        raise _Verdict(_not_in_ce())
    spec, dest, key, config = _service_account_prepared(type_slug, config)
    project = str(config.get(gcp_auth.PROJECT_FIELD) or "").strip() or key.project_id
    if not project:
        raise _Verdict(_failure(f"{_field_label(spec, gcp_auth.PROJECT_FIELD)} is required: the key names no project."))
    if not _GCP_PROJECT_PATTERN.fullmatch(project):
        raise _Verdict(_failure(f"{_field_label(spec, gcp_auth.PROJECT_FIELD)} is not a Google Cloud project id."))
    return spec, dest, key, project


@_verdict_guard
def test_gcp(config: dict[str, Any], deadline: float) -> TestResult:
    """The key exchanged for a token at `oauth2.googleapis.com/token` (a signed JWT bearer grant, the key itself
    sent nowhere), then `GET compute/v1/projects/{project}` with it — the project the key's nodes would launch in.

    A refused key or project reads Google's own sentence ("Invalid JWT Signature.", "Required
    'compute.projects.get' permission …")."""
    spec, dest, key, project = _google_project_ready("gcp", config)
    access = _google_access_token(spec, dest, key, deadline)
    gated = _google_get(spec, _GCP_COMPUTE, f"{_GCP_COMPUTE.base}/compute/v1/projects/{project}", access, deadline)
    named = _identity_text(_json_field(gated, "name"))
    who = _identity_text(key.client_email)
    if named is None or who is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} as {who} (project {named}).")


def _google_catalog_read(
    type_spec: CredentialTypeSpec, dest: _Destination, key: "gcp_auth.ServiceAccountKey", deadline: float,
    api: _Destination, url: str, *, vendor: str, said: str, remedy: Optional[str] = None,
    scopes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """A catalog vendor's Google read after `_google_project_ready`: the token minted for `scopes`
    (`_google_access_token`), then `GET url` (`_google_get`). A 401/403 is AC2.6's sentence — the vendor and the key
    field by name, then Google's own reason after `said` (a missing role, a disabled API), or `remedy` when Google
    gave none."""
    what = _mid_sentence(_field_label(type_spec, gcp_auth.KEY_FIELD), vendor)
    access = _google_access_token(type_spec, dest, key, deadline, scopes)
    return _google_get(
        type_spec, api, url, access, deadline,
        rejected=lambda status, reason: _catalog_rejected(
            vendor, what, status, remedy=f"{said}: {reason}" if reason else remedy),
    )


# ── Discovery Engine (a catalog `tester` hook) ────────────────────────────────

#: Each Discovery Engine location and the API host that serves it (a fixed map, so no value moves the call).
_DISCOVERY_ENGINE_HOSTS: Mapping[str, str] = {
    "global": "discoveryengine.googleapis.com",
    "us": "us-discoveryengine.googleapis.com",
    "eu": "eu-discoveryengine.googleapis.com",
}
_DISCOVERY_ENGINE_HOOK = f"{__name__}:test_discovery_engine"
#: The spec field naming the location (the hook's spec must declare it; `tests/integrations/` pins that).
DISCOVERY_ENGINE_LOCATION_FIELD = "location"


@_verdict_guard
def test_discovery_engine(config: dict[str, Any], deadline: float) -> TestResult:
    """The catalog hook of the vendor whose spec names it: a service-account key exchanged for a token
    (`_google_access_token`), then `GET v1/projects/{project}/locations/{location}/collections/default_collection/
    engines` — a read the `roles/discoveryengine.viewer` role allows, naming how many apps the key can see.

    Its type is the catalog spec that names this hook, so no vendor name is written here. Run under
    `catalog_tester`'s verdict guard; the prelude is `_google_project_ready`'s."""
    spec = next(item for item in catalog.INTEGRATIONS.values() if item.tester == _DISCOVERY_ENGINE_HOOK)
    location = str(config.get(DISCOVERY_ENGINE_LOCATION_FIELD) or "").strip().lower() or "global"
    host = _DISCOVERY_ENGINE_HOSTS.get(location)
    type_spec, dest, key, project = _google_project_ready(spec.credential_type, config)
    if host is None:
        return _failure(f"{_field_label(type_spec, DISCOVERY_ENGINE_LOCATION_FIELD)} must be one of: "
                        f"{', '.join(_DISCOVERY_ENGINE_HOSTS)}.")
    api = _Destination(base=f"https://{host}", host=host, operator_supplied=False)
    body = _google_catalog_read(
        type_spec, dest, key, deadline, api,
        f"{api.base}/v1/projects/{project}/locations/{location}/collections/default_collection/engines",
        vendor=spec.label, said="Google said", remedy="check its roles.",
    )
    engines = _json_field(body, "engines")
    count = len(engines) if isinstance(engines, list) else 0
    who = _identity_text(key.client_email)
    seen = f"{count} app{'' if count == 1 else 's'} in {location}"
    if who is None:
        return _success(f"Connected to {spec.label} - {seen}.")
    return _success(f"Connected to {spec.label} as {who} ({seen}).", identity=who)


# ── BigQuery (a catalog `tester` hook) ────────────────────────────────────────

_BIGQUERY = _Destination(base="https://bigquery.googleapis.com", host="bigquery.googleapis.com",
                         operator_supplied=False)
_BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"


@_verdict_guard
def test_bigquery(config: dict[str, Any], deadline: float) -> TestResult:
    """The catalog BigQuery vendor's Test hook (`integrations/vendors/bigquery.toml`): a read-only token minted
    from the key (`_google_access_token`), then `GET bigquery/v2/projects/{project}/datasets?maxResults=1`, the
    cheapest read that proves the key may use BigQuery in that project. A 401/403 names the vendor, the field
    and the status, followed by Google's own sentence (AC2.6). The prelude is `_google_project_ready`'s."""
    spec, dest, key, project = _google_project_ready("bigquery", config)
    _google_catalog_read(
        spec, dest, key, deadline, _BIGQUERY, f"{_BIGQUERY.base}/bigquery/v2/projects/{project}/datasets?maxResults=1",
        vendor=spec.label, said="Google says", scopes=[_BIGQUERY_SCOPE],
    )
    who = _identity_text(key.client_email)
    if who is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} as {who} (project {project}).")


def google_service_account_grant(
    type_slug: str, scopes: Sequence[str], config: dict[str, Any], deadline: float
) -> TestResult:
    """A catalog vendor's Test whose API is a Google one with no read-only call: the pasted service account key
    (`gcp_auth.KEY_FIELD`) signs a JWT for `scopes` and `oauth2.googleapis.com/token` exchanges it — the grant every
    call starts with, so a revoked, deleted or malformed key fails here; the key itself is sent nowhere. The type
    must name that host as its base (its spec's `default_base_url`). A refusal names the vendor, the field and the
    status, plus Google's own sentence; success names the key's account and project. A spec's `tester` hook binds
    the type and scopes, and `catalog_tester`'s `_verdict_guard` turns a raised refusal into the result. The signer
    is the cloud edition's (`app/gcp`), so the community edition answers `_not_in_ce`."""
    if not edition_features.enabled("cloud_nodes"):
        return _not_in_ce()
    spec, dest, key, config = _service_account_prepared(type_slug, config)
    what = _mid_sentence(_field_label(spec, gcp_auth.KEY_FIELD), spec.label)
    outcome = _service_account_grant(key, list(scopes), dest, deadline)
    if outcome.status_code in (400, 401, 403):
        # The token endpoint answers a bad signature, a deleted key or a disabled account `400 invalid_grant`.
        rejected = _catalog_rejected(spec.label, what, outcome.status_code)
        said = _google_says(outcome)
        if said is not None:
            rejected = rejected._replace(message=f"{rejected.message} Google says: {said}")
        return rejected._replace(http=outcome)
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure._replace(http=outcome)
    access = _json_field(_json_body(outcome.body), "access_token")
    if not isinstance(access, str) or not access:
        return _unexpected_body(spec, dest)
    who = _identity_text(key.client_email)
    project = _identity_text(str(config.get(gcp_auth.PROJECT_FIELD) or "").strip() or key.project_id)
    if who is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} as {who}" + (f" (project {project})." if project else "."))


def _mid_sentence(label: str, vendor: str) -> str:
    """A field label as it reads after "the": "Access token" -> "access token", but "API key", "PostHog host" and
    "Semantic Layer URL" (dbt Semantic Layer) keep their first word, an acronym or a brand. A brand is caught as
    a word of the vendor's own label, or by any capital past its first letter."""
    first, space, rest = label.partition(" ")
    if not (first.isalpha() and first.istitle()) or first in vendor.split():
        return label
    return first.lower() + space + rest


def _field_label(spec: CredentialTypeSpec, key: str) -> str:
    """The operator-facing label for one field, read from the registry.

    Defaults to the key rather than raising `StopIteration`, so a registry rename cannot turn a test
    result into a 500.
    """
    return next((field.label for field in spec.fields if field.key == key), key)


# ── Headscale ─────────────────────────────────────────────────────────────────

def _headscale_key_verdict(spec: CredentialTypeSpec, key: str, listed: list[Any]) -> TestResult:
    """The headscale `list_verdict` (`GET {api_url}/api/v1/apikey`, REST v1): green names the calling key's own
    expiry, found by prefix - headscale lists every key as its public prefix plus `***`
    (`hscontrol/types/api_key.go::Proto`), and a silently lapsed key stops every node join. `login_server` is
    only format-checked (https, by `_field_problem`); nodes dial it, the CP does not."""
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
#: A Connect row whose cached access token has lapsed: the refresh path renews it, so a Test spends nothing.
_RENEWS_MESSAGE = "The cached access token has expired - Jaah renews it before the next use; test again then."
#: A Connect row holding a refresh token but no access token yet: the refresh path mints one before its next use.
_MINTS_MESSAGE = "No access token cached yet - Jaah mints one before the next use; test again then."


def _cached_token_expired(config: dict[str, Any]) -> bool:
    """Whether a Connect row's cached access token is past its `expiry_date` (epoch milliseconds, as
    `oauth2_tokens.py` writes it). A missing or malformed expiry is not expired: the vendor judges the token."""
    expiry = config.get("expiry_date")
    return isinstance(expiry, str) and expiry.strip().isdigit() and int(expiry) / 1000 <= utcnow().timestamp()

# `invalid_grant` (RFC 6749 §5.2, imported from `oauth2_wire`) is the one refusal a retry cannot fix,
# and gets its own sentence because the fix is to reauthorize.


def _refresh_exchange(slug: str, config: dict[str, Any], deadline: float) -> TestResult:
    """`POST <token endpoint>` with a `refresh_token` grant: the whole of both testers below.

    The endpoint comes from the pinned provider and nowhere else; a type whose provider pins none has
    no tester (see the assertion below). No `scope` is sent: on a refresh it can only narrow (RFC 6749
    §6), and the check should verify everything the grant carries.
    """
    spec, dest = _prepare(slug, config)

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
    # The form below is the RFC shape (client pair in the body, `grant_type` sent): a provider on the
    # deployment's own app (no client on the row) or with Basic auth or its own refresh wire (Figma) has no
    # refresh-grant test; its type is tested otherwise (`figma`: a `key_test` on a pasted token or the cached Connect token).
    if (provider.platform_app or provider.basic_auth or provider.refresh_url or not provider.refresh_grant_type
            or provider.client_id_param != "client_id"):
        raise RuntimeError(f"{slug}: provider {provider.key!r} does not speak the refresh form this test sends")
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


# A refresh grant against `https://oauth2.googleapis.com/token`. A row with the operator's own OAuth client is
# exchanged for real; a CLI-minted row (no client; see `registry.py`'s `client_id` field) gets `_NO_CLIENT_MESSAGE`.
test_google = _verdict_guard(functools.partial(_refresh_exchange, "google"))


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


def _workspace_id_clause(value: str) -> Optional[str]:
    """It rides a request header on every model call, so it is refused unless it is one."""
    return None if account_auth.is_valid_workspace_id(value) else "must be wrkspc_ followed by letters and digits"


# Keyed by type and field, so `write_time_problem` and the tester read the same check.
_WRITE_TIME_CHECKS = {
    (account_auth.API_KEY_CREDENTIAL_TYPE, account_auth.WORKSPACE_ID_CONFIG_KEY): _workspace_id_clause,
    ("stripe", "publishable_key"): _stripe_publishable_clause,
    ("generic_api_key", "header_name"): _header_name_clause,
    ("generic_api_key", "value_prefix"): _value_prefix_clause,
    ("bitbucket", "kind"): _kind_clause(BITBUCKET_KINDS),
    ("azure_devops", "kind"): _kind_clause(AZURE_DEVOPS_KINDS),
    ("azure_devops", "test_org"): _azure_org_clause,
    ("mcp_server", "transport"): _kind_clause(mcp_client.TRANSPORTS),
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


@_verdict_guard
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
    The URL is optional with no default (the registry's rule), so a blank one has nowhere to send it.
    """
    if is_blank(config.get("base_url")):
        return _failure("URL is required before testing.")
    spec, dest = _prepare("generic_api_key", config)

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
    # No `detail` on a green (`CredentialEditPage.tsx` renders it only on failure).
    return _success(
        f"{dest.host} answered HTTP {outcome.status_code} to a request carrying the "
        f"{_field_label(spec, 'api_key')}. A generic endpoint has no documented success shape, so "
        "this is not proof the key is valid."
    )


# ── Stripe ────────────────────────────────────────────────────────────────────


@_verdict_guard
def test_stripe(config: dict[str, Any], deadline: float) -> TestResult:
    """The Stripe keys' connection test (`app/billing/stripe_tester.py`); the community edition has no billing."""
    if not edition_features.enabled("billing"):
        return _not_in_ce()
    from app.billing import stripe_tester

    return stripe_tester.connection_test(config, deadline)


# ── OIDC ──────────────────────────────────────────────────────────────────────

_OIDC_DISCOVERY_PATH = "/.well-known/openid-configuration"


@_verdict_guard
def test_oidc_client(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET {issuer}/.well-known/openid-configuration`, green only when the document's `issuer` is exactly the
    operator's (OIDC Discovery 1.0 §4.3: a sign-in refuses any other spelling, a trailing slash included).

    Proves the issuer, not the client: an unauthenticated call (property 1's limit), since the only check of the
    client pair is a real sign-in. Sends no secret. Each failure names the Issuer field, the one thing to fix.
    """
    spec, dest = _prepare("oidc_client", config)
    issuer_label = _field_label(spec, "issuer")

    try:
        outcome = _request(
            "GET", f"{dest.base}{_OIDC_DISCOVERY_PATH}", dest=dest, deadline=deadline,
            headers={"Accept": "application/json"},
        )
    except _Verdict as verdict:
        if verdict.result.detail == "transport_error":
            return _failure(f"Could not reach {dest.host} - check the {issuer_label}.", detail="transport_error")
        raise
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

@_verdict_guard
def test_telegram_bot(config: dict[str, Any], deadline: float) -> TestResult:
    """`GET https://api.telegram.org/bot<token>/getMe`: bespoke because the token rides the URL PATH, which no
    `KeyTestSpec` can place. Telegram answers 401 for a bad token and `{"ok": true, "result": {...}}` for a good
    one; `ok is True` is required. No message or log line carries the URL, which carries the token. Success
    names the bot's @username."""
    spec, dest = _prepare("telegram_bot", config)
    rejected = f"Authentication rejected - check the {_field_label(spec, 'bot_token')}."
    outcome = _request(
        "GET",
        f"{dest.base}/bot{str(config['bot_token']).strip()}/getMe",
        dest=dest,
        deadline=deadline,
        headers={"Accept": "application/json"},
    )
    if outcome.status_code in (401, 404):  # 404 is Telegram's answer to a token that is not even token-shaped
        return _failure(rejected, detail=f"HTTP {outcome.status_code}")
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure
    parsed = _ok_envelope(spec, dest, outcome, rejected=rejected)
    username = _identity_text(_json_field(parsed, "result", "username"))
    if username is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} @{username}.")


# The hand-written testers: every type whose Test is more than the one GET a `KeyTestSpec` declares.
# ── MCP servers (`app/mcp_client.py`) ─────────────────────────────────────────

def _mcp_token(config: dict[str, Any], *, bearer_field: Optional[str]) -> tuple[Optional[str], Optional[TestResult]]:
    """The token a Test sends, or the verdict that there is none worth sending yet: a Connect grant's live
    access token first (its expiry judged as `_catalog_http` does), else the pasted bearer field, else None (a
    server that takes no auth)."""
    token = config.get("access_token")
    if not is_blank(token):
        if _cached_token_expired(config):
            return None, _unsupported(_RENEWS_MESSAGE)
        return str(token), None
    if not is_blank(config.get("refresh_token")):
        return None, _unsupported(_MINTS_MESSAGE)
    if bearer_field is not None and not is_blank(config.get(bearer_field)):
        return str(config[bearer_field]).strip(), None
    return None, None


def mcp_probe_result(label: str, url: str, transport: str, token: Optional[str], deadline: float) -> TestResult:
    """The Test verdict for one MCP server: the handshake and the tool list (`mcp_client.probe`), its tool count on
    success, "<Vendor> rejected the token (401)" on a 401/403, and the probe's own sentence otherwise."""
    try:
        found = mcp_client.probe(url, transport=transport, token=token, deadline=deadline)
    except mcp_client.McpRefused as exc:
        if exc.kind == "rejected":
            what = "token" if token else "request (no token was sent)"
            remedy = " - press Connect, or check the bearer token." if token else " - add a token or press Connect."
            return _failure(f"{label} rejected the {what} ({exc.status}){remedy}", detail=f"HTTP {exc.status}")
        return _failure(str(exc), detail=exc.kind)
    tools = f"{found.tool_count} tool{'s' if found.tool_count != 1 else ''}"
    named = f" ({found.server_name})" if found.server_name else ""
    return _success(f"Connected to {label}{named} - {tools} available.", identity=found.server_name)


def _mcp_label(url: str) -> str:
    """A custom server's name in a sentence: its host."""
    try:
        return httpx.URL(url).host or "The MCP server"
    except (httpx.InvalidURL, ValueError, UnicodeError):
        return "The MCP server"


@_verdict_guard
def test_mcp_server(config: dict[str, Any], deadline: float) -> TestResult:
    """An `mcp_server` row: the MCP handshake against its URL with its bearer token or Connect grant."""
    spec = CREDENTIAL_TYPES["mcp_server"]
    problem = _field_problem(spec, config)
    if problem is not None:
        return problem
    url = str(config.get("url") or "").strip()
    transport = str(config.get("transport") or "streamable_http").strip()
    token, verdict = _mcp_token(config, bearer_field="bearer_token")
    if verdict is not None:
        return verdict
    return mcp_probe_result(_mcp_label(url), url, transport, token, deadline)


# ── Custom connectors (`app/integrations/custom.py`) ────────────────────────────
# Not a `TESTERS` entry: a custom connection is a generic-type row whose Test is its connector's, run by
# `routes/integrations_custom.py` with the connector in hand.


def grant_token(config: dict[str, Any]) -> tuple[Optional[str], Optional[TestResult]]:
    """An OAuth row's live access token, or the verdict that none is worth sending yet (`_mcp_token`'s rule)."""
    return _mcp_token(config, bearer_field=None)


def custom_rest_result(
    label: str,
    url: str,
    deadline: float,
    *,
    header: Optional[tuple[str, str]] = None,
    basic: Optional[tuple[str, str]] = None,
) -> TestResult:
    """`GET url` carrying one header or HTTP Basic, at addresses `http_bounded.vetted_addresses` passed (THE SSRF
    rule: the URL is an org's own definition, so the metadata service or an RFC 1918 host is refused before
    anything is sent, and the vetted addresses are the only ones dialled). Green on a 2xx; a 401/403 names the
    secret; nothing the server wrote is echoed."""
    refusal, addresses = http_bounded.vetted_addresses(url)
    if refusal is not None or not addresses:
        return _failure(f"{label} cannot be reached at that address - {refusal or 'it did not resolve'}.",
                        detail="refused_destination")
    host = httpx.URL(url).host
    dest = _Destination(base=url, host=host, operator_supplied=True, host_label="Base URL")
    headers = {"Accept": "application/json"}
    auth: Optional[httpx.Auth] = None
    if basic is not None:
        auth = httpx.BasicAuth(*basic)
    elif header is not None:
        if header[0].lower() == _DEFAULT_API_KEY_HEADER.lower():
            auth = _HeaderAuth(_DEFAULT_API_KEY_HEADER, header[1])
        else:
            headers[header[0]] = header[1]
    try:
        outcome = _request(
            "GET", url, dest=dest, deadline=deadline, auth=auth, headers=headers,
            allowed_origin=request_origin(url), addresses=addresses,
        )
    except _Verdict as verdict:
        return verdict.result
    if outcome.status_code in (401, 403):
        what = "login and password" if basic is not None else "secret"
        return _failure(f"{host} rejected the request ({outcome.status_code}) - check the {what}.",
                        detail=f"HTTP {outcome.status_code}")
    failure = _status_failure(outcome, label)
    if failure is not None:
        return failure
    return _success(f"{host} answered HTTP {outcome.status_code} at {httpx.URL(url).path or '/'}.")


#: `KeyTestSpec.list_verdict` by name: the green for a gated list, given the spec, the key sent and the list.
_LIST_VERDICTS: dict[str, Callable[[CredentialTypeSpec, str, list[Any]], TestResult]] = {
    "headscale_key_expiry": _headscale_key_verdict,
}

_BESPOKE_TESTERS: dict[str, Tester] = {
    "xero": test_xero,
    "slack": _slack_tester("slack", "token", "app_token"),
    "slack_workspace": _slack_tester("slack_workspace", "bot_token"),
    "telegram_bot": test_telegram_bot,
    "github": test_github,
    "github_app": test_github_app,
    "github_install_app": test_github_install_app,
    "bitbucket": test_bitbucket,
    "azure_devops": test_azure_devops,
    "aws": test_aws,
    "azure": test_azure,
    "gcp": test_gcp,
    "stripe": test_stripe,
    "google": test_google,
    "generic_api_key": test_generic_api_key,
    "oidc_client": test_oidc_client,
    "mcp_server": test_mcp_server,
}


# ── Catalog vendors (`app/integrations/catalog.py`) ───────────────────────────
# One tester per catalog-defined credential type, built from its spec: a declared HTTP call, a format check, or a
# `module:function` hook. A URL carrying a tenant value is dialled only at addresses `vetted_addresses` passed.

#: The format check's verdict (AC3.2): the vendor was not called, and says so.
FORMAT_CHECKED = "Format checked \u2014 {label} was not called."

# One DNS label: what a `{field}` in a Test URL's host may hold, so a tenant value never leaves the vendor's domain.
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _unsupported(message: str) -> TestResult:
    # mypy reads the NamedTuple's `__test__` class attribute as a required field; it is not one.
    return TestResult(ok=False, status="unsupported", message=message)  # type: ignore[call-arg]


def _catalog_rejected(label: str, what: str, status: int | str, *, reconnect: bool = False,
                      remedy: Optional[str] = None) -> TestResult:
    """A catalog vendor's 401/403 (AC2.6): names the vendor, the field and the status (or the vendor's own refusal
    code, for a `rejected_when` body), never a generic sentence."""
    remedy = remedy or ("press Connect to reauthorize." if reconnect else "check it.")
    return _failure(f"{label} rejected the {what} ({status}) - {remedy}",
                    detail=f"HTTP {status}" if isinstance(status, int) else "rejected")


def _catalog_fill(type_spec: CredentialTypeSpec, template: str, config: dict[str, Any]) -> str:
    """A header value with each `{field}` placeholder filled from the row (stripped), or the raised failure naming
    the first blank field. Transmittability was checked by `_field_problem`; the loader limited which fields."""

    def value(match: re.Match[str]) -> str:
        filled = str(config.get(match.group(1)) or "").strip()
        if not filled:
            raise _Verdict(_failure(f"{_field_label(type_spec, match.group(1))} is required before testing."))
        return filled

    return catalog.PLACEHOLDER.sub(value, template)


def _under_suffixes(value: str, suffixes: tuple[str, ...]) -> bool:
    """Whether `value` is a bare https origin (no path, query, userinfo or port) whose host, every label a plain
    DNS label, is one of `suffixes` or a subdomain of one: the parsed host is what is compared."""
    try:
        parsed = httpx.URL(value.rstrip("/"))
    except (httpx.InvalidURL, ValueError):
        return False
    host = (parsed.host or "").lower().rstrip(".")
    return (parsed.scheme == "https" and not parsed.userinfo and parsed.port is None
            and parsed.raw_path in (b"", b"/") and not parsed.query and not parsed.fragment
            and all(_DNS_LABEL.fullmatch(label) for label in host.split("."))
            and any(host == suffix or host.endswith("." + suffix) for suffix in suffixes))


def _catalog_url(type_spec: CredentialTypeSpec, test: "catalog.HttpTestSpec", config: dict[str, Any]) -> str:
    """The Test URL with its one `{field}` placeholder filled, or the raised failure naming the field."""
    url = test.url
    for name in test.placeholders:
        field = next(item for item in type_spec.fields if item.key == name)
        value = str(config.get(name) or "").strip()
        if not value:
            raise _Verdict(_failure(f"{field.label} is required before testing."))
        if field.kind == "url":
            url = url.replace(f"{{{name}}}", value.rstrip("/"))
            if test.host_suffixes and not _under_suffixes(value, test.host_suffixes):
                raise _Verdict(_failure(
                    f"{field.label} must be an https address under "
                    + " or ".join(test.host_suffixes) + " - check it."
                ))
        else:
            if not _DNS_LABEL.fullmatch(value):
                raise _Verdict(_failure(
                    f"{field.label} must be a single name such as 'acme' - no dots, slashes or spaces."
                ))
            url = url.replace(f"{{{name}}}", value.lower())
    return url


_BODY_VALUE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?")


def _catalog_token(
    spec: "catalog.IntegrationSpec", type_spec: CredentialTypeSpec, dest: _Destination, config: dict[str, Any],
    deadline: float,
) -> str:
    """A `[test.token]` Test's call 1: the client-credentials access token, or the raised failure.

    The id and secret travel in the form body only. A refusal (400/401/403/404 - a vendor login answers a wrong
    pair with any of them) names the vendor and the two fields; nothing the vendor wrote is echoed."""
    test = spec.http_test
    assert test is not None and test.token is not None  # narrowing: the caller dispatched on it
    token = test.token
    url = token.url
    addresses: tuple[str, ...] = ()
    for name in token.placeholders:
        field = next(item for item in type_spec.fields if item.key == name)
        value = str(config.get(name) or "").strip()
        if not value:
            raise _Verdict(_failure(f"{field.label} is required before testing."))
        if field.kind == "url":
            url = url.replace(f"{{{name}}}", value.rstrip("/"))
            refusal, addresses = http_bounded.vetted_addresses(url)
            if refusal is not None or not addresses:
                raise _Verdict(_failure(
                    f"{spec.label} cannot be reached at that address - {refusal or 'it did not resolve'}.",
                    detail="refused_destination",
                ))
        elif _PATH_SEGMENT.fullmatch(value):
            url = url.replace(f"{{{name}}}", value)
        else:
            raise _Verdict(_failure(f"{field.label} must be a single value with no slashes or spaces."))
    client_id = str(config.get(token.client_id_field) or "").strip()
    for name in (token.client_id_field, token.client_secret_field):
        if is_blank(config.get(name)):
            raise _Verdict(_failure(f"{_field_label(type_spec, name)} is required before testing."))
    secret = str(config.get(token.client_secret_field) or "")
    basic = token.client_auth == "basic"
    # Basic: the pair in a header, never also in the body (a vendor that refuses the body pair, Aikido).
    form: dict[str, str] = {} if basic else {"client_id": client_id, "client_secret": secret}
    if token.grant_type:
        form["grant_type"] = token.grant_type
    if token.scope:
        form["scope"] = token.scope
    if token.audience:
        form["audience"] = token.audience
    outcome = _request(
        "POST", url, dest=dest, deadline=deadline,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        content=urlencode(form).encode("utf-8"), allowed_origin=request_origin(url), addresses=addresses,
        auth=httpx.BasicAuth(client_id, secret) if basic else None,
    )
    if outcome.status_code in (400, 401, 403, 404):
        what = " and ".join(_mid_sentence(_field_label(type_spec, name), spec.label)
                            for name in (token.client_id_field, token.client_secret_field))
        raise _Verdict(_catalog_rejected(spec.label, what, outcome.status_code, remedy="check both.")._replace(http=outcome))
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        raise _Verdict(failure._replace(http=outcome))
    parsed = _json_body(outcome.body)
    access_token = _json_field(parsed, "access_token") if isinstance(parsed, dict) else None
    if not isinstance(access_token, str) or is_blank(access_token) or not _is_transmittable(access_token):
        raise _Verdict(_unexpected_body(type_spec, dest))
    return access_token


def _catalog_http(spec: "catalog.IntegrationSpec", config: dict[str, Any], deadline: float) -> TestResult:
    test = spec.http_test
    assert test is not None  # narrowing: the caller dispatched on it
    type_spec, dest = _prepare(spec.credential_type, config)
    url = _catalog_url(type_spec, test, config)
    addresses: tuple[str, ...] = ()
    # The Test URL is the spec's own constant (or vetted below), so its origin is the pin, not the type's base.
    allowed_origin = request_origin(url)
    if test.placeholders:
        # A host the tenant chose (THE SSRF rule): every address it resolves to must be public, and only those
        # are dialled, so the metadata service or an RFC 1918 host is refused before anything is sent.
        refusal, addresses = http_bounded.vetted_addresses(url)
        if refusal is not None or not addresses:
            return _failure(f"{spec.label} cannot be reached at that address - {refusal or 'it did not resolve'}.",
                            detail="refused_destination")
        dest = dest._replace(host=httpx.URL(url).host, operator_supplied=True)

    headers: dict[str, str] = {}
    if test.accept is not None:
        headers["Accept"] = test.accept
    headers.update(dict(test.extra_headers))
    headers.update({header: str(config[name]).strip() for header, name in test.field_headers
                    if not is_blank(config.get(name))})
    body = test.body
    for name in test.body_placeholders:
        value = str(config.get(name) or "").strip()
        if not value:
            return _failure(f"{_field_label(type_spec, name)} is required before testing.")
        if not _BODY_VALUE.fullmatch(value):
            return _failure(f"{_field_label(type_spec, name)} may hold only letters, digits, dots, dashes and underscores.")
        assert body is not None  # narrowing: a body placeholder implies a body
        body = body.replace(f"{{{name}}}", value)
    content = body.encode("utf-8") if body is not None else None
    if content is not None and test.content_type:
        headers["Content-Type"] = test.content_type
    auth: Optional[httpx.Auth] = None

    key = config.get(test.key_field) if test.key_field else None
    oauth_row = spec.provider is not None and (test.key_field is None or is_blank(key))
    if test.token is not None and not oauth_row:
        minted = _catalog_token(spec, type_spec, dest, config, deadline)
        # The pair was accepted; a refusal now is the account's access, not its key (named by its own sentence).
        what = "access token it issued"
        auth = _HeaderAuth("Authorization", f"{test.token.prefix} {minted}")
    elif oauth_row:
        token = config.get("access_token")
        if is_blank(token):
            return _unsupported(_NOT_CONNECTED_MESSAGE if is_blank(config.get("refresh_token")) else _MINTS_MESSAGE)
        if _cached_token_expired(config):
            if is_blank(config.get("refresh_token")):
                # Nothing can renew it (`OAuthProvider.expiring_grant_ok`): only the person can, by reconnecting.
                return _failure(f"The {spec.label} access token has expired - press Connect to reauthorize.",
                                detail="expired")
            return _unsupported(_RENEWS_MESSAGE)
        what = "access token"
        auth = _HeaderAuth("Authorization", f"{test.oauth_scheme} {token}")
    else:
        what = _mid_sentence(_field_label(type_spec, test.key_field), spec.label) if test.key_field else "request"
        if test.auth == "header":
            if test.value_template is not None:
                value = _catalog_fill(type_spec, test.value_template, config)
            else:
                value = f"{test.value_prefix} {key}" if test.value_prefix else str(key)
            if test.header_name.lower() == _DEFAULT_API_KEY_HEADER.lower():
                auth = _HeaderAuth(test.header_name, value)
            else:
                headers[test.header_name] = value
        elif test.auth == "basic":
            # No user field: the key is the Basic user name and the password a constant (BambooHR's "x").
            auth = (httpx.BasicAuth(str(key).strip(), test.basic_password or "") if test.user_field is None
                    else httpx.BasicAuth(str(config.get(str(test.user_field)) or "").strip() + test.user_suffix, str(key)))
        elif test.auth == "query":
            # The vendor's own scheme (a key in the query string): the URL is never logged or echoed.
            url = f"{url}{'&' if '?' in url else '?'}{urlencode({str(test.query_param): str(key)})}"
        elif test.auth == "sigv4":
            # The spec's x-amz-* headers are signed, not merely sent: S3 refuses an unsigned one.
            amz = {name: headers.pop(name) for name in list(headers) if name.lower().startswith("x-amz-")}
            try:
                headers.update(sigv4.signed_headers(
                    method=test.method, url=url, region=str(test.sigv4_region), service=str(test.sigv4_service),
                    access_key_id=str(config.get(str(test.user_field)) or "").strip(), secret_access_key=str(key),
                    payload=content or b"", content_type=test.content_type or "application/x-www-form-urlencoded",
                    amz_date=utcnow().strftime("%Y%m%dT%H%M%SZ"), amz_headers=amz,
                ))
            except ValueError:
                return untransmittable_field_result(_field_label(type_spec, str(test.user_field)))

    outcome = _request(test.method, url, dest=dest, deadline=deadline, auth=auth, headers=headers,
                       content=content, allowed_origin=allowed_origin, addresses=addresses)
    remedy = (f"the client credentials are valid, so check the app's access in {spec.label}."
              if test.token is not None and not oauth_row else None)
    if test.rejected_path:
        code = _json_field(_json_body(outcome.body), *test.rejected_path)
        if isinstance(code, str) and code in test.rejected_values:
            return _catalog_rejected(spec.label, what, code, reconnect=oauth_row, remedy=remedy)._replace(http=outcome)
    if test.rejected_text is not None:
        said = re.search(test.rejected_text, outcome.body.decode("utf-8", "replace"))
        if said is not None:
            # The vendor's own refusal code, bounded by the spec's pattern and sanitised as any third-party text.
            return _catalog_rejected(spec.label, what, _identity_text(said.group(0)) or "refused", reconnect=oauth_row,
                                     remedy=remedy)._replace(http=outcome)
    if outcome.status_code in test.rejected_statuses:
        return _catalog_rejected(spec.label, what, outcome.status_code, reconnect=oauth_row,
                                 remedy=remedy)._replace(http=outcome)
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure._replace(http=outcome)
    if test.accept is None:
        return _success(f"Connected to {spec.label} - {what} accepted.")
    if test.require_true and _json_field(_gated_body(type_spec, dest, outcome), *test.require_true) is not True:
        # A vendor that answers 200 for an unknown key says so in a flag, not a status.
        return _failure(f"{spec.label} did not accept the {what} - check it.", detail="not_accepted")._replace(
            http=outcome)
    if test.require_equal_field is not None:
        expected = str(config.get(test.require_equal_field) or "").strip()
        found = _json_field(_gated_body(type_spec, dest, outcome), *test.require_equal_path)
        if not isinstance(found, (str, int)) or isinstance(found, bool) or str(found).strip() != expected:
            # A token valid for another app: the check passes, then every real call fails (Twitch's Client-Id).
            other = _mid_sentence(_field_label(type_spec, test.require_equal_field), spec.label)
            return _failure(f"{spec.label} says the {what} belongs to another {other} - mint it for this {other}, "
                            f"or change the {other} to the one it was minted for.", detail="mismatch")._replace(
                http=outcome)
    if test.list_field is not None:
        if not isinstance(_json_field(_gated_body(type_spec, dest, outcome), test.list_field), list):
            return _unexpected_body(type_spec, dest)
        return _credentials_accepted(type_spec)
    return _named(type_spec, dest, outcome, *test.identity, phrase=test.identity_phrase)


def _catalog_mcp(spec: "catalog.IntegrationSpec", config: dict[str, Any], deadline: float) -> TestResult:
    """A catalog MCP vendor's Test: its server URL (the row's own where the vendor lets it be edited, its `{field}`
    placeholders filled from the row), the spec's transport, and the token its `auth` names."""
    mcp = spec.mcp
    assert mcp is not None  # narrowing: the caller dispatched on it
    url, problem = mcp.server_url(config)
    if url is None:
        return _failure(problem or "The server URL is required before testing.")
    bearer = mcp.auth.partition(":")[2] if mcp.auth.startswith("bearer_field:") else None
    token, verdict = _mcp_token(config, bearer_field=bearer or None)
    if verdict is not None:
        return verdict
    if token is None and mcp.auth != "none":
        return _unsupported(_NOT_CONNECTED_MESSAGE if bearer is None else
                            f"Enter the {bearer} before testing, or press Connect.")
    return mcp_probe_result(spec.label, url, mcp.transport, token, deadline)


def _catalog_format(spec: "catalog.IntegrationSpec", config: dict[str, Any]) -> TestResult:
    check = spec.format_test
    assert check is not None  # narrowing: the caller dispatched on it
    type_spec = CREDENTIAL_TYPES[spec.credential_type]
    problem = _field_problem(type_spec, config)
    if problem is not None:
        return problem
    for key, pattern in check.patterns:
        value = config.get(key)
        if not is_blank(value) and not re.fullmatch(pattern, str(value).strip()):
            return _failure(f"{_field_label(type_spec, key)} is not in the format {spec.label} uses.")
    if check.public_host_field is not None:
        raw = str(config.get(check.public_host_field) or "").strip()
        host = urlsplit(raw).hostname if "://" in raw else raw.rsplit("@", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        refusal = http_bounded.public_destination_refusal(f"https://{host}") if host else "it names no host"
        if refusal is not None:
            return _failure(
                f"{_field_label(type_spec, check.public_host_field)} must name a public address - {refusal}."
            )
    return _success(FORMAT_CHECKED.format(label=spec.label))


def resolve_hook(target: str) -> Callable[..., Any]:
    """A `module:function` named by a spec (`tester`, `on_grant`), imported when first used so no spec can
    create an import cycle; a missing target is a registry bug, raised."""
    module_name, _, attribute = target.partition(":")
    function = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(function):
        raise RuntimeError(f"{target}: names nothing callable")
    return function


# ── Microsoft Entra service principals (catalog `tester` hooks) ────────────────────────────────────────────
# Two calls across two fixed hosts, as `test_azure`: the client-credentials grant at Entra (the destination, which
# receives the secret), then `GET graph.microsoft.com/v1.0/organization` with the token only. Not `azure_wire`: that
# module is cloud-only, and these types ship in every edition.

_ENTRA_LOGIN = "https://login.microsoftonline.com"
_GRAPH_BASE = "https://graph.microsoft.com"
_ENTRA_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
# A tenant GUID or a verified domain (`yourtenant.onmicrosoft.com`): DNS labels only, so a safe URL path segment.
_ENTRA_TENANT = re.compile(r"(?=.{4,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")


def _entra_graph(type_slug: str, config: dict[str, Any], deadline: float) -> TestResult:
    """A Graph token for the service principal, then the tenant's organization read with it: names the tenant."""
    spec, dest = _prepare(type_slug, config)
    tenant, client_id = str(config["tenant_id"]).strip(), str(config["client_id"]).strip()
    if not (_ENTRA_GUID.fullmatch(tenant) or _ENTRA_TENANT.fullmatch(tenant)):
        return _failure(f"{_field_label(spec, 'tenant_id')} must be a tenant ID (a GUID) or a domain such as "
                        "yourtenant.onmicrosoft.com.")
    if not _ENTRA_GUID.fullmatch(client_id):
        return _failure(f"{_field_label(spec, 'client_id')} must be a GUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).")
    secret = str(config["client_secret"])
    form = urlencode({"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret,
                      "scope": f"{_GRAPH_BASE}/.default"}).encode("ascii")
    outcome = _request(
        "POST", f"{_ENTRA_LOGIN}/{tenant}/oauth2/v2.0/token", dest=dest, deadline=deadline,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, content=form,
    )
    if outcome.status_code in (400, 401):
        # Entra answers a wrong secret 401 (AADSTS7000215) and an unknown tenant or client 400 (AADSTS90002/700016).
        what = "client secret" if outcome.status_code == 401 else "tenant ID or client ID"
        return _catalog_rejected(spec.label, what, outcome.status_code)._replace(http=outcome)
    failure = _status_failure(outcome, spec.label)
    if failure is not None:
        return failure._replace(http=outcome)
    access_token = _access_token(spec, dest, outcome)
    read = _follow_up(
        f"{_GRAPH_BASE}/v1.0/organization?$select=id,displayName", dest=dest, deadline=deadline,
        headers=_bearer_headers(access_token), allowed_origin=request_origin(_GRAPH_BASE),
        could_not=f"{spec.label} accepted the client secret but Microsoft Graph could not be reached",
    )
    if read.status_code in (401, 403):
        return _failure(
            f"{spec.label} accepted the client secret, but Microsoft Graph refused the organization read "
            f"({read.status_code}) - grant the app the Organization.Read.All application permission and admin consent.",
            detail=f"HTTP {read.status_code}",
        )._replace(http=read)
    failure = _status_failure(read, spec.label)
    if failure is not None:
        return failure._replace(http=read)
    name = _identity_text(_json_field(_json_body(read.body), "value", 0, "displayName"))
    if name is None:
        return _credentials_accepted(spec)
    return _success(f"Connected to {spec.label} in tenant {name}.", identity=name)


# The `tester` hooks of `vendors/azure_graph.toml` and `vendors/microsoft_copilot.toml`.
test_azure_graph = functools.partial(_entra_graph, "azure_graph")
test_microsoft_copilot = functools.partial(_entra_graph, "microsoft_copilot")


def _mcp_signed_in(spec: "catalog.IntegrationSpec", config: Mapping[str, Any]) -> bool:
    """Whether a row of a vendor with both an HTTP Test and an MCP sign-in holds the sign-in's grant and no key."""
    key = spec.http_test.key_field if spec.http_test is not None else None
    return (spec.mcp is not None and spec.mcp.sign_in and (key is None or is_blank(config.get(key)))
            and not (is_blank(config.get("refresh_token")) and is_blank(config.get("access_token"))))


def catalog_tester(spec: "catalog.IntegrationSpec") -> Tester:
    """The Test of a catalog-defined credential type: its hook, its format check, or its declared HTTP call."""

    def test(config: dict[str, Any], deadline: float) -> TestResult:
        if spec.tester is not None:
            return resolve_hook(spec.tester)(config, deadline)
        if spec.format_test is not None:
            return _catalog_format(spec, config)
        if spec.mcp is not None and (spec.http_test is None or _mcp_signed_in(spec, config)):
            # A spec whose only Test is its `[mcp]` block gets the MCP handshake with no code of its own, and so
            # does a row connected by the MCP sign-in alone (no key for the HTTP Test to send).
            return _catalog_mcp(spec, config, deadline)
        return _catalog_http(spec, config, deadline)

    return _verdict_guard(test)


#: The catalog's testers, by credential type (its adopting specs keep their type's own tester).
_CATALOG_TESTERS: dict[str, Tester] = {
    spec.credential_type: catalog_tester(spec)
    for spec in catalog.INTEGRATIONS.values() if not spec.adopts_existing_type
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
        if key_test.connect_token_field is not None and getattr(fields.get(key_test.connect_token_field), "kind", None) != "secret":
            problems.append(f"connect_token_field {key_test.connect_token_field!r} is not a declared secret field")
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
        # A field in a `require_one_of` group with the key is filled whenever the key is (a type a Connect extends,
        # `catalog.extended_type`, whose Connect rows the grant Test takes instead, `_with_grant_test`).
        grouped = {key for group in spec.require_one_of if key_test.key_field in group for key in group}
        if key_field is not None and (
            (key_test.blank_key_reason is not None and key_field.required)
            or (key_test.blank_key_reason is None and not key_field.required and key_field.key not in grouped)
        ):
            problems.append("blank_key_reason must be set exactly when key_field is optional (a grouped one may omit it)")
        if key_test.list_field is not None and key_test.identity:
            problems.append("list_field and identity are exclusive")
        for name in ("basic_user_field", "identity_fallback_field"):
            # Required so a blank never reaches the wire; never a secret: the fallback is echoed in the green
            # sentence, and a Basic user is sent beside the key, not as it.
            if (field := getattr(key_test, name)) is not None and (
                (declared := fields.get(field)) is None
                or not (declared.required or field in grouped) or declared.kind == "secret"
            ):
                problems.append(f"{name} {field!r} is not a declared, required, non-secret field")
        if key_test.basic_user_field is not None and (
            key_test.connect_token_field or key_test.bearer_when
            or key_test.header_name != _DEFAULT_API_KEY_HEADER or key_test.value_prefix
        ):
            # Basic auth carries the key itself: a header name, prefix or bearer rule beside it would be ignored.
            problems.append("basic_user_field excludes header_name, value_prefix, bearer_when and connect_token_field")
        if key_test.identity_fallback_field is not None and (key_test.accept is not None or not key_test.identity):
            problems.append("identity_fallback_field needs accept=None and identity paths")
        if key_test.list_verdict is not None and (key_test.list_field is None or key_test.list_verdict not in _LIST_VERDICTS):
            problems.append(f"list_verdict {key_test.list_verdict!r} needs list_field and a _LIST_VERDICTS entry")
        if problems:
            raise RuntimeError(f"{slug}: key_test and its registry entry disagree - {'; '.join(problems)}")


_check_tester_registry(CREDENTIAL_TYPES, {**_BESPOKE_TESTERS, **_CATALOG_TESTERS})

def _with_grant_test(own: Tester, spec: "catalog.IntegrationSpec") -> Tester:
    """A hand-written type's Test with the Connect a catalog spec layers over it (`catalog.extended_type`): a row
    holding a refresh token is a Connect row, tested by the spec's `[test]` with its access token; any other row
    keeps the type's own Test, unchanged."""
    grant = catalog_tester(spec)
    # A key row is checked against the type as written: each field it required still is (the extension made them
    # optional only so a Connect row saves without them), in the same order and sentences as before.
    as_written = catalog.hand_written_type(spec.credential_type)

    def test(config: dict[str, Any], deadline: float) -> TestResult:
        if not is_blank(config.get("refresh_token")):
            return grant(config, deadline)
        problem = _field_problem(as_written, config) if as_written is not None else None
        return problem if problem is not None else own(config, deadline)

    return test


def _testers() -> dict[str, Tester]:
    testers: dict[str, Tester] = {
        **_BESPOKE_TESTERS,
        **_CATALOG_TESTERS,
        **{slug: _declared_tester(slug) for slug, spec in CREDENTIAL_TYPES.items() if spec.key_test is not None},
    }
    for slug, own in list(testers.items()):
        spec = catalog.grant_test(slug)
        if spec is not None:
            testers[slug] = _with_grant_test(own, spec)
    return testers


#: Every tester by slug: the bespoke functions, the catalog's, and one `_declared_tester` per `key_test` - each a
#: type a catalog Connect extends answering its Connect rows with the spec's grant Test (`_with_grant_test`).
TESTERS: dict[str, Tester] = _testers()

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
        if spec.free_form_url and (spec.require_https_base_url or spec.allowed_hosts):
            raise RuntimeError(f"{slug}: free_form_url cannot be combined with a url rule it would never enforce")
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
