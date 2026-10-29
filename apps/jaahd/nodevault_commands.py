"""NodeVault's CP → node `cmd` frames (architecture §4.3): the six verbs the CP's durable workflows send a node.

    nodevault.install_key   {org_id, key_hash, public_key, blob, blob_kind}  → {held} | {error}
    nodevault.retire_key    {key_hash}                                       → {held}
    nodevault.held_keys     {}                                               → {identity_fp, capable, held}
    nodevault.wrap_for_node {org_id, target_node_id, target_identity_key, key_hashes} → {blobs:{hash: blob}}
    nodevault.rekey         {old_hash, new_hash, new_public_key, items:[{ref, envelope, aad}]} → {items:[{ref, ek}|{ref, error}]}
    nodevault.verify        {items:[{ref, envelope, aad}]}                   → {items:[{ref, ok, error?}]}

Each is a native daemon handler and a `commands.GUARDED_COMMAND_TYPES` member (§3.1); like every frame,
it runs only after `node_frame_auth` accepts its `cp_frame_proof`. A refusal replies `failed` with `error` (and
`reason`) set to ONE word of `ERRORS`; a per-item failure of `rekey`/`verify` is that item's `error` and
never aborts the batch. `held` is this node's truth after the call — the CP reconciles grant state from it.

NOTHING HERE LOGS OR RETURNS KEY MATERIAL. What leaves a handler is key hashes, error words, re-wrapped
data keys (`rekey`'s `ek`, wrapped to a public key) and key blobs (`wrap_for_node`, wrapped to the target's
identity key) — ciphertext the CP cannot open. Log lines carry hashes and words only, never a blob,
an envelope, an AAD or a plaintext.

Blob kinds (§2): `node` (setup) and `peer` (enrollment from a holder node) are wrapped to this node's
identity key under recipient `node:<NODE_ID>`; `chain` (rotation, E2) is wrapped to the NVK the blob's `k`
names (the CP's current key, which a lagging node may hold as `old`), under recipient `chain:<that hash>`. A `node` or `chain` install becomes this node's
current key; a `peer` one only when the node has none (a late joiner may receive the old key after the new).
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import threading
from typing import Any, Callable

import control_plane_api
import nodevault_crypto as nc
import nodevault_identity
import nodevault_store
from commands import native_command

log = logging.getLogger("jaahd.nodevault_commands")

INSTALL_KEY = "nodevault.install_key"
RETIRE_KEY = "nodevault.retire_key"
HELD_KEYS = "nodevault.held_keys"
WRAP_FOR_NODE = "nodevault.wrap_for_node"
REKEY = "nodevault.rekey"
VERIFY = "nodevault.verify"
COMMAND_TYPES = frozenset({INSTALL_KEY, RETIRE_KEY, HELD_KEYS, WRAP_FOR_NODE, REKEY, VERIFY})

BLOB_KINDS = frozenset({"node", "chain", "peer"})

# The closed `error` vocabulary. The first three are §4.3's install refusals; `missing_key` is a key this
# node does not hold (rekey's old key, verify's sealing key); `too_many_items` a batch over `MAX_ITEMS`;
# the rest are `nodevault_crypto`'s own reasons.
ERR_WRONG_RECIPIENT = nc.ERR_WRONG_RECIPIENT
ERR_HASH_MISMATCH = nc.ERR_HASH_MISMATCH
ERR_MISSING_WRAPPING_KEY = "missing_wrapping_key"
ERR_MISSING_KEY = "missing_key"
ERR_TOO_MANY_ITEMS = "too_many_items"
ERRORS = frozenset({
    ERR_WRONG_RECIPIENT, ERR_HASH_MISMATCH, ERR_MISSING_WRAPPING_KEY, ERR_MISSING_KEY, ERR_TOO_MANY_ITEMS,
    nc.ERR_MALFORMED, nc.ERR_OPEN_FAILED, nc.ERR_WRONG_KEY, nc.ERR_UNAVAILABLE,
})

# §4.3's batch bound for `rekey`/`verify`; `wrap_for_node` is held to it too.
MAX_ITEMS = 50
_MAX_REF_CHARS = 128
_MAX_AAD_BYTES = 4096
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")

# Native handlers run in worker threads, concurrently; the keystore has one writer by contract, so every
# keystore mutation here is serialized.
_WRITE_LOCK = threading.Lock()


class _Refused(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _ok(**result: Any) -> dict:
    return {"exit_code": 0, **result}


def _refused(command_type: str, error: str) -> dict:
    log.info("%s refused: %s", command_type, error)
    return {"exit_code": 1, "error": error, "reason": error}


def _keystore() -> nodevault_store.Keystore:
    return nodevault_store.Keystore()


def _held(keystore: nodevault_store.Keystore) -> list[str]:
    return [row["key_hash"] for row in keystore.keys()]


def _current(keystore: nodevault_store.Keystore) -> str | None:
    return next((row["key_hash"] for row in keystore.keys() if row["state"] == nodevault_store.STATE_CURRENT), None)


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not _HASH_RE.match(value):
        raise _Refused(nc.ERR_MALFORMED)
    return value


def _ref(value: Any) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or len(str(value)) > _MAX_REF_CHARS:
        raise _Refused(nc.ERR_MALFORMED)
    return value


def _aad(value: Any) -> bytes:
    if not isinstance(value, str) or not value or len(value) > (_MAX_AAD_BYTES * 4) // 3 + 4:
        raise _Refused(nc.ERR_MALFORMED)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _Refused(nc.ERR_MALFORMED) from None


def _items(args: dict) -> list:
    items = args.get("items")
    if not isinstance(items, list):
        raise _Refused(nc.ERR_MALFORMED)
    if len(items) > MAX_ITEMS:
        raise _Refused(ERR_TOO_MANY_ITEMS)
    return items


def _public_key_with_hash(value: Any, expected_hash: str) -> Any:
    """The public key `value` (b64 SPKI), refused `hash_mismatch` unless its DERIVED hash is `expected_hash`."""
    public = nc.public_key_from_b64(value)
    if nc.key_hash(public) != expected_hash:
        raise _Refused(ERR_HASH_MISMATCH)
    return public


# ── install_key / retire_key ──────────────────────────────────────────────────────────────────────────

def _install_key(args: dict) -> dict:
    key_hash = _hash(args.get("key_hash"))
    blob_kind = args.get("blob_kind")
    blob = args.get("blob")
    if blob_kind not in BLOB_KINDS or not isinstance(blob, dict):
        raise _Refused(nc.ERR_MALFORMED)
    org_id = args.get("org_id")
    _public_key_with_hash(args.get("public_key"), key_hash)
    keystore = _keystore()
    if not nodevault_identity.node_capable():
        raise _Refused(nc.ERR_UNAVAILABLE)
    with _WRITE_LOCK:
        current = _current(keystore)
        if key_hash in _held(keystore) and (blob_kind == "peer" or current == key_hash):
            return _ok(held=_held(keystore))   # idempotent: a duplicate delivery changes nothing
        if blob_kind == "chain":
            head, sep, predecessor = str(blob.get("k") or "").partition(":")
            if not sep or f"{head}:" != nc.NV_KEY_ID_PREFIX:
                raise _Refused(ERR_WRONG_RECIPIENT)
            unwrapping_key = keystore.key(_hash(predecessor))
            recipient = f"chain:{predecessor}"
        else:
            unwrapping_key = keystore.identity_key()
            node_id = control_plane_api.this_node_id()
            if not node_id:
                raise _Refused(nc.ERR_UNAVAILABLE)
            recipient = f"node:{node_id}"
        if unwrapping_key is None:
            raise _Refused(ERR_MISSING_WRAPPING_KEY)
        nvk = nc.unwrap_key_blob(blob, unwrapping_key=unwrapping_key, org_id=org_id, key_hash_hex=key_hash,
                                 recipient=recipient)
        # A held key reaches here only to be promoted (a peer delivery that landed before its chain blob).
        make_current = blob_kind != "peer" or current is None
        keystore.install_key(nvk, make_current=make_current)
        log.info("nodevault: installed key %s from a %s blob (current=%s)", key_hash, blob_kind, make_current)
        return _ok(held=_held(keystore))


def _retire_key(args: dict) -> dict:
    key_hash = _hash(args.get("key_hash"))
    keystore = _keystore()
    with _WRITE_LOCK:
        if key_hash in _held(keystore):
            # The CP retires only a key it no longer seals under; a node whose own `current` lags (a peer
            # delivery out of order) must not wedge the retirement on it.
            if _current(keystore) == key_hash:
                keystore.clear_current()
            keystore.remove_key(key_hash)
            log.info("nodevault: retired key %s", key_hash)
        return _ok(held=_held(keystore))   # an absent key is already retired


def _held_keys(_args: dict) -> dict:
    keystore = _keystore()
    return _ok(identity_fp=keystore.fingerprint(), capable=nodevault_identity.node_capable(),
               held=_held(keystore))


# ── wrap_for_node ─────────────────────────────────────────────────────────────────────────────────────

def _wrap_for_node(args: dict) -> dict:
    hashes = args.get("key_hashes")
    if not isinstance(hashes, list):
        raise _Refused(nc.ERR_MALFORMED)
    if len(hashes) > MAX_ITEMS:
        raise _Refused(ERR_TOO_MANY_ITEMS)
    hashes = [_hash(h) for h in hashes]
    target_node_id = args.get("target_node_id")
    if not isinstance(target_node_id, str) or not target_node_id:
        raise _Refused(nc.ERR_MALFORMED)
    target = nc.public_key_from_b64(args.get("target_identity_key"))
    wrapping_key_id = nc.nik_key_id(nc.key_hash(target))
    keystore = _keystore()
    blobs = {}
    for key_hash in dict.fromkeys(hashes):
        nvk = keystore.key(key_hash)
        if nvk is None:
            continue   # not held here: absent from `blobs`, so the CP asks another holder
        blobs[key_hash] = nc.wrap_key_blob(nvk, wrapping_public_key=target, wrapping_key_id=wrapping_key_id,
                                           org_id=args.get("org_id"), recipient=f"node:{target_node_id}")
    log.info("nodevault: wrapped %d of %d key(s) for node identity %s", len(blobs), len(hashes),
             wrapping_key_id)
    return _ok(blobs=blobs)


# ── rekey / verify ────────────────────────────────────────────────────────────────────────────────────

def _per_item(items: list, one: Callable[[dict], dict]) -> list[dict]:
    """`one(item)` for each item; a refusal is that item's `error`, never the batch's."""
    out = []
    for raw in items:
        ref = raw.get("ref") if isinstance(raw, dict) else None
        try:
            if not isinstance(raw, dict):
                raise _Refused(nc.ERR_MALFORMED)
            ref = _ref(raw.get("ref"))
            out.append({"ref": ref, **one(raw)})
        except _Refused as exc:
            out.append({"ref": ref if _safe_ref(ref) else None, "error": exc.error})
        except nc.NodeVaultCryptoError as exc:
            out.append({"ref": ref if _safe_ref(ref) else None, "error": _word(exc.reason)})
        except nodevault_store.KeystoreError:
            out.append({"ref": ref if _safe_ref(ref) else None, "error": nc.ERR_UNAVAILABLE})
    return out


def _safe_ref(ref: Any) -> bool:
    try:
        _ref(ref)
        return True
    except _Refused:
        return False


def _word(reason: str) -> str:
    return reason if reason in ERRORS else nc.ERR_MALFORMED


def _rekey(args: dict) -> dict:
    old_hash = _hash(args.get("old_hash"))
    new_hash = _hash(args.get("new_hash"))
    new_public = _public_key_with_hash(args.get("new_public_key"), new_hash)
    items = _items(args)
    old_nvk = _keystore().key(old_hash)
    if old_nvk is None:
        raise _Refused(ERR_MISSING_KEY)

    def one(item: dict) -> dict:
        return {"ek": nc.rekey_secret(item.get("envelope"), aad=_aad(item.get("aad")), old_nvk=old_nvk,
                                      new_public_key=new_public)}

    out = _per_item(items, one)
    log.info("nodevault: rekeyed %d of %d item(s) from %s to %s",
             sum("ek" in r for r in out), len(out), old_hash, new_hash)
    return _ok(items=out)


def _verify(args: dict) -> dict:
    items = _items(args)
    keystore = _keystore()
    nvks: dict[str, Any] = {}

    def one(item: dict) -> dict:
        envelope = item.get("envelope")
        key_id = envelope.get("k") if isinstance(envelope, dict) else None
        head, sep, key_hash = str(key_id or "").partition(":")
        if not sep or f"{head}:" != nc.NV_KEY_ID_PREFIX:
            raise _Refused(nc.ERR_WRONG_KEY)
        key_hash = _hash(key_hash)
        if key_hash not in nvks:
            nvks[key_hash] = keystore.key(key_hash)
        if nvks[key_hash] is None:
            raise _Refused(ERR_MISSING_KEY)
        nc.verify_secret(envelope, aad=_aad(item.get("aad")), nvk=nvks[key_hash])
        return {"ok": True}

    out = [r if "error" not in r else {**r, "ok": False} for r in _per_item(items, one)]
    log.info("nodevault: verified %d of %d item(s)", sum(r["ok"] for r in out), len(out))
    return _ok(items=out)


_HANDLERS: dict[str, Callable[[dict], dict]] = {
    INSTALL_KEY: _install_key, RETIRE_KEY: _retire_key, HELD_KEYS: _held_keys,
    WRAP_FOR_NODE: _wrap_for_node, REKEY: _rekey, VERIFY: _verify,
}


@native_command(*sorted(COMMAND_TYPES))
def handle(command_type: str, args: dict, _run: Callable[..., dict]) -> dict:
    """The six verbs' one entry point. A refusal is a closed-vocabulary word; anything unexpected is
    logged by TYPE only (an exception message could quote an argument) and answered `malformed`."""
    try:
        return _HANDLERS[command_type](args if isinstance(args, dict) else {})
    except _Refused as exc:
        return _refused(command_type, exc.error)
    except nc.NodeVaultCryptoError as exc:
        return _refused(command_type, _word(exc.reason))
    except nodevault_store.KeystoreError:
        log.warning("%s: the keystore refused an unsafe file (see the keystore's own log)", command_type)
        return _refused(command_type, nc.ERR_UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001 — never let a message (which could echo input) reach the reply
        log.warning("%s failed: %s", command_type, type(exc).__name__)
        return _refused(command_type, nc.ERR_MALFORMED)
