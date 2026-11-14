"""A third party's JWT signing keys, cached: the key lookup behind a public route that verifies a signer's token
(`app/ci_oidc.py`: GitHub Actions OIDC; `app/trackers/forge_fit.py`: Atlassian Forge)."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

HTTP_TIMEOUT_SECONDS = 5.0
# At most one fetch of a signer's keys per this many seconds: the routes are public, and a forged token naming an
# unknown `kid` would otherwise buy one outbound fetch each.
JWKS_REFRESH_SECONDS = 60.0


@dataclass(eq=False)
class SigningKeys:
    """`signer`'s key set at `url`, for tokens whose `alg` is one of `algorithms` and whose `kid` is at most
    `kid_max` long; `label` and `log` are the owning module's, for the one warning."""

    url: str
    signer: str
    label: str
    algorithms: tuple[str, ...]
    kid_max: int
    log: logging.Logger
    keys: Optional[dict[str, Any]] = None
    fetched_at: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def key_for(self, token: str) -> Any:
        """The signer's key for `token`'s `kid`, off a key set fetched at most once per `JWKS_REFRESH_SECONDS`.

        A `kid` absent from the current set is refused, never fetched for; the set is refetched once it has aged
        past the interval, which is how a key the signer rotated in is picked up. A failed refetch keeps the last set."""
        import jwt

        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") not in self.algorithms or not isinstance(kid, str) or not 0 < len(kid) <= self.kid_max:
            raise jwt.InvalidTokenError("bad token header")
        with self.lock:
            now = time.monotonic()
            # The interval binds a cold start too: with the signer unreachable, one fetch per interval, never one per
            # request (each would hold this lock and a worker thread for up to the timeout).
            if self.fetched_at is None or now - self.fetched_at >= JWKS_REFRESH_SECONDS:
                self.fetched_at = now
                client = jwt.PyJWKClient(self.url, cache_jwk_set=False, timeout=HTTP_TIMEOUT_SECONDS)
                try:
                    self.keys = {key.key_id: key for key in client.get_signing_keys()}
                except Exception as exc:  # noqa: BLE001 — a non-JSON body escapes PyJWKClientError; either way, no keys
                    self.log.warning("%s: %s's signing keys could not be fetched (%s) — keeping the last set",
                                     self.label, self.signer, type(exc).__name__)
            if self.keys is None:
                raise jwt.InvalidTokenError(f"{self.signer}'s signing keys are unavailable")
            key = self.keys.get(kid)
        if key is None:
            raise jwt.InvalidTokenError("unknown signing key")
        return key
