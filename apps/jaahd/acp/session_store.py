"""Which sessions this node runs under the ACP host runtime, and the provider-native id each one maps to.

The APP session id (the Jaah session, `runner_registry.RunnerIdentity.session_id`) is the primary key; the
provider's own session id is a REF recorded at open, read from the agent's `session/new` answer — never
derived from a filename — so a resume names the conversation the engine itself issued. One small JSON file
per session under the daemon's own state root (a runner cannot plant one there)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import env_helpers

# Absent on Windows, where a final-component symlink needs a privilege the runner account does not hold.
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_BINARY = getattr(os, "O_BINARY", 0)

# The ids a file name is built from: no separator, no `..`, bounded.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
RUNTIME = "acp"
# A provider-native ref is opaque but bounded: it is written to disk and posted to the CP.
_MAX_REF_CHARS = 512


def valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and bool(SESSION_ID_RE.match(session_id)) and session_id not in (".", "..")


def private_dir(path: Path) -> Path:
    """`path` created if needed and 0700 — the single home of the rule (`microvm_host` re-exports it), kept in
    this stdlib-only module so it imports on every OS. The daemon runs with `UMask=0002` in the runners' group,
    so a directory made without the chmod is group-writable by every runner (which could then plant a symlink
    the daemon writes through, a file it reads as its own, swap an image, or plant a git hook the daemon runs)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_text_private(path: Path, text: str, *, fsync: bool = False) -> None:
    """Atomic 0600 write (`env_helpers.atomic_write_text`: a `mkstemp` temp — exclusive, never through a
    symlink — renamed over `path`), so a crash leaves the old file or the new one, never half of either."""
    env_helpers.atomic_write_text(path, text.encode("utf-8"), mode=0o600, fsync=fsync)


def write_json_private(path: Path, obj: object, *, fsync: bool = False) -> None:
    write_text_private(path, json.dumps(obj, separators=(",", ":")), fsync=fsync)


def read_json(path: Path) -> object:
    """The parsed file, or None when absent, unreadable or a symlink (a torn file is treated as never
    written)."""
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_BINARY)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


class SessionStore:
    """`<sid>.session.json` = {session_id, task_id, provider, native_session_ref, runtime, opened_at}, plus
    `jaah_account` for a Jaah-account run (`set_jaah_account`) and `key_sign_in` for a run that signs in with its
    account's key (`set_key_sign_in`: `pending`, then `served` — never the key)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> Path:
        if not valid_session_id(session_id):
            raise ValueError("invalid session id")
        return self.root / f"{session_id}.session.json"

    def open(self, *, session_id: str, task_id: str, provider: str, native_session_ref: str = "") -> dict:
        """Record the session (idempotent). A later open never blanks a known ref, nor rebinds the task."""
        path = self._path(session_id)
        ref = str(native_session_ref or "")[:_MAX_REF_CHARS]
        with self._lock:
            current = read_json(path)
            if isinstance(current, dict):
                changed = False
                if ref and current.get("native_session_ref") != ref:
                    current["native_session_ref"] = ref
                    changed = True
                if provider and not current.get("provider"):
                    current["provider"] = str(provider)
                    changed = True
                if changed:
                    write_json_private(path, current)
                return dict(current)
            record = {"session_id": session_id, "task_id": str(task_id), "provider": str(provider or ""),
                      "native_session_ref": ref, "runtime": RUNTIME, "opened_at": time.time()}
            private_dir(self.root)
            write_json_private(path, record)
            return dict(record)

    def set_native_ref(self, session_id: str, ref: str) -> None:
        ref = str(ref or "")[:_MAX_REF_CHARS]
        if not ref:
            return
        path = self._path(session_id)
        with self._lock:
            current = read_json(path)
            if isinstance(current, dict) and current.get("native_session_ref") != ref:
                current["native_session_ref"] = ref
                write_json_private(path, current)

    def set_jaah_account(self, session_id: str, *, task_id: str, provider: str, meter: dict) -> None:
        """Record, before the host starts, that this session runs on the Jaah account and where its usage tail
        reads (`meter`: `jaah_session_id`, `config_dir`, `worktree`), opening the session if needed. Daemon-side
        only: never in launch.json, which the runner can read."""
        self.open(session_id=session_id, task_id=task_id, provider=provider)
        path = self._path(session_id)
        with self._lock:
            current = read_json(path)
            if isinstance(current, dict):
                current["jaah_account"] = dict(meter)
                write_json_private(path, current)

    def set_key_sign_in(self, session_id: str, *, task_id: str, provider: str) -> None:
        """Record, before the host starts, that this session's launch signs in with its account's key (launch.json
        `authenticate`), opening the session if needed: `runtime.account_key` serves only such a session."""
        self.open(session_id=session_id, task_id=task_id, provider=provider)
        path = self._path(session_id)
        with self._lock:
            current = read_json(path)
            if isinstance(current, dict) and current.get("key_sign_in") is None:
                current["key_sign_in"] = "pending"
                write_json_private(path, current)

    def take_key_sign_in(self, session_id: str) -> str:
        """Spend the session's one key read: `"pending"` → `"served"`, returning what it was (`"pending"` when this
        call took it; `"served"` when an earlier one did; `""` when the launch signs in with no key)."""
        path = self._path(session_id)
        with self._lock:
            current = read_json(path)
            state = str(current.get("key_sign_in") or "") if isinstance(current, dict) else ""
            if state == "pending":
                current["key_sign_in"] = "served"
                write_json_private(path, current)
            return state

    def get(self, session_id: str) -> dict | None:
        if not valid_session_id(session_id):
            return None
        current = read_json(self._path(session_id))
        return current if isinstance(current, dict) else None

    def is_acp(self, session_id: str) -> bool:
        record = self.get(session_id)
        return record is not None and record.get("runtime") == RUNTIME
