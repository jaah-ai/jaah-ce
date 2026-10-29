"""The node-local lock every /etc/passwd-touching operation serialises behind, and its retry.

`useradd` does not block on contention: it exits immediately — 1 ("can't update password file") or 10
("can't update group file") in shadow-utils — so concurrent account writers (the vnode helper, the GHA
runner installer) fail rather than queue. A lock failure is therefore a RETRY, never an operation failure:
a node-local flock with a visible retry line per attempt, plus a retry on `useradd`'s busy codes (our lock
does not exclude a hand-run `useradd`).
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: the node lock guards vnode and GHA account ops, Linux-only
    fcntl = None
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .runner import CommandFailed

log = logging.getLogger("jaah.rootlib.lock")

# Root-only (its directory is re-moded 0700 on every take); its contents are never read.
LOCK_PATH = "/var/lib/jaah/account-lock/useradd.lock"

RETRY_INTERVAL = 0.25
DEFAULT_TIMEOUT = 120.0

#: `useradd`/`groupadd`/`userdel` exit codes that mean "someone else holds the file", not "this failed".
#: 1 = can't update password file, 10 = can't update group file (shadow-utils).
BUSY_EXIT_CODES = (1, 10)


class LockTimeout(Exception):
    """The lock was held past the budget. Reported as `lock_timeout` — still a retry for the caller,
    never an operation failure."""


@contextmanager
def node_lock(timeout: float = DEFAULT_TIMEOUT, path: str | None = None) -> Iterator[None]:
    """Hold the node-wide ACCOUNT lock — /etc/passwd, /etc/group and /etc/sub{u,g}id — retrying visibly.

    Deliberately narrow: hold it around account work, never around a teardown.
    """
    lock_path = Path(path or LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    with _flock(lock_path, timeout, "node account"):
        yield


@contextmanager
def _flock(lock_path: Path, timeout: float, what: str) -> Iterator[None]:
    """Non-blocking acquire in a retry loop, so contention is visible in the log (a blocking `flock`
    is not)."""
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"{what} lock held by another operation for {timeout:.0f}s "
                        f"({attempt} attempts)"
                    )
                log.info("%s lock contended — retry %d", what, attempt)
                time.sleep(RETRY_INTERVAL)
        if attempt > 1:
            log.info("%s lock acquired after %d attempts", what, attempt)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def retry_on_busy(call, *, attempts: int = 40, interval: float = RETRY_INTERVAL, busy_codes=BUSY_EXIT_CODES):
    """Run `call()`, retrying while it reports one of `busy_codes` — `useradd`'s exit 10 above all.

    Our lock does not exclude a `useradd` run by an operator, a postinst or cloud-init.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except CommandFailed as exc:
            if exc.returncode not in busy_codes:
                raise
            last = exc
            if attempt == attempts:
                break
            log.info("account database busy (exit %d) — retry %d", exc.returncode, attempt)
            time.sleep(interval)
    raise LockTimeout(f"account database stayed busy across {attempts} attempts: {last}")
