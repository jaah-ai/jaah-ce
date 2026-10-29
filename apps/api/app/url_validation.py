"""Decides whether a URL an agent attested is safe to store (`tasks.outcome_pr_url`, a compliance
group's `pr_url`). These render as clickable links in a superadmin session, so a stored
`javascript:` would own it.

Complements the console's `safeHref` rather than replacing it, and is stricter (refuses userinfo), so
a stored row may still render no link. Pure; raises `ValueError` so a pydantic `field_validator`
yields a 422. Bind only to request models: on a response model a pre-validator row would 500. Not for
`projects.repo_url`, which uses the scheme denylist in `routes/projects.py::_validated_repo_url`.
`https_url_problem` is the same check narrowed to `https`, for a link an operator setting publishes to
signed-out visitors and emails (`app/operator_settings/knobs.py::Knob.https_url`)."""
from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # annotations only: stdlib-only importers (app/operator_settings/) need no pydantic
    from pydantic import ValidationInfo

__all__ = ["validated_http_url", "https_url_problem"]

# An allowlist, since a denylist misses whatever scheme a browser learns next. `http` stays because
# a forge reachable only over the tailnet is a legitimate link.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Space, C0 controls and DEL are refused before parsing: `urlsplit` (like a browser) strips `\t\r\n`
# anywhere, so `java\tscript:` reads as `javascript:`. Refusing rather than normalizing keeps the
# stored value byte-identical to the checked one.
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f]")


def validated_http_url(value: Optional[str], info: ValidationInfo) -> Optional[str]:
    """`value` unchanged when it is an absolute `http(s)` URL or carries no URL at all; else `ValueError`.

    `None` and `""` pass through: these columns are nullable, and fields that must not be blank use
    their own `min_length`.
    """
    if not value:
        return value
    field = info.field_name or "url"
    if _FORBIDDEN_CHARS.search(value):
        raise ValueError(
            f"{field} contains whitespace or a control character; a URL that needs stripping before it "
            f"parses is a scheme-smuggling attempt, not a PR link"
        )
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        # A URL `urlsplit` refuses (malformed IPv6 literal) must be a 422, not a 500.
        raise ValueError(f"{field} is not a parsable URL ({exc})") from exc
    # Lowered again so a future `urlsplit` change cannot admit `JavaScript:`. A non-empty `netloc`
    # refuses scheme-relative `//evil.example` and relative paths, which resolve to the console origin.
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
        # Never echo the value: this message reaches the node daemon's journal via
        # `cred_broker._rejected_reason`, where it would be attacker-chosen text.
        raise ValueError(
            f"{field} must be an absolute http:// or https:// URL; it is rendered as a "
            f"clickable link in an authenticated operator session"
        )
    return value


def https_url_problem(value: str) -> Optional[str]:
    """Why `value` is not an absolute `https` URL — a reader-facing phrase — or None when it is one.

    Stricter than `validated_http_url`: `http` is refused, and so is anything that could make a link
    shown to signed-out visitors and in emails open somewhere other than it reads — a user name or `@`
    (`https://jaah.ai@evil.example`), a backslash (browsers read it as `/`), a missing host, or a non-ASCII
    character (look-alike hosts, bidi overrides; an international domain is entered in its `xn--` form).
    Never echoes the value."""
    if not value:
        return "is required"
    if _FORBIDDEN_CHARS.search(value) or not value.isascii() or "\\" in value:
        return "must be a plain web address: no spaces, backslashes or accented letters"
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port  # noqa: B018 — raises on a malformed port
    except ValueError:
        return "is not a valid web address"
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return "must start with https:// and be a full web address"
    if "@" in parsed.netloc:
        return "must not contain a user name or @ before the domain"
    return None
