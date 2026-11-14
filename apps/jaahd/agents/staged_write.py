"""agents/staged_write.py — THE ONE HOME of the staged write renamed over its target, for code that runs AT THE
ACCOUNT'S OWN UID (or must not hand the file to another uid).

The daemon's own writes into a session user's tree go through `session_user.replace_file_as_session_user`; this
file is for the callers that cannot reach that seam: scripts run as `python3 [-I] -c <script>` or
`python3 -I <file>.py` at the account's uid (they put this file's directory on `sys.path` and
`import staged_write`), and in-process callers whose write must stay at the caller's uid.

Stdlib only, and never relies on the `agents` package: under `-I` nothing else of the daemon is importable.
"""
from __future__ import annotations

import os
import secrets
import tempfile


def replace_file(path: str, data: bytes | str, *, mode: int | None = None, uid: int | None = None,
                 gid: int | None = None, owner_required: bool = False, fsync: bool = False,
                 suffix: str = ".tmp", unchanged_from: bytes | None = None) -> bool:
    """Replace `path` atomically with `data`, so a reader never sees half a file.

    The temp is created in `path`'s directory `O_CREAT|O_EXCL|O_NOFOLLOW` at 0600 (`mkstemp`, named
    `.<name>.*<suffix>`) and, when `mode` is given, set to it before any byte lands (`chmod` where the platform
    has no `fchmod`); `uid`/`gid` (when given) are the owner it is given (`fchown`, before the mode so a chown never clears a
    setuid/setgid bit). A str is written as text
    (UTF-8, the platform's newline), bytes as they are; `fsync` flushes the bytes to disk before the rename
    (the caller fsyncs the directory when the rename itself must survive a power loss). The rename replaces a
    symlink at `path`, never writes through it.

    `unchanged_from`: the bytes the caller read from `path`. When the file no longer holds them, nothing is
    written and False is returned (raced); otherwise True. Best effort: a writer landing between that re-read
    and the rename is not seen. The temp never outlives a failure."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=f".{os.path.basename(path)}.",
                               suffix=suffix)
    try:
        if uid is not None or gid is not None:
            try:
                os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
            except (OSError, AttributeError):   # AttributeError: no `fchown` on Windows
                # A gid alone is a courtesy (a non-member writer keeps going); a uid, or `owner_required`
                # (a root helper's store), is an owner the caller asserts, so its failure propagates.
                if uid is not None or owner_required:
                    raise
        if mode is not None:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            else:
                os.chmod(tmp, mode)
        with os.fdopen(fd, "w" if isinstance(data, str) else "wb",
                       encoding="utf-8" if isinstance(data, str) else None) as out:
            out.write(data)
            if fsync:
                out.flush()
                os.fsync(out.fileno())
        if unchanged_from is not None:
            with open(path, "rb") as current:
                if current.read() != unchanged_from:
                    os.unlink(tmp)
                    return False
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def replace_symlink(target: str, link: str) -> None:
    """Point `link` at `target` atomically (`ln -sfn`): staged at a unique name beside it and renamed over it,
    so there is never a moment with no link and the old one is never resolved through."""
    # PID plus random: concurrent swaps in one process (threads) must not unlink each other's staging.
    staging = os.path.join(os.path.dirname(link),
                           f".{os.path.basename(link).lstrip('.')}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        os.symlink(target, staging)
        os.replace(staging, link)
    finally:
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass
