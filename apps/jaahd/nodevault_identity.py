"""Register this node's NodeVault identity with the control plane (architecture §3.1, §4.2).

On first start of a NodeVault-aware daemon the keystore mints the node identity key (NIK); this step then
posts its public half and the node's capability to `POST /api/agent/nodevault/identity`
`{public_key, capable}` until the control plane acknowledges it — a 200 whose `fingerprint` (CP-computed)
equals the one this node derives. Then it stops for the life of the process; every restart posts once
more, which the CP answers idempotently for the same key.

FAILURE NEVER STOPS THE DAEMON. A CP without the route yet (404), a 5xx, a timeout, a 409 (another key is
registered for this node — only an operator reset clears it) or a fingerprint that does not match: each is
logged and retried with exponential backoff (`RETRY_BASE` doubling to `RETRY_CAP`). The step is a
`PeriodicStep` in `main.py`, so it is single-flight and an exception inside it is logged, never fatal.

`capable` (`node_capable()`) is the vendored crypto probe AND a uid boundary between the daemon and its
runners: on a one-uid node the runner IS the daemon uid and could read every key, so it is never capable —
except the sandbox posture (the local dev node, one tenant by construction, `docker-compose.yml` § node).
A node that is not capable mints no NIK and reports `{public_key: null, capable: false}` so the console can
say why it gets no key. Only the PUBLIC key ever leaves this module.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
from typing import Callable

import control_plane_api
import environment
import nodevault_crypto
import nodevault_store
import session_user

log = logging.getLogger("jaahd.nodevault_identity")

IDENTITY_PATH = "/api/agent/nodevault/identity"
# The step's tick; the backoff below decides whether a tick actually posts.
INTERVAL_SECONDS = 30.0
RETRY_BASE = 30.0
RETRY_CAP = 3600.0


def node_capable() -> bool:
    """Whether this node may hold NodeVault keys: the crypto works here AND the runner uid cannot read the
    daemon's keystore (or this is the one-tenant sandbox) — the same rule on every OS: the keystore's privacy is
    `nodevault_private`'s, mode bits or an access list. The CLI's `capable` is this same answer."""
    return nodevault_crypto.probe() and (session_user.switch_needed() or environment.is_sandbox())


def post_identity(public_key: str | None, capable: bool) -> dict:
    """One POST. Raises `urllib.error.HTTPError` on a refusal (every status the registrar retries is declared
    expected, so it logs at DEBUG here and once at the caller) and any transport error as-is."""
    return control_plane_api._agent_api_post(
        IDENTITY_PATH, {"public_key": public_key, "capable": capable}, expected=(401, 403, 404, 409, 422, 429, 500, 502, 503, 504))


class IdentityRegistrar:
    """One `PeriodicStep`: ensure the NIK exists, then post it until acknowledged, backing off on failure."""

    def __init__(
        self,
        *,
        keystore: nodevault_store.Keystore | None = None,
        post: Callable[[str | None, bool], dict] = post_identity,
        probe: Callable[[], bool] = node_capable,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keystore = keystore
        self._post = post
        self._probe = probe
        self._clock = clock
        self._capable: bool | None = None
        self.acknowledged = False
        self.failures = 0
        self._next_attempt = 0.0

    def _backoff(self, now: float) -> float:
        self.failures += 1
        delay = min(RETRY_BASE * (2 ** (self.failures - 1)), RETRY_CAP)
        self._next_attempt = now + delay
        return delay

    def pass_once(self) -> bool:
        """One pass, synchronously. True when this pass got the acknowledgement."""
        if self.acknowledged:
            return False
        now = self._clock()
        if now < self._next_attempt:
            return False
        if self._capable is None:
            self._capable = bool(self._probe())
        fingerprint: str | None = None
        public_key: str | None = None
        if self._capable:
            try:
                keystore = self._keystore or nodevault_store.Keystore()
                nik, _created = keystore.ensure_identity()
                fingerprint = nodevault_crypto.key_hash(nik)
                public_key = nodevault_crypto.public_key_b64(nik)
            except (nodevault_store.KeystoreError, nodevault_crypto.NodeVaultCryptoError, OSError) as exc:
                delay = self._backoff(now)
                log.error("nodevault: cannot open this node's identity key (%s); retrying in %.0fs", exc, delay)
                return False
        try:
            answer = self._post(public_key, self._capable)
        except urllib.error.HTTPError as exc:
            delay = self._backoff(now)
            if exc.code == 409:
                log.error("nodevault: the control plane holds a DIFFERENT identity key for this node (409); an "
                          "operator must reset this node's NodeVault identity. Retrying in %.0fs", delay)
            else:
                log.warning("nodevault: identity registration refused with HTTP %s; retrying in %.0fs",
                            exc.code, delay)
            return False
        except Exception as exc:  # noqa: BLE001 — every transport failure is one outcome: try again later
            delay = self._backoff(now)
            log.warning("nodevault: identity registration failed (%s); retrying in %.0fs",
                        type(exc).__name__, delay)
            return False
        returned = answer.get("fingerprint") if isinstance(answer, dict) else None
        if fingerprint is not None and returned != fingerprint:
            delay = self._backoff(now)
            log.error("nodevault: the control plane acknowledged fingerprint %r, not this node's %s; "
                      "retrying in %.0fs", returned, fingerprint, delay)
            return False
        self.acknowledged = True
        self.failures = 0
        log.info("nodevault: identity registered (fingerprint %s, capable=%s)", fingerprint, self._capable)
        return True

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)
