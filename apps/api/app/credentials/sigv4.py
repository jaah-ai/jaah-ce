"""AWS Signature Version 4 request signing — pure `hmac`/`hashlib`, no I/O, no clock.

Stdlib, not botocore: `requirements.txt` ships into the runtime image. One helper per SigV4 step.

`host` is derived from `url` and `x-amz-date` is the `amz_date` argument, never a clock. The signed
header set is frozen at `content-type;host;x-amz-date`, plus `x-amz-security-token` when the caller signs
with temporary (STS) credentials, whose session token AWS refuses the request without. Any input that would
make the signed bytes differ from the sent bytes raises `ValueError` naming the parameter.

Caller contract: send the returned mapping verbatim, add no `Host`, pass the same `payload` bytes, and
pin the host — `url` must be a constant no operator input can reach, or a valid signature and real key
id go to a host of the operator's choosing.
"""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import SplitResult, quote, urlsplit

# Names itself in both the string-to-sign and the `Authorization` header.
ALGORITHM = "AWS4-HMAC-SHA256"

# The fixed signed header set: sorted, lowercase, matching `canonical_request`'s headers block.
CANONICAL_SIGNED_HEADERS = "content-type;host;x-amz-date"
# The same set when a session token rides the request (temporary credentials).
SESSION_SIGNED_HEADERS = CANONICAL_SIGNED_HEADERS + ";x-amz-security-token"

# httpx elides `:port` from `Host` for exactly these defaults.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Spelled out rather than imported, to keep the import set I/O- and clock-free (asserted by a test).
_UPPERCASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWERCASE = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"

# Narrower than "sendable in a header": a `,` or `=` would restructure the `Authorization` header, and
# a `/` in region would add a credential-scope field.
_ACCESS_KEY_ID_CHARACTERS = frozenset(_UPPERCASE + _LOWERCASE + _DIGITS)
_METHOD_CHARACTERS = frozenset(_UPPERCASE)
_SCOPE_CHARACTERS = frozenset(_LOWERCASE + _DIGITS + "-")
_SCOPE_RULE = "lowercase letters, digits and hyphens"

# Characters httpx rewrites in a path or query (measured against `httpx.URL(...).raw_path`). Path and
# query must arrive in wire form or the signature covers bytes the wire does not carry.
_REWRITTEN_BY_THE_TRANSPORT = frozenset(" <>")

# The authority set differs: httpx encodes `^` in a host but not a path. A test re-derives both.
_REWRITTEN_IN_THE_AUTHORITY = frozenset(" <>^")


def _reject_unsendable(name: str, value: str) -> None:
    """Refuse any argument that cannot travel verbatim in a header, naming the field, never the value.

    Printable ASCII only: a `\\r` or `\\n` in `content_type` would forge an extra canonical-header
    line, and a non-ASCII byte would be sent encoded differently from how it was signed.
    """
    if not value:
        raise ValueError(f"{name} is required and must not be empty")
    if any(character < " " or character > "~" for character in value):
        raise ValueError(f"{name} contains characters that cannot be sent in a request")
    if value != " ".join(value.split()):
        # AWS trims and collapses spaces before recomputing, so an untrimmed value would sign a
        # different document. Refused, not normalised.
        raise ValueError(f"{name} must not have leading, trailing or repeated spaces")


def _is_not_wire_form(value: str, rewritten: frozenset = _REWRITTEN_BY_THE_TRANSPORT) -> bool:
    """True when httpx would rewrite this url component instead of sending it as given."""
    return any(
        character < "!" or character > "~" or character in rewritten for character in value
    )


def _reject_outside(name: str, value: str, allowed: frozenset, rule: str) -> None:
    """Refuse an argument drawing on any character outside the set its position permits.

    `rule` states that set in words, so the operator reads what is wrong rather than a charset dump.
    """
    if not value:
        raise ValueError(f"{name} is required and must not be empty")
    if not allowed.issuperset(value):
        raise ValueError(f"{name} may contain only {rule}")


def _reject_bad_amz_date(amz_date: str) -> None:
    """`amz_date` must be exactly an ISO-8601 basic UTC timestamp: `20150830T123600Z`.

    A malformed one silently scopes the signature to a wrong date. Field ranges are checked too, done
    arithmetically to keep the import set clock-free; day-of-month is bounded at 31, not per month.
    """
    digits = amz_date[:8] + amz_date[9:15]
    if (
        len(amz_date) != 16
        or amz_date[8] != "T"
        or amz_date[15] != "Z"
        or any(character not in _DIGITS for character in digits)
    ):
        raise ValueError("amz_date must be an ISO-8601 basic UTC timestamp, e.g. 20150830T123600Z")
    month, day = int(amz_date[4:6]), int(amz_date[6:8])
    hour, minute, second = int(amz_date[9:11]), int(amz_date[11:13]), int(amz_date[13:15])
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 60):
        raise ValueError("amz_date must be a real UTC timestamp, e.g. 20150830T123600Z")


def _signing_url(url: str) -> SplitResult:
    """Parse `url`, refusing everything this signer cannot sign faithfully.

    Each rejection is a case where signing would otherwise succeed over bytes httpx will not send.
    """
    # `urlsplit` errors quote the netloc (userinfo included), so one guard around the whole parse
    # re-raises without the value — outside the `except`, so `__context__` does not keep it either.
    if any(character in url for character in "\t\r\n"):
        # `urlsplit` silently strips these, so `https://sts.amazonaws.com\r\n/` would sign that host.
        raise ValueError("url must not contain control characters")
    try:
        parts = urlsplit(url)
        userinfo = parts.username or parts.password
        host = parts.hostname
        _ = parts.port  # forces the lazy port validation inside the guard, where it cannot leak
    except ValueError:
        failed_to_parse = True
    else:
        failed_to_parse = False
    if failed_to_parse:
        raise ValueError("url could not be parsed")
    if parts.scheme not in _DEFAULT_PORTS:
        raise ValueError("url must be an http(s) URL, with the scheme included")
    if userinfo:
        # Unsignable, and httpx would turn it into a Basic `Authorization` replacing the signature.
        raise ValueError("url must not carry a credential in its userinfo")
    if not host:
        raise ValueError("url has no host")
    if not host.isascii():
        # httpx sends it punycoded; no AWS endpoint is internationalised.
        raise ValueError("url host must be ASCII")
    if _is_not_wire_form(host, _REWRITTEN_IN_THE_AUTHORITY):
        # httpx percent-encodes these in `Host`.
        raise ValueError("url host contains characters that cannot be sent in a Host header")
    if _is_not_wire_form(parts.query):
        # `canonical_query_string` reorders but never rewrites, so it must arrive in wire form.
        raise ValueError("url query string must already be percent-encoded")
    if _is_not_wire_form(parts.path):
        # `canonical_uri` does double-encode (correct for non-S3 services), but the input must still be
        # in the form httpx sends.
        raise ValueError("url path must already be percent-encoded")
    if any(segment in (".", "..") for segment in parts.path.split("/")) or "//" in parts.path:
        # AWS normalises dot segments and collapses slashes; this signer does neither, so refuse rather
        # than sign the wrong resource. `//` is checked directly so `/` and a trailing `/` stay legal.
        raise ValueError("url path must not contain a ., .. or empty segment")
    return parts


def _host_header(parts: SplitResult) -> str:
    """`host_header` over an ALREADY-VALIDATED parse — the form the two internal callers use."""
    host = parts.hostname or ""
    if ":" in host:  # an IPv6 literal — `hostname` strips the brackets the header needs
        host = f"[{host}]"
        # `.hostname` lowercases but httpx keeps the case it was given, so a mixed-case IPv6 literal
        # would sign one host and send another: refused.
        bracketed = parts.netloc.rpartition("@")[2]
        if bracketed[: bracketed.find("]") + 1] != host:
            raise ValueError("url IPv6 host must be lowercase")
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(parts.scheme):
        return host
    return f"{host}:{port}"


def host_header(url: str) -> str:
    """The `Host` header httpx will send for `url` — the value this module signs.

    Derived, never supplied. Userinfo is excluded (it is not part of `Host`), the host is lowercased,
    an IPv6 literal keeps its brackets, and the port appears only when it is not the scheme's default.
    """
    return _host_header(_signing_url(url))


def _canonical_uri(parts: SplitResult) -> str:
    """The canonical URI: the path, percent-encoded per AWS's `UriEncode` — `/` left alone.

    A non-S3 service re-encodes the received request-target, so an encoded path is encoded twice
    (`/a%20b` → `/a%2520b`, as botocore does). An empty path is `/`.
    """
    return quote(parts.path or "/", safe="/")


def _canonical_query_string(parts: SplitResult) -> str:
    """The canonical query string: the query's own parameters, REORDERED and never rewritten.

    AWS sorts by name then value; a valueless `?flag` becomes `flag=`. Decoding and re-encoding would
    change what httpx sends (`+`, `%7E`), so raw reordering relies on `_signing_url`'s precondition.
    """
    if not parts.query:
        return ""
    pairs = []
    for pair in parts.query.split("&"):
        name, _, value = pair.partition("=")
        pairs.append((name, value))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def canonical_request(
    *,
    method: str,
    url: str,
    payload: bytes,
    content_type: str,
    amz_date: str,
    session_token: str | None = None,
) -> str:
    """Step 1: the canonical request — the exact document whose hash the signature commits to.

    Six lines in this order: method, canonical URI, canonical query string, the canonical-headers
    block (itself newline-terminated, hence the blank line the join produces), the signed-header
    list, and the payload hash. `session_token` (temporary credentials only) is signed as
    `x-amz-security-token`; `None` signs exactly the three fixed headers.
    """
    _reject_unsendable("method", method)
    if method != method.upper():
        # httpx uppercases the sent method.
        raise ValueError("method must be uppercase")
        # Otherwise refused later by h11 with a message naming neither this parameter nor module.
    _reject_outside("method", method, _METHOD_CHARACTERS, "uppercase letters")
    _reject_unsendable("content_type", content_type)
    _reject_bad_amz_date(amz_date)
    if not isinstance(payload, bytes):
        raise ValueError("payload must be the exact bytes the request will send")
    parts = _signing_url(url)
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{_host_header(parts)}\n"
        f"x-amz-date:{amz_date}\n"
    )
    if session_token is not None:
        _reject_bad_session_token(session_token)
        canonical_headers += f"x-amz-security-token:{session_token}\n"
    return "\n".join(
        (
            method,
            _canonical_uri(parts),
            _canonical_query_string(parts),
            canonical_headers,
            _signed_header_list(session_token),
            hashlib.sha256(payload).hexdigest(),
        )
    )


def _reject_bad_session_token(session_token: str) -> None:
    """A session token travels verbatim in a header: printable ASCII with no space (AWS issues base64)."""
    _reject_unsendable("session_token", session_token)
    if " " in session_token:
        raise ValueError("session_token contains characters that cannot be sent in a request")


def _signed_header_list(session_token: str | None) -> str:
    return CANONICAL_SIGNED_HEADERS if session_token is None else SESSION_SIGNED_HEADERS


def _credential_scope(*, amz_date: str, region: str, service: str) -> str:
    """The scope a signature is valid in: date (not datetime), region, service, terminator."""
    _reject_bad_amz_date(amz_date)
    _reject_outside("region", region, _SCOPE_CHARACTERS, _SCOPE_RULE)
    _reject_outside("service", service, _SCOPE_CHARACTERS, _SCOPE_RULE)
    return f"{amz_date[:8]}/{region}/{service}/aws4_request"


def string_to_sign(*, request: str, amz_date: str, region: str, service: str) -> str:
    """Step 2: algorithm, timestamp, credential scope, and the canonical request's hash."""
    return "\n".join(
        (
            ALGORITHM,
            amz_date,
            _credential_scope(amz_date=amz_date, region=region, service=service),
            hashlib.sha256(request.encode("utf-8")).hexdigest(),
        )
    )


def signing_key(*, secret_access_key: str, amz_date: str, region: str, service: str) -> bytes:
    """Step 3: the scoped signing key — HMAC-SHA256 chained over date, region, service, terminator.

    Seeded once by the secret key; a key for one date/region/service cannot sign for another.
    """
    # Checked before `.encode()`: a `UnicodeEncodeError` would carry the whole secret in its args.
    if not secret_access_key or not secret_access_key.isascii():
        raise ValueError("secret_access_key is required and must be ASCII")
    _reject_bad_amz_date(amz_date)
    _reject_outside("region", region, _SCOPE_CHARACTERS, _SCOPE_RULE)
    _reject_outside("service", service, _SCOPE_CHARACTERS, _SCOPE_RULE)
    key = b"AWS4" + secret_access_key.encode("ascii")
    for element in (amz_date[:8], region, service, "aws4_request"):
        key = hmac.new(key, element.encode("utf-8"), hashlib.sha256).digest()
    return key


def signed_headers(
    *,
    method: str,
    url: str,
    region: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
    payload: bytes,
    content_type: str,
    amz_date: str,
    session_token: str | None = None,
) -> dict[str, str]:
    """Sign one request and return the COMPLETE header mapping to send with it.

    Steps 1-4 composed. See the module docstring for the caller contract. Any input this signer cannot
    sign faithfully raises `ValueError`. `session_token` is the temporary credentials' token, or `None`
    for a long-lived key; set, it is signed and sent as `X-Amz-Security-Token`.
    """
    _reject_outside(
        "access_key_id", access_key_id, _ACCESS_KEY_ID_CHARACTERS, "letters and digits"
    )
    request = canonical_request(
        method=method, url=url, payload=payload, content_type=content_type, amz_date=amz_date,
        session_token=session_token,
    )
    to_sign = string_to_sign(request=request, amz_date=amz_date, region=region, service=service)
    key = signing_key(
        secret_access_key=secret_access_key, amz_date=amz_date, region=region, service=service
    )
    # Step 4: the signature itself.
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    scope = _credential_scope(amz_date=amz_date, region=region, service=service)
    headers = {
        "Content-Type": content_type,
        "X-Amz-Date": amz_date,
        "Authorization": (
            f"{ALGORITHM} Credential={access_key_id}/{scope}, "
            f"SignedHeaders={_signed_header_list(session_token)}, Signature={signature}"
        ),
    }
    if session_token is not None:
        headers["X-Amz-Security-Token"] = session_token
    return headers
