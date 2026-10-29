"""Canary for the OAuth token endpoint's request contract — the one thing no unit test can cover.

Every `collector._refresh_oauth_token` test mocks `urlopen`, so the real endpoint's contract is untested
by construction; a silent break blanks quota, which disqualifies accounts from dispatch. The probe POSTs
the refresh grant with a DELIBERATELY INVALID token and requires exactly `400 invalid_grant` (URL,
headers and grant shape all accepted). 403 = edge/bot block before the API; 404 = moved; 2xx or a
non-JSON body = not the endpoint we think. No credentials used, so it runs anywhere on a schedule and
catches the break before any account needs a refresh.

Constants are imported from `collector`, never restated: the canary must exercise the SAME url,
client_id, User-Agent and timeout the real refresh uses, or it proves nothing about it.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

import environment
from collector import (
    _OAUTH_CLIENT_ID,
    _OAUTH_TIMEOUT,
    _OAUTH_TOKEN_URL,
    _OAUTH_USER_AGENT,
)

log = logging.getLogger("jaahd.oauth_endpoint_check")

# Any syntactically plausible value the auth server cannot possibly know. Never a real token.
_JUNK_REFRESH_TOKEN = "sk-ant-ortr-canary-invalid"  # gitleaks:allow (deliberately invalid probe value)


class EndpointVerdict(NamedTuple):
    healthy: bool
    detail: str  # operator-facing; carries the status code and what it implies


def check_token_endpoint(urlopen=None) -> EndpointVerdict:
    """Probe the token endpoint with an invalid grant. Healthy iff it answers `400 invalid_grant`.

    `urlopen` is injectable for tests. Not probed in sandbox posture: unlike every other outward dial
    (derived from the CP, so absent in a sandbox), this vendor canary needs nothing and fires at startup,
    so it must be guarded explicitly. That returns `healthy=True` as a NON-VERDICT (logged at INFO, exit
    0, like `inconclusive`): a sandbox must not alert about a probe nobody ran."""
    if environment.is_sandbox():
        return EndpointVerdict(
            True, "not probed - sandbox posture, which dials nothing off this machine")
    urlopen = urlopen or urllib.request.urlopen
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": _JUNK_REFRESH_TOKEN,
        "client_id": _OAUTH_CLIENT_ID,
    }).encode()
    request = urllib.request.Request(
        _OAUTH_TOKEN_URL, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _OAUTH_USER_AGENT,
        },
        method="POST",
    )
    try:
        response = urlopen(request, timeout=_OAUTH_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            hint = {
                403: "refused before reaching the API (edge/bot block — check the User-Agent)",
                404: "endpoint moved or withdrawn",
            }.get(exc.code, "unexpected status")
            return EndpointVerdict(False, f"HTTP {exc.code} — {hint}")
        try:
            error = json.loads(exc.read()).get("error")
        except Exception:
            error = None
        if error == "invalid_grant":
            return EndpointVerdict(True, "HTTP 400 invalid_grant — request contract intact")
        # A 400 that is NOT invalid_grant means the endpoint rejected the SHAPE of our request (a renamed
        # grant type or client_id parameter), which a real refresh would hit too.
        return EndpointVerdict(False, f"HTTP 400 but error={error!r}, not invalid_grant — request rejected")
    except Exception as exc:
        # Network/DNS/timeout: not evidence the contract broke, so it is not a failure verdict — but it is
        # still reported so a permanently unreachable endpoint cannot hide behind "inconclusive" forever.
        return EndpointVerdict(True, f"inconclusive — {type(exc).__name__} (endpoint unreachable, not a contract break)")

    # A junk token must never be accepted.
    return EndpointVerdict(False, f"HTTP {getattr(response, 'status', '2xx')} for an INVALID token — not the real endpoint")


def main() -> int:
    """Exit 0 when the contract holds (or the probe was inconclusive), 1 when it is broken. Logs at WARNING
    on a break so it lands in the node log the same way the collector's own refresh failures do."""
    verdict = check_token_endpoint()
    if verdict.healthy:
        log.info("oauth token endpoint canary: %s", verdict.detail)
        return 0
    log.warning(
        "oauth token endpoint canary FAILED: %s. Token refresh is broken or about to be — every account "
        "whose access token expires will stop reporting quota, and unknown quota disqualifies it from "
        "dispatch. Fix %s / the request headers in collector.py.",
        verdict.detail, _OAUTH_TOKEN_URL,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    # The `Z` is part of the format string, so the clock has to be UTC.
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
