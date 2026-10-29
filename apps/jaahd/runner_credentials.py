"""runner_credentials.py — a credential this daemon may not read itself, read by the runner account.

On a Windows node with a NodeLink mounted at the token store (`{jaah-home}\\auth\\.shared`), the store is a link
into the `jaah-engine` WSL distro, and `\\\\wsl.localhost` is per-user: only the runner account sees it
(`shared_storage` module docstring). Every daemon read of a credential there — the dispatch credential question,
the quota probe, the login's check — must go through the runner account (the `session_user` seam), or it reads
"no credential" for a token that is there.

`read_through_runner` says when; `credential_state` answers the dispatch three-state question as the runner
account. Never a token value: the far side prints one word.
"""
from __future__ import annotations

import logging
import ntpath
import sys

import host_platform
import session_user
import shared_storage

log = logging.getLogger(__name__)

# A spool round trip doing no work of its own (as `accounts._DELEGATED_TIMEOUT`).
_TIMEOUT = 30.0

SHAPE_FILE = "file"

# Runs AS THE RUNNER ACCOUNT: stdlib only; prints `token`/`none`/`unreadable` and NOTHING else — never the file's
# contents. `O_NOFOLLOW` + `S_ISREG` + the size bound, as `agents.base.credential_file_present`. Windows has no
# `O_NOFOLLOW`, so a link or reparse point is refused by `lstat` first; the race left is the runner's own, and
# that account may write a real file there anyway.
_STATE_PY = (
    "import json, os, stat, sys\n"
    "path, shape, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
    "def failed(exc):\n"
    "    # Absent only when the folder it lives in answers: an unreachable share is not proof of absence.\n"
    "    gone = isinstance(exc, FileNotFoundError) and os.path.isdir(os.path.dirname(path))\n"
    "    print('none' if gone else 'unreadable')\n"
    "    sys.exit(0)\n"
    "try:\n"
    "    info = os.lstat(path)\n"
    "except OSError as exc:\n"
    "    failed(exc)\n"
    "if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):\n"
    "    print('none')\n"
    "    sys.exit(0)\n"
    "flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)\n"
    "try:\n"
    "    fd = os.open(path, flags)\n"
    "except OSError as exc:\n"
    "    failed(exc)\n"
    "with os.fdopen(fd, 'rb') as fh:\n"
    "    raw = fh.read(limit + 1) if stat.S_ISREG(os.fstat(fh.fileno()).st_mode) else b''\n"
    "ok = 0 < len(raw) <= limit\n"
    "if ok and shape == 'oauth':\n"
    "    try:\n"
    "        data = json.loads(raw)\n"
    "    except ValueError:\n"
    "        data = None\n"
    "    oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None\n"
    "    ok = isinstance(oauth, dict) and bool(oauth.get('accessToken'))\n"
    "print('token' if ok else 'none')\n"
)


def read_through_runner(path: object) -> bool:
    """Must a read of PATH go through the runner account? True on a Windows node for a path inside the token
    store while a NodeLink is mounted there; False everywhere else (macOS/Linux read their store directly), and
    False for the runner account itself (the quota probe runs as it), which reads the store directly."""
    if not host_platform.is_windows() or session_user.is_session_user() or not shared_storage.share_mounted():
        return False
    root = shared_storage.account_token_root()
    if not root:
        return False
    # Normalised first: a `..` that walks out of the store is no read of the store.
    inside = ntpath.normcase(ntpath.normpath(str(path)))
    return inside.startswith(ntpath.normcase(ntpath.normpath(root)).rstrip("\\") + "\\")


def credential_state(path: object, *, shape: str, max_bytes: int) -> str:
    """Which of `agent_config_dirs`' three states the credential at PATH is in, asked of the runner account.

    A check that could not run is `unreadable`, never `none`: `none` is what the autoclose ladder reaps on, and a
    hung seam is no proof the credential is gone."""
    import agent_config_dirs  # noqa: PLC0415 — the three-state vocabulary's home; a hook-loaded leaf

    try:
        proc = session_user.run_as_session_user(
            [sys.executable, "-I", "-c", _STATE_PY, str(path), shape, str(max_bytes)], timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (seam down), TimeoutExpired, OSError
        log.warning("the runner account could not check the credential at %s (%s: %s)",
                    path, type(exc).__name__, exc)
        return agent_config_dirs.CREDS_UNREADABLE
    answer = (proc.stdout or "").strip()
    if answer in (agent_config_dirs.CREDS_TOKEN, agent_config_dirs.CREDS_NONE, agent_config_dirs.CREDS_UNREADABLE):
        return answer
    log.warning("the runner account's credential check at %s answered nothing usable (rc %s)", path, proc.returncode)
    return agent_config_dirs.CREDS_UNREADABLE

