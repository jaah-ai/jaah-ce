"""The on-use revocation check for a git connection.

A token revoked at the forge otherwise surfaces only as a failed clone deep inside a runner. So before a
token is served, and when Git Access is listed, the forge's own connection tester (`credentials/testers.py`,
the one forge read the control plane allows itself, since no control-plane decision rests on Git content)
is run against it — at most once per credential per `CHECK_INTERVAL`, bounded at `CHECK_BUDGET_SECONDS`. Only
`forges.revoked` decides that an answer is a revocation; anything else (a 403, a 429, a timeout, an unreachable
host) is `UNKNOWN`, and the token is served as before. A revocation is recorded as `config.grant_dead_at`,
which `git_access` refuses on.

The last-check clock is per process: a restart or a second worker re-checks early, which costs one extra
tester call and nothing else. Nothing here logs a token.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Literal, Optional
from uuid import UUID

from app import forges, http_bounded
from app.clock import now_iso
from app.credential_crypto import SealedSecret
from app.credentials.testers import TESTERS, TestResult, unsupported_result

log = logging.getLogger("app.git_probe")

#: How long one successful (or inconclusive) check stands before the next use checks again.
CHECK_INTERVAL_SECONDS = 300.0
#: The bound on one check, and on the whole Git Access page's parallel checks.
CHECK_BUDGET_SECONDS = 5.0
#: How long after a token is stored a 401 is not believed. Atlassian answers 401 to a Bitbucket grant for
#: its first second or so (observed: the connect callback's own account read and the probe a second later
#: both 401, the same token 200 minutes after), and a token stored that recently cannot have been revoked.
FRESH_GRANT_SECONDS = 120.0

Verdict = Literal["alive", "revoked", "unknown"]
ALIVE: Verdict = "alive"
REVOKED: Verdict = "revoked"
UNKNOWN: Verdict = "unknown"

_checked_at: dict[UUID, float] = {}
_checked_lock = threading.Lock()


def is_stale(credential_id: UUID) -> bool:
    """Whether `credential_id` has gone unchecked for longer than `CHECK_INTERVAL_SECONDS` here."""
    with _checked_lock:
        last = _checked_at.get(credential_id)
    return last is None or monotonic() - last >= CHECK_INTERVAL_SECONDS


def claim_check(credential_id: UUID) -> bool:
    """Atomically: True (and the clock restarted) when `credential_id` is stale, so exactly one of many
    concurrent callers runs the check; False when it is fresh or another caller has just claimed it."""
    with _checked_lock:
        last = _checked_at.get(credential_id)
        if last is not None and monotonic() - last < CHECK_INTERVAL_SECONDS:
            return False
        _checked_at[credential_id] = monotonic()
    return True


def just_stored(updated_at: Optional[datetime]) -> bool:
    """Whether a credential row last written at `updated_at` holds a token too new for a `REVOKED` verdict
    to be recorded (`FRESH_GRANT_SECONDS`). A connect or a renewal is a write, so it restarts the window."""
    if updated_at is None:
        return False
    return (datetime.now(timezone.utc) - updated_at).total_seconds() < FRESH_GRANT_SECONDS


def forget(credential_id: UUID) -> None:
    """Drop the clock for `credential_id`, so its next use checks at once (a new token was stored)."""
    with _checked_lock:
        _checked_at.pop(credential_id, None)


def tester_config(config: dict[str, Any], token: str) -> dict[str, Any]:
    """The config a tester is given: the row's non-secret fields plus the opened `token`. Other sealed
    fields (a refresh token) are left out — no tester sends them."""
    plain = {key: value for key, value in config.items() if not isinstance(value, SealedSecret)}
    plain["token"] = token
    return plain


def private_host_problem(root: str) -> Optional[str]:
    """Why the control plane must not dial a self-managed GitLab at `root`, or None when it may.

    The one self-managed forge has an operator-chosen host, and a connection test is an outbound call
    FROM the control plane, so the destination rule (`http_bounded.public_destination_refusal`) applies.
    A fixed forge host is never refused."""
    name = root.split("/", 1)[0].partition(":")[0]
    if name in forges.KNOWN_HOSTS:
        return None
    return http_bounded.public_destination_refusal(f"https://{root}")


def _run_tester(credential_type: str, config: dict[str, Any], deadline: float) -> TestResult:
    """The forge's connection tester, or the neutral result for a type with none. The one seam tests
    replace (no test may reach a real forge)."""
    tester = TESTERS.get(credential_type)
    if tester is None:
        return unsupported_result(credential_type)
    return tester(config, deadline)


def verdict_of(forge: str, result: TestResult) -> Verdict:
    """`ALIVE` for a green test, `REVOKED` only when `forges.revoked` says the answer is one, else
    `UNKNOWN`."""
    if result.ok:
        return ALIVE
    http = result.http
    if http is not None and forges.revoked(forge, http.status_code, http.body, http.headers):
        return REVOKED
    return UNKNOWN


def probe(credential_type: str, root: str, config: dict[str, Any], *, deadline: float) -> Verdict:
    """Test one token (already in `config`, see `tester_config`) against its forge. Never raises."""
    forge = forges.forge_of_root(root)
    if forge is None:
        return UNKNOWN
    try:
        if forge == "gitlab" and root != "gitlab.com" and private_host_problem(root) is not None:
            return UNKNOWN
        return verdict_of(forge, _run_tester(credential_type, config, deadline))
    except Exception:  # noqa: BLE001 - a check that cannot run must never stop the token being served
        log.warning("git-probe: the %s check could not run", forge, exc_info=True)
        return UNKNOWN


def new_deadline() -> float:
    return monotonic() + CHECK_BUDGET_SECONDS


#: `mark_revoked`'s default reason: the forge said so (`probe` answered `REVOKED`).
REASON_FORGE = "was reported revoked by its forge"


def mark_revoked(credential_id: UUID, *, reason: str = REASON_FORGE) -> None:
    """Record `grant_dead_at` on the credential, on its own session bound from the row (the caller's
    session may be scoped to a node, not the credential's org). A row already marked keeps its time.
    `reason` completes the log line ("credential id=... <reason>"); a fixed phrase, never forge text.

    ENTITLEMENT for the `detached()` + `bind_from` pairing (`seam.detached`): `credential_id` is only
    ever a row the caller itself resolved through a scoped read — `git_access.resolve_*` for the token it
    is about to serve, or the person's own Git Access listing — and the one write is a timestamp that
    stops that token being served; nothing is read out."""
    from app.db import SessionLocal
    from app.models.credentials import Credential
    from app.tenancy import exclusion_log, seam

    with exclusion_log.caller("git_probe.mark_revoked"):
        try:
            with seam.detached(), SessionLocal() as session, seam.bind_from(session, Credential, credential_id):
                row = session.get(Credential, credential_id, with_for_update=True)
                if row is None:
                    return
                config = dict(row.config or {})
                if not config.get("grant_dead_at"):
                    config["grant_dead_at"] = now_iso()
                    row.config = config
                    session.commit()
        except seam.TenancyRowMissing:
            return
    log.warning("git-probe: credential id=%s %s", credential_id, reason)
