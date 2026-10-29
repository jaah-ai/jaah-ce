"""Encryption at rest for `credentials.config` — two tiers per row, sealed in the browser, opened on
demand (the `credentials` table only — the DBOS-checkpointed stores keep the v1 envelope).

THE STORED VALUE (`Credential.config_sealed`, the `config` JSONB column) is
``{"v": 2, "t1": {"n", "ct"}, "t2": {"<field>": {"v", "k", "alg", "ek", "n", "ct"}}}``:

  * TIER 1 — every non-secret field (base URL, email, tenant id, ...) as ONE AES-256-GCM envelope under
    `CREDENTIAL_ENCRYPTION_KEY` (`resolve_credential_encryption_key`), so the console lists and edits a
    row with no KMS call. AAD `credentials.config|v2|<credential id>`.
  * TIER 2 — every `kind == "secret"` field (and, fail-closed, every key the registry does not
    classify, `registry.secret_keys`) as ITS OWN hybrid envelope: a random AES-256-GCM data key
    encrypts the value; the data key is wrapped under the deployment's RSA-4096 public key
    (`app/credential_keys.py`, RSAES-OAEP-SHA-256). Wrapping needs only the public half, so the browser
    seals a typed secret before it leaves the page (`apps/web/src/lib/credentialSeal.ts`) and the API
    never sees the plaintext; server-side writers (OAuth grants, rotations) seal the same way. AAD
    `credentials.secret|v2|<credential id>|<field>|<destination>`, the destination being the row's own
    host field or the type's default (`registry.sealing_destination`) — rebuilt from the CURRENT tier 1 at
    open time, so an envelope moved to another row, another field or a re-pointed URL fails to open and
    nothing is sent. Only `type`, `name` and `description` stay plaintext.

WHAT THE ORM SEES: `Credential.config` is a property. Reading it opens tier 1 and presents each secret
as a `SealedSecret` marker (truthy, never renderable, never a str), so listing, `secrets_set` and the
edit form cost no private-key operation. Writing it seals: a `str` under a secret key is wrapped here, a
`SealedSecret` (kept from the stored row, or posted by the browser) is stored as-is. The plaintext of a
secret exists in this process only inside `open_secret`, the one seam that unwraps (KMS `Decrypt` off a
local stack, through `credential_keys.unwrap_data_key`'s bounded cache), called only where the secret is
consumed — never by a roster, a by-id read or a PATCH.

WHY NOT the alternatives: disk encryption and `pgcrypto` still hand plaintext to any SQL reader; the v1
whole-config AES envelope (this module's own previous design) put every secret in the API process's
memory on every ORM load, bound nothing to the row, and reached the server in the clear from the browser.
KMS was rejected then for its per-decrypt call and IAM surface; it is accepted now because decrypts
happen only on consumption, the public half needs no call at all, and the private half never sits in
a process any SQL or memory read can reach.

KEY ROTATION, BOTH TIERS: each tier is a KEYRING — one current key every write uses, plus the keys a
rotation retired, which only open. Tier 1's ring is `CREDENTIAL_ENCRYPTION_KEY` + the comma-separated
`CREDENTIAL_ENCRYPTION_PREVIOUS_KEYS` (`resolve_credential_key_ring`); a ciphertext names its key as a
`<key id>:` prefix on `ct`, and one without a prefix (everything sealed before the keyring) is tried under
every key in the ring — AES-GCM authenticates, so the wrong key fails rather than misreads. Tier 2's ring
is `app/credential_keys.py` (`provider_for`, by the envelope's `k`). `reseal_to_current_keys` moves one
stored value onto the current keys — tier 1 re-sealed, each tier-2 DATA KEY rewrapped (the secret's own
ciphertext is untouched and its plaintext never materialises) — and verifies it opens before it is
returned; `app/credential_reseal.py` runs it over the table.

NODEVAULT: a tier-2 envelope may instead be sealed under the org's NodeVault key — the same
shape and AAD, `k = "nv:<key hash>"` (`credential_keys.NodeVaultPublicKeyProvider`). The control plane holds
no NodeVault private key, so `open_sealed`/`open_secret` refuse such an envelope with `NodeVaultSealed`
(AC-8.1); only the org's nodes open it. `Credential.nodevault` says which provider a row's secrets are under,
and the invariant `nodevault ⇔ every tier-2 k starts with "nv:"` is checked over every write's final config
(`check_nodevault_invariant`, and at flush `refuse_broken_nodevault_invariant`). KMS rotation never touches
an `nv:` envelope.

The v1 primitives (`seal`/`unseal`, a fixed per-home AAD) stay for the durable stores
(`DURABLE_GIT_TOKEN_AAD`, `DURABLE_CLOUD_NODE_AAD`); for `credentials.config` a v1 value is refused —
migration 0283 re-sealed every row.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from typing import Any, Mapping, Optional
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import credential_keys
from app.credential_keys import (
    WRAP_ALGORITHM,
    NodeVaultPublicKeyProvider,
    NodeVaultSealed,
    NodeVaultUnavailable,
    SecretKeyProvider,
    is_nodevault_key_id,
)
from app.credentials.registry import sealing_destination, secret_keys
from app.dev_bypass import is_fixture_env, is_local_env
from app.fixture_keys import FIXTURE_CREDENTIAL_KEY

# Pinned literal: spelled by the ECS task definition and the local compose stack; a rename silently
# unconfigures the key.
KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY"
# The keys a tier-1 rotation retired: comma- or whitespace-separated, open only (never sealed under).
PREVIOUS_KEYS_ENV = "CREDENTIAL_ENCRYPTION_PREVIOUS_KEYS"
# Between a ciphertext's key id and its base64 body; outside the base64 alphabet, so unambiguous.
_KID_SEP = ":"

# AES-256 — 32 raw bytes, however they were spelled on the way in.
_KEY_BYTES = 32
# See `_decode_key` for why a key with fewer distinct bytes than this is refused.
_MIN_DISTINCT_KEY_BYTES = 16
# AES-GCM's standard nonce length.
_NONCE_BYTES = 12

# Envelope keys. `v` lets a future key rotation or cipher change recognise its own ciphertexts.
_V, _N, _CT = "v", "n", "ct"
_VERSION = 1

# Separator for `app/durable/git_token.py`'s checkpointed OAuth grant in DBOS's system DB. Distinct
# from every other separator so a `durable_dbos_sys` write cannot be lifted into the credential store
# or vice versa.
DURABLE_GIT_TOKEN_AAD = b"durable.git_token|v1"

# Separator for `app/durable/cloud_node.py` (node agent token, Tailscale key) and
# `app/durable/bootstrap_key.py`. One separator for both: a lift within this domain requires write
# access that already covers the whole workflow input. Values are kept apart inside the plaintext by
# `cloud_node.seal_secret`'s kind tag.
DURABLE_CLOUD_NODE_AAD = b"durable.cloud_node|v1"

# The local-dev key seed this repository once published (like `app/auth.py::_DEV_SESSION_SECRET`): refused as
# the current key everywhere. A local stack keeps it as a decrypt-only previous key, so rows sealed under it
# before `./dc` generated the stack's own key still open. Hashed to 32 bytes below.
_DEV_KEY_SEED = "dev-credential-encryption-key-change-me"

# ── The v2 stored shape ──────────────────────────────────────────────────────────────────────────────
STORED_VERSION = 2
_T1, _T2 = "t1", "t2"
# Tier-2 envelope keys: the wrapping key's id, the wrap algorithm, the wrapped data key, nonce, ciphertext.
_K, _ALG, _EK = "k", "alg", "ek"
_TIER1_AAD_PREFIX = b"credentials.config|v2|"
_TIER2_AAD_PREFIX = b"credentials.secret|v2|"
# Bounds a posted (browser-sealed) envelope must respect: a 4096-bit OAEP block is 512 bytes and a
# 2048-bit one 256; a value is at most `routes/credentials/credential_wire.py::_VALUE_MAX_LEN` characters (the GCM tag
# adds 16 bytes; UTF-8 up to 4 bytes per character).
_MAX_WRAPPED_BYTES = 1024
_MAX_SECRET_CIPHERTEXT_BYTES = 4 * 8192 + 16


def _decode_key(raw: str) -> bytes:
    """`raw` as 32 raw bytes, or a ValueError naming what was wrong — never echoing the value.

    Base64 (padded or not) and hex are both accepted, since both are what key generators emit.
    """
    text = raw.strip()
    for decode in (_from_hex, _from_b64):
        decoded = decode(text)
        if decoded is not None and len(decoded) == _KEY_BYTES:
            # A foot-gun guard, not a cryptographic test: a typed or padded value can decode to 32
            # bytes. Random 32 bytes hold ~30 distinct values; fewer than 16 is practically impossible.
            if len(set(decoded)) < _MIN_DISTINCT_KEY_BYTES:
                raise ValueError(
                    f"{KEY_ENV} decodes to {_KEY_BYTES} bytes but only "
                    f"{len(set(decoded))} distinct ones, which is not a randomly generated key — it "
                    "looks like a typed or padded value. Generate one: `openssl rand -base64 32`."
                )
            return decoded
    raise ValueError(
        f"{KEY_ENV} must decode to exactly {_KEY_BYTES} bytes (AES-256) from base64 or hex — the value "
        f"present decodes to something else. Generate one: `openssl rand -base64 32`."
    )


def _from_hex(text: str) -> bytes | None:
    # Length-checked first so hex and base64 cannot both claim a value.
    if len(text) != _KEY_BYTES * 2:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _from_b64(text: str) -> bytes | None:
    """Standard or url-safe base64, padded or not (the caller's error just says "base64")."""
    padded = text + "=" * (-len(text) % 4)
    # `altchars` rather than `urlsafe_b64decode`, which takes no `validate`.
    for altchars in (None, b"-_"):
        try:
            # `validate=True`: stray characters are refused, not silently dropped.
            return base64.b64decode(padded, altchars=altchars, validate=True)
        except (binascii.Error, ValueError):
            continue
    return None


def _is_dev_seed(raw: str) -> bool:
    # Bytes: `hmac.compare_digest` raises TypeError on a non-ASCII str.
    return hmac.compare_digest(raw.strip().encode(), _DEV_KEY_SEED.encode())


def _is_fixture_key(raw: str) -> bool:
    """Does `raw` decode — hex, or base64 in any spelling `_decode_key` reads — to the fixture key's bytes?"""
    text = raw.strip()
    fixture = base64.b64decode(FIXTURE_CREDENTIAL_KEY)
    return any(
        decoded is not None and hmac.compare_digest(decoded, fixture) for decoded in (_from_hex(text), _from_b64(text))
    )


def is_published_key(raw: str) -> bool:
    """Is `raw` a tier-1 key this repository publishes: the dev seed, or any spelling of the test fixture's key
    (`app/fixture_keys.py`)? One of the two homes of that question (the other: `app/credential_keys.py`'s
    keypair twin); what `app/ce_bootstrap.py` counts as absent."""
    return _is_dev_seed(raw) or _is_fixture_key(raw)


def _refuse_fixture_key(raw: str, variable: str) -> None:
    if _is_fixture_key(raw) and not is_fixture_env():
        raise RuntimeError(
            f"{variable} is the test fixture key published in app/fixture_keys.py, which is refused when APP_ENV "
            "is not 'local', 'ci' or 'test'. Generate one: `openssl rand -base64 32`."
        )


def resolve_credential_encryption_key() -> bytes:
    """The AES-256 key tier 1 (and the durable stores) are sealed under, or a refusal to start.

    Unset and the published `_DEV_KEY_SEED` are the same input (as in
    `app/auth.py::resolve_session_secret`), refused in every environment: `./dc` generates a real key into a
    local checkout's `.env.local`. The test
    fixture's key (`app/fixture_keys.py`) is refused outside a fixture env, cloud and community edition alike.
    What counts as published is `is_published_key`'s, and only its.

    Not cached, so tests can monkeypatch the environment and a redeploy picks up a rotated key.
    """
    # The community edition's generated key, in every process that reads one (a no-op in the cloud).
    from app import ce_bootstrap

    ce_bootstrap.ensure_secrets()
    # Stripped first, so whitespace-only reads as unset rather than a malformed key.
    raw = os.environ.get(KEY_ENV, "").strip()
    if raw and not _is_dev_seed(raw):
        _refuse_fixture_key(raw, KEY_ENV)
        return _decode_key(raw)
    cause = "is set to the published dev seed from app/credential_crypto.py" if raw else "is unset"
    raise RuntimeError(
        f"{KEY_ENV} must be set to a real value — it {cause}, and `credentials.config` cannot be encrypted at "
        "rest without it. Refusing to start rather than falling back to plaintext or a key that is in git. "
        "A local stack: run it through ./dc, which generates one into .env.local. A deployment: "
        "`openssl rand -base64 32`, in the environment's app-secrets entry under `credential_encryption_key`."
    )


def key_id_of(key: bytes) -> str:
    """The tier-1 key id stamped on a ciphertext: 8 hex characters of a domain-separated SHA-256 of the
    key — names the key without revealing anything usable about it."""
    return hashlib.sha256(b"credential-encryption-key-id|" + key).hexdigest()[:8]


def resolve_credential_key_ring() -> tuple[bytes, tuple[bytes, ...]]:
    """`(current, previous)`: the key every tier-1 (and durable-store) write seals under, and the keys a
    rotation retired, which only open. Refuses to start on a malformed previous key, naming its position
    and never its value. A previous key equal to the current one is dropped; duplicates collapse.

    The published dev seed is a previous key on a local stack whether listed or not (rows a stack sealed
    before `./dc` generated its own key), and refused as one anywhere else; the fixture key is accepted as a
    previous key only where it is accepted as the current one.
    """
    current = resolve_credential_encryption_key()
    previous: list[bytes] = []
    if is_local_env():
        previous.append(hashlib.sha256(_DEV_KEY_SEED.encode()).digest())
    entries = [e for e in re.split(r"[\s,]+", os.environ.get(PREVIOUS_KEYS_ENV, "")) if e]
    for position, entry in enumerate(entries, start=1):
        _refuse_fixture_key(entry, f"{PREVIOUS_KEYS_ENV} entry {position}")
        if _is_dev_seed(entry):
            if not is_local_env():
                raise RuntimeError(
                    f"{PREVIOUS_KEYS_ENV} entry {position} is the published dev seed, which is refused "
                    "when APP_ENV is not 'local'."
                )
            key = hashlib.sha256(_DEV_KEY_SEED.encode()).digest()
        else:
            try:
                key = _decode_key(entry)
            except ValueError:
                raise ValueError(
                    f"{PREVIOUS_KEYS_ENV} entry {position} is not a {_KEY_BYTES}-byte key in base64 or hex "
                    "with enough distinct bytes to be a generated one."
                ) from None
        if key != current and key not in previous:
            previous.append(key)
    ids = [key_id_of(k) for k in (current, *previous)]
    if len(set(ids)) != len(ids):
        raise RuntimeError(f"two keys in the {KEY_ENV} ring share a key id; generate a fresh current key")
    return current, tuple(previous)


def current_key_id() -> str:
    """The tier-1 key id every write now seals under."""
    return key_id_of(resolve_credential_encryption_key())


def _aes_seal(plaintext: bytes, aad: bytes) -> tuple[str, str]:
    """`(nonce, ct)` base64 under the ring's current key. The `<key id>:` prefix is written only while a
    ring EXISTS (a previous key is configured, or a local stack, which always carries the dev seed): with none, every ciphertext keeps the legacy unprefixed
    shape, which a release from before the keyring also opens — so this release's own deploy overlap
    cannot strand a row the outgoing task must read. Unprefixed is tried under every key, so both shapes
    open under any ring."""
    current, previous = resolve_credential_key_ring()
    nonce = os.urandom(_NONCE_BYTES)
    ct = base64.b64encode(AESGCM(current).encrypt(nonce, plaintext, aad)).decode()
    if previous:
        ct = key_id_of(current) + _KID_SEP + ct
    return base64.b64encode(nonce).decode(), ct


def _aes_open(nonce_b64: str, ct_field: str, aad: bytes) -> tuple[bytes, str]:
    """`(plaintext, key id that opened it)`. `binascii.Error`/`ValueError` for undecodable fields and
    `InvalidTag` when no key in the ring opens it — the callers' existing error arms."""
    current, previous = resolve_credential_key_ring()
    ring = {key_id_of(k): k for k in (current, *previous)}
    nonce = base64.b64decode(nonce_b64, validate=True)
    kid, sep, body = ct_field.partition(_KID_SEP)
    if sep:
        candidates = [ring[kid]] if kid in ring else []
        ct = base64.b64decode(body, validate=True)
    else:
        candidates = list(ring.values())
        ct = base64.b64decode(ct_field, validate=True)
    for key in candidates:
        try:
            return AESGCM(key).decrypt(nonce, ct, aad), key_id_of(key)
        except InvalidTag:
            continue
    raise InvalidTag()


# ── The v1 envelope (durable stores; `credentials.config` refuses it) ──────────────────────────────


def _envelope_shaped(value: Any) -> bool:
    """Does this stored value have a v1 envelope's shape — exactly `{v, n, ct}`, with `n` and `ct`
    strings — whatever version it claims?

    The key set is exact: an old release in a deploy overlap writes `{**envelope, "token": ...}`
    (config merged onto the raw envelope), which a subset test would read as the stale config while the
    new secret sits in the clear. Exact keys make `unseal` refuse it loudly.
    """
    return (
        isinstance(value, dict)
        and set(value) == {_V, _N, _CT}
        and isinstance(value.get(_N), str)
        and isinstance(value.get(_CT), str)
    )


def seal(value: dict[str, Any], *, aad: bytes) -> dict[str, Any]:
    """Seal `value` into a v1 envelope bound to the domain separator `aad` (the durable stores' shape).

    The separator is an argument so every home shares one envelope implementation while keeping
    ciphertexts non-interchangeable. No default: a default would let a forgetful caller seal into
    another home's domain. Never a no-op.
    """
    # Stable, minimal encoding; the plaintext is never read by a human.
    plaintext = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    nonce, ct = _aes_seal(plaintext, aad)
    return {_V: _VERSION, _N: nonce, _CT: ct}


def unseal(value: Any, *, aad: bytes, label: str) -> Any:
    """Open a v1 envelope sealed under `aad`, or raise a ValueError naming `label`. Never a pass-through:
    nothing writes these stores in the clear, so a non-envelope is a bug, a rotated key or a tamper.

    `label` is a caller-supplied literal naming the store, never a value out of the envelope.
    """
    if not _envelope_shaped(value):
        # The message carries the TYPE and nothing out of the value.
        raise ValueError(
            f"{label} was read as {type(value).__name__}, not an encrypted envelope. Nothing writes "
            "this store in the clear, so it was not written by this system."
        )
    if value.get(_V) != _VERSION:
        # An unknown version is refused, never passed through — a pass-through would turn a future
        # cipher change into silent auth failures during its deploy overlap.
        raise ValueError(
            f"{label} carries envelope version {value.get(_V)!r}, which this release cannot "
            f"open (it writes and reads version {_VERSION}). This row was written by a NEWER release."
        )
    try:
        plaintext, _kid = _aes_open(value[_N], value[_CT], aad)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} envelope is not decodable base64") from exc
    except InvalidTag as exc:
        # Wrong key, tampering, and an envelope from another home are indistinguishable by design.
        raise ValueError(
            f"{label} failed authenticated decryption: either the row was sealed under a "
            f"different {KEY_ENV} or the stored value was tampered with."
        ) from exc
    return json.loads(plaintext)


# ── The sealed-secret marker ─────────────────────────────────────────────────────────────────────────


class SealedSecret:
    """One tier-2 envelope as it sits in `Credential.config`: proof a secret is set, never its value.

    Truthy (so `secrets_set` and blankness checks see "set"), comparable by envelope, hashable; NOT a
    `str` and not renderable — `str()`/f-string interpolation raises, `json.dumps` raises, `repr` shows
    nothing — so a consumer that forgot `open_secret` fails loudly rather than sending an envelope to a
    vendor or a log.
    """

    __slots__ = ("_envelope",)

    def __init__(self, envelope: Mapping[str, Any]) -> None:
        self._envelope = dict(envelope)

    @property
    def envelope(self) -> dict[str, Any]:
        """The stored envelope, a copy: what the column holds for this field."""
        return dict(self._envelope)

    @property
    def key_id(self) -> str:
        return str(self._envelope.get(_K, ""))

    def __bool__(self) -> bool:
        return True

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SealedSecret) and other._envelope == self._envelope

    def __hash__(self) -> int:
        return hash(self._envelope.get(_EK))

    def __repr__(self) -> str:
        return "<SealedSecret>"

    def __str__(self) -> str:
        raise TypeError(
            "a SealedSecret is opened with credential_crypto.open_secret(row, field), never rendered"
        )


# ── AADs ─────────────────────────────────────────────────────────────────────────────────────────────


def _id_bytes(credential_id: UUID | str) -> bytes:
    text = str(credential_id).strip().lower()
    # Canonical hyphenated form; a malformed id is a caller bug, refused before anything is sealed.
    return str(UUID(text)).encode()


def tier1_aad(credential_id: UUID | str) -> bytes:
    """`credentials.config|v2|<id>`."""
    return _TIER1_AAD_PREFIX + _id_bytes(credential_id)


def tier2_aad(credential_id: UUID | str, field: str, destination: str) -> bytes:
    """`credentials.secret|v2|<id>|<field>|<destination>` — MIRRORED in `credentialSeal.ts`.

    `field` may not contain `|` (a registry key never does); the destination is last so it may.
    """
    if not isinstance(field, str) or not field or "|" in field:
        raise ValueError("a secret field name is a non-empty string without '|'")
    try:
        # JSON parsing admits a lone surrogate that UTF-8 cannot spell; a ValueError, not a 500.
        destination_bytes = destination.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"the destination URL for {field} is not encodable text") from None
    return _TIER2_AAD_PREFIX + _id_bytes(credential_id) + b"|" + field.encode() + b"|" + destination_bytes


# ── Tier 2: one hybrid envelope per secret field ─────────────────────────────────────────────────────


def seal_secret(
    value: str,
    *,
    credential_id: UUID | str,
    field: str,
    destination: str,
    key_provider: Optional[SecretKeyProvider] = None,
) -> SealedSecret:
    """`value` as a fresh tier-2 envelope for (`credential_id`, `field`, `destination`).

    The server-side writer's path (OAuth grants, key rotations, a plaintext secret posted by a
    non-browser client): a random data key, wrapped under the public key — no private operation, no
    KMS call. The browser's `sealSecret` produces the identical shape.
    """
    if not isinstance(value, str):
        raise ValueError(f"the secret field {field!r} must be a string")
    provider = key_provider or credential_keys.provider()
    data_key = os.urandom(credential_keys.DATA_KEY_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    aad = tier2_aad(credential_id, field, destination)
    try:
        plaintext = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"the secret field {field!r} is not encodable text") from None
    ct = AESGCM(data_key).encrypt(nonce, plaintext, aad)
    wrapped = credential_keys.wrap_data_key(data_key, key_provider=provider)
    return SealedSecret(
        {
            _V: STORED_VERSION,
            _K: provider.key_id,
            _ALG: WRAP_ALGORITHM,
            _EK: base64.b64encode(wrapped).decode(),
            _N: base64.b64encode(nonce).decode(),
            _CT: base64.b64encode(ct).decode(),
        }
    )


def _b64(value: Any, *, label: str, max_bytes: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is not a base64 string")
    if len(value) > (max_bytes * 4) // 3 + 4:
        raise ValueError(f"{label} is longer than an envelope field can be")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{label} is not decodable base64") from None
    if not decoded or len(decoded) > max_bytes:
        raise ValueError(f"{label} has an out-of-bounds length")
    return decoded


def _secret_envelope_parts(envelope: Mapping[str, Any], *, label: str) -> tuple[str, bytes, bytes, bytes]:
    """(`key id`, wrapped data key, nonce, ciphertext) out of a tier-2 envelope, or a ValueError naming
    `label` and no value. Exact key set, version 2, the one wrap algorithm, bounded decodable fields."""
    if not isinstance(envelope, Mapping) or set(envelope) != {_V, _K, _ALG, _EK, _N, _CT}:
        raise ValueError(f"{label} is not a sealed-secret envelope")
    if envelope.get(_V) != STORED_VERSION:
        raise ValueError(f"{label} carries envelope version {envelope.get(_V)!r}, not {STORED_VERSION}")
    if envelope.get(_ALG) != WRAP_ALGORITHM:
        raise ValueError(f"{label} names a wrap algorithm this release does not write")
    key_id = envelope.get(_K)
    if not isinstance(key_id, str) or not key_id or len(key_id) > 512:
        raise ValueError(f"{label} names no wrapping key")
    wrapped = _b64(envelope.get(_EK), label=f"{label} wrapped key", max_bytes=_MAX_WRAPPED_BYTES)
    nonce = _b64(envelope.get(_N), label=f"{label} nonce", max_bytes=_NONCE_BYTES)
    if len(nonce) != _NONCE_BYTES:
        raise ValueError(f"{label} nonce has the wrong length")
    ct = _b64(envelope.get(_CT), label=f"{label} ciphertext", max_bytes=_MAX_SECRET_CIPHERTEXT_BYTES)
    return key_id, wrapped, nonce, ct


class NodeVaultInvariantError(ValueError):
    """`Credential.nodevault` disagrees with its envelopes, or an envelope names a NodeVault key
    the write may not use (a 422 at the route, where a malformed envelope is a 400). The message names a field, never a value."""


def sealed_secret_from_wire(
    value: Any,
    *,
    field: str,
    nodevault: bool = False,
    session: Any = None,
    org_id: Any = None,
) -> SealedSecret:
    """A browser-posted envelope for `field`, checked for shape and bounds (never opened), or a
    ValueError naming the field. The wrapping key must be one the write may name: an envelope under any
    other key could never be opened, and refusing it at the write is the honest moment.

    An `nv:` envelope is admitted only for a NodeVault credential (`nodevault=True`), and only under the
    org's `current` or `previous` NodeVault key (`previous`: a browser that sealed just before a rotation's
    promotion; the rekey sweeps it) — so the caller threads `session` and the row's `org_id`. Any other
    envelope must be under this deployment's KMS key. Whether the row's envelopes AGREE with its flag is
    the invariant's, over the merged config (`check_nodevault_invariant`), not this one field's."""
    from app.models.credentials import NODEVAULT_SEALABLE_KEY_STATES

    key_id, _wrapped, _nonce, _ct = _secret_envelope_parts(value, label=field)
    if is_nodevault_key_id(key_id):
        if not nodevault:
            raise NodeVaultInvariantError(
                f"{field} is sealed under NodeVault, but the credential is not a NodeVault credential"
            )
        key_hash = credential_keys.nodevault_key_hash_of(key_id)
        if key_hash is None or credential_keys.nodevault_provider(
            session, org_id, key_hash, states=NODEVAULT_SEALABLE_KEY_STATES, share_lock=True,
        ) is None:
            raise NodeVaultInvariantError(
                f"{field} was sealed under a NodeVault key that is not this organization's current or previous key"
            )
        return SealedSecret(value)
    if key_id != credential_keys.provider().key_id:
        raise ValueError(f"{field} was sealed under a key this deployment does not hold")
    return SealedSecret(value)


# ── NodeVault: the invariant, the derived key hash, and the migration's reseal ───────────────────────


def _nodevault_invariant_problem(key_ids: Mapping[str, str], *, nodevault: bool) -> Optional[str]:
    for field in sorted(key_ids):
        if is_nodevault_key_id(key_ids[field]) != nodevault:
            if nodevault:
                return f"{field} must be sealed under the organization's NodeVault key"
            return f"{field} is sealed under NodeVault, but the credential is not a NodeVault credential"
    return None


def check_nodevault_invariant(config: Mapping[str, Any], *, nodevault: bool) -> None:
    """Raise `NodeVaultInvariantError` unless `nodevault ⇔ every sealed secret of config is under an nv: key`.

    Checked over the FINAL config of a write — posted and kept fields together — so a PATCH can neither mix
    a kept KMS secret into a NodeVault credential nor keep an `nv:` one on a credential turned off."""
    problem = _nodevault_invariant_problem(
        {k: v.key_id for k, v in config.items() if isinstance(v, SealedSecret)}, nodevault=nodevault
    )
    if problem is not None:
        raise NodeVaultInvariantError(problem)


def check_nodevault_invariant_stored(stored: Any, *, nodevault: bool) -> None:
    """`check_nodevault_invariant` over a STORED column value, reading only each envelope's `k` — no
    tier-1 key, no private operation. A value without a tier-2 map has no envelope to disagree."""
    tier2 = stored.get(_T2) if isinstance(stored, dict) else None
    if not isinstance(tier2, dict):
        return
    key_ids = {f: str(e.get(_K, "")) for f, e in tier2.items() if isinstance(e, Mapping)}
    problem = _nodevault_invariant_problem(key_ids, nodevault=nodevault)
    if problem is not None:
        raise NodeVaultInvariantError(problem)


def refuse_broken_nodevault_invariant(session: Any, _flush_context: Any, _instances: Any) -> None:
    """`before_flush` listener (`app/db.py`): refuse a flush that would store a credential breaking the
    invariant, whichever writer built it. The route's 422 is the readable half; this is the backstop."""
    from app.models.credentials import Credential

    for obj in (*session.new, *session.dirty):
        if isinstance(obj, Credential) and obj.config_sealed is not None:
            check_nodevault_invariant_stored(obj.config_sealed, nodevault=bool(obj.nodevault))


def nodevault_key_hash(config: Mapping[str, Any]) -> Optional[str]:
    """The NodeVault key hash a credential's secrets are sealed under (A9: the envelopes' `k`, derived for
    listing, never stored twice), or None for a KMS credential. Envelopes under two NodeVault keys (mid-
    rekey) answer the first field's in sorted order — every field is swept to the newer one by the rekey."""
    for field in sorted(config):
        value = config[field]
        if isinstance(value, SealedSecret):
            key_hash = credential_keys.nodevault_key_hash_of(value.key_id)
            if key_hash is not None:
                return key_hash
    return None


def reseal_secret_to_nodevault(session: Any, row: Any, field: str, key_hash: str) -> SealedSecret:
    """`row.config[field]` opened under KMS and sealed under the ROW'S OWN org's NodeVault key `key_hash`
    (its `current` or `previous` key, looked up here so no caller can seal one org's secret to another's
    nodes), in this one call — the migration's only plaintext moment (arch §3.4 `seal`, R-d). The plaintext
    is a local of this frame: never returned, logged, cached or checkpointed; the result is ciphertext under
    the same AAD (row id, field, current destination), which only the org's nodes open."""
    from app.models.credentials import NODEVAULT_SEALABLE_KEY_STATES

    key_provider = credential_keys.nodevault_provider(
        session, row.org_id, key_hash, states=NODEVAULT_SEALABLE_KEY_STATES
    )
    if key_provider is None:
        raise ValueError("the key is not this organization's current or previous NodeVault key")
    config = row.config or {}
    value = config.get(field)
    if not isinstance(value, SealedSecret):
        raise ValueError(f"{field} is not a sealed secret on this row")
    if is_nodevault_key_id(value.key_id):
        raise ValueError(f"{field} is already sealed under NodeVault")
    return seal_secret(
        open_secret(row, field),
        credential_id=row.id,
        field=field,
        destination=sealing_destination(row.type, config),
        key_provider=key_provider,
    )


def open_sealed(
    sealed: SealedSecret,
    *,
    credential_id: UUID | str,
    field: str,
    destination: str,
    key_provider: Optional[SecretKeyProvider] = None,
) -> str:
    """The plaintext of one tier-2 envelope, bound to (`credential_id`, `field`, `destination`).

    THE PRIVATE OPERATION: the data key is unwrapped (KMS `Decrypt` off a local stack, once per envelope
    per cache TTL) and the value opened under the AAD the caller rebuilt from the CURRENT row. A moved,
    tampered or re-pointed envelope raises ValueError naming `field` only. A NodeVault envelope raises
    `NodeVaultUnavailable` (a `NodeVaultSealed`): this process holds no key that opens it (AC-8.1), so every
    consumer that reaches one is told so (D1, `app/credential_consumers.py`).
    """
    key_id, wrapped, nonce, ct = _secret_envelope_parts(sealed.envelope, label=field)
    if is_nodevault_key_id(key_id) or isinstance(key_provider, NodeVaultPublicKeyProvider):
        raise NodeVaultUnavailable(f"{field} is sealed under NodeVault; only the organization's nodes can open it")
    # The key the envelope names, current or retired (`credential_keys.provider_for`) — unless the caller
    # pinned one, which must then be that key.
    provider = key_provider or credential_keys.provider_for(key_id)
    if provider is None or key_id != provider.key_id:
        raise ValueError(
            f"{field} was sealed under a different wrapping key than this deployment holds"
        )
    data_key = credential_keys.unwrap_data_key(wrapped, key_provider=provider)
    try:
        plaintext = AESGCM(data_key).decrypt(nonce, ct, tier2_aad(credential_id, field, destination))
    except InvalidTag:
        raise ValueError(
            f"{field} failed authenticated decryption: the envelope was moved to another row or field, "
            "its row's URL was changed, or the stored value was tampered with. Re-enter the secret."
        ) from None
    return plaintext.decode("utf-8")


# ── Tier 1 + the stored column value ────────────────────────────────────────────────────────────────


def _seal_tier1(fields: dict[str, Any], *, credential_id: UUID | str) -> dict[str, str]:
    plaintext = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    nonce, ct = _aes_seal(plaintext, tier1_aad(credential_id))
    return {_N: nonce, _CT: ct}


def _open_tier1(envelope: Any, *, credential_id: UUID | str) -> tuple[dict[str, Any], str]:
    """Tier 1's fields and the id of the ring key that opened them."""
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {_N, _CT}
        or not isinstance(envelope.get(_N), str)
        or not isinstance(envelope.get(_CT), str)
    ):
        raise ValueError("credentials.config tier 1 is not an envelope")
    try:
        plaintext, kid = _aes_open(envelope[_N], envelope[_CT], tier1_aad(credential_id))
    except (binascii.Error, ValueError):
        raise ValueError("credentials.config tier 1 is not decodable base64") from None
    except InvalidTag:
        raise ValueError(
            "credentials.config failed authenticated decryption: either the row was sealed under a "
            f"different {KEY_ENV}, the envelope was moved to another row, or the stored value was "
            "tampered with."
        ) from None
    opened = json.loads(plaintext)
    if not isinstance(opened, dict):
        raise ValueError("credentials.config tier 1 did not hold a JSON object")
    return opened, kid


def seal_config(
    config: Mapping[str, Any],
    *,
    credential_id: UUID | str,
    type_slug: str,
    key_provider: Optional[SecretKeyProvider] = None,
) -> dict[str, Any]:
    """`config` as the stored column value for a row of type `type_slug` with id `credential_id`.

    Secret keys (`registry.secret_keys` over this very config, so an unclassified key is a secret) go to
    tier 2: a non-blank `str` is sealed here, a blank one is dropped (no secret), a `SealedSecret` is kept
    as-is (the "leave blank to keep" path and the browser's own envelopes), anything else is refused. Every other key goes to tier 1 and must be JSON;
    a `SealedSecret` under a non-secret key is refused (it could never be opened there). Tier 2 is
    bound to the destination the NEW tier 1 spells (`sealing_destination`), so a plaintext secret written
    beside a new URL binds to that URL. Never a no-op: this is the only write path.
    """
    if not isinstance(config, Mapping):
        raise ValueError("credentials.config must be a mapping")
    secrets = secret_keys(type_slug, config)
    destination = sealing_destination(type_slug, config)
    tier1: dict[str, Any] = {}
    tier2: dict[str, Any] = {}
    for key, value in config.items():
        if not isinstance(key, str):
            raise ValueError("credentials.config keys must be strings")
        if key in secrets:
            if isinstance(value, SealedSecret):
                tier2[key] = value.envelope
            elif isinstance(value, str):
                if not value.strip():
                    # A blank secret is NO secret (`entity_shape.is_blank` is the rule): it is
                    # not stored, so `secrets_set` and the roster's narrowing read its absence.
                    continue
                tier2[key] = seal_secret(
                    value,
                    credential_id=credential_id,
                    field=key,
                    destination=destination,
                    key_provider=key_provider,
                ).envelope
            else:
                raise ValueError(f"the secret field {key!r} must be a string or a sealed secret")
            continue
        if isinstance(value, SealedSecret):
            raise ValueError(f"the field {key!r} is not secret-classified and cannot hold a sealed secret")
        tier1[key] = value
    return {_V: STORED_VERSION, _T1: _seal_tier1(tier1, credential_id=credential_id), _T2: tier2}


def open_config(stored: Any, *, credential_id: UUID | str) -> dict[str, Any]:
    """The stored column value as the dict `Credential.config` presents: tier 1 opened, each tier-2
    field a `SealedSecret`. NO private operation. A plaintext row, or any shape but v2, is refused —
    nothing writes this column in the clear."""
    return _open_config_with_key_id(stored, credential_id=credential_id)[0]


def _open_config_with_key_id(stored: Any, *, credential_id: UUID | str) -> tuple[dict[str, Any], str]:
    """`open_config`, plus the id of the tier-1 key that opened it."""
    if (
        not isinstance(stored, dict)
        or set(stored) != {_V, _T1, _T2}
        or stored.get(_V) != STORED_VERSION
        or not isinstance(stored.get(_T2), dict)
    ):
        raise ValueError(
            f"credentials.config was read as {type(stored).__name__}, not a v{STORED_VERSION} sealed "
            "config. Nothing writes this column in the clear, so it was not written by this system."
        )
    opened, kid = _open_tier1(stored[_T1], credential_id=credential_id)
    for key, envelope in stored[_T2].items():
        if key in opened:
            raise ValueError(f"credentials.config holds {key!r} in both tiers")
        # Shape-checked (bounded, decodable) so a planted envelope fails here, not in a consumer.
        _secret_envelope_parts(envelope, label=key)
        opened[key] = SealedSecret(envelope)
    return opened, kid


# ── Rotation: moving a stored value onto the current keys ────────────────────────────────────────────


def stale_key_fields(stored: Any, *, credential_id: UUID | str) -> list[str]:
    """Which parts of a stored value are NOT on the current keys: `"t1"` for tier 1, then each tier-2
    field whose `k` is not the current wrapping key. Empty means current. Opens tier 1 (no private
    operation); raises ValueError, like `open_config`, for a value no key in either ring opens. A NodeVault
    envelope is never stale here: KMS rotation does not touch it (NodeVault rotates by its own rekey)."""
    opened, kid = _open_config_with_key_id(stored, credential_id=credential_id)
    current_wrap = credential_keys.provider().key_id
    stale = ["t1"] if kid != current_key_id() else []
    stale += [
        k
        for k, v in opened.items()
        if isinstance(v, SealedSecret) and v.key_id != current_wrap and not is_nodevault_key_id(v.key_id)
    ]
    return stale


def _rewrap(sealed: SealedSecret, *, aad: bytes, field: str) -> SealedSecret:
    """`sealed` with its data key rewrapped under the CURRENT wrapping key, then VERIFIED: the new block is
    unwrapped by the current key's private operation and the untouched ciphertext opened under it. The
    plaintext is checked and discarded, never decoded to a str."""
    key_id, wrapped, nonce, ct = _secret_envelope_parts(sealed.envelope, label=field)
    if is_nodevault_key_id(key_id):
        raise NodeVaultSealed(f"{field} is sealed under NodeVault; KMS rotation never rewraps it")
    old = credential_keys.provider_for(key_id)
    if old is None:
        raise ValueError(f"{field} was sealed under a wrapping key this process holds neither as current nor previous")
    current = credential_keys.provider()
    data_key = credential_keys.unwrap_data_key(wrapped, key_provider=old)
    new_wrapped = credential_keys.wrap_data_key(data_key, key_provider=current)
    # New bytes are never cached, so this is the current key's own private operation — the proof it opens.
    check = credential_keys.unwrap_data_key(new_wrapped, key_provider=current)
    if not hmac.compare_digest(check, data_key):
        raise ValueError(f"{field} did not open after rewrapping; the row is left as it was")
    try:
        AESGCM(check).decrypt(nonce, ct, aad)
    except InvalidTag:
        raise ValueError(f"{field} did not open after rewrapping; the row is left as it was") from None
    return SealedSecret({**sealed.envelope, _K: current.key_id, _EK: base64.b64encode(new_wrapped).decode()})


def reseal_to_current_keys(stored: Any, *, credential_id: UUID | str, type_slug: str) -> Optional[dict[str, Any]]:
    """`stored` moved onto the current keys of both tiers, or None when it already is.

    Tier 1 is re-sealed under the current AES key; each tier-2 envelope on a retired wrapping key keeps
    its nonce and ciphertext and gets its data key rewrapped (`_rewrap`, verified per field); an `nv:`
    envelope is skipped (`stale_key_fields`), copied byte-identical. The result
    is opened once more as a whole — tier 1 must read back identical — before it is returned, so a value
    this returns is one the current keys open. Raises ValueError, leaving nothing changed, otherwise."""
    stale = stale_key_fields(stored, credential_id=credential_id)
    if not stale:
        return None
    opened, _kid = _open_config_with_key_id(stored, credential_id=credential_id)
    tier1 = {k: v for k, v in opened.items() if not isinstance(v, SealedSecret)}
    destination = sealing_destination(type_slug, opened)
    tier2 = dict(stored[_T2])
    for field in stale:
        if field == "t1":
            continue
        tier2[field] = _rewrap(
            SealedSecret(tier2[field]), aad=tier2_aad(credential_id, field, destination), field=field
        ).envelope
    new_t1 = _seal_tier1(tier1, credential_id=credential_id) if "t1" in stale else stored[_T1]
    resealed = {_V: STORED_VERSION, _T1: new_t1, _T2: tier2}
    reopened, _ = _open_config_with_key_id(resealed, credential_id=credential_id)
    if {k: v for k, v in reopened.items() if not isinstance(v, SealedSecret)} != tier1:
        raise ValueError("credentials.config tier 1 did not read back identical after re-sealing")
    if stale_key_fields(resealed, credential_id=credential_id):
        raise ValueError("credentials.config is still on a retired key after re-sealing")
    return resealed


# ── The consumption seam ─────────────────────────────────────────────────────────────────────────────


def open_secret(row: Any, field: str) -> str:
    """The plaintext of `row.config[field]` — THE one seam that performs the private operation.

    Call it only where the secret is consumed (sent to the vendor, handed to a runner). The AAD is
    rebuilt from the row's id, the field and its CURRENT tier-1 destination, so a stored URL edited
    since the seal makes this raise rather than open. Raises `KeyError` for an absent field and
    `ValueError` for one that is not a sealed secret or cannot be opened.
    """
    config = row.config or {}
    if field not in config:
        raise KeyError(field)
    value = config[field]
    if not isinstance(value, SealedSecret):
        raise ValueError(f"{field} is not a sealed secret on this row")
    return open_sealed(
        value,
        credential_id=row.id,
        field=field,
        destination=sealing_destination(row.type, config),
    )


def opened_config(row: Any, *, keep_nodevault: bool = False) -> dict[str, Any]:
    """`row.config` with EVERY sealed secret opened — one private operation per secret field (cached).
    App code reaches it only through `data_access_events.opened_config_for`, which records the
    `credential_read`; tests may call it directly to read a row back.

    With `keep_nodevault`, a NodeVault envelope (`k = "nv:…"`) is left as its `SealedSecret` rather than
    raising `NodeVaultSealed`: the runner fetch hands it to the node sealed (`nodevault_wire`)."""
    config = dict(row.config or {})
    for key, value in list(config.items()):
        if isinstance(value, SealedSecret):
            if keep_nodevault and is_nodevault_key_id(value.key_id):
                continue
            config[key] = open_secret(row, key)
    return config


def nodevault_wire(row: Any, config: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Split `config` (from `opened_config(row, keep_nodevault=True)`) into what a node is sent: the
    config WITHOUT its NodeVault fields, and `{field: {envelope, aad}}` for each of them — the stored
    envelope as it is and its tier-2 AAD (base64), rebuilt from the CURRENT row exactly as `open_secret`
    would, so a re-pointed row fails to open on the node too. Ciphertext only: nothing here is opened."""
    plain: dict[str, Any] = {}
    sealed: dict[str, dict[str, Any]] = {}
    destination = sealing_destination(row.type, row.config or {})
    for key, value in config.items():
        if isinstance(value, SealedSecret) and is_nodevault_key_id(value.key_id):
            aad = tier2_aad(row.id, key, destination)
            sealed[key] = {"envelope": value.envelope, "aad": base64.b64encode(aad).decode()}
        else:
            plain[key] = value
    return plain, sealed


def has_sealed_secret(config: Mapping[str, Any], field: str) -> bool:
    """Whether `field` holds a sealed secret — the no-KMS presence test a consumer makes before opening."""
    return isinstance(config.get(field), SealedSecret)
