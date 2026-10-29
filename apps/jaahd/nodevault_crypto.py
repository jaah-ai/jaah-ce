"""NodeVault's cryptographic constructions, node side: pure functions, no I/O.

The single Python home of every construction the NodeVault architecture pins. The browser
mirrors them (its key module, a sibling task of the same epic); the two are held byte-compatible by the shared vector
file `tests/fixtures/nodevault_vectors.json`, which `tests/test_nodevault_crypto.py` opens here.

  * Every asymmetric key is RSA-4096, e=65537, RSAES-OAEP with SHA-256 and MGF1-SHA-256, no label
    (`RSA-OAEP-256`, the tier-2 wrap). Symmetric encryption is AES-256-GCM, 12-byte nonce,
    128-bit tag. Every binary field on the wire is standard padded base64.
  * Key hash / NIK fingerprint: `hex(sha256(SPKI DER))`. Derived from the public key by whoever needs it;
    a hash sent alongside a key is never trusted.
  * Secret envelope: v2 tier 2 unchanged — `{v:2,k:"nv:<hash>",alg,ek,n,ct}` under
    `credentials.secret|v2|<credential id>|<field>|<destination>` (mirror of
    `apps/api/app/credential_crypto.py::tier2_aad`).
  * Key blob (an NVK in transit): `{v:1,k:<wrapping key id>,alg,ek,n,ct}`, `ct` = AES-GCM of the NVK's
    PKCS#8 DER under `nodevault.key|v1|<org_id>|<key_hash>|<recipient>`. The receiver derives the public
    key from what it unwrapped and refuses the blob unless that hash is the declared one.
  * Rekey: unwrap `ek` with the old NVK, trial-open `ct` under its AAD, rewrap the data key to the new
    public key. Only the new `ek` is returned; `n` and `ct` are untouched.

Every failure raises `NodeVaultCryptoError` whose `reason` is a closed vocabulary word and whose message
never carries key material, a data key or a plaintext — callers may log and return it as-is.

`cryptography` is the pinned dependency the node's venv installs (infra/jaah/requirements.txt); it is imported
lazily, so importing THIS module never fails on an interpreter where it is absent — `probe()` answers that
question, fail-closed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from pathlib import Path
from functools import cache
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import UUID

RSA_BITS = 4096
RSA_PUBLIC_EXPONENT = 65537
WRAP_ALGORITHM = "RSA-OAEP-256"
DATA_KEY_BYTES = 32
NONCE_BYTES = 12

NV_KEY_ID_PREFIX = "nv:"
NIK_KEY_ID_PREFIX = "nik:"
KEY_BLOB_VERSION = 1
SECRET_ENVELOPE_VERSION = 2

# Mirror of `credential_crypto._TIER2_AAD_PREFIX`; the key-blob prefix is NodeVault's own (§2).
TIER2_AAD_PREFIX = b"credentials.secret|v2|"
KEY_BLOB_AAD_PREFIX = b"nodevault.key|v1|"

# Bounds, so a hostile blob is refused before any arithmetic: a 4096-bit OAEP block is 512 bytes; a
# PKCS#8 RSA-4096 key is ~2.4 KB (+16 tag); a secret ciphertext mirrors `_MAX_SECRET_CIPHERTEXT_BYTES`.
_WRAPPED_BYTES = RSA_BITS // 8
_MAX_KEY_CT_BYTES = 4096
_MAX_SECRET_CT_BYTES = 4 * 8192 + 16
_MAX_SPKI_BYTES = 1024

_ENVELOPE_KEYS = frozenset({"v", "k", "alg", "ek", "n", "ct"})
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")

# The closed vocabulary of `NodeVaultCryptoError.reason` (the §4.3 `error` words where one applies).
ERR_MALFORMED = "malformed"
ERR_WRONG_RECIPIENT = "wrong_recipient"
ERR_HASH_MISMATCH = "hash_mismatch"
ERR_OPEN_FAILED = "open_failed"
ERR_WRONG_KEY = "wrong_key"
ERR_UNAVAILABLE = "crypto_unavailable"


class NodeVaultCryptoError(Exception):
    """A refused or failed construction. `reason` is one closed-vocabulary word; the message names the
    field at fault and never a value."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# The hash lock the node's venv was built from, beside this module in every layout (a checkout, /usr/lib/jaahd,
# the macOS/Windows bundles, the community edition's bundle): `tools/lock_jaahd_requirements.py` writes it.
_LOCK_PATH = Path(__file__).resolve().with_name("requirements.txt")
_LOCK_PIN = re.compile(r"^cryptography==(\S+)", re.M)


def _check_pinned_version(found: str) -> None:
    """`found` must be the lock's `cryptography==` version. No lock, or another version, is an ImportError:
    the bytes doing NodeVault's crypto must be the ones the lock vouched for."""
    try:
        match = _LOCK_PIN.search(_LOCK_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ImportError(f"no dependency lock at {_LOCK_PATH}: {exc}") from None
    if match is None:
        raise ImportError(f"{_LOCK_PATH} pins no cryptography version")
    if found != match.group(1):
        raise ImportError(f"cryptography {found} is not the lock's {match.group(1)}")


@cache
def _lib() -> Any:
    """The `cryptography` primitives, imported on first use. A failure is not cached (`functools.cache`
    keeps only returns), so a later call tries again."""
    try:
        # FAIL CLOSED, twice: an interpreter without the library (the venv was not built, or this is not its
        # python) raises here; one with a SUBSTITUTED library — a root `pip install`, a stray PYTHONPATH copy —
        # is refused by `_check_pinned_version` (the lock beside this module says which bytes are the node's).
        # Either way `probe()` answers False.
        import cryptography
        _check_pinned_version(cryptography.__version__)
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:  # noqa: BLE001 — any import failure is one outcome: this node cannot do it
        raise NodeVaultCryptoError(ERR_UNAVAILABLE, type(exc).__name__) from None
    return SimpleNamespace(
        InvalidTag=InvalidTag, serialization=serialization, rsa=rsa, AESGCM=AESGCM,
        oaep=padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )


# ── Keys ──────────────────────────────────────────────────────────────────────────────────────────────

def generate_private_key() -> Any:
    """A fresh RSA-4096 private key (a NIK, or an NVK in tests)."""
    return _lib().rsa.generate_private_key(public_exponent=RSA_PUBLIC_EXPONENT, key_size=RSA_BITS)


def _check_rsa(key: Any) -> Any:
    lib = _lib()
    public = key.public_key() if isinstance(key, lib.rsa.RSAPrivateKey) else key
    if not isinstance(public, lib.rsa.RSAPublicKey):
        raise NodeVaultCryptoError(ERR_MALFORMED, "not an RSA key")
    if public.key_size != RSA_BITS or public.public_numbers().e != RSA_PUBLIC_EXPONENT:
        raise NodeVaultCryptoError(ERR_MALFORMED, "not an RSA-4096 e=65537 key")
    return key


def private_key_to_pem(key: Any) -> bytes:
    """PKCS#8 PEM, unencrypted — the keystore's on-disk form (its 0600 mode is the protection)."""
    s = _lib().serialization
    return key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption())


def private_key_from_pem(pem: bytes) -> Any:
    try:
        key = _lib().serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        raise NodeVaultCryptoError(ERR_MALFORMED, "private key PEM") from None
    return _check_rsa(key)


def _private_key_from_pkcs8_der(der: bytes) -> Any:
    try:
        key = _lib().serialization.load_der_private_key(der, password=None)
    except (ValueError, TypeError):
        raise NodeVaultCryptoError(ERR_MALFORMED, "private key DER") from None
    return _check_rsa(key)


def _private_key_to_pkcs8_der(key: Any) -> bytes:
    s = _lib().serialization
    return key.private_bytes(s.Encoding.DER, s.PrivateFormat.PKCS8, s.NoEncryption())


def public_spki_der(key: Any) -> bytes:
    """The SubjectPublicKeyInfo DER of a private or public key."""
    lib = _lib()
    public = key.public_key() if isinstance(key, lib.rsa.RSAPrivateKey) else key
    s = lib.serialization
    return public.public_bytes(s.Encoding.DER, s.PublicFormat.SubjectPublicKeyInfo)


def public_key_b64(key: Any) -> str:
    """The wire form of a public key: base64 of its SPKI DER (what `POST …/identity` carries)."""
    return base64.b64encode(public_spki_der(key)).decode()


def public_key_from_b64(value: Any) -> Any:
    der = _b64(value, label="public key", max_bytes=_MAX_SPKI_BYTES)
    try:
        key = _lib().serialization.load_der_public_key(der)
    except (ValueError, TypeError):
        raise NodeVaultCryptoError(ERR_MALFORMED, "public key") from None
    return _check_rsa(key)


def key_hash(key: Any) -> str:
    """`hex(sha256(SPKI DER))` of a key — the NVK key hash and the NIK fingerprint alike."""
    return hashlib.sha256(public_spki_der(key)).hexdigest()


def nv_key_id(hash_hex: str) -> str:
    return NV_KEY_ID_PREFIX + _check_hash(hash_hex)


def nik_key_id(fingerprint: str) -> str:
    return NIK_KEY_ID_PREFIX + _check_hash(fingerprint)


def _check_hash(value: Any) -> str:
    if not isinstance(value, str) or not _HASH_RE.match(value):
        raise NodeVaultCryptoError(ERR_MALFORMED, "key hash")
    return value


# ── Wire helpers ──────────────────────────────────────────────────────────────────────────────────────

def _b64(value: Any, *, label: str, max_bytes: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > (max_bytes * 4) // 3 + 4:
        raise NodeVaultCryptoError(ERR_MALFORMED, label)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise NodeVaultCryptoError(ERR_MALFORMED, label) from None
    if not decoded or len(decoded) > max_bytes:
        raise NodeVaultCryptoError(ERR_MALFORMED, label)
    return decoded


def _enc(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _envelope_parts(envelope: Any, *, version: int, label: str, max_ct: int) -> tuple[str, bytes, bytes, bytes]:
    """(key id, wrapped data key, nonce, ciphertext) of an envelope of `version`, shape-checked."""
    if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_KEYS:
        raise NodeVaultCryptoError(ERR_MALFORMED, label)
    if envelope.get("v") != version or isinstance(envelope.get("v"), bool):
        raise NodeVaultCryptoError(ERR_MALFORMED, f"{label} version")
    if envelope.get("alg") != WRAP_ALGORITHM:
        raise NodeVaultCryptoError(ERR_MALFORMED, f"{label} algorithm")
    key_id = envelope.get("k")
    if not isinstance(key_id, str) or not key_id or len(key_id) > 512:
        raise NodeVaultCryptoError(ERR_MALFORMED, f"{label} key id")
    ek = _b64(envelope.get("ek"), label=f"{label} wrapped key", max_bytes=_WRAPPED_BYTES)
    nonce = _b64(envelope.get("n"), label=f"{label} nonce", max_bytes=NONCE_BYTES)
    if len(nonce) != NONCE_BYTES:
        raise NodeVaultCryptoError(ERR_MALFORMED, f"{label} nonce")
    ct = _b64(envelope.get("ct"), label=f"{label} ciphertext", max_bytes=max_ct)
    return key_id, ek, nonce, ct


def _aad_part(value: Any, label: str) -> bytes:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None
    if not text or "|" in text:
        raise NodeVaultCryptoError(ERR_MALFORMED, label)
    return text.encode("utf-8")


def _key_id_matches(key_id: str, private_key: Any, prefix: str) -> bool:
    """Whether `key_id` is `prefix` + the DERIVED hash of `private_key` (a declared hash is never trusted)."""
    head, sep, digest = key_id.partition(":")
    return (bool(sep) and f"{head}:" == prefix and _HASH_RE.match(digest) is not None
            and hmac.compare_digest(digest, key_hash(private_key)))


def _wrap(data_key: bytes, public_key: Any) -> bytes:
    return _check_rsa(public_key).encrypt(data_key, _lib().oaep)


def _unwrap(wrapped: bytes, private_key: Any) -> bytes:
    try:
        data_key = private_key.decrypt(wrapped, _lib().oaep)
    except ValueError:
        raise NodeVaultCryptoError(ERR_OPEN_FAILED, "wrapped key") from None
    if len(data_key) != DATA_KEY_BYTES:
        raise NodeVaultCryptoError(ERR_OPEN_FAILED, "wrapped key length")
    return data_key


def _gcm_open(data_key: bytes, nonce: bytes, ct: bytes, aad: bytes) -> bytes:
    lib = _lib()
    try:
        return lib.AESGCM(data_key).decrypt(nonce, ct, aad)
    except lib.InvalidTag:
        raise NodeVaultCryptoError(ERR_OPEN_FAILED, "ciphertext") from None


# ── Key blob (an NVK in transit) ──────────────────────────────────────────────────────────────────────

def key_blob_aad(org_id: Any, key_hash_hex: str, recipient: str) -> bytes:
    """`nodevault.key|v1|<org_id>|<key_hash>|<recipient>`. The recipient is last, so it alone may hold
    a `:` — but no part may hold a `|`."""
    return (KEY_BLOB_AAD_PREFIX + _aad_part(org_id, "org id") + b"|" + _check_hash(key_hash_hex).encode()
            + b"|" + _aad_part(recipient, "recipient"))


def wrap_key_blob(nvk: Any, *, wrapping_public_key: Any, wrapping_key_id: str, org_id: Any,
                  recipient: str) -> dict:
    """`nvk` (a private key) as a key blob for `wrapping_public_key`, named `wrapping_key_id`
    (`nik:<fp>` for a node, `nv:<hash>` for a rotation chain). Used by peer enrollment and tests."""
    aad = key_blob_aad(org_id, key_hash(nvk), recipient)
    if not isinstance(wrapping_key_id, str) or not wrapping_key_id.startswith((NIK_KEY_ID_PREFIX, NV_KEY_ID_PREFIX)):
        raise NodeVaultCryptoError(ERR_MALFORMED, "wrapping key id")
    if wrapping_key_id.partition(":")[2] != key_hash(wrapping_public_key):
        raise NodeVaultCryptoError(ERR_WRONG_RECIPIENT, "wrapping key id does not name the wrapping key")
    data_key = os.urandom(DATA_KEY_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ct = _lib().AESGCM(data_key).encrypt(nonce, _private_key_to_pkcs8_der(nvk), aad)
    return {"v": KEY_BLOB_VERSION, "k": wrapping_key_id, "alg": WRAP_ALGORITHM,
            "ek": _enc(_wrap(data_key, wrapping_public_key)), "n": _enc(nonce), "ct": _enc(ct)}


def unwrap_key_blob(blob: Any, *, unwrapping_key: Any, org_id: Any, key_hash_hex: str, recipient: str) -> Any:
    """The NVK a key blob carries, opened with `unwrapping_key` (this node's NIK, or the current NVK for
    a chain blob). Refuses `wrong_recipient` when the blob names another key, `open_failed` when it does
    not open under the AAD, and `hash_mismatch` unless the unwrapped key's DERIVED hash is
    `key_hash_hex` — a declared hash is never trusted. The recipient fixes the one wrapping-key kind it
    admits: `chain:<hash>` is wrapped to an NVK (`nv:`), anything else (`node:<id>`) to a NIK (`nik:`)."""
    expected = _check_hash(key_hash_hex)
    key_id, ek, nonce, ct = _envelope_parts(blob, version=KEY_BLOB_VERSION, label="key blob",
                                            max_ct=_MAX_KEY_CT_BYTES)
    prefix = NV_KEY_ID_PREFIX if str(recipient).startswith("chain:") else NIK_KEY_ID_PREFIX
    if not _key_id_matches(key_id, unwrapping_key, prefix):
        raise NodeVaultCryptoError(ERR_WRONG_RECIPIENT, "the blob is wrapped to another key")
    aad = key_blob_aad(org_id, expected, recipient)
    der = _gcm_open(_unwrap(ek, unwrapping_key), nonce, ct, aad)
    nvk = _private_key_from_pkcs8_der(der)
    if not hmac.compare_digest(key_hash(nvk), expected):
        raise NodeVaultCryptoError(ERR_HASH_MISMATCH, "the unwrapped key is not the declared one")
    return nvk


# ── Secret envelope (v2 tier 2 under an NVK) ──────────────────────────────────────────────────────────

def tier2_aad(credential_id: UUID | str, field: str, destination: str) -> bytes:
    """`credentials.secret|v2|<id>|<field>|<destination>` — mirror of `credential_crypto.tier2_aad`."""
    try:
        cid = str(UUID(str(credential_id).strip().lower())).encode()
    except ValueError:
        raise NodeVaultCryptoError(ERR_MALFORMED, "credential id") from None
    if not isinstance(field, str) or not field or "|" in field:
        raise NodeVaultCryptoError(ERR_MALFORMED, "field")
    if not isinstance(destination, str):
        raise NodeVaultCryptoError(ERR_MALFORMED, "destination")
    try:
        dest = destination.encode("utf-8")
    except UnicodeEncodeError:
        raise NodeVaultCryptoError(ERR_MALFORMED, "destination") from None
    return TIER2_AAD_PREFIX + cid + b"|" + field.encode() + b"|" + dest


def seal_secret(value: str, *, aad: bytes, public_key: Any) -> dict:
    """`value` as a v2 envelope under the NVK `public_key` — the browser's and the CP's job; here for
    the vectors and the tests that pin interop."""
    data_key = os.urandom(DATA_KEY_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ct = _lib().AESGCM(data_key).encrypt(nonce, value.encode("utf-8"), aad)
    return {"v": SECRET_ENVELOPE_VERSION, "k": nv_key_id(key_hash(public_key)), "alg": WRAP_ALGORITHM,
            "ek": _enc(_wrap(data_key, public_key)), "n": _enc(nonce), "ct": _enc(ct)}


def _secret_parts(envelope: Any, nvk: Any) -> tuple[bytes, bytes, bytes, bytes]:
    key_id, ek, nonce, ct = _envelope_parts(envelope, version=SECRET_ENVELOPE_VERSION, label="secret envelope",
                                            max_ct=_MAX_SECRET_CT_BYTES)
    if not _key_id_matches(key_id, nvk, NV_KEY_ID_PREFIX):
        raise NodeVaultCryptoError(ERR_WRONG_KEY, "the envelope is sealed under another key")
    return ek, nonce, ct, _unwrap(ek, nvk)


def open_secret(envelope: Any, *, aad: bytes, nvk: Any) -> str:
    """The plaintext of a NodeVault secret envelope. The caller hands it on and never logs or caches it."""
    _ek, nonce, ct, data_key = _secret_parts(envelope, nvk)
    try:
        return _gcm_open(data_key, nonce, ct, aad).decode("utf-8")
    except UnicodeDecodeError:
        raise NodeVaultCryptoError(ERR_OPEN_FAILED, "plaintext is not text") from None


def verify_secret(envelope: Any, *, aad: bytes, nvk: Any) -> None:
    """Trial-open: raises unless the envelope opens under `nvk` and `aad`. The plaintext is discarded."""
    _ek, nonce, ct, data_key = _secret_parts(envelope, nvk)
    _gcm_open(data_key, nonce, ct, aad)


def rekey_secret(envelope: Any, *, aad: bytes, old_nvk: Any, new_public_key: Any) -> str:
    """The new `ek` (base64) for `envelope` moved from `old_nvk` to `new_public_key`: the data key is
    unwrapped, the ciphertext trial-opened under `aad` (a tampered `ct` is refused, `open_failed`), and
    the data key rewrapped. `n` and `ct` stay as they are; the plaintext is discarded."""
    _ek, nonce, ct, data_key = _secret_parts(envelope, old_nvk)
    _gcm_open(data_key, nonce, ct, aad)
    return _enc(_wrap(data_key, new_public_key))


# ── Capability ────────────────────────────────────────────────────────────────────────────────────────

_PROBED_OK = False


def probe() -> bool:
    """Whether this node can do NodeVault at all: an RSA-OAEP and an AES-GCM round trip on the vendored
    library. ANY exception — a wrong arch, a wrong CPython, a glibc below 2.28 — answers False. A small
    throwaway RSA key keeps the probe fast; it exercises the same OAEP code path the 4096-bit keys use.
    A success is cached for the process (a local verb serves it); a failure is re-tried on the next call."""
    global _PROBED_OK
    if _PROBED_OK:
        return True
    try:
        lib = _lib()
        key = lib.rsa.generate_private_key(public_exponent=RSA_PUBLIC_EXPONENT, key_size=2048)
        data_key = os.urandom(DATA_KEY_BYTES)
        if key.decrypt(key.public_key().encrypt(data_key, lib.oaep), lib.oaep) != data_key:
            return False
        nonce = os.urandom(NONCE_BYTES)
        _PROBED_OK = lib.AESGCM(data_key).decrypt(
            nonce, lib.AESGCM(data_key).encrypt(nonce, b"probe", b"aad"), b"aad") == b"probe"
        return _PROBED_OK
    except Exception:  # noqa: BLE001 — fail closed on anything
        return False
