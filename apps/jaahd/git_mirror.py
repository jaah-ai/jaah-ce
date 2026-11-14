#!/usr/bin/env python3
"""The single home of CREDENTIALED git for a slot: a daemon-owned bare mirror per forge URL (`slot-<hash>.git`).

A slot is cloned and refreshed with the leasing task's login (`clone_identity.acting_for`), and every network op
of it runs HERE, as the daemon uid, with its cwd in a mirror no runner uid can write, reading only the system
gitconfig, the mirror's own daemon-written config and command-line pins (`git_env.DECLARED_OP` cuts the global
scope). No config, hook or include in a runner-writable tree can steer it, because no such tree is ever its
repository. The slot is then filled by a LOCAL fetch from the mirror (`local_fetch_args`) that runs undeclared —
the broker serves it nothing — and is restricted to the `file` transport, so a planted `url.*.insteadOf`,
`remote.*.*` or proxy can only point it at another local path.

The root is `<daemon state>/pool-mirrors`, or `$JAAH_POOL_MIRRORS` where the session user cannot traverse the
daemon's state dir (a vnode's, a macOS node's): 2750 in the session group, each mirror `core.sharedRepository=0640`,
which the session uid must READ to fetch from locally but can never write. On a host with no uid split the runner IS
the daemon uid, and there is no such boundary to keep. `sweep` removes a slot mirror unused for
`SLOT_MIRROR_GRACE_S`, and what an older daemon kept there (its per-project mirrors).

IMPORTS: stdlib + the leaves `clone_identity`, `env_helpers`, `environment`, `git_env` — `slot_clone` imports
this module."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import clone_identity
import env_helpers
import environment
import git_env

log = logging.getLogger("jaahd.git_mirror")

#: Mode of the static pool's mirrors: group-readable for the session uid's local fetch, never group-writable.
_SHARED_MODE = "0640"
#: The process umask of every git call that writes a shared mirror: the daemon runs with `UMask=0002`, and a file
#: git writes outside `core.sharedRepository`'s reach (`FETCH_HEAD`) would otherwise be group-writable.
_SHARED_UMASK = 0o027

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
#: Mirror path → (monotonic time its last fetch completed, the URL it fetched from), read by the sweep's grace.
_refreshed_at: dict[str, tuple[float, str]] = {}
#: The environment of every `local_fetch_args` call. `GIT_ALLOW_PROTOCOL` is git's one transport allowlist that
#: outranks every `protocol.<name>.allow` key, which a tree's config could otherwise set past the argv's pins.
LOCAL_FETCH_ENV: dict[str, str] = {"GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}


class MirrorError(RuntimeError):
    """A fetch, ls-remote or push of a mirror failed. Its text carries git's head and tail and never URL
    userinfo."""


def mirrors_root() -> Path:
    relocated = environment.pool_mirrors_root()
    return Path(relocated) if relocated else env_helpers.daemon_state_dir("pool-mirrors")


#: A slot's mirror (`_slot_mirror`); `_LEGACY_NAME` is an older daemon's per-project or per-URL mirror, read by
#: nothing now; `_ASIDE_NAME` is one moved aside (`_remove_mirror`) or a temp left by a crash (`_create`,
#: `shallow_for`).
_SLOT_NAME = re.compile(r"slot-[0-9a-f]{16}\.git")
_LEGACY_NAME = re.compile(r"(?:\d+(?:-\d+)?|url-[0-9a-f]{16})\.git")
_ASIDE_NAME = re.compile(r"\.(?P<mirror>(?:\d+(?:-\d+)?|(?:url|slot)-[0-9a-f]{16})\.git)\.[0-9a-f]{8}")
#: A mirror fetched, or a temp written, this recently is never swept: a lease may still be fetching from it. Above
#: a clone's budget, so a crashed `_create` temp is never one a live clone still writes.
SWEEP_GRACE_S = 3600.0
#: A slot mirror goes only once unused this long: long enough that an idle project's next lease fetches a delta
#: rather than cloning the repository again.
SLOT_MIRROR_GRACE_S = 7 * 86400.0


def sweep() -> list[str]:
    """Remove from the root every slot mirror unused for `SLOT_MIRROR_GRACE_S`, and every legacy mirror, aside or
    temp unused for `SWEEP_GRACE_S` → the names removed. A mirror in use (its lock is held) is skipped and swept on
    a later pass."""
    root = mirrors_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    removed: list[str] = []
    for name in names:
        aside = _ASIDE_NAME.fullmatch(name)
        if _SLOT_NAME.fullmatch(name):
            grace = SLOT_MIRROR_GRACE_S
        elif aside or _LEGACY_NAME.fullmatch(name):
            grace = SWEEP_GRACE_S
        else:
            continue
        lock_path = root / (aside.group("mirror") if aside else name)
        if _remove_mirror(root / name, lock_path, grace=grace):
            removed.append(name)
    return removed


def _recently_used(path: Path, grace: float = SWEEP_GRACE_S) -> bool:
    stamp, _url = _refreshed_at.get(str(path), (float("-inf"), ""))
    if time.monotonic() - stamp < grace:
        return True
    # Across a daemon restart: a fetch rewrites FETCH_HEAD, a clone's (or a temp's) directory is as new as it.
    for probe in (path, path / "FETCH_HEAD"):
        try:
            if time.time() - os.lstat(probe).st_mtime < grace:
                return True
        except OSError:
            continue
    return False


def _remove_mirror(path: Path, lock_path: Path, *, grace: float = SWEEP_GRACE_S) -> bool:
    """Delete `path` under `lock_path`'s lock (the mirror's own), moved aside first so no reader ever sees half
    a mirror. False when busy, recently used, gone, not a real directory, or not fully deleted (logged)."""
    lock = _lock(lock_path)
    if not lock.acquire(blocking=False):
        return False
    try:
        if not path.is_dir() or path.is_symlink() or _recently_used(path, grace):
            return False
        aside = path if path != lock_path else path.with_name(f".{path.name}.{secrets.token_hex(4)}")
        if aside != path:
            os.rename(path, aside)
            _refreshed_at.pop(str(path), None)
        shutil.rmtree(aside, ignore_errors=True)
        if os.path.lexists(aside):
            log.warning("the pool mirror %s was only partly removed; left as %s", path.name, aside.name)
            return False
        return True
    except OSError as exc:
        log.warning("could not remove the pool mirror %s: %s", path.name, exc)
        return False
    finally:
        lock.release()


def _lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


@contextmanager
def _held(path: Path, wait_s: float) -> Iterator[None]:
    """`path`'s lock, waited for at most `wait_s`; `ProjectBusy` past it."""
    budget = max(0.0, wait_s)
    lock = _lock(path)
    if not lock.acquire(timeout=budget):
        raise clone_identity.ProjectBusy(
            f"the mirror {path.name} was still in use by another operation after {budget:.0f}s")
    try:
        yield
    finally:
        lock.release()


def _ensure_root() -> Path:
    root = mirrors_root()
    # setgid: every mirror under it lands in the root's group, the session user's, whatever the daemon's own.
    root.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(root, 0o2750)
    _grant_runner_read(root)
    return root


#: Roots whose Windows access list already lets the runner read; one `icacls` per root per daemon run.
_RUNNER_READ: set[str] = set()


def _grant_runner_read(root: Path) -> None:
    """Windows' half of the setgid 2750 above, which sets no access list there: the runner account reads every
    mirror under `root` (inherited), as the session group does on Linux. Without it a slot's local fetch, which
    runs as that account, could not open the mirror. Raises `MirrorError`; a no-op off Windows."""
    import host_platform  # noqa: PLC0415 — the seam; see the module's IMPORTS rule

    if not host_platform.is_windows() or str(root) in _RUNNER_READ:
        return
    from host_platform import windows  # noqa: PLC0415 — Windows-only

    try:
        proc = subprocess.run(["icacls", str(root), "/grant", f"*{windows.runner_sid()}:(OI)(CI)RX", "/L", "/q"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MirrorError(f"cannot let the runner account read the mirrors at {root}: {exc}") from None
    if proc.returncode != 0:
        raise MirrorError(f"cannot let the runner account read the mirrors at {root}: icacls rc {proc.returncode}: "
                          f"{(proc.stderr or proc.stdout or '').strip() or '(no output)'}")
    _RUNNER_READ.add(str(root))


def _run(cwd: Path, *args: str, timeout: float, network: bool = False) -> str:
    """`git <args>` in `cwd` (inside the mirror root) as the daemon uid (`git_env.run_own_git`). A network op
    carries `git_env.DECLARED_OP`. Raises `MirrorError`."""
    return git_env.run_own_git(args, cwd=cwd, timeout=timeout, error_cls=MirrorError,
                               env=git_env.DECLARED_OP if network else git_env.NO_USER_CONFIG,
                               umask=_SHARED_UMASK, where=f" in the mirror {cwd.name}")


def _create(url: str, path: Path, *, timeout: float) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    try:
        root = _ensure_root()
        _run(root, "clone", "--quiet", "--bare", "--", url, str(tmp), timeout=timeout, network=True)
        _run(tmp, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*", timeout=60)
        _run(tmp, "config", "core.sharedRepository", _SHARED_MODE, timeout=60)
        os.replace(tmp, path)
    except OSError as exc:
        raise MirrorError(git_env.redact_userinfo(f"the mirror {path.name} could not be created: {exc}")) from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def branch_sha(mirror: Path, branch: str) -> str | None:
    """The sha `refs/heads/<branch>` names in `mirror` (after a fetch: what origin names), or None. Local, read
    as the daemon, undeclared."""
    sha = _read(mirror, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    return sha if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else None


def has_branch(mirror: Path) -> bool:
    """Whether `mirror` holds any branch — after a fetch, whether the forge has one (an empty repository has none).
    Local, read as the daemon, undeclared. Raises `MirrorError` when git cannot say: a failed read is never "none"."""
    done = subprocess.run(["git", *git_env.NO_REPO_CODE, "for-each-ref", "--count=1", "--format=%(refname)",
                           "refs/heads"], cwd=mirror, capture_output=True, text=True,
                          env={**os.environ, **git_env.NO_USER_CONFIG})
    if done.returncode != 0:
        raise MirrorError(f"the branches of the mirror {mirror.name} could not be read")
    return bool(done.stdout.strip())


def head_branch(mirror: Path) -> str | None:
    """The branch `mirror`'s HEAD names (origin's default when it was cloned), or None. Local, as `branch_sha`."""
    ref = _read(mirror, "symbolic-ref", "--quiet", "HEAD")
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None


def _read(mirror: Path, *args: str) -> str:
    done = subprocess.run(["git", *git_env.NO_REPO_CODE, *args], cwd=mirror, capture_output=True, text=True,
                          env={**os.environ, **git_env.NO_USER_CONFIG})
    return done.stdout.strip() if done.returncode == 0 else ""


def local_fetch_args(mirror: Path, *refspecs: str, depth: int | None = None, prune: bool = False
                     ) -> tuple[str, ...]:
    """git arguments (the caller prepends `-C <tree>`) fetching `refspecs` from `mirror` with no credential. Run it
    UNDECLARED, so the broker serves it nothing, with `LOCAL_FETCH_ENV` in its environment.

    The tree's config is the runner's, so nothing it says may reach past the mirror: only the `file` transport
    is allowed (`-c protocol.*` here and `GIT_ALLOW_PROTOCOL` in `LOCAL_FETCH_ENV`, which no config key
    outranks: a planted insteadOf, remote or proxy reaches no network), no credential helper is
    left, `--upload-pack` on the command line outranks a configured one, and the mirror is named by a one-call
    alias that `-c url.<mirror>.insteadOf` maps back to it. Why an alias: git keeps the LONGEST insteadOf match,
    and no planted prefix can be as long as a random alias it never saw, so no rewrite in the tree can redirect
    the fetch; and a name with a `/` is never a remote nick, so no `remote.<name>.*` (`vcs`, `uploadpack`) the
    tree holds applies. `safe.directory`: the session uid reads a mirror the daemon owns. It is pinned twice
    because the local transport strips the caller's `-c` config from the `upload-pack` it spawns: a git that
    applies the ownership check there (2.47 does, 2.53 does not) only sees the pin inside `--upload-pack`, which
    git runs through the shell, hence the quoting. Raises `ValueError` for a mirror path that cannot be a `-c`
    key."""
    if "=" in str(mirror) or any(ord(ch) < 32 for ch in str(mirror)):
        raise ValueError(f"not a usable mirror path: {str(mirror)!r}")
    upload_pack = f"git -c {shlex.quote(f'safe.directory={mirror}')} upload-pack"
    alias = f"jaah-mirror/{secrets.token_hex(16)}"
    return ("-c", f"safe.directory={mirror}", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
            "-c", "credential.helper=", "-c", f"url.{mirror}.insteadOf={alias}",
            # Two programs and one URL a tree's config could otherwise make the fetch reach: a bundle URI is
            # fetched over its own transport before the remote, and `alternateRefsCommand` runs for a planted
            # `objects/info/alternates`.
            "-c", "fetch.bundleURI=", "-c", "transfer.bundleURI=false", "-c", "core.alternateRefsCommand=true",
            "fetch", "--no-tags", "--no-recurse-submodules", f"--upload-pack={upload_pack}",
            *(("--depth", str(depth)) if depth else ()), *(("--prune",) if prune else ()),
            "--end-of-options", alias, *refspecs)


# ── Acting: a slot's forge ops, with the access of the task (or trigger) it is for ───────────────────────
#
# The network op runs as the daemon uid (`git_env.run_own_git` → `clone_identity.run_own`, registered under the
# acting subject), never on the session user's tmux channel: a process the daemon forks itself is the only one the
# credential broker serves a login. One mirror per URL (swept by `sweep` once unused for `SLOT_MIRROR_GRACE_S`):
# no project declaration, so no node-wide `clone_identity.cloning` lock —
# two slots of two projects fetch at once, and two of one URL wait on the mirror's own lock for at most the op's
# budget. Every call fetches with its own subject's login (no `_refreshed_at` short cut): a task whose login
# cannot see the repository is refused even when another task's fetch just filled the mirror.

#: What a slot mirror keeps of the forge: every branch and every tag, pruned with them.
_SLOT_REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")


def _slot_mirror(url: str) -> Path:
    return mirrors_root() / f"slot-{hashlib.sha256(url.encode()).hexdigest()[:16]}.git"


def fetch_for(acting: "clone_identity.Acting", url: str, *refspecs: str, timeout: float, head: bool = False
              ) -> Path:
    """The mirror of `url`, created (`clone --bare`) or fetched (`--prune` of its branches and tags, plus
    `refspecs`: forge refs a slot needs beyond them, each `+<ref>:<ref>`) now with `acting`'s access. `head` also
    points the mirror's HEAD at the forge's default branch now (a clone's `origin/HEAD`). Returns its path.
    Raises `ProjectBusy` (another op held it past `timeout`) and `MirrorError`."""
    path = _slot_mirror(url)
    with _held(path, timeout), clone_identity.acting_for(acting):
        created = not (path / "HEAD").is_file()
        if created:
            _create(url, path, timeout=timeout)
        else:
            _grant_runner_read(path.parent)
        if refspecs or not created:
            _run(path, "fetch", "--prune", "origin", *_SLOT_REFSPECS, *refspecs, timeout=timeout, network=True)
        if head and not created:
            _repoint_head(path, timeout)
        _refreshed_at[str(path)] = (time.monotonic(), url)
    return path


def _repoint_head(path: Path, timeout: float) -> None:
    """HEAD → the forge's default branch as it names it now (a bare mirror's HEAD is set once, at its clone)."""
    out = _run(path, "ls-remote", "--symref", "origin", "HEAD", timeout=timeout, network=True)
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            ref = line[len("ref: "):-len("\tHEAD")]
            if branch_sha(path, ref[len("refs/heads/"):]):
                _run(path, "symbolic-ref", "HEAD", ref, timeout=60)
            return


@contextmanager
def shallow_for(acting: "clone_identity.Acting", url: str, branch: str, *, depth: int, timeout: float
                ) -> Iterator[Path]:
    """A throwaway bare clone of `url`'s `branch` at `depth`, made now with `acting`'s access in the static pool's
    root and removed on exit — for a one-off shallow tree (a trigger's preflight), which must not make the
    slot mirror shallow. Named as a temp of `url`'s mirror, so one a crash left is swept. Raises `MirrorError`."""
    tmp = _slot_mirror(url)
    tmp = tmp.with_name(f".{tmp.name}.{secrets.token_hex(4)}")
    try:
        with clone_identity.acting_for(acting):
            try:
                root = _ensure_root()
            except OSError as exc:
                raise MirrorError(f"the mirrors root could not be prepared: {exc}") from None
            _run(root, "clone", "--quiet", "--bare", "--depth", str(depth), "--single-branch", "--branch", branch,
                 "--", url, str(tmp), timeout=timeout, network=True)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def push_for(acting: "clone_identity.Acting", url: str, sha: str, dst: str, *, timeout: float) -> None:
    """`push origin <sha>:<dst>` from `url`'s mirror with `acting`'s access; `sha` must be an object the mirror
    holds (`fetch_for` first). Raises `ProjectBusy`/`MirrorError` as `fetch_for` does."""
    path = _slot_mirror(url)
    with _held(path, timeout), clone_identity.acting_for(acting):
        _run(path, "push", "--quiet", "origin", f"{sha}:{dst}", timeout=timeout, network=True)


def object_format(mirror: Path) -> str:
    """The hash `mirror` stores objects by (`sha1`, `sha256`); `sha1` when git does not say."""
    return _read(mirror, "rev-parse", "--show-object-format") or "sha1"
