"""The bounded outbound read shared by every module reaching an untrusted or credential-bearing host:
redirect-off, identity-encoded client, capped raw read, origin pin. Callers keep their own timeout,
deadline, error class and monkeypatched `_client` seam; only the policy lives here.

* `follow_redirects=False` — a 3xx is a real failure, and following one would carry the caller's
  credentials to a host of the response's choosing.
* A capped streamed read — `client.get`/`httpx.post` buffer the whole body, a memory DoS in the
  single-worker API. `Accept-Encoding: identity` plus `iter_raw` makes the cap a cap (`iter_bytes`
  decodes before the check; a hostile host may compress anyway). A body that filled the cap is
  treated as unreadable, never parsed as a prefix.
* `public_destination_refusal` — the one destination rule for a host a caller chose: https, and only
  addresses that are globally routable.
"""
from __future__ import annotations

import functools
import ipaddress
import json
import socket
import ssl
from typing import Any, Callable, Mapping, Optional

import httpx

DEFAULT_PORTS = {"http": 80, "https": 443}

#: IPv6 blocks `is_global` passes that still reach an IPv4 host or a retired private scope: NAT64
#: (`64:ff9b::/96`, a NAT gateway's route to private v4), IPv4-compatible (`::/96`), SIIT
#: (`::ffff:0:0:0/96`) and site-local (`fec0::/10`). Refused outright.
_REFUSED_V6 = tuple(
    ipaddress.ip_network(block) for block in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96", "fec0::/10")
)


@functools.cache
def _verify_context() -> ssl.SSLContext:
    """The TLS context `httpx.Client(verify=True)` would build, built ONCE per process.

    httpx builds it per client, re-loading the whole CA bundle each time — ~3% of the api's CPU, since every
    AWS and vendor call here makes a fresh client. Same arguments as httpx's own default
    (`verify=True`, `trust_env=True`, so `SSL_CERT_FILE`/`SSL_CERT_DIR` still apply), so verification is
    unchanged; an `SSLContext` is safe to share across threads once built."""
    return httpx.create_ssl_context(verify=True, trust_env=True)


def client(timeout: float, headers: Optional[dict[str, str]] = None) -> httpx.Client:
    """A client carrying the policy above; `headers` ADD to `Accept-Encoding: identity`, never replace it."""
    merged = httpx.Headers(headers or {})
    merged["Accept-Encoding"] = "identity"  # case-insensitive: replaces any spelling a caller passed
    return httpx.Client(timeout=timeout, follow_redirects=False, headers=merged, verify=_verify_context())


def read_capped(
    response: httpx.Response, max_bytes: int, stop: Optional[Callable[[], bool]] = None
) -> tuple[bytes, bool]:
    """At most `max_bytes` of raw body, and whether the read stopped early.

    Stops once the accumulated body reaches `max_bytes` or when `stop()` (a caller's wall-clock deadline;
    a per-operation timeout never bounds a drip) returns True. A body of exactly `max_bytes` also reports True.
    """
    received = bytearray()
    for chunk in response.iter_raw():
        received += chunk
        if len(received) >= max_bytes or (stop is not None and stop()):
            return bytes(received[:max_bytes]), True
    return bytes(received), False


def retry_after_seconds(headers: Mapping[str, str], *, absent: float, floor: float, cap: float) -> float:
    """A `Retry-After` header as a sleep, clamped to `[floor, cap]`; `absent` (also clamped) when the header
    is missing or not a number (an HTTP-date). The cap keeps a hostile value from parking a worker; a
    non-zero `absent` keeps a rate limit from becoming a hot retry. `headers` must look up case-insensitively
    (`httpx.Headers`, or a dict already lower-cased: Slack has changed the header's casing before)."""
    raw = (headers.get("retry-after") or "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        seconds = absent
    return max(floor, min(seconds, cap))


def json_or_none(raw: bytes) -> Any:
    """`raw` as JSON, or None when unreadable. `RecursionError` is caught: CPython raises it on deeply
    nested input that fits any cap.
    """
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def origin(url: str) -> Optional[tuple[str, str, int]]:
    """`(scheme, host, port)` as httpx will dial them, or None when there is no comparable origin.

    Parsed with `httpx.URL`, the request's own parser: `urlsplit` disagrees on ports like `+443` and on
    IDNA hosts. The port is always concrete. None never compares equal to anything, including None.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, UnicodeError):
        return None
    scheme = parsed.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        return None
    try:
        host = parsed.raw_host.decode("ascii")  # IDNA-encoded, i.e. what the connection will use
        port = parsed.port
    except (UnicodeDecodeError, ValueError, AttributeError):
        return None
    if not host:
        return None
    return scheme, host, port if port is not None else DEFAULT_PORTS[scheme]


def public_destination_refusal(url: str) -> Optional[str]:
    """Why the control plane must not dial `url`, or None when it may.

    For a host the caller chose (a row's Token URL, a self-managed forge): the scheme must be `https`
    and EVERY address the host resolves to must be global — so loopback, RFC 1918, link-local and the
    169.254/16 metadata service, 100.64/10 (the tailnet's CGNAT range) and every other reserved block are
    refused, since dialing them would reach the control plane's own network. Same rule for every org.
    The sentence names the host, never a resolved address. Residual, accepted here: the dial resolves again
    (DNS rebinding between this check and the request); a caller that must close it dials an address
    `vetted_addresses` returns instead.
    """
    return vetted_addresses(url)[0]


def vetted_addresses(url: str) -> tuple[Optional[str], tuple[str, ...]]:
    """`public_destination_refusal`'s check, and on a pass every address it vetted, in resolver order:
    `(refusal, ())` or `(None, addresses)`. Dialing those (the URL's host kept as SNI and `Host`) rather than the
    name means a rebinding domain cannot swap in a private address after the check."""
    try:
        parsed = httpx.URL(url.strip())
        host = parsed.raw_host.decode("ascii")  # IDNA-encoded, what the connection will resolve
        port = parsed.port
    except (httpx.InvalidURL, ValueError, UnicodeError, AttributeError):
        return "the address could not be read", ()
    if not host:
        return "the address names no host", ()
    if parsed.scheme.lower() != "https":
        return f"{host} must be reached over https", ()
    try:
        infos = socket.getaddrinfo(host, port or DEFAULT_PORTS["https"], proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return f"{host} could not be resolved", ()
    if not infos:
        return f"{host} could not be resolved", ()
    for info in infos:
        # A scoped IPv6 literal (`fe80::1%eth0`) carries its zone after `%`; an IPv4-mapped IPv6 address is
        # judged as the IPv4 address it reaches.
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or (
            isinstance(address, ipaddress.IPv6Address) and any(address in block for block in _REFUSED_V6)
        ):
            return f"{host} resolves to a private or reserved address, which the control plane does not dial", ()
    return None, tuple(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))
