"""The asymmetric key that wraps every credential secret's data key — where it lives, and the one seam
that unwraps.

`app/credential_crypto.py` seals each `kind == "secret"` field of `credentials.config` as its own hybrid
envelope: a random AES-256-GCM data key encrypts the value, and the data key is wrapped under an RSA-4096
public key with RSAES-OAEP-SHA-256. Wrapping needs only the PUBLIC half, so the browser (WebCrypto,
`apps/web/src/lib/credentialSeal.ts`) and every server-side writer encrypt without a KMS call; unwrapping
needs the PRIVATE half, which off a local stack lives only in KMS (`app/aws/kms.py`, `kms:Decrypt`).

Two providers behind one shape (`SecretKeyProvider`):
  * `KmsKeyProvider` — `CREDENTIAL_KMS_KEY_ID` names a KMS key ARN; the public key is fetched once per
    process (`kms:GetPublicKey`) and each unwrap is one `Decrypt`.
  * `LocalKeyProvider` — an RSA private key in-process, for `./dc`, the test suites and the community edition
    ONLY: a key in `CREDENTIAL_LOCAL_PRIVATE_KEY` is accepted solely on a process that declares itself
    throwaway (`dev_bypass.is_fixture_env`: `local`, `test`, `ci`) or is the community edition (`edition.IS_CE`,
    whose install generates its own keypair, `app/ce_bootstrap.py::ensure_secrets`), and the PUBLISHED dev
    keypair below stands in when nothing is set on an affirmatively local cloud-tree process — never in the
    community edition. There is no non-KMS cloud deployment mode: every other `APP_ENV` needs
    `CREDENTIAL_KMS_KEY_ID`, or the process refuses to start.

ROTATION (an asymmetric KMS key cannot auto-rotate): a NEW key becomes `CREDENTIAL_KMS_KEY_ID`, every
key it replaces is listed in `CREDENTIAL_KMS_PREVIOUS_KEY_IDS` — UNWRAP ONLY, never wrapped under — and
`python -m app.credential_reseal` (`app/credential_reseal.py`) rewraps every stored data key under the new
one. An envelope names its key (`k`), so `provider_for` picks the key that opens it; nothing is guessed.
The procedure, and when a previous key may be retired: `app/credential_reseal.py`.

A THIRD provider has no private half here at all: `NodeVaultPublicKeyProvider` (an envelope under an
org's NodeVault key, `k = "nv:<key hash>"`). It wraps, so the CP can seal a migrated secret, and its unwrap
raises `NodeVaultSealed`: the private key lives only on the org's nodes, never in this process or its DB.
`provider_for` reaches it only when handed the org (`session`, `org_id`), off that org's `nodevault_keys` row.

`unwrap_data_key` is the only path to a provider's private operation, and it goes through
`_DataKeyCache`: a short-TTL, bounded, in-process map from wrapped bytes to the data key, so a hot path
(a runner fetching the same credential repeatedly) pays KMS latency once per envelope per TTL. The cache
holds key material and is never logged, pickled or rendered; `repr` shows a count.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional, Protocol

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import edition, edition_features
from app.dev_bypass import is_fixture_env, is_local_env

# Pinned literals: spelled by the deployment's terraform (the KMS ARN) and the local compose stack;
# a rename silently unconfigures the key.
KMS_KEY_ENV = "CREDENTIAL_KMS_KEY_ID"
LOCAL_KEY_ENV = "CREDENTIAL_LOCAL_PRIVATE_KEY"
# The keys a rotation retires: KMS ARNs, comma- or whitespace-separated, unwrap only.
PREVIOUS_KMS_KEYS_ENV = "CREDENTIAL_KMS_PREVIOUS_KEY_IDS"

# The wrapping algorithm, one spelling for the local provider, the KMS request and the envelope's
# `alg`: OAEP with SHA-256 as both the hash and the MGF1 hash, no label.
WRAP_ALGORITHM = "RSA-OAEP-256"
_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)

# AES-256 data keys.
DATA_KEY_BYTES = 32

# The unwrapped-data-key cache: ~5 minutes, bounded. A longer TTL widens the window in which a
# revoked KMS grant still opens already-cached envelopes; a larger size holds more key material in RAM.
CACHE_TTL_SECONDS = 300.0
CACHE_MAX_ENTRIES = 1024

# The local-dev keypair this repository once published (like `credential_crypto._DEV_KEY_SEED`): RSA-4096,
# PKCS#8 DER, base64. NOT A SECRET — it is in the repo — which is exactly why it is refused as the configured
# key everywhere. A local stack keeps it as an unwrap-only previous key (`resolve_previous_key_providers`),
# so secrets wrapped under it before `./dc` generated the stack's own keypair still open.
_DEV_LOCAL_PRIVATE_KEY_B64 = (
    "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"
)


class WrappingKeyUnavailable(RuntimeError):
    """The deployment's wrapping key could not be used: KMS unreachable, or it refused the call (IAM, a
    disabled key). An OUTAGE, distinct from a `ValueError` (a bad envelope): a route answers 503, a
    background loop skips and retries. The message carries the class and AWS's error name only."""


class NodeVaultSealed(ValueError):
    """A NodeVault envelope reached a private operation on the control plane, which holds no NodeVault key
    and so can never open one (AC-8.1). A `ValueError`, so every consumer that already degrades on a secret
    it cannot open degrades here too; `main.py` answers a route that lets it escape with a 409, never a 500.
    The message names the field at most, never a value."""


class NodeVaultUnavailable(NodeVaultSealed):
    """A control-plane CONSUMER reached a NodeVault credential (D1, AC-5.3): raised where a secret is opened
    for use (`credential_crypto.open_sealed`) and by `credential_consumers.refuse_if_nodevault` when a
    consumer is pointed at one. `consumer` is the registry label of what was refused, or None at an open
    that cannot name it; `main.py` answers either with a 409 reading "NodeVault credential — not available
    to Jaah", prefixed by the label when there is one."""

    def __init__(self, message: str = "NodeVault credential — not available to Jaah", *,
                 consumer: Optional[str] = None) -> None:
        super().__init__(message)
        self.consumer = consumer


class SecretKeyProvider(Protocol):
    """What the envelope layer needs from a key: an id to stamp, a public key to wrap with, and one
    private operation. Both providers count their unwraps so a test can assert a path made none."""

    key_id: str
    unwrap_count: int

    def public_key_der(self) -> bytes:
        """The DER SubjectPublicKeyInfo (what WebCrypto imports as `spki`)."""
        ...

    def unwrap(self, wrapped: bytes) -> bytes:
        """The private operation: `wrapped` (one OAEP block) back to the data key. Not cached here."""
        ...


def _fingerprint(spki: bytes) -> str:
    return hashlib.sha256(spki).hexdigest()[:16]


class LocalKeyProvider:
    """An RSA private key held in-process. `key_id` is `local:<spki fingerprint>` so an envelope written
    under one local key names it and is refused under another."""

    def __init__(self, private_key: rsa.RSAPrivateKey) -> None:
        self._private = private_key
        self._spki = private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.key_id = f"local:{_fingerprint(self._spki)}"
        self.unwrap_count = 0

    def public_key_der(self) -> bytes:
        return self._spki

    def unwrap(self, wrapped: bytes) -> bytes:
        self.unwrap_count += 1
        # `ValueError` on a block not wrapped to this key; the caller translates it.
        return self._private.decrypt(wrapped, _OAEP)

    def __repr__(self) -> str:
        return f"<LocalKeyProvider {self.key_id}>"


class KmsKeyProvider:
    """The KMS-held key named by its ARN. The public key is fetched on first use and kept for the
    process's life (a KMS key's material never changes; rotation is a new key — see the module docstring)."""

    def __init__(self, key_arn: str) -> None:
        if not edition_features.enabled("aws"):
            # Unreachable: `provider()` builds one only with `aws` (an edition without it refuses the KMS settings).
            raise RuntimeError("KMS is not in this edition")
        from app.aws import kms

        # Validates the ARN now (boot-time refusal of a malformed one); no network until first use.
        kms.region_of(key_arn)
        self._kms = kms
        self.key_id = key_arn
        self.unwrap_count = 0
        self._spki: Optional[bytes] = None
        self._lock = threading.Lock()

    def public_key_der(self) -> bytes:
        with self._lock:
            if self._spki is None:
                self._spki = self._kms.get_public_key(self.key_id)
            return self._spki

    def unwrap(self, wrapped: bytes) -> bytes:
        self.unwrap_count += 1
        return self._kms.decrypt(self.key_id, wrapped)

    def __repr__(self) -> str:
        return f"<KmsKeyProvider {self.key_id}>"


# ── NodeVault: an org's key, wrap-only here (PINNED) ────────────────────

# An envelope's `k` for a NodeVault key: `nv:` + the key hash. A KMS ARN and a `local:` id never start so.
NODEVAULT_KEY_PREFIX = "nv:"
# RSA-4096, e = 65537, the one construction (same OAEP as `_OAEP`).
NODEVAULT_KEY_BITS = 4096
NODEVAULT_PUBLIC_EXPONENT = 65537
_KEY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# A base64 SPKI of a 4096-bit RSA key is ~736 characters; anything far past that is not one.
_MAX_SPKI_B64 = 2048


def nodevault_key_hash(spki: bytes) -> str:
    """`hex(sha256(SPKI DER))` — a NodeVault key's hash and a node identity key's fingerprint (AC-4.6).
    Always derived from the public key, never trusted from beside it."""
    return hashlib.sha256(spki).hexdigest()


def is_nodevault_key_id(key_id: str) -> bool:
    return isinstance(key_id, str) and key_id.startswith(NODEVAULT_KEY_PREFIX)


def nodevault_key_id(key_hash: str) -> str:
    """`nv:<key hash>`, refusing anything that is not a key hash."""
    if not isinstance(key_hash, str) or not _KEY_HASH_RE.match(key_hash):
        raise ValueError("a NodeVault key hash is 64 lowercase hex characters")
    return NODEVAULT_KEY_PREFIX + key_hash


def nodevault_key_hash_of(key_id: str) -> Optional[str]:
    """The key hash an `nv:` key id names, or None for any other id (or a malformed `nv:` one)."""
    if not is_nodevault_key_id(key_id):
        return None
    key_hash = key_id[len(NODEVAULT_KEY_PREFIX):]
    return key_hash if _KEY_HASH_RE.match(key_hash) else None


def parse_nodevault_public_key(b64_spki: object, *, label: str = "public_key") -> bytes:
    """A posted base64 SPKI DER as its bytes, refused (ValueError naming `label`, never the value) unless it
    is an RSA-4096 key with e = 65537 — the pinned construction for NodeVault keys and node identity keys.
    Shape only: nothing here can tell whose key it is."""
    if not isinstance(b64_spki, str) or not b64_spki.strip() or len(b64_spki) > _MAX_SPKI_B64:
        raise ValueError(f"{label} is not a base64 public key")
    text = "".join(b64_spki.split())
    try:
        der = base64.b64decode(text, validate=True)
        public = serialization.load_der_public_key(der)
    except (binascii.Error, ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError(f"{label} is not a DER SubjectPublicKeyInfo") from None
    if (
        not isinstance(public, rsa.RSAPublicKey)
        or public.key_size != NODEVAULT_KEY_BITS
        or public.public_numbers().e != NODEVAULT_PUBLIC_EXPONENT
    ):
        raise ValueError(f"{label} must be an RSA-{NODEVAULT_KEY_BITS} key with exponent {NODEVAULT_PUBLIC_EXPONENT}")
    # Re-encoded, so the hash is over the canonical DER whatever whitespace or encoding variant was posted.
    return public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


class NodeVaultPublicKeyProvider:
    """An org's NodeVault key as the CP holds it: the PUBLIC half only. `wrap_data_key` seals under it (the
    migration's server-side seal); `unwrap` raises `NodeVaultSealed`, because the private half is on the
    org's nodes and nowhere in this process. `key_id` is `nv:<key hash>`, the hash recomputed here from the
    SPKI so a row whose hash disagrees with its key is refused, not used."""

    def __init__(self, key_hash: str, spki: bytes) -> None:
        if not isinstance(spki, bytes) or nodevault_key_hash(spki) != key_hash:
            raise ValueError("the NodeVault public key does not hash to its recorded key hash")
        self.key_hash = key_hash
        self.key_id = nodevault_key_id(key_hash)
        self._spki = spki
        self.unwrap_count = 0

    def public_key_der(self) -> bytes:
        return self._spki

    def unwrap(self, wrapped: bytes) -> bytes:
        raise NodeVaultSealed("a NodeVault envelope is opened only by the org's nodes")

    def __repr__(self) -> str:
        return f"<NodeVaultPublicKeyProvider {self.key_id}>"


def nodevault_provider(
    session: object, org_id: object, key_hash: str, *, states: Optional[tuple[str, ...]] = None,
    share_lock: bool = False,
) -> Optional[NodeVaultPublicKeyProvider]:
    """The provider for `org_id`'s NodeVault key `key_hash` (in one of `states`, any state when None), or
    None when the org holds no such key. Read off `nodevault_keys`; the org is the CALLER's to supply.

    `share_lock` reads the key row `FOR SHARE`, held to the caller's commit: a credential write admitting an
    envelope under the key then serializes with a retirement's `FOR UPDATE` on that row, so the retirement's
    credential count can never miss a seal that read the key as sealable (`durable/nodevault.py::begin_retire`)."""
    from sqlalchemy import select

    from app.models.credentials import NodeVaultKey

    if session is None or org_id is None or not isinstance(key_hash, str) or not _KEY_HASH_RE.match(key_hash):
        return None
    query = select(NodeVaultKey.key_hash, NodeVaultKey.public_key).where(
        NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash == key_hash
    )
    if states is not None:
        query = query.where(NodeVaultKey.state.in_(states))
    if share_lock:
        query = query.with_for_update(read=True)
    row = session.execute(query).first()  # type: ignore[attr-defined]
    if row is None:
        return None
    return NodeVaultPublicKeyProvider(row.key_hash, parse_nodevault_public_key(row.public_key))


def load_private_key(b64_der: str) -> rsa.RSAPrivateKey:
    """A base64 PKCS#8 DER private key, or a `ValueError` naming what was wrong — never the value."""
    text = "".join(b64_der.split())
    try:
        der = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{LOCAL_KEY_ENV} is not base64") from None
    try:
        key = serialization.load_der_private_key(der, password=None)
    except (ValueError, TypeError):
        raise ValueError(f"{LOCAL_KEY_ENV} is not an unencrypted PKCS#8 DER private key") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError(f"{LOCAL_KEY_ENV} must be an RSA key of at least 2048 bits")
    return key


def published_dev_private_key_b64() -> str:
    """The published dev keypair, whitespace-free."""
    return "".join(_DEV_LOCAL_PRIVATE_KEY_B64.split())


@functools.cache
def _published_moduli() -> frozenset[int]:
    from app.fixture_keys import FIXTURE_LOCAL_PRIVATE_KEY

    return frozenset(
        load_private_key(b64).public_key().public_numbers().n
        for b64 in (published_dev_private_key_b64(), FIXTURE_LOCAL_PRIVATE_KEY)
    )


def _is_dev_keypair(key_b64: str) -> bool:
    """Is this the published dev keypair (not the fixture's), however spelled? A value that does not load is not."""
    try:
        key = load_private_key(key_b64)
    except ValueError:
        return False
    return key.public_key().public_numbers().n == _dev_modulus()


@functools.cache
def _dev_modulus() -> int:
    return load_private_key(published_dev_private_key_b64()).public_key().public_numbers().n


def is_published_key(key_b64: str) -> bool:
    """Is this a keypair this repository publishes — the dev keypair or the test fixture's
    (`app/fixture_keys.py`) — however it is spelled or re-encoded? Compared by modulus, which names the key whatever its encoding. One of the
    two homes of that question (the other: `app/credential_crypto.py`'s tier-1 twin); what `app/ce_bootstrap.py`
    counts as absent. A value that does not load is not one (its reader refuses it)."""
    try:
        key = load_private_key(key_b64)
    except ValueError:
        return False
    return key.public_key().public_numbers().n in _published_moduli()


def resolve_secret_key_provider() -> SecretKeyProvider:
    """The provider this process wraps and unwraps credential secrets with, or a refusal to start.

    Exactly one of `CREDENTIAL_KMS_KEY_ID` / `CREDENTIAL_LOCAL_PRIVATE_KEY` may be set (two settings over
    one decision are refused). The KMS ARN wins wherever it is set, in an edition with `aws` (the community
    edition refuses it at boot, `app/ce_bootstrap.py::refuse_removed_settings`).
    A local keypair — an operator's own or the test fixture's, never the published dev keypair — is
    accepted only on a process that declares itself throwaway (`dev_bypass.is_fixture_env`: `local`,
    `test`, `ci`); neither set is refused everywhere (`./dc` generates a local stack's). The community edition (`edition.IS_CE`)
    accepts its own generated keypair whatever `APP_ENV` says, and never the published one: its boot
    generates the key first (`app/ce_bootstrap.py::ensure_secrets`); a published one (`is_published_key`) is
    refused there whatever `APP_ENV` says. Everything else refuses: an absent
    `APP_ENV` is not local (`app/dev_bypass.py::is_local_env`), and a deployment has no non-KMS mode — a
    private key in an environment variable sits in the very process memory KMS keeps it out of.

    Not cached here (`provider()` below caches), so a test can monkeypatch the environment.
    """
    # The community edition's generated keypair, in every process that reads one (a no-op in the cloud).
    from app import ce_bootstrap

    ce_bootstrap.ensure_secrets()
    kms_arn = os.environ.get(KMS_KEY_ENV, "").strip()
    local_b64 = "".join(os.environ.get(LOCAL_KEY_ENV, "").split())
    if kms_arn and local_b64:
        raise RuntimeError(
            f"both {KMS_KEY_ENV} and {LOCAL_KEY_ENV} are set; the credential store wraps secrets under "
            "exactly one key. Unset one."
        )
    if kms_arn and edition_features.enabled("aws"):
        return KmsKeyProvider(kms_arn)
    if edition.IS_CE:
        if local_b64 and is_published_key(local_b64):
            raise RuntimeError(
                f"{LOCAL_KEY_ENV} is a keypair published in this repository; the community edition wraps "
                f"secrets only under its own. Unset {LOCAL_KEY_ENV} and restart: the boot generates one."
            )
        if local_b64:
            return LocalKeyProvider(load_private_key(local_b64))
    elif local_b64 and is_fixture_env() and not _is_dev_keypair(local_b64):
        return LocalKeyProvider(load_private_key(local_b64))
    if edition.IS_CE:
        raise RuntimeError(
            f"the credential store has no secret-wrapping key — {LOCAL_KEY_ENV} is unset, and the community edition's "
            "boot generates it into its secrets volume (`app/ce_bootstrap.py::ensure_secrets`). Check that "
            "volume is mounted and writable, then restart."
        )
    if local_b64 and is_fixture_env():
        cause = f"{LOCAL_KEY_ENV} is the dev keypair published in this repository"
    elif local_b64:
        cause = f"{LOCAL_KEY_ENV} is set, and a local keypair is accepted only when APP_ENV is 'local', 'test' or 'ci'"
    else:
        cause = f"neither {KMS_KEY_ENV} nor {LOCAL_KEY_ENV} is set"
    raise RuntimeError(
        f"the credential store has no secret-wrapping key — {cause}. A local stack: run it through ./dc, which "
        f"generates {LOCAL_KEY_ENV} into .env.local. A deployment: set {KMS_KEY_ENV} to the environment's KMS key "
        "ARN; there is no non-KMS deployment mode. Refusing to start rather than sealing secrets under a key "
        "that sits in process memory. An absent APP_ENV counts as not-local by design."
    )


def resolve_previous_key_providers() -> tuple[SecretKeyProvider, ...]:
    """The unwrap-only providers for the keys a rotation retired (`CREDENTIAL_KMS_PREVIOUS_KEY_IDS`), or a
    refusal to start. Empty when unset — the steady state.

    Only beside a KMS current key: a previous ARN is a rotation of THAT key, and a local stack has no KMS.
    The current ARN listed again is dropped (it already opens its own envelopes); duplicates collapse.
    A local stack instead gets the published dev keypair, unwrap-only (see `_DEV_LOCAL_PRIVATE_KEY_B64`).
    """
    if is_local_env():
        return (LocalKeyProvider(load_private_key(published_dev_private_key_b64())),)
    arns = [a for a in re.split(r"[\s,]+", os.environ.get(PREVIOUS_KMS_KEYS_ENV, "")) if a]
    if not arns or not edition_features.enabled("aws"):
        # Without `aws` the setting is refused at boot (`app/ce_bootstrap.py::refuse_removed_settings`).
        return ()
    current = os.environ.get(KMS_KEY_ENV, "").strip()
    if not current:
        raise RuntimeError(
            f"{PREVIOUS_KMS_KEYS_ENV} is set but {KMS_KEY_ENV} is not: a previous key is the key a rotation "
            f"of the KMS wrapping key retired, so it needs the current KMS key beside it. Unset "
            f"{PREVIOUS_KMS_KEYS_ENV} or set {KMS_KEY_ENV}."
        )
    return tuple(KmsKeyProvider(arn) for arn in dict.fromkeys(arns) if arn != current)


# ── The process-wide provider, and the unwrapped-data-key cache in front of it ───────────────────────

_provider: Optional[SecretKeyProvider] = None
_previous: Optional[tuple[SecretKeyProvider, ...]] = None
_provider_lock = threading.Lock()


def provider() -> SecretKeyProvider:
    """The one provider this process uses, resolved on first use. `reset_provider()` drops it (tests,
    after an environment change)."""
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = resolve_secret_key_provider()
        return _provider


def previous_providers() -> tuple[SecretKeyProvider, ...]:
    """The unwrap-only providers of every retired key, resolved on first use (empty in the steady state)."""
    global _previous
    with _provider_lock:
        if _previous is None:
            _previous = resolve_previous_key_providers()
        return _previous


def provider_for(
    key_id: str, *, session: object = None, org_id: object = None
) -> Optional[SecretKeyProvider]:
    """The provider whose key an envelope stamped `key_id` names — the current one or a retired one — or
    None when this process holds no such key.

    An `nv:` id is an org's NodeVault key: resolved off that org's `nodevault_keys` row when the caller
    threads `session` and `org_id`, None otherwise. Its provider WRAPS only; its unwrap raises
    `NodeVaultSealed`."""
    if is_nodevault_key_id(key_id):
        key_hash = nodevault_key_hash_of(key_id)
        return None if key_hash is None else nodevault_provider(session, org_id, key_hash)
    current = provider()
    if key_id == current.key_id:
        return current
    return next((p for p in previous_providers() if p.key_id == key_id), None)


def reset_provider(
    replacement: Optional[SecretKeyProvider] = None,
    *,
    previous: Optional[tuple[SecretKeyProvider, ...]] = None,
) -> None:
    """Drop (or replace) the cached providers and every cached data key. Tests only. `previous` None
    re-resolves the retired keys from the environment on next use."""
    global _provider, _previous
    with _provider_lock:
        _provider = replacement
        _previous = previous
    _cache.clear()


class _DataKeyCache:
    """Wrapped bytes → data key, bounded and expiring. Keyed by the SHA-256 of the wrapped block so the
    map's keys carry nothing usable either. Thread-safe: routes run in uvicorn's thread pool."""

    def __init__(self, *, ttl: float, max_entries: int) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._entries: OrderedDict[bytes, tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, wrapped: bytes) -> Optional[bytes]:
        digest = hashlib.sha256(wrapped).digest()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                return None
            expires_at, data_key = entry
            if expires_at <= time.monotonic():
                del self._entries[digest]
                return None
            self._entries.move_to_end(digest)
            return data_key

    def put(self, wrapped: bytes, data_key: bytes) -> None:
        digest = hashlib.sha256(wrapped).digest()
        with self._lock:
            self._entries[digest] = (time.monotonic() + self._ttl, data_key)
            self._entries.move_to_end(digest)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        # Never the contents.
        return f"<_DataKeyCache entries={len(self)}>"

    def __getstate__(self) -> None:
        raise TypeError("the data-key cache holds key material and is never serialized")


_cache = _DataKeyCache(ttl=CACHE_TTL_SECONDS, max_entries=CACHE_MAX_ENTRIES)


def wrap_data_key(data_key: bytes, *, key_provider: Optional[SecretKeyProvider] = None) -> bytes:
    """`data_key` wrapped under the provider's PUBLIC key — a local computation, never a KMS call."""
    if not isinstance(data_key, bytes) or len(data_key) != DATA_KEY_BYTES:
        raise ValueError(f"a data key is {DATA_KEY_BYTES} bytes")
    try:
        spki = (key_provider or provider()).public_key_der()
    except _kms_errors() as exc:
        raise WrappingKeyUnavailable(_unavailable(exc)) from None
    public = serialization.load_der_public_key(spki)
    if not isinstance(public, rsa.RSAPublicKey):
        raise ValueError("the secret-wrapping public key is not RSA")
    return public.encrypt(data_key, _OAEP)


def unwrap_data_key(wrapped: bytes, *, key_provider: Optional[SecretKeyProvider] = None) -> bytes:
    """`wrapped` back to its data key, through the cache: the provider's private operation runs only on
    a miss. Raises `ValueError` for a block the key cannot open (wrong key, tampered), and lets a
    KMS transport/refusal error propagate. A NodeVault provider raises `NodeVaultSealed` before the cache is
    even consulted: this process holds no NodeVault private key, cached or otherwise."""
    if isinstance(key_provider, NodeVaultPublicKeyProvider):
        raise NodeVaultSealed("a NodeVault envelope is opened only by the org's nodes")
    cached = _cache.get(wrapped)
    if cached is not None:
        return cached
    active = key_provider or provider()
    try:
        data_key = active.unwrap(wrapped)
    except ValueError:
        # `cryptography` raises a bare "Decryption failed."; re-raised with a name and no value.
        raise ValueError("the wrapped data key could not be unwrapped by the configured key") from None
    except _kms_errors() as exc:
        if getattr(exc, "code", None) == "InvalidCiphertextException":
            # KMS's spelling of the local provider's `ValueError`: a block this key did not wrap.
            raise ValueError("the wrapped data key could not be unwrapped by the configured key") from None
        raise WrappingKeyUnavailable(_unavailable(exc)) from None
    if len(data_key) != DATA_KEY_BYTES:
        raise ValueError("the unwrapped data key has the wrong length")
    _cache.put(wrapped, data_key)
    return data_key


def _kms_errors() -> tuple[type[Exception], ...]:
    """What a KMS call raises, for an `except`: nothing in an edition without `aws`, which never builds a
    `KmsKeyProvider` (its boot refuses the KMS settings, `app/ce_bootstrap.py::refuse_removed_settings`)."""
    if not edition_features.enabled("aws"):
        return ()
    from app.aws.kms import KmsError

    return (KmsError,)


def _unavailable(exc: Exception) -> str:
    """A `KmsError` (`_kms_errors`) as a message: its class and AWS's error code, never a key or the account."""
    code = getattr(exc, "code", None)
    return f"the secret-wrapping key is unavailable ({type(exc).__name__}" + (f": {code})" if code else ")")

