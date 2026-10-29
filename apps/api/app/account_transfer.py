"""The account export bundle: one runner account carried from one control plane to another.

A control plane's credential store is sealed under keys only it holds (`app/credential_crypto.py`), so a
stored API key cannot travel as it is stored. Export opens the key and seals it again under a passphrase the
operator types on both consoles: scrypt derives an AES-256-GCM key from it, and the bundle's own header is
the AAD, so a bundle whose provider or kind was edited does not open. Import opens it and hands the key to
the ordinary create path (`app/routes/accounts.py::create_account`), which seals it under the target's own
keys and probes it with its vendor.

An OAuth account carries no token: the control plane holds none (its login lives on nodes), so its bundle
is the record alone and the imported account works once someone logs it in on a node.

The bundle names no slug, node, org or owner: each is the target's to decide. The key never appears in the
bundle in the clear, in an error, or in a log line here."""
from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = "jaah-account-export"
VERSION = 1
MIN_PASSPHRASE_LENGTH = 12
# Bounded so a pasted novel cannot make scrypt the request's cost.
MAX_PASSPHRASE_LENGTH = 1024

# scrypt's interactive-login cost (RFC 7914 §2 guidance), about 32 MiB and well under a second.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12

# Every header field, in a fixed order: the AAD binds them all, so editing any one breaks the seal.
_BOUND_FIELDS = ("format", "version", "provider", "auth_kind", "name", "description", "max_runners")
# Each derivation holds about 32 MiB: a few at a time, so a loop of wrong passphrases cannot exhaust memory.
_KDF_SLOTS = threading.BoundedSemaphore(4)
_KDF_WAIT_SECONDS = 10


class BundleError(ValueError):
    """A bundle that cannot be imported. The message is operator-facing and never carries a secret."""


def validated_passphrase(value: Any) -> str:
    """The passphrase, or `BundleError` naming the rule it breaks (never echoing it)."""
    if not isinstance(value, str) or len(value) < MIN_PASSPHRASE_LENGTH:
        raise BundleError(f"passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters")
    if len(value) > MAX_PASSPHRASE_LENGTH:
        raise BundleError(f"passphrase must be at most {MAX_PASSPHRASE_LENGTH} characters")
    return value


def _derive(passphrase: str, salt: bytes) -> bytes:
    if not _KDF_SLOTS.acquire(timeout=_KDF_WAIT_SECONDS):
        raise BundleError("too many exports or imports at once; try again")
    try:
        return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(passphrase.encode())
    finally:
        _KDF_SLOTS.release()


def _aad(header: dict[str, Any]) -> bytes:
    return json.dumps([header.get(f) for f in _BOUND_FIELDS], separators=(",", ":")).encode()


def build(
    *, provider: str, auth_kind: str, name: str, description: str, max_runners: Optional[int],
    api_key: Optional[str], passphrase: Optional[str],
) -> dict[str, Any]:
    """The bundle for one account. `api_key` (with `passphrase`) is sealed when given; `None` for OAuth."""
    bundle: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "provider": provider,
        "auth_kind": auth_kind,
        "name": name,
        "description": description,
        "max_runners": max_runners,
        "key": None,
    }
    if api_key is not None:
        salt, nonce = os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)
        sealed = AESGCM(_derive(validated_passphrase(passphrase), salt)).encrypt(
            nonce, api_key.encode(), _aad(bundle))
        bundle["key"] = {
            "kdf": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(sealed).decode(),
        }
    return bundle


def header(bundle: Any) -> dict[str, Any]:
    """The bundle's non-secret fields, checked for shape, or `BundleError` saying what is wrong."""
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise BundleError("this file is not a Jaah account export")
    if bundle.get("version") != VERSION:
        raise BundleError(f"this export is version {bundle.get('version')!r}; this console reads version {VERSION}")
    for text_field in ("provider", "auth_kind", "name", "description"):
        if not isinstance(bundle.get(text_field), str):
            raise BundleError(f"the export's '{text_field}' is missing or not text")
    # Blank would fall back to a default on create: an account of another kind than the one exported.
    for named in ("provider", "auth_kind"):
        if not bundle[named].strip():
            raise BundleError(f"the export's '{named}' is blank")
    max_runners = bundle.get("max_runners")
    if max_runners is not None and (not isinstance(max_runners, int) or isinstance(max_runners, bool)):
        raise BundleError("the export's 'max_runners' is not a whole number")
    if bundle.get("key") is not None and not isinstance(bundle.get("key"), dict):
        raise BundleError("the export's 'key' is malformed")
    return {f: bundle[f] for f in ("format", "version", "provider", "auth_kind", "name", "description",
                                   "max_runners")}


def opened_key(bundle: dict[str, Any], passphrase: Any) -> str:
    """The API key sealed in `bundle`, or `BundleError` — one message for a wrong passphrase and a tampered
    bundle, since AES-GCM cannot tell them apart."""
    sealed = bundle.get("key")
    if not isinstance(sealed, dict):
        raise BundleError("this export carries no API key")
    if (sealed.get("kdf"), sealed.get("n"), sealed.get("r"), sealed.get("p")) != (
            "scrypt", _SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
        raise BundleError("this export's key is sealed with parameters this console does not read")
    try:
        salt = base64.b64decode(sealed["salt"], validate=True)
        nonce = base64.b64decode(sealed["nonce"], validate=True)
        ciphertext = base64.b64decode(sealed["ciphertext"], validate=True)
    except (KeyError, TypeError, binascii.Error):
        raise BundleError("this export's key is malformed") from None
    key = _derive(validated_passphrase(passphrase), salt)
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext, _aad(bundle))
    except (InvalidTag, ValueError):
        # Dropped, never chained: nothing in it is useful and the passphrase was an argument.
        raise BundleError("wrong passphrase, or the export was changed after it was made") from None
    try:
        return plain.decode()
    except UnicodeDecodeError:
        raise BundleError("this export's key is not text") from None
