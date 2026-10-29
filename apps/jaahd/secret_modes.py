"""secret_modes — the one home of which secret-bearing node files must stay private, and the pass that keeps them so.

Three families (G-22, CH-16a):

  node.env              the node's control-plane token. ROOT-owned, so this daemon can only AUDIT it: at most
                        `0640` with its group the daemon's own (only the daemon reads it). The root-side
                        writers (`jaah auth login`, the vnode helper) set that mode.
  account credentials   every provider's `credential_file` under `<jaah home>/auth/<account>/`. At most
                        `0660` on a two-uid node (group = the session group, whose only other member is the
                        daemon — it reads and refreshes them), `0600` on a one-uid node. Never world bits.
  worktree secrets      `worktree_mechanics.env_file.WORKTREE_PRIVATE_MODES` (`.env` 0600, …) in every slot under
                        `<jaah home>/repos/<project>/<slot>/`.
  NodeVault keys        the daemon's own keystore (`nodevault_store.py`): the identity key, the state file and
                        every NodeVault key, `0600` — owner only, the daemon's.

WHY A PERIODIC PASS, not only fix-on-write: every one of these files is rewritten by something this daemon does
not control — a vendor CLI refreshing its token at the umask, a checkout recreating `.env`, an operator `cp`.
A mode fixed once is re-loosened by the next such write, so `enforce()` runs at startup and on an interval
(`main.py`'s `secret-modes` step), and the fleet doctor prints `audit()` read-only.

The pass only ever NARROWS a mode (the opened inode's own mode `& allowed`); it never chowns and never widens.
Every path is walked below its account/slot dir with `O_NOFOLLOW` on each component, so a runner-planted link
redirects nothing; the daemon chmods in-process only a single-link credential file it owns, and a runner's
file only through the session user. Anything else — another uid's file, a group other than the session group's,
a non-regular file — is a finding, logged, never raised.

NOT ON WINDOWS: mode bits there are synthesised from the read-only flag and mean nothing; the boundary is the
installer's protected access lists on `etc`, `home` and `run`, which inherit to every file written below them.
So the pass narrows no mode bits there (`_scan`), rather than "narrowing" bits no kernel enforces — except the
NodeVault keystore, whose DACL `nodevault_private` checks: the pass runs `Keystore.repair()` over it, so an
access list an admin widened is repaired back on the next pass, not only the next read.
"""
from __future__ import annotations

import asyncio
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import agent_providers
import edition
import env_helpers
import environment
import host_platform
import nodevault_store
import session_user
from worktree_mechanics import env_file

log = logging.getLogger("jaahd.secret_modes")

# Seconds between passes. Cheap — one `lstat` per file; a delegated chmod only where a mode is too wide.
INTERVAL_SECONDS = 300.0

# The node identity file: root rw, the daemon's group r, nothing else.
NODE_ENV_MAX_MODE = 0o640

# The previous pass's standing findings, so an unchanged one is not re-warned every pass.
_LAST_UNFIXED: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Finding:
    """One secret file whose mode is (or was) wider than allowed. `fixed` = this pass narrowed it."""

    path: str
    mode: int
    allowed: int
    fixed: bool = False
    note: str = ""
    dacl: bool = False   # a Windows access list, not mode bits: `mode`/`allowed` carry nothing

    def line(self) -> str:
        state = "tightened" if self.fixed else "TOO WIDE"
        tail = f" — {self.note}" if self.note else ""
        if self.dacl:
            return f"{state}: {self.path} access list{tail}"
        return f"{state}: {self.path} {self.mode:04o} (allowed ≤ {self.allowed:04o}){tail}"


def credential_max_mode() -> int:
    """`0660` where a session user exists (its group is shared with the daemon, which refreshes the token),
    else `0600`. Graded off the node's posture, never off the caller's uid, so the doctor agrees with the daemon."""
    return 0o660 if session_user.session_uid() is not None else 0o600


# One file the pass guards: `root` is a directory it trusts by construction (an account or slot dir, never a
# symlink), `rel` the path below it walked one component at a time without following a link, `allowed` the
# mode ceiling, and `daemon_chmod` whether this daemon may chmod a file IT owns there (a runner-owned slot
# holding a daemon-owned file is an anomaly to report, never to touch).
@dataclass(frozen=True)
class Target:
    root: str
    rel: str
    allowed: int
    daemon_chmod: bool

    @property
    def path(self) -> str:
        return f"{self.root}/{self.rel}"


def _real_subdirs(parent: Path) -> list[Path]:
    try:
        return sorted(d for d in parent.iterdir() if not d.is_symlink() and d.is_dir())
    except OSError:
        return []


def account_credential_files() -> list[Target]:
    """Every provider credential file name under every account dir — present or not."""
    try:
        auth_root = env_helpers.jaah_dir("auth")
    except (RuntimeError, ValueError):
        return []      # no JAAH_HOME: no accounts to guard
    names = sorted({p.credential_file for p in agent_providers.PROVIDERS if p.credential_file})
    allowed = credential_max_mode()
    # A dot-dir is never an account (no slug starts with `.`): `.shared` is the accounts NodeLink's mount.
    return [Target(str(account), name, allowed, True) for account in _real_subdirs(auth_root)
            if not account.name.startswith(".") for name in names]


def worktree_secret_files() -> list[Target]:
    """`WORKTREE_PRIVATE_MODES` in every slot under `repos/<project>/<N>` (the `main` clone is never leased)."""
    try:
        repos = env_helpers.jaah_dir("repos")
    except (RuntimeError, ValueError):
        return []
    return [Target(str(slot), rel, mode, False)
            for project in _real_subdirs(repos) for slot in _real_subdirs(project) if slot.name.isdigit()
            for rel, mode in env_file.WORKTREE_PRIVATE_MODES]


def nodevault_files() -> list[Target]:
    """The NodeVault keystore's files (`nodevault_store.secret_files`), owner-only, daemon-owned."""
    return [Target(root, rel, nodevault_store.FILE_MODE, True) for root, rel in nodevault_store.secret_files()]


def _session_gids() -> set[int]:
    """Every gid the session user holds — the groups a runner reads through."""
    name = session_user.session_user()
    gid = session_user.session_gid()
    if not name or gid is None:
        return set()
    try:
        return set(os.getgrouplist(name, gid))
    except OSError:
        return {gid}


def node_env_finding(path: str | None = None, runner_gids: set[int] | None = None,
                     owner_uid: int = 0) -> Finding | None:
    """A finding when node.env is not root's, grants more than owner rw + group r, or its group is one a
    runner holds. Audit only: the file is root's."""
    path = path or environment.node_env_path()
    runner_gids = _session_gids() if runner_gids is None else runner_gids
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return Finding(path, 0, NODE_ENV_MAX_MODE, note=f"cannot stat: {exc}")
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return Finding(path, mode, NODE_ENV_MAX_MODE, note="not a regular file")
    # The native local unit runs as the login user and owns its own node.env (single-user, no runner split).
    if st.st_uid != owner_uid and not (edition.IS_CE and st.st_uid == os.geteuid()):
        return Finding(path, mode, NODE_ENV_MAX_MODE, note=f"owned by uid {st.st_uid}, not root")
    if mode & ~NODE_ENV_MAX_MODE:
        own = st.st_uid != owner_uid  # the local carve-out above: the daemon's own file, not root's
        return Finding(path, mode, NODE_ENV_MAX_MODE,
                       note="fix with `chmod 0600`" if own else "root-owned: fix with `chmod 0640` as root")
    if mode & 0o070 and st.st_gid in runner_gids:
        return Finding(path, mode, NODE_ENV_MAX_MODE,
                       note=f"group-readable by gid {st.st_gid}, which the session (runner) user holds")
    return None


def _open_parent(target: Target) -> tuple[int, str]:
    """`(dir fd, leaf name)` for `target`, every component below `root` opened `O_NOFOLLOW`: a runner that
    swaps `.claude` or an account's provider dir for a link to elsewhere gets ELOOP/ENOTDIR, never a walk."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    *dirs, leaf = target.rel.split("/")
    fd = os.open(target.root, flags)
    try:
        for part in dirs:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd, leaf


def _narrow(target: Target, fix: bool, chmod: Callable[[str, int], str]) -> Finding | None:
    """Check ONE file; with `fix`, narrow it to `mode & allowed`. Absent is fine; never raises."""
    path, allowed = target.path, target.allowed
    try:
        dir_fd, leaf = _open_parent(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return Finding(path, 0, allowed, note=f"cannot walk without following a link: {exc}")
    try:
        try:
            st = os.lstat(leaf, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return Finding(path, 0, allowed, note=f"cannot stat: {exc}")
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            return None    # a Shared account's token link: the share's own file carries the mode (nodelink.md)
        if st.st_uid not in _own_uids():
            return Finding(path, mode, allowed, note=f"owned by uid {st.st_uid}, not this node's session or daemon uid")
        session_gid = session_user.session_gid()
        group_wrong = bool(mode & 0o070) and session_gid is not None and st.st_gid != session_gid
        if not mode & ~allowed and not group_wrong:
            return None
        if not stat.S_ISREG(st.st_mode):
            return Finding(path, mode, allowed, note="not a regular file")
        # A group bit is allowed only for the session group: any other group narrows to owner-only.
        ceiling = allowed & ~0o070 if group_wrong else allowed
        note = f"group gid {st.st_gid} is not the session group" if group_wrong else ""
        if not fix:
            return Finding(path, mode, ceiling, note=note)
        if st.st_uid == os.geteuid():
            # On a two-uid node a daemon-owned file in a runner's slot is an anomaly (a planted link, a
            # stray write): reported, never chmodded as the daemon. On a one-uid node the daemon IS the runner.
            err = (_fchmod_at(dir_fd, leaf, ceiling) if target.daemon_chmod or not session_user.switch_needed()
                   else "daemon-owned file in a runner slot — reported, not touched")
        else:
            # As the session user: it can reach only files that uid could chmod itself.
            err = chmod(path, mode & ceiling)
        if err:
            return Finding(path, mode, ceiling, note=f"could not tighten: {err}")
        return Finding(path, mode, ceiling, fixed=True, note=note)
    finally:
        os.close(dir_fd)


def _own_uids() -> set[int]:
    """The uids a node-local secret may belong to: this daemon and its session user."""
    session = session_user.session_uid()
    return {os.geteuid()} | ({session} if session is not None else set())


def _fchmod_at(dir_fd: int, leaf: str, allowed: int) -> str:
    """`fchmod` through an `O_NOFOLLOW` descriptor: the inode narrowed is the one opened, from ITS OWN mode,
    and a hardlinked file (a runner can link a daemon file it may write) is refused."""
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        return str(exc)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            return "not a single-link regular file this daemon owns"
        os.fchmod(fd, stat.S_IMODE(st.st_mode) & allowed)
    except OSError as exc:
        return str(exc)
    finally:
        os.close(fd)
    return ""


def nodevault_dacl_findings(fix: bool) -> list[Finding]:
    """Windows: the keystore's access lists (`nodevault_store.Keystore.repair`), when there is a keystore."""
    keystore = nodevault_store.Keystore()
    if not keystore.root.is_dir():
        return []
    return [Finding(path, 0, 0, fixed=fixed, note=note, dacl=True) for path, note, fixed in keystore.repair(fix=fix)]


def _scan(fix: bool, chmod: Callable[[str, int], str]) -> list[Finding]:
    if host_platform.is_windows():
        return nodevault_dacl_findings(fix)
    findings = [f for target in account_credential_files() + worktree_secret_files() + nodevault_files()
                if (f := _narrow(target, fix, chmod)) is not None]
    node_env = node_env_finding()
    if node_env is not None:
        findings.append(node_env)
    return findings


def audit() -> list[Finding]:
    """Every secret file wider than allowed. Read-only — the fleet doctor prints this."""
    return _scan(False, session_user.chmod_as_session_user)


def enforce(chmod: Callable[[str, int], str] = session_user.chmod_as_session_user) -> list[Finding]:
    """Narrow every too-wide secret file this node can; log and return what it saw. Never raises."""
    global _LAST_UNFIXED
    findings = _scan(True, chmod)
    unfixed = frozenset(f.line() for f in findings if not f.fixed)
    for f in findings:
        if f.fixed:
            log.info("secret-modes %s", f.line())
        else:
            # A standing finding is warned once, then repeated at debug — not every five minutes forever.
            (log.debug if unfixed == _LAST_UNFIXED else log.warning)("secret-modes %s", f.line())
    _LAST_UNFIXED = unfixed
    return findings


async def step() -> None:
    """The daemon's periodic `secret-modes` step (also fired at startup)."""
    await asyncio.to_thread(enforce)
