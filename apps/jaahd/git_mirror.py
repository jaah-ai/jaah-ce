#!/usr/bin/env python3
"""The single home of CREDENTIALED git on a node: a daemon-owned bare mirror per (project, repository).

Every network git op the daemon declares (`clone_identity.cloning`) runs HERE, with its cwd in a mirror no runner
uid can write, as the daemon uid, reading only the system gitconfig, the mirror's own daemon-written config and
command-line pins (`git_env.DECLARED_OP` cuts the global scope). No config, hook or include in a runner-writable
tree can steer it, because no such tree is ever its repository. Runner clones and worktrees are then updated by
a LOCAL fetch from the mirror (`local_fetch_args`) that runs undeclared — the broker serves it
nothing — and is restricted to the `file` transport, so a planted `url.*.insteadOf`, `remote.*.*` or proxy can
only point it at another local path.

Two roots, chosen by the caller (a node is either static or MicroVM, so no node-kind predicate is needed):
`shared=False` is the MicroVM root (`<daemon state>/mirrors`, 0700, as it always was); `shared=True` is the static
pool's (`<daemon state>/pool-mirrors`, or `$JAAH_POOL_MIRRORS` where the session user cannot traverse the daemon's
state dir: a vnode's, 0700, and a macOS node's; 2750 in the session group, each mirror `core.sharedRepository=0640`),
which the session uid must READ to fetch from locally but can never write. On a host with no uid split the runner IS the daemon uid,
and there is no such boundary to keep.

IMPORTS: stdlib + the leaves `acp.session_store`, `clone_identity`, `env_helpers`, `environment`, `git_env` — `worktree_mechanics`
imports this module, and its import rules apply here."""
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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import clone_identity
import env_helpers
import environment
import git_env
from acp.session_store import private_dir

log = logging.getLogger("jaahd.git_mirror")

#: Mode of the static pool's mirrors: group-readable for the session uid's local fetch, never group-writable.
_SHARED_MODE = "0640"
#: The process umask of every git call that writes a shared mirror: the daemon runs with `UMask=0002`, and a file
#: git writes outside `core.sharedRepository`'s reach (`FETCH_HEAD`) would otherwise be group-writable.
_SHARED_UMASK = 0o027

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
#: Mirror path → (monotonic time its last refresh completed, the URL it refreshed from): a refresh of the same URL
#: that waited on the lock while another completed skips its own network fetch (no TTL: only a refresh that
#: finished after this one asked counts).
_refreshed_at: dict[str, tuple[float, str]] = {}
#: The longest wait for a mirror another thread is refreshing (a first clone holds it for minutes), as
#: `clone_identity`'s own bound; a caller's `wait_s` lowers it.
_LOCK_WAIT_S = 60.0
#: The environment of every `local_fetch_args` call. `GIT_ALLOW_PROTOCOL` is git's one transport allowlist that
#: outranks every `protocol.<name>.allow` key, which a tree's config could otherwise set past the argv's pins.
LOCAL_FETCH_ENV: dict[str, str] = {"GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}


class MirrorError(RuntimeError):
    """A refresh, ls-remote or push of a mirror failed. Its text carries git's head and tail and never URL
    userinfo."""


@dataclass(frozen=True)
class MirrorKey:
    #: `projects.id`; None only on an older CP's roster (or a test's), when the mirror is keyed by its URL.
    project_id: int | None
    #: The mirror's repository: None = the project's primary (`<pid>.git`), else `<pid>-<rid>.git`.
    repo_id: int | None
    #: The declaration's name (`clone_identity.cloning(slug, ...)`).
    slug: str
    #: The CP's credential-free URL.
    clone_url: str
    #: The `repo_id` the declaration names (a non-primary repository's), so the CP serves that one's login.
    declared_repo_id: int | None = None


def gist(text: str, limit: int = 460) -> str:
    """Git's output kept under the rail's 500-character report: its head AND its tail. The head is where a
    credential refusal names the person and the fix; the tail is git's own `fatal:` line. A tail-only cut
    reported the launcher as `h.ai`."""
    if len(text) <= limit:
        return text
    tail = 150
    return f"{text[:limit - tail - 3]} … {text[-tail:]}"


def mirrors_root(*, shared: bool) -> Path:
    if not shared:
        return env_helpers.daemon_state_dir("mirrors")
    relocated = environment.pool_mirrors_root()
    return Path(relocated) if relocated else env_helpers.daemon_state_dir("pool-mirrors")


def mirror_path(key: MirrorKey, *, shared: bool) -> Path:
    """`<pid>.git` or `<pid>-<rid>.git`: the caller's `MirrorKey.repo_id` decides. MicroVM keys every repository
    of a multi-repository project by its id, so a primary change never retargets a mirror another VM clones
    from; the static pool keys its primary `<pid>.git` and each other repository `<pid>-<rid>.git`. With no
    project id, a name derived from the URL."""
    if key.project_id is None:
        name = f"url-{hashlib.sha256(key.clone_url.encode()).hexdigest()[:16]}.git"
    elif key.repo_id is None:
        name = f"{int(key.project_id)}.git"
    else:
        name = f"{int(key.project_id)}-{int(key.repo_id)}.git"
    return mirrors_root(shared=shared) / name


#: A static-pool mirror's name (`mirror_path`): `<pid>.git`, `<pid>-<rid>.git`, or an id-less roster's
#: `url-<hash>.git`; `_ASIDE_NAME` is one moved aside (`_remove_mirror`) or a `_create` temp left by a crash.
_SHARED_NAME = re.compile(r"(?:(?P<pid>\d+)(?:-(?P<rid>\d+))?|url-[0-9a-f]{16})\.git")
_ASIDE_NAME = re.compile(r"\.(?P<mirror>(?:\d+(?:-\d+)?|url-[0-9a-f]{16})\.git)\.[0-9a-f]{8}")
#: A mirror refreshed, or a temp written, this recently is never swept: a caller that refreshed it may still be
#: fetching from it unlocked, under a newer roster than the sweeping pass read (a `create` of a just-enrolled
#: project), or a caller no roster names may be using it (a trigger preflight's `url-<hash>.git`). Above a
#: clone's budget, so a crashed `_create` temp is never one a live clone still writes.
SWEEP_GRACE_S = 3600.0


def sweep_shared(keep: dict[int, frozenset[int] | None], *, ids_complete: bool) -> list[str]:
    """Remove the static pool's mirrors the roster no longer names → the names removed.

    `keep` maps each rostered `projects.id` to its repository ids (None = unknown, so every `<pid>-<rid>.git`
    of it stays). A `<pid>*.git` whose project is absent, or whose `rid` its project no longer lists, goes;
    a `url-<hash>.git` goes only when `ids_complete` (every rostered project carries an id, so none is keyed
    by URL). The caller passes only a roster it parsed whole. A mirror being refreshed (its lock is held) or
    used within `SWEEP_GRACE_S` is skipped and swept on a later pass; so is a leftover aside or temp."""
    root = mirrors_root(shared=True)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    removed: list[str] = []
    for name in names:
        match = _SHARED_NAME.fullmatch(name)
        aside = None if match else _ASIDE_NAME.fullmatch(name)
        if aside is not None:
            stale = True
        elif match is None:
            continue
        elif match.group("pid") is None:
            stale = ids_complete
        elif int(match.group("pid")) not in keep:
            stale = True
        else:
            repos, rid = keep[int(match.group("pid"))], match.group("rid")
            stale = rid is not None and repos is not None and int(rid) not in repos
        lock_path = root / (aside.group("mirror") if aside else name)
        if stale and _remove_mirror(root / name, lock_path):
            removed.append(name)
    return removed


def _recently_used(path: Path) -> bool:
    stamp, _url = _refreshed_at.get(str(path), (float("-inf"), ""))
    if time.monotonic() - stamp < SWEEP_GRACE_S:
        return True
    # Across a daemon restart: a fetch rewrites FETCH_HEAD, a clone's (or a temp's) directory is as new as it.
    for probe in (path, path / "FETCH_HEAD"):
        try:
            if time.time() - os.lstat(probe).st_mtime < SWEEP_GRACE_S:
                return True
        except OSError:
            continue
    return False


def _remove_mirror(path: Path, lock_path: Path) -> bool:
    """Delete `path` under `lock_path`'s lock (the mirror's own), moved aside first so no reader ever sees half
    a mirror. False when busy, recently used, gone, not a real directory, or not fully deleted (logged)."""
    lock = _lock(lock_path)
    if not lock.acquire(blocking=False):
        return False
    try:
        if not path.is_dir() or path.is_symlink() or _recently_used(path):
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
def _held(path: Path, wait_s: float | None) -> Iterator[None]:
    """`path`'s lock, waited for at most `wait_s` (capped at `_LOCK_WAIT_S`); `ProjectBusy` past it, the
    refusal every caller already translates (e.g. "clone-busy: ")."""
    budget = _LOCK_WAIT_S if wait_s is None else max(0.0, min(wait_s, _LOCK_WAIT_S))
    lock = _lock(path)
    if not lock.acquire(timeout=budget):
        raise clone_identity.ProjectBusy(
            f"the mirror {path.name} was still being refreshed by another operation after {budget:.0f}s")
    try:
        yield
    finally:
        lock.release()


def _ensure_root(shared: bool) -> Path:
    root = mirrors_root(shared=shared)
    if not shared:
        return private_dir(root)
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


def _run(cwd: Path, *args: str, shared: bool, timeout: float, network: bool = False) -> str:
    """`git <args>` in `cwd` (inside the mirror root) as the daemon uid, registered under this thread's
    declaration (`clone_identity.run_own`). A network op carries `git_env.DECLARED_OP`. Raises `MirrorError`."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(git_env.DECLARED_OP if network else git_env.NO_USER_CONFIG)}
    try:
        done = clone_identity.run_own(["git", *git_env.NO_REPO_CODE, *args], cwd=cwd, text=True, timeout=timeout,
                                      env=env, umask=_SHARED_UMASK if shared else -1)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MirrorError(git_env.redact_userinfo(f"git {args[0]} in the mirror {cwd.name} failed: {exc}")) from None
    if done.returncode != 0:
        out = git_env.redact_userinfo((done.stderr or done.stdout or "").strip())
        raise MirrorError(f"git {' '.join(args[:2])} in the mirror {cwd.name} failed: {gist(out)}")
    return done.stdout


def _declared(key: MirrorKey, wait_s: float | None):
    extra = {"repo_id": key.declared_repo_id} if key.declared_repo_id is not None else {}
    return clone_identity.cloning(key.slug, wait_s=wait_s, project_id=key.project_id, **extra)


def refresh(key: MirrorKey, *, shared: bool, timeout: float, wait_s: float | None = None,
            create: bool = True) -> Path | None:
    """The mirror of `key`, created (`clone --bare`) or fetched (`--prune`, branch heads only: a forge's PR refs
    would multiply the fetch for nothing a tree uses) now, inside `key`'s declaration. Returns its path, or None
    when it does not exist yet and `create` is False.

    `timeout` bounds each git call — a creation is a full clone, so a caller that may create passes a clone's
    budget (`node_clone_timeout_secs`). `wait_s` lowers the declaration's wait (`clone_identity.cloning`). Raises `clone_identity.ProjectBusy` unchanged (each
    caller keeps its own translation, e.g. "clone-busy: ") and `MirrorError`."""
    path = mirror_path(key, shared=shared)
    asked = time.monotonic()
    with _held(path, wait_s):
        exists = (path / "HEAD").is_file()
        stamp, url = _refreshed_at.get(str(path), (float("-inf"), ""))
        if exists and stamp > asked and url == key.clone_url:
            return path      # another refresh of this mirror completed while this one waited
        if not exists and not create:
            return None
        with _declared(key, wait_s):
            if not exists:
                _create(key, path, shared=shared, timeout=timeout)
            else:
                if shared:
                    # A root made before the grant existed: its mirrors are read by the runner too.
                    _grant_runner_read(path.parent)
                _run(path, "remote", "set-url", "origin", key.clone_url, shared=shared, timeout=min(60.0, timeout))
                _run(path, "fetch", "--prune", "origin", shared=shared, timeout=timeout, network=True)
        _refreshed_at[str(path)] = (time.monotonic(), key.clone_url)
    return path


def _create(key: MirrorKey, path: Path, *, shared: bool, timeout: float) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    try:
        root = _ensure_root(shared)
        _run(root, "clone", "--quiet", "--bare", "--", key.clone_url, str(tmp), shared=shared, timeout=timeout,
             network=True)
        _run(tmp, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*", shared=shared, timeout=60)
        if shared:
            _run(tmp, "config", "core.sharedRepository", _SHARED_MODE, shared=shared, timeout=60)
        os.replace(tmp, path)
    except OSError as exc:
        raise MirrorError(git_env.redact_userinfo(f"the mirror {path.name} could not be created: {exc}")) from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remote_head(key: MirrorKey, *, shared: bool, timeout: float = 120) -> str | None:
    """The branch origin's HEAD names (its default branch), asked from the mirror; None when origin answers no
    symref. Raises `ProjectBusy`/`MirrorError` as `refresh` does. The mirror must exist (`refresh` first)."""
    path = mirror_path(key, shared=shared)
    with _held(path, None), _declared(key, None):
        out = _run(path, "ls-remote", "--symref", "origin", "HEAD", shared=shared, timeout=timeout, network=True)
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return line[len("ref: refs/heads/"):-len("\tHEAD")]
    return None


def push_ref(key: MirrorKey, *, shared: bool, sha: str, ref: str, timeout: float = 120) -> None:
    """`push origin <sha>:<ref>` from the mirror, declared. `sha` must be an object the mirror holds. Raises
    `ProjectBusy`/`MirrorError` as `refresh` does."""
    path = mirror_path(key, shared=shared)
    with _held(path, None), _declared(key, None):
        _run(path, "push", "--quiet", "origin", f"{sha}:{ref}", shared=shared, timeout=timeout, network=True)


def branch_sha(mirror: Path, branch: str) -> str | None:
    """The sha `refs/heads/<branch>` names in `mirror` (after a refresh: what origin names), or None. Local, read
    as the daemon, undeclared."""
    sha = _read(mirror, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    return sha if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else None


def head_branch(mirror: Path) -> str | None:
    """The branch `mirror`'s HEAD names (origin's default when it was cloned), or None. Local, as `branch_sha`."""
    ref = _read(mirror, "symbolic-ref", "--quiet", "HEAD")
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None


def _read(mirror: Path, *args: str) -> str:
    done = subprocess.run(["git", *git_env.NO_REPO_CODE, *args], cwd=mirror, capture_output=True, text=True,
                          env={**os.environ, **git_env.NO_USER_CONFIG})
    return done.stdout.strip() if done.returncode == 0 else ""


def local_fetch_args(mirror: Path, *refspecs: str, depth: int | None = None) -> tuple[str, ...]:
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
            *(("--depth", str(depth)) if depth else ()), "--end-of-options", alias, *refspecs)
