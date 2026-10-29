"""groups — the one home of which groups collapse a dedicated uid's isolation, and which a user holds.

Shared by the GHA runner installer (the `gha` user), `vnodehelper` and the fleet doctor, so
the boundaries cannot drift. Imported by root and unprivileged callers alike: stdlib-only, no import-time
side effects.
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
from typing import Callable, Iterable

# Root-equivalent or log-reading. `docker`/`lxd` can bind-mount any path into a container they run as
# root, `sudo`/`root` need no argument, and `adm` reads /var/log, which carries other services' output.
PRIVILEGED_GROUPS = ("sudo", "docker", "lxd", "adm", "root")

# The groups that OWN the fleet's secrets: /home/jaah is `jaah:jaah` setgid and the credentials
# under it are 0660 — group membership, not the world bits, is the wider door. Refused for the same
# reason `docker` is, and never a per-host choice.
CREDENTIAL_GROUPS = ("jaah", "jaahd")


def groups_of(user: str) -> list[str]:
    """Every group `user` belongs to: supplementary memberships from /etc/group plus the primary group.

    Raises KeyError when the user does not exist — the caller decides whether absence is fine (an
    installer about to create the user) or a fault.
    """
    entry = pwd.getpwnam(user)
    names = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
    try:
        names.append(grp.getgrgid(entry.pw_gid).gr_name)
    except KeyError:
        # A primary gid with no /etc/group entry names no group, so it cannot be a forbidden one.
        pass
    return names


def held(user: str, forbidden: Iterable[str],
         groups_of: Callable[[str], list[str]] = groups_of) -> list[str]:
    """The forbidden groups `user` holds, sorted. KeyError propagates: the caller decides absence."""
    return sorted(set(groups_of(user)) & set(forbidden))
