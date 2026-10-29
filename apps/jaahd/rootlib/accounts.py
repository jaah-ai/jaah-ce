"""Account presence and subordinate-id hygiene for the privileged helpers' dedicated users.

Lookups only, plus dropping a user's /etc/subuid + /etc/subgid lines; creating and removing the users is
the caller's (`vnodehelper/ops.py`).
"""
from __future__ import annotations

try:
    import grp
except ImportError:  # Windows: the helpers' users are Linux-only
    grp = None
try:
    import pwd
except ImportError:  # Windows: the helpers' users are Linux-only
    pwd = None
from pathlib import Path

from .durable import write_durably


def exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False


def group_exists(name: str, gid: int) -> bool:
    """By NAME or by GID — either one already present means `groupadd` would refuse."""
    for lookup, key in ((grp.getgrnam, name), (grp.getgrgid, gid)):
        try:
            lookup(key)
            return True
        except KeyError:
            continue
    return False


def remove_subids(user: str) -> None:
    """Drop the user's lines from /etc/subuid and /etc/subgid.

    Hygiene, not reclamation: the range is never reused.
    """
    for path in (Path("/etc/subuid"), Path("/etc/subgid")):
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        lines = text.splitlines()
        kept = [ln for ln in lines if not ln.strip().startswith(f"{user}:")]
        if len(kept) == len(lines):
            continue
        # tmp-file + rename: a truncated /etc/subuid breaks rootless docker node-wide.
        write_durably(path, "".join(f"{ln}\n" for ln in kept), mode=0o644)
