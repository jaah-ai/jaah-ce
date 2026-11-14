"""libsodium `crypto_box_seal` (anonymous sealed box) in pure standard-library Python.

GitHub's Actions-secrets API only accepts a value sealed to the repository's public key with a libsodium
sealed box; `jaah-forge secret set` uses this module to produce it. It is in-house because the daemon's
environment has no libsodium binding and the forge CLI is stdlib-only: the operator chose this over adding
PyNaCl. Verified against RFC 7748 / RFC 8439 / NaCl reference vectors and a sealed box made by
real libsodium (tests/test_forge_cli_sealed_box.py).

Pieces: X25519 (RFC 7748), HSalsa20 + XSalsa20 (Salsa20 spec), Poly1305 (RFC 8439), BLAKE2b (hashlib).
Not constant-time beyond reasonable care: it only encrypts, client-side, under a fresh ephemeral key.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import struct

_P = 2**255 - 19
_A24 = 121665
_SIGMA = struct.unpack("<4I", b"expand 32-byte k")


def x25519(scalar: bytes, u: bytes) -> bytes:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] = (k[31] & 127) | 64
    kn = int.from_bytes(k, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (kn >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def _rounds(x: list[int]) -> list[int]:
    x = list(x)

    def qr(a: int, b: int, c: int, d: int) -> None:
        for dst, s1, s2, r in ((b, a, d, 7), (c, b, a, 9), (d, c, b, 13), (a, d, c, 18)):
            v = (x[s1] + x[s2]) & 0xFFFFFFFF
            x[dst] ^= ((v << r) | (v >> (32 - r))) & 0xFFFFFFFF

    # Columns, then rows.
    quarters = ((0, 4, 8, 12), (5, 9, 13, 1), (10, 14, 2, 6), (15, 3, 7, 11),
                (0, 1, 2, 3), (5, 6, 7, 4), (10, 11, 8, 9), (15, 12, 13, 14))
    for _ in range(10):
        for quarter in quarters:
            qr(*quarter)
    return x


def _state(key: bytes, sixteen: bytes) -> list[int]:
    k, n = struct.unpack("<8I", key), struct.unpack("<4I", sixteen)
    s = _SIGMA
    return [s[0], *k[:4], s[1], *n, s[2], *k[4:], s[3]]


def hsalsa20(key: bytes, nonce16: bytes) -> bytes:
    z = _rounds(_state(key, nonce16))  # no feed-forward: take the diagonal and the nonce words
    return struct.pack("<8I", *(z[i] for i in (0, 5, 10, 15, 6, 7, 8, 9)))


def salsa20_stream(key: bytes, nonce8: bytes, length: int) -> bytes:
    out = bytearray()
    for counter in range((length + 63) // 64):
        st = _state(key, nonce8 + struct.pack("<Q", counter))
        out += struct.pack("<16I", *((a + b) & 0xFFFFFFFF for a, b in zip(_rounds(st), st, strict=True)))
    return bytes(out[:length])


def poly1305(key: bytes, msg: bytes) -> bytes:
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p, acc = (1 << 130) - 5, 0
    for i in range(0, len(msg), 16):
        acc = (acc + int.from_bytes(msg[i : i + 16] + b"\x01", "little")) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def secretbox(msg: bytes, nonce24: bytes, key: bytes) -> bytes:
    """crypto_secretbox_easy (XSalsa20-Poly1305): tag || ciphertext."""
    stream = salsa20_stream(hsalsa20(key, nonce24[:16]), nonce24[16:], 32 + len(msg))
    ct = bytes(a ^ b for a, b in zip(msg, stream[32:], strict=True))
    return poly1305(stream[:32], ct) + ct


def seal(public_key: bytes, message: bytes, *, ephemeral_secret: bytes | None = None) -> bytes:
    if len(public_key) != 32:
        raise ValueError("public key must be 32 bytes")
    esk = ephemeral_secret if ephemeral_secret is not None else secrets.token_bytes(32)
    epk = x25519(esk, (9).to_bytes(32, "little"))
    nonce = hashlib.blake2b(epk + public_key, digest_size=24).digest()
    k = hsalsa20(x25519(esk, public_key), bytes(16))  # crypto_box_beforenm
    return epk + secretbox(message, nonce, k)


def seal_b64(public_key_b64: str, message: str) -> str:
    sealed = seal(base64.b64decode(public_key_b64), message.encode("utf-8"))
    return base64.b64encode(sealed).decode("ascii")
