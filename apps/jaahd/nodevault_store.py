"""NodeVault's node keystore: this node's identity key (NIK) and the org's NodeVault keys (NVKs) it holds.

Layout (architecture §3.1, decision E5), under `$JAAH_NODEVAULT_DIR`, default `$HOME/nodevault` —
`/var/lib/jaahd/nodevault` for the packaged daemon; each vnode daemon runs with its own HOME
(`vnodehelper/layout.py::daemon_home`), so each holds its own keystore under its own uid:

    nodevault/              0700, owned by the daemon uid
      identity.pem          0600  the NIK (RSA-4096 PKCS#8 PEM) — generated here, never leaves the node
      state.json            0600  {"current": <key hash> | null}
      keys/                 0700
        <key hash>.pem      0600  one NVK each; every one that is not `current` is `old`

Owner-only on purpose: no group bit, so neither the runner uid (`jaah`) nor a sibling vnode's uid can read
a key (AC-3.1 as amended by E5, AC-3.2). Every file is written to a fresh exclusive temp file in its own
directory and then renamed (or, for a file that must never be replaced, hard-linked) into place, so a
reader never sees half a key. Every read opens the entry itself, never through a link, and checks what it
opened: a regular, single-link file THIS daemon owns. A file owned by anyone else is refused; one of ours
that is too wide is narrowed (repaired) in place and logged. `secret_modes.py` does the same on its periodic
pass. How "private" is made and checked per OS — mode bits on POSIX, a protected access list on Windows
(`C:\\ProgramData\\Jaah\\state\\nodevault`) — is `nodevault_private.py`.

Nothing here logs or returns key bytes except `identity_key()`/`key()`, which return key OBJECTS to
in-daemon callers. `fingerprint()` and `keys()` are public facts — what the `nodevault.*` local verbs serve.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import env_helpers
import nodevault_crypto
import nodevault_private

# 0 where the platform lacks the flag (it is POSIX-only).
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

log = logging.getLogger("jaahd.nodevault_store")

DIR_ENV = "JAAH_NODEVAULT_DIR"
DIR_MODE = nodevault_private.DIR_MODE
FILE_MODE = nodevault_private.FILE_MODE
IDENTITY_FILE = "identity.pem"
STATE_FILE = "state.json"
KEYS_DIR = "keys"

STATE_CURRENT = "current"
STATE_OLD = "old"

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILE_BYTES = 64 * 1024

# Writers run on several daemon threads (the identity registrar, the `nodevault.*` cmd handlers): a temp-file
# sweep must never unlink another thread's in-flight write.
_WRITE_LOCK = threading.Lock()


KeystoreError = nodevault_private.KeystoreError


def store_dir() -> Path:
    """`$JAAH_NODEVAULT_DIR`, else `<daemon state dir>/nodevault`. Read at call time (tests set it late).
    Set-but-empty means unset."""
    override = (os.environ.get(DIR_ENV) or "").strip()
    return Path(override) if override else env_helpers.daemon_state_dir("nodevault")


def secret_files(root: Path | None = None) -> list[tuple[str, str]]:
    """`(root, rel)` of every keystore file that exists or may — for `secret_modes`' periodic pass."""
    base = root or store_dir()
    rels = [IDENTITY_FILE, STATE_FILE]
    try:
        rels += sorted(f"{KEYS_DIR}/{p.name}" for p in (base / KEYS_DIR).iterdir()
                       if p.name.endswith(".pem") and not nodevault_private.is_reparse(p))
    except OSError:
        pass
    return [(str(base), rel) for rel in rels]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Keystore:
    """One daemon's keystore at `root` (default `store_dir()`). Cheap to construct; touches the disk only
    when asked. Not thread-safe across writers — the daemon is its one writer."""

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = Path(root) if root is not None else store_dir()

    # ── Directories ──

    def _ensure_dir(self, path: Path) -> None:
        """Create `path` private, or check an existing one: a real directory this daemon owns, narrowed
        (repaired) if wider. A link or another owner's directory is refused."""
        nodevault_private.create_dir(path)
        nodevault_private.check(path, None, is_dir=True)

    def _ensure_dirs(self) -> None:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_dir(self.root)
        self._ensure_dir(self.root / KEYS_DIR)

    def sweep_temp_files(self) -> int:
        """Remove `.<name>.<pid>.<rand>.tmp` leftovers of a write a crash interrupted (the daemon is the one
        writer, so at its start none is live). A leftover hard link would otherwise keep `identity.pem` at
        two links, which every read refuses. Returns how many were removed."""
        removed = 0
        with _WRITE_LOCK:
            removed = self._sweep_temp_files()
        return removed

    def _sweep_temp_files(self) -> int:
        removed = 0
        for directory in (self.root, self.root / KEYS_DIR):
            try:
                names = os.listdir(directory)
            except FileNotFoundError:
                continue
            for name in names:
                if name.startswith(".") and name.endswith(".tmp"):
                    try:
                        os.unlink(directory / name)
                        removed += 1
                    except FileNotFoundError:
                        pass
        return removed

    # ── Files ──

    def _write(self, rel: str, data: bytes, *, replace: bool) -> bool:
        """Write `data` at `rel`, 0600, atomically. `replace=False` never clobbers: an existing file wins
        and False is returned. The temp file is `O_EXCL` in the target's own directory."""
        with _WRITE_LOCK:
            return self._write_locked(rel, data, replace=replace)

    def _write_locked(self, rel: str, data: bytes, *, replace: bool) -> bool:
        self._ensure_dirs()
        final = self.root / rel
        tmp = final.parent / f".{final.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        fd = nodevault_private.create_file_excl(tmp)
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            if replace:
                os.replace(tmp, final)
            else:
                try:
                    os.link(tmp, final)
                except FileExistsError:
                    return False
            return True
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            _fsync_dir(final.parent)   # the new name (and the dropped temp one) survive a power loss

    def _check_existing_dirs(self) -> None:
        """The read-side half of `_ensure_dir`: every keystore dir that exists is checked (ours, a real dir)
        and narrowed to 0700 — so a loosened dir is fixed on the next read, not only the next write."""
        for directory in (self.root, self.root / KEYS_DIR):
            if os.path.lexists(directory):
                self._ensure_dir(directory)

    def _read(self, rel: str) -> bytes | None:
        """The bytes at `rel`, or None if absent. Opened without following a link; what was opened must be a
        regular, single-link file this daemon owns, else `KeystoreError`. A wider one is narrowed and logged."""
        self._check_existing_dirs()
        path = self.root / rel
        fd = nodevault_private.open_read(path)
        if fd is None:
            return None
        try:
            st = nodevault_private.check(path, fd, is_dir=False).stat
            assert st is not None
            if st.st_size > _MAX_FILE_BYTES:
                raise KeystoreError(f"{path} is larger than a key file can be")
            chunks = []
            while chunk := os.read(fd, _MAX_FILE_BYTES + 1):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def repair(self, *, fix: bool = True) -> list[tuple[str, str, bool]]:
        """Check the root, `keys/` and every keystore file (`nodevault_private.check`) and, with `fix`, narrow
        or repair each one that is too wide. `[(path, what, fixed)]` for every entry that was too wide or was
        refused — never raises. `secret_modes` runs it where mode bits mean nothing (Windows)."""
        findings: list[tuple[str, str, bool]] = []
        with _WRITE_LOCK:
            for directory in (self.root, self.root / KEYS_DIR):
                if not os.path.lexists(directory):
                    continue
                try:
                    seen = nodevault_private.check(directory, None, is_dir=True, fix=fix)
                except (KeystoreError, OSError, ValueError) as exc:
                    findings.append((str(directory), f"refused: {exc}", False))
                    continue
                if seen.repaired:
                    findings.append((str(directory), seen.repaired, seen.fixed))
            for root, rel in secret_files(self.root):
                path = Path(root) / rel
                try:
                    fd = nodevault_private.open_read(path)
                    if fd is None:
                        continue
                    try:
                        seen = nodevault_private.check(path, fd, is_dir=False, fix=fix)
                    finally:
                        os.close(fd)
                except (KeystoreError, OSError, ValueError) as exc:
                    findings.append((str(path), f"refused: {exc}", False))
                    continue
                if seen.repaired:
                    findings.append((str(path), seen.repaired, seen.fixed))
        return findings

    # ── Identity (NIK) ──

    def identity_key(self) -> Any | None:
        """The NIK private key object, or None before the first `ensure_identity()`."""
        pem = self._read(IDENTITY_FILE)
        return None if pem is None else nodevault_crypto.private_key_from_pem(pem)

    def ensure_identity(self) -> tuple[Any, bool]:
        """`(NIK, created)` — the existing identity, or a freshly generated one. Never replaces an existing
        file: a concurrent creator's key wins and is the one returned."""
        self.sweep_temp_files()
        key = self.identity_key()
        if key is not None:
            return key, False
        fresh = nodevault_crypto.generate_private_key()
        created = self._write(IDENTITY_FILE, nodevault_crypto.private_key_to_pem(fresh), replace=False)
        if created:
            log.info("nodevault: generated this node's identity key, fingerprint %s",
                     nodevault_crypto.key_hash(fresh))
            return fresh, True
        return self.identity_key(), False

    def fingerprint(self) -> str | None:
        """The NIK fingerprint `hex(sha256(SPKI DER))`, or None when there is no identity yet."""
        key = self.identity_key()
        return None if key is None else nodevault_crypto.key_hash(key)

    # ── NodeVault keys (NVK) ──

    def _state(self) -> dict:
        raw = self._read(STATE_FILE)
        if raw is None:
            return {"current": None}
        try:
            state = json.loads(raw)
        except ValueError:
            raise KeystoreError(f"{self.root / STATE_FILE} is not JSON") from None
        current = state.get("current") if isinstance(state, dict) else None
        if current is not None and not (isinstance(current, str) and _HASH_RE.match(current)):
            raise KeystoreError(f"{self.root / STATE_FILE} names no valid key")
        return {"current": current}

    def _held(self) -> list[str]:
        try:
            names = os.listdir(self.root / KEYS_DIR)
        except FileNotFoundError:
            return []
        return sorted(n[:-4] for n in names if n.endswith(".pem") and _HASH_RE.match(n[:-4]))

    def key(self, key_hash: str) -> Any | None:
        """The NVK with `key_hash`, or None if this node does not hold it. The file's content is checked
        against its name: a key whose derived hash differs is refused, never served."""
        if not isinstance(key_hash, str) or not _HASH_RE.match(key_hash):
            return None
        pem = self._read(f"{KEYS_DIR}/{key_hash}.pem")
        if pem is None:
            return None
        nvk = nodevault_crypto.private_key_from_pem(pem)
        if nodevault_crypto.key_hash(nvk) != key_hash:
            raise KeystoreError(f"{self.root / KEYS_DIR / key_hash}.pem does not hold the key its name says")
        return nvk

    def install_key(self, nvk: Any, *, make_current: bool) -> str:
        """Store `nvk` (idempotent: a key already held is left as it is) and, with `make_current`, mark it
        current — the previous current becomes `old`, and is kept. Returns its key hash."""
        key_hash = nodevault_crypto.key_hash(nvk)
        if not self._write(f"{KEYS_DIR}/{key_hash}.pem", nodevault_crypto.private_key_to_pem(nvk), replace=False):
            self.key(key_hash)   # already held: prove the file is the key its name says
        if make_current:
            self.set_current(key_hash)
        return key_hash

    def set_current(self, key_hash: str) -> None:
        if key_hash not in self._held():
            raise KeystoreError(f"no key {key_hash[:12]} held to make current")
        self._write(STATE_FILE, json.dumps({"current": key_hash}).encode(), replace=True)

    def clear_current(self) -> None:
        """Mark no key current (every held key `old`) — so a retirement can remove the one that was."""
        self._write(STATE_FILE, json.dumps({"current": None}).encode(), replace=True)

    def remove_key(self, key_hash: str) -> bool:
        """Delete a held NVK (retirement). Refuses the current one. True if a file was removed."""
        if not isinstance(key_hash, str) or not _HASH_RE.match(key_hash):
            return False
        if self._state()["current"] == key_hash:
            raise KeystoreError(f"key {key_hash[:12]} is current and cannot be removed")
        try:
            os.unlink(self.root / KEYS_DIR / f"{key_hash}.pem")
        except FileNotFoundError:
            return False
        return True

    def keys(self) -> list[dict]:
        """`[{key_hash, state}]` for every NVK held, current first. Public facts only — no key bytes."""
        current = self._state()["current"]
        held = self._held()
        rows = [{"key_hash": h, "state": STATE_CURRENT if h == current else STATE_OLD} for h in held]
        return sorted(rows, key=lambda r: (r["state"] != STATE_CURRENT, r["key_hash"]))
