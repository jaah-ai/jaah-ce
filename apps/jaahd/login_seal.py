"""The login seal: what keeps the control plane BLIND to a subscription login.

A subscription login has two secrets-in-flight that used to cross the control plane in the clear: the
authorization URL the CLI mints (node → browser) and the one-time code the operator pastes back
(browser → node). Both now travel SEALED — RSA-OAEP to a public key the receiving side minted for this
one login — so the control plane stores and relays ciphertext it cannot read, and the sign-in completes
between the operator's browser and the vendor's own flow with nobody in between.

One construction, both directions, spelled here once and mirrored byte-for-byte by the browser
(`apps/web/src/lib/loginSeal.ts` — change one, change both):

  * Keys: RSA-2048, on the wire as base64 of the SPKI DER (`peer_key`, the browser's; `seal_key`, this
    node's). Private keys never travel: the node's is a 0600 file in the login's runtime dir, the
    browser's is a non-extractable WebCrypto key held in page memory.
  * A sealed value: the UTF-8 plaintext split into ≤190-byte chunks (the most RSA-2048 OAEP-SHA256
    admits), each chunk RSA-OAEP (SHA-256, MGF1-SHA256, no label) → exactly 256 bytes, the chunk
    ciphertexts concatenated in order, the whole base64-encoded (`sealed_url`, `sealed_code`). Chunking
    is what lets a ~550-character vendor consent URL through; a code is one to three chunks.
  * Chunk boundaries are byte-based and the decoder joins chunks before decoding UTF-8, so a multi-byte
    character split across two chunks survives.

Only `openssl` (the CLI, Priority: important on Ubuntu — the package depends on it) does the
arithmetic: the daemon deliberately vendors no compiled crypto, and the stdlib has no RSA. No secret ever
rides argv: a private key reaches `openssl` on stdin, ciphertext on a passed pipe — never a path, so a
runner-writable runtime dir cannot swap a symlink under the read.

Windows runs the bundle's own `bin/openssl.exe` (`packaging/platform-pins.toml`; the vendored
`cryptography` ships Linux builds only). It has neither `pass_fds` nor `/dev/fd`/`/dev/stdin`, so one input
rides stdin and the other a short-lived file created exclusively in the daemon-only state dir and removed
in a `finally` — never `run/` or `home/`, which the runner account can write. Plaintext and ciphertext
always take stdin; only a key ever lands in that file.

Threat model: the control plane is trusted infrastructure that must not HOLD credential material, not a
hostile party. A key substitution by the relay is therefore out of scope, exactly as it is for the plain
authorize link today; the browser still applies `safeHref` and the vendor anchor to what it unseals.
"""
from __future__ import annotations

import base64
import binascii
import ntpath
import os
import re
import subprocess
import tempfile

import host_platform  # stdlib-only seam: which OS, and the Windows bundle and state roots

RSA_BITS = 2048
# RSA-2048 OAEP-SHA256: k − 2·hLen − 2 = 256 − 64 − 2 bytes of plaintext per chunk.
CHUNK_PLAINTEXT_BYTES = 190
CHUNK_CIPHERTEXT_BYTES = RSA_BITS // 8
# The longest sealed value either side accepts on the wire, in base64 characters. 11 chunks hold the
# control plane's 2048-character URL bound (`routes/commands.py::_OAUTH_URL_MAX_LEN`) with room to spare;
# a 512-character code (`_OAUTH_CODE_RE`) is 3.
MAX_SEALED_CHARS = 4096
# A 2048-bit SPKI is 294 DER bytes; the bound leaves room for a longer modulus without admitting a blob.
MAX_KEY_CHARS = 1024

_OAEP = (
    "-pkeyopt", "rsa_padding_mode:oaep",
    "-pkeyopt", "rsa_oaep_md:sha256",
    "-pkeyopt", "rsa_mgf1_md:sha256",
)
_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")
_OPENSSL_TIMEOUT = 20.0
_OPENSSL_ENV = {"PATH": "/usr/bin:/bin"}


class SealError(Exception):
    """A seal or unseal that could not be carried out: a malformed key or blob, or `openssl` refusing.
    Never carries plaintext or key material in its message."""


def is_wire_key(value: object) -> bool:
    """Shape check for a public key as received on the wire: base64 that DECODES (the regex alone admits
    bad padding, which would fail late, after the login spawn), bounded. Whether it is a usable RSA key
    is `openssl`'s verdict at seal time (`SealError`)."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_KEY_CHARS or not _B64_RE.match(value):
        return False
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def is_wire_sealed(value: object) -> bool:
    """Shape check for a sealed value as received on the wire: base64, bounded, whole 256-byte chunks."""
    if not isinstance(value, str) or not value or len(value) > MAX_SEALED_CHARS or not _B64_RE.match(value):
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) > 0 and len(raw) % CHUNK_CIPHERTEXT_BYTES == 0


def _openssl(argv: list[str], *, stdin: bytes, side_input: bytes | None = None) -> bytes:
    """Run `openssl` with STDIN on its stdin and, when given, SIDE_INPUT readable at the `{side}` placeholder
    in ARGV — a passed pipe's `/dev/fd/N` on posix, a daemon-only file on Windows (module docstring).
    Nothing secret is in argv. Any failure is a `SealError` naming only the exit status."""
    if host_platform.is_windows():
        return _openssl_windows(argv, stdin=stdin, side_input=side_input)
    pass_fds: tuple[int, ...] = ()
    read_fd = -1
    if side_input is not None:
        read_fd, write_fd = os.pipe()
        try:
            # A key or a few ciphertext chunks fit a pipe buffer many times over; written before the spawn
            # so the child never blocks on a writer that is us.
            os.write(write_fd, side_input)
        finally:
            os.close(write_fd)
        pass_fds = (read_fd,)
        argv = [a.format(side=f"/dev/fd/{read_fd}") for a in argv]
    try:
        # A minimal explicit environment, nothing inherited: the daemon's own env could otherwise steer the
        # binary (`OPENSSL_CONF`/`OPENSSL_MODULES`) or a PATH entry a session group can write could plant one.
        proc = subprocess.run(
            ["openssl", *argv], input=stdin, capture_output=True, timeout=_OPENSSL_TIMEOUT,
            pass_fds=pass_fds, check=False, env=_OPENSSL_ENV,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SealError(f"openssl could not run: {exc.__class__.__name__}") from exc
    finally:
        if read_fd >= 0:
            os.close(read_fd)
    return _checked(proc)


def _openssl_windows(argv: list[str], *, stdin: bytes, side_input: bytes | None) -> bytes:
    """`_openssl` on Windows: the bundle's `openssl.exe` by absolute path, SIDE_INPUT in a file `mkstemp`
    creates exclusively (`O_EXCL`, `O_NOINHERIT`) in `_side_dir()`, closed before the spawn (so the child
    can open it) and removed in the `finally` whatever happened."""
    side_path = ""
    try:
        if side_input is not None:
            fd, side_path = tempfile.mkstemp(prefix="login-seal-", dir=_side_dir())
            try:
                os.write(fd, side_input)
            finally:
                os.close(fd)
            argv = [a.format(side=side_path) for a in argv]
        # Only `SystemRoot`, which a Windows process needs to load system DLLs at all; nothing else is
        # inherited, so `OPENSSL_CONF`/`OPENSSL_MODULES` cannot steer it and no PATH lookup happens.
        proc = subprocess.run(
            [ntpath.join(host_platform.paths().lib_root, "bin", "openssl.exe"), *argv], input=stdin,
            capture_output=True, timeout=_OPENSSL_TIMEOUT, check=False,
            env={"SystemRoot": os.environ.get("SystemRoot") or "C:\\Windows"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SealError(f"openssl could not run: {exc.__class__.__name__}") from exc
    finally:
        if side_path:
            try:
                os.unlink(side_path)
            except OSError:
                pass
    return _checked(proc)


def _side_dir() -> str:
    """Where a Windows side file lives: the daemon-only state dir as the OS layout pins it — never a
    `$HOME`-derived root a service environment could point elsewhere."""
    return host_platform.paths().state_dir


def _checked(proc: subprocess.CompletedProcess) -> bytes:
    if proc.returncode != 0:
        raise SealError(f"openssl exited {proc.returncode}")
    return proc.stdout


def new_private_key() -> bytes:
    """A fresh RSA-2048 private key, PEM, for one login. The caller stores it (0600, O_EXCL) — it never
    touches disk here."""
    pem = _openssl(["genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{RSA_BITS}"], stdin=b"")
    if b"PRIVATE KEY" not in pem:
        raise SealError("openssl produced no private key")
    return pem


def public_key_b64(private_pem: bytes) -> str:
    """The wire form (base64 SPKI DER) of PRIVATE_PEM's public half."""
    der = _openssl(["pkey", "-pubout", "-outform", "DER"], stdin=private_pem)
    if not der:
        raise SealError("openssl produced no public key")
    return base64.b64encode(der).decode("ascii")


def seal(public_key_b64: str, text: str) -> str:
    """TEXT sealed to the wire key PUBLIC_KEY_B64 — the construction in the module docstring."""
    if not is_wire_key(public_key_b64):
        raise SealError("malformed public key")
    try:
        der = base64.b64decode(public_key_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SealError("malformed public key") from exc
    data = text.encode("utf-8")
    if not data:
        raise SealError("nothing to seal")
    out = bytearray()
    for start in range(0, len(data), CHUNK_PLAINTEXT_BYTES):
        chunk = data[start:start + CHUNK_PLAINTEXT_BYTES]
        ciphertext = _openssl(
            ["pkeyutl", "-encrypt", "-pubin", "-keyform", "DER", "-inkey", "{side}", *_OAEP],
            stdin=chunk, side_input=der,
        )
        if len(ciphertext) != CHUNK_CIPHERTEXT_BYTES:
            raise SealError("openssl produced a ciphertext of the wrong size")
        out += ciphertext
    return base64.b64encode(bytes(out)).decode("ascii")


def unseal(private_pem: bytes, sealed: str) -> str:
    """SEALED (a wire value sealed to the public half of PRIVATE_PEM) back to its text."""
    if not is_wire_sealed(sealed):
        raise SealError("malformed sealed value")
    try:
        raw = base64.b64decode(sealed, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SealError("malformed sealed value") from exc
    data = bytearray()
    windows = host_platform.is_windows()
    for start in range(0, len(raw), CHUNK_CIPHERTEXT_BYTES):
        chunk = raw[start:start + CHUNK_CIPHERTEXT_BYTES]
        if windows:
            # No `/dev/stdin` and `-inkey -` is not stdin: the key takes the side file, the chunk stdin
            # (`pkeyutl`'s default `-in`).
            data += _openssl(
                ["pkeyutl", "-decrypt", "-inkey", "{side}", *_OAEP], stdin=chunk, side_input=private_pem)
        else:
            data += _openssl(
                ["pkeyutl", "-decrypt", "-inkey", "/dev/stdin", "-in", "{side}", *_OAEP],
                stdin=private_pem, side_input=chunk,
            )
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SealError("unsealed bytes are not UTF-8") from exc
