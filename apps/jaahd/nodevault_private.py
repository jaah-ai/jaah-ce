"""How a NodeVault keystore entry (`nodevault_store.py`) is made private to this daemon, per OS — the one place
that knows a keystore file's protection is mode bits on POSIX and an access list on Windows.

Four calls, each dispatched on `host_platform.is_windows()`:

  create_dir(path)                 make the directory, or leave the one there for `check` to judge
  create_file_excl(path) -> fd     a NEW write-only file nobody else may open (never an existing name)
  open_read(path) -> fd | None     the entry itself, never what a link there points at; None when absent
  check(path, fd, *, is_dir)       refuse what is not ours; narrow or repair what is ours but too wide

POSIX (Linux, macOS): the owner is this uid and the mode is at most 0700/0600 — a wider mode is narrowed and
logged; a link, another uid's entry or a file with a second link is refused.

Windows: mode bits mean nothing, so the protection is the entry's access list. Allowed principals are this
daemon's token user (read off its own process token, never looked up by name), SYSTEM and Administrators:

  REFUSE  a reparse point (link, junction), the wrong kind, a file with a second hard link, or an owner
          outside the allowed three — the daemon will not use what someone else planted or owns.
  REPAIR  an ALLOWED ACE naming anyone else (the runner account, Users, Everyone), or a keystore directory
          whose access list is not protected (inheriting from its parent): the canonical protected DACL is
          written back through the open handle and a warning names the SIDs removed. Rotating the org's key
          is the operator's remedy, since a reader may have copied a key while the grant stood.
  PASS    an access list whose every allowed ACE is one of the three, explicit or inherited; DENY ACEs.

Nothing here reads, logs or returns key bytes.
"""
from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
from pathlib import Path
from typing import NamedTuple

import host_platform
from host_platform import windows

# 0 where the platform lacks the flag (it is POSIX-only).
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

log = logging.getLogger("jaahd.nodevault_store")

DIR_MODE = 0o700
FILE_MODE = 0o600


class KeystoreError(Exception):
    """The keystore refused an unsafe path or a malformed file. The message names a path, never a key."""


class Checked(NamedTuple):
    """What `check` saw: the opened file's stat (None for a directory on Windows), and `repaired` — what was
    narrowed or repaired, "" when nothing was too wide. `fixed` is False when a too-wide entry was only
    reported (`fix=False`)."""

    stat: os.stat_result | None
    repaired: str = ""
    fixed: bool = False


def is_reparse(path: Path) -> bool:
    """A symlink, or (Windows) any reparse point — a junction included."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0)
                                            & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def create_dir(path: Path) -> None:
    if host_platform.is_windows():
        windows.create_private_dir(str(path), windows.keystore_sddl(windows.current_sid(), directory=True))
        return
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        pass


def create_file_excl(path: Path) -> int:
    if host_platform.is_windows():
        return windows.create_private_file(str(path), windows.keystore_sddl(windows.current_sid(), directory=False))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)   # the umask may have narrowed it further; never wider
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return fd


def open_read(path: Path) -> int | None:
    if host_platform.is_windows():
        return windows.open_private(str(path))
    try:
        return os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise KeystoreError(f"{path} is a symlink") from None
        raise


def check(path: Path, fd: int | None, *, is_dir: bool, fix: bool = True) -> Checked:
    """Judge one keystore entry: a directory by its `path`, a file by the descriptor `open_read` gave. Raises
    `KeystoreError` to refuse it; narrows or repairs (with `fix`) one that is ours but too wide."""
    if host_platform.is_windows():
        return _check_windows(path, fd, is_dir=is_dir, fix=fix)
    if is_dir:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise KeystoreError(f"{path} is not a directory")
        _check_owner(path, st)
        return _narrow(path, st, DIR_MODE, fix, lambda m: os.chmod(path, m, follow_symlinks=False))
    assert fd is not None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise KeystoreError(f"{path} is not a single-link regular file")
    _check_owner(path, st)
    return _narrow(path, st, FILE_MODE, fix, lambda m: os.fchmod(fd, m))


def _check_owner(path: Path, st: os.stat_result) -> None:
    """Refuse a store entry another uid owns. A POSIX-less platform without `geteuid` is refused too, rather
    than an AttributeError."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        raise KeystoreError(f"{path}: this platform has no file owners to check; NodeVault keys are not kept here")
    if st.st_uid != geteuid():
        raise KeystoreError(f"{path} is owned by uid {st.st_uid}, not this daemon")


def _narrow(path: Path, st: os.stat_result, allowed: int, fix: bool, chmod) -> Checked:
    mode = stat.S_IMODE(st.st_mode)
    if not mode & ~allowed:
        return Checked(st)
    note = f"mode {mode:04o} wider than {allowed:04o}"
    if fix:
        chmod(mode & allowed)
        log.warning("nodevault: narrowed %s from %04o to %04o", path, mode, mode & allowed)
    return Checked(st, note, fix)


def _check_windows(path: Path, fd: int | None, *, is_dir: bool, fix: bool) -> Checked:
    own = fd is None
    opened = windows.open_private(str(path), directory=True) if own else fd
    if opened is None:
        raise FileNotFoundError(errno.ENOENT, "no such keystore entry", str(path))
    fd = opened
    try:
        attributes, links = windows.handle_facts(fd)
        if attributes & windows.FILE_ATTRIBUTE_REPARSE_POINT:
            raise KeystoreError(f"{path} is a reparse point (a link or junction)")
        if is_dir and not attributes & windows.FILE_ATTRIBUTE_DIRECTORY:
            raise KeystoreError(f"{path} is not a directory")
        if not is_dir and (attributes & windows.FILE_ATTRIBUTE_DIRECTORY or links != 1):
            raise KeystoreError(f"{path} is not a single-link regular file")
        daemon = windows.current_sid()
        allowed = {daemon, windows.SYSTEM_SID, windows.ADMINISTRATORS_SID}
        owner, protected, aces = windows.file_security(fd)
        if owner not in allowed:
            raise KeystoreError(f"{path} is owned by {owner}, not this daemon")
        foreign = sorted({sid or f"an ACE of type {kind}" for kind, sid in aces
                          if kind not in windows.DENY_ACE_TYPES and sid not in allowed})
        repaired = ""
        if foreign or (is_dir and not protected):
            removed = ", ".join(foreign) or "inherited entries"
            repaired = f"access list wider than allowed (removed {removed})"
            if fix:
                windows.set_protected_dacl(fd, windows.keystore_sddl(daemon, directory=is_dir))
                log.warning("nodevault: repaired access list on %s (removed %s)", path, removed)
        return Checked(None if is_dir else os.fstat(fd), repaired, bool(repaired) and fix)
    finally:
        if own:
            os.close(fd)
