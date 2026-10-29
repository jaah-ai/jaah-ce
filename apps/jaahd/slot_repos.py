"""Multi-repository slots: every repository of a project, checked out side by side in one slot, and
the converge that brings a slot to the project's current repository set at the START of a session.

LAYOUT (`apps/api/app/node_clone.py` mirrors it for the CP):

    <home>/repos/<pid>/main                  the anchor clone; `git config jaah.repoId` names its repository
    <home>/repos/<pid>/<N>                   slot N — ALWAYS the PRIMARY repository's worktree
    <home>/repos/<pid>/_repos/<rid>/main     clone of repository <rid> when the anchor is not it
    <home>/repos/<pid>/_repos/<rid>/<N>      slot N's worktree of non-primary repository <rid>
    <home>/repos/<pid>/_repos/slot-<N>.json  slot N's manifest (what the last converge left there)

A clone never moves and never changes repository: `pool_origins` re-points a clone only to its OWN row's URL
(a rename), because every worktree of a clone shares its `origin`. A primary change instead MOVES the slot's
worktrees (`git worktree move`), so a held slot's uncommitted work travels with its tree and no other slot of
the project is touched. `_repos` is not a slot name: every slot scan reads only `main` and digit names.

WHEN: only at the start of a session in the slot — a fresh dispatch (`claim`), a resume (`resume`) or a
console that opens a new session (`console`) — never from the reconcile pass, so nothing changes under a
running session (N8). The primary alone gets the gate, stack, ports and `.env` (AC3.5): a non-primary
worktree gets none of them. One non-primary repository failing (a refused credential, a failed clone) is
recorded under `failed` and the session starts anyway; a failed PRIMARY is a refused claim, as today.

A single-repository project that never had another repository converges nothing and writes no manifest
(AC2.1): its slot is byte for byte today's.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, replace

import session_user
from worktree_mechanics import core, env_file as wm_env_file, ports, managed_env, reset, lifecycle
from worktree_mechanics.core import PoolConfig, ResetRefused

log = logging.getLogger("jaahd.slot_repos")

NON_PRIMARY_DIRNAME = "_repos"
# The clone's own identity: which `project_repos.id` it holds. Written by `stamp_anchor` (the anchor) and
# `_ensure_repo_clone` (a `_repos/<rid>/main` clone); read by `clone_repo_id`.
REPO_ID_KEY = "jaah.repoId"

MODE_CLAIM = "claim"
MODE_RESUME = "resume"
MODE_CONSOLE = "console"
MODES = frozenset({MODE_CLAIM, MODE_RESUME, MODE_CONSOLE})

# `failed[].reason` / `kept[].reason` (the manifest's vocabulary, read by the drawer; the runner's SessionStart
# block is Task C4's).
FAILED_CREDENTIAL = "credential_refused"
FAILED_CLONE = "clone_failed"
FAILED_FETCH = "fetch_failed"
FAILED_RESET = "reset_refused"
# The project's access guard could not be installed in the clone: it is withheld rather than handed over
# unguarded.
FAILED_GUARD = "guard_failed"
KEPT_UNCOMMITTED = "uncommitted"
KEPT_UNPUSHED = "unpushed"
# The probe itself failed: kept, because "could not tell" is never "clean".
KEPT_UNVERIFIED = "unverified"

# What a refused credential looks like in git's stderr, across forges (a 401/403, a prompt we disabled).
_CREDENTIAL_REFUSAL = re.compile(
    r"authentication failed|could not read username|terminal prompts disabled|"
    r"\b(401|403)\b|permission denied|access denied|not authorized",
    re.IGNORECASE,
)
_DETAIL_MAX = 300


@dataclass(frozen=True)
class RepoSpec:
    """One repository of the project, as the roster's `repos[]` carries it (S4)."""

    id: int
    clone_url: str
    repo_path: str
    forge: str
    base: str
    primary: bool


def repos_from_wire(raw: object) -> tuple[RepoSpec, ...]:
    """The roster entry's `repos` → `RepoSpec`s, or `()` for anything unusable.

    A row with no id, URL or base is dropped on its own. A list left without exactly ONE primary, or with a
    repeated id, is `()` — no list, which `converge` refuses for a slot that already is multi-repo
    (`legacy_refusal`) rather than guessing."""
    if not isinstance(raw, list) or not raw:
        return ()
    out: list[RepoSpec] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        clone_url, base = str(row.get("clone_url") or ""), str(row.get("base") or "")
        if not isinstance(rid, int) or isinstance(rid, bool) or rid <= 0 or not clone_url or not base:
            # One unusable NON-primary row is dropped alone; an unusable primary leaves no primary below.
            log.warning("slot-repos: roster repos row unusable, dropped: %r", row)
            continue
        out.append(RepoSpec(
            id=rid, clone_url=clone_url, repo_path=str(row.get("repo_path") or ""),
            forge=str(row.get("forge") or ""), base=base, primary=row.get("primary") is True,
        ))
    if sum(1 for spec in out if spec.primary) != 1 or len({spec.id for spec in out}) != len(out):
        log.warning("slot-repos: roster repos list has no single primary or repeats an id — ignored: %r", raw)
        return ()
    return tuple(out)


# ── Paths ──────────────────────────────────────────────────────────────────────────────────────────────
def repos_root(clone: str) -> str:
    """`<home>/repos/<pid>/_repos` for the anchor clone `<home>/repos/<pid>/main`."""
    return posixpath.join(posixpath.dirname(clone.rstrip("/")), NON_PRIMARY_DIRNAME)


def repo_clone_path(clone: str, repo_id: int) -> str:
    return posixpath.join(repos_root(clone), str(repo_id), "main")


def repo_worktree_path(clone: str, repo_id: int, n: int | str) -> str:
    return posixpath.join(repos_root(clone), str(repo_id), str(n))


def manifest_path(clone: str, n: int | str) -> str:
    return posixpath.join(repos_root(clone), f"slot-{n}.json")


def manifest_path_for_slot(wt: str) -> str:
    """The manifest of the slot at `wt` (`<home>/repos/<pid>/<N>`), derived from the slot path alone."""
    base = wt.rstrip("/")
    return posixpath.join(posixpath.dirname(base), NON_PRIMARY_DIRNAME, f"slot-{posixpath.basename(base)}.json")


# A manifest is a few KB; the session user owns `_repos/`, so the daemon reads it as an untrusted file.
_MANIFEST_READ_CAP = 1 << 20


def read_manifest(wt: str) -> dict | None:
    """The manifest the last converge wrote for the slot at `wt`, or None (none, or unreadable).

    Read like any session-user file the daemon opens: no symlink followed, never a FIFO or device (non-blocking
    open, regular files only), and bounded — a planted `/dev/zero` or FIFO must not stall or exhaust the daemon."""
    # `getattr`: Windows has none of the three (`_read_untrusted_text` says why that is safe there).
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(manifest_path_for_slot(wt), flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MANIFEST_READ_CAP:
            return None
        data = json.loads(os.read(fd, _MANIFEST_READ_CAP + 1))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None


def _read_untrusted_text(path: str, cap: int) -> str | None:
    """A small session-user file's text, or None — `read_manifest`'s open rules (no symlink, regular file
    only, non-blocking, bounded), for the one-line git pointer files `checkout_repo_id` reads."""
    # `getattr`: Windows has none of the three (the broker serves Windows runners too); there a symlink or FIFO
    # is not what a session user can plant in a worktree, and the regular-file and size checks still hold.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None
        return os.read(fd, cap + 1).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


_GIT_POINTER_CAP = 4096


def checkout_repo_id(slot_wt: str, named: str) -> int | None:
    """Which repository the checkout holding `named` checks out, judged only among the checkouts of the slot
    at `slot_wt` (`<home>/repos/<pid>/<N>`, the runner's recorded worktree): the credential broker's answer to
    "which of this task's repositories is the caller working in" (S5).

    The LAYOUT answers, never a caller-supplied id and never a clone's `jaah.repoId` (a file the session user can
    rewrite): slot `N` itself is always the PRIMARY's worktree (None — the primary, as before) and
    `_repos/<rid>/<N>` is repository `<rid>`'s. The checkout's git pointer files must still lead to one of this
    project's clones (`<home>/repos/<pid>/main` or `_repos/<x>/main`), else None. Read off the files with
    `_read_untrusted_text`'s rules, never through git in the session user's tree: what a git command there would
    open, or run, is the session user's to choose."""
    if not slot_wt or not named:
        return None
    slot = os.path.realpath(slot_wt)
    n = os.path.basename(slot)
    if not n.isdigit():
        return None
    project_dir = os.path.dirname(slot)
    root = os.path.join(project_dir, NON_PRIMARY_DIRNAME)
    here = os.path.realpath(named)
    if here == slot or here.startswith(slot + os.sep):
        return None
    if not here.startswith(root + os.sep):
        return None
    parts = here[len(root) + 1:].split(os.sep)
    if len(parts) < 2 or not re.fullmatch(r"[0-9]+", parts[0]) or parts[1] != n:
        return None
    checkout = os.path.join(root, parts[0], n)
    pointer = _read_untrusted_text(os.path.join(checkout, ".git"), _GIT_POINTER_CAP)
    match = re.fullmatch(r"gitdir:\s*(.+?)\s*", pointer or "")
    if not match:
        return None
    gitdir = os.path.join(checkout, match.group(1))
    commondir = _read_untrusted_text(os.path.join(gitdir, "commondir"), _GIT_POINTER_CAP)
    common = os.path.realpath(os.path.join(gitdir, commondir.strip()) if commondir else gitdir)
    clone = os.path.dirname(common)
    ours = clone == os.path.join(project_dir, "main") or (
        os.path.basename(clone) == "main" and os.path.dirname(os.path.dirname(clone)) == root
        and os.path.basename(os.path.dirname(clone)).isdigit())
    return int(parts[0]) if os.path.basename(common) == ".git" and ours else None


def _primary(config: PoolConfig) -> RepoSpec:
    return next(spec for spec in config.repos if spec.primary)


# ── Clone identity ─────────────────────────────────────────────────────────────────────────────────────
def is_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))


def _config_get(clone: str, key: str) -> str:
    """One key of `<clone>/.git/config`, read off the file (no repository discovery), or ""."""
    try:
        proc = subprocess.run(
            ["git", "config", "-f", os.path.join(clone, ".git", "config"), "--get", key],
            capture_output=True, text=True, timeout=10, check=False, cwd=os.sep,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _repo_identity(url: str) -> str:
    import pool_origins  # noqa: PLC0415 — `pool_origins` imports this module

    return pool_origins._repo_identity(url)


def clone_repo_id(clone: str) -> int | None:
    """The `project_repos.id` stamped on the clone at `clone`, or None (unstamped, or no clone)."""
    raw = _config_get(clone, REPO_ID_KEY) if is_repo(clone) else ""
    return int(raw) if raw.isdigit() else None


def anchor_repo_id(config: PoolConfig) -> int | None:
    """Which repository the anchor clone holds: its stamp, else the roster repository its `origin` names,
    else the primary (the first converge after deploy adopts an unstamped anchor as the primary's, which is
    what a single-repo project's anchor always was). None when there is no anchor clone yet."""
    if not is_repo(config.clone):
        return None
    stamped = clone_repo_id(config.clone)
    if stamped is not None:
        return stamped
    origin = _repo_identity(_config_get(config.clone, "remote.origin.url"))
    for spec in config.repos:
        if origin and _repo_identity(spec.clone_url) == origin:
            return spec.id
    return _primary(config).id if config.repos else None


def repo_clone(config: PoolConfig, repo_id: int) -> str:
    """Where repository `repo_id`'s objects live on this node: the anchor when it holds that repository (or no
    anchor exists yet and `repo_id` is the primary, which the first clone then creates there), else
    `_repos/<rid>/main`."""
    held = anchor_repo_id(config)
    if held == repo_id or (held is None and config.repos and _primary(config).id == repo_id):
        return config.clone
    return repo_clone_path(config.clone, repo_id)


def primary_clone(config: PoolConfig) -> str:
    """`PoolConfig.source_clone` for a multi-repo project: the clone the primary's slot worktree belongs to."""
    return repo_clone(config, _primary(config).id)


def stamp_anchor(config: PoolConfig) -> None:
    """Write `jaah.repoId` on an unstamped anchor clone (`anchor_repo_id`'s answer). Idempotent; a clone is
    stamped once and keeps it. A failure is logged and changes nothing: the unstamped answer is the same."""
    if not config.repos or not is_repo(config.clone) or clone_repo_id(config.clone) is not None:
        return
    held = anchor_repo_id(config)
    if held is None:
        return
    rc, _o, err = core._git(config.clone, "config", REPO_ID_KEY, str(held))
    if rc != 0:
        log.warning("slot-repos: could not stamp %s with %s=%s: %s", config.clone, REPO_ID_KEY, held, err.strip())


def worktree_clone(wt: str) -> str:
    """The clone a linked worktree belongs to (its git common dir's parent), or "" when unreadable."""
    rc, out, _e = core._git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = out.strip()
    if rc != 0 or not common:
        return ""
    return os.path.dirname(common.rstrip("/"))


def worktree_repo_id(config: PoolConfig, wt: str) -> int | None:
    """Which repository the worktree at `wt` checks out: the anchor's `anchor_repo_id`, else its clone's
    stamp, else the `<rid>` of a `_repos/<rid>/main` clone path; None when it cannot be told."""
    clone = worktree_clone(wt)
    if not clone:
        return None
    if os.path.realpath(clone) == os.path.realpath(config.clone):
        return anchor_repo_id(config)
    stamped = clone_repo_id(clone)
    if stamped is not None:
        return stamped
    rid = os.path.basename(os.path.dirname(clone.rstrip("/")))
    under = os.path.realpath(os.path.dirname(os.path.dirname(clone.rstrip("/"))))
    return int(rid) if rid.isdigit() and under == os.path.realpath(repos_root(config.clone)) else None


def _repo_config(config: PoolConfig, spec: RepoSpec) -> PoolConfig:
    """The mechanics config of one NON-primary repository: its own clone, base and remote, the project's
    prefix, and `repo_id` so its network ops ask for that repository's credential. No port vars: a
    non-primary worktree has no `.env`."""
    # `clone` is the PATH `_repos/<rid>/main`, so `wt_path(N)` is `_repos/<rid>/N` even for a repository the
    # anchor holds (an old primary); `git_clone` is where its objects really are.
    return replace(
        config, clone=repo_clone_path(config.clone, spec.id), git_clone=repo_clone(config, spec.id),
        base=spec.base, clone_url=spec.clone_url, repo_path=spec.repo_path, port_vars=[], repos=(),
        repo_id=spec.id,
    )


# ── Guards (per clone, AS1/N2) ─────────────────────────────────────────────────────────────────────────
def _copy_access_guard(anchor: str, clone: str) -> bool:
    """Give a `_repos/<rid>/main` clone the anchor's access guard: the same pre-push hook and the same
    project-level `branch_prefix`/`push_enabled` (the anchor's markers are what the CP last synced,
    `worktree_ops.sync_access_guard`, which also re-syncs every `_repos` clone). An anchor without markers
    (no guard declared) copies nothing; a clone already carrying the anchor's markers is left alone.

    Returns whether the clone now carries the anchor's guard (True when there is none to carry): a False is a
    clone the session must NOT get — it could push where the project may not (fails closed)."""
    prefix = _config_get(anchor, "access-guard.branch-prefix")
    push = _config_get(anchor, "access-guard.push-enabled")
    if push not in ("true", "false"):
        return True

    def carries() -> bool:
        return (_config_get(clone, "access-guard.push-enabled") == push
                and _config_get(clone, "access-guard.branch-prefix") == prefix)

    if carries():
        return True
    try:
        lifecycle.install_access_guard(clone, prefix, push == "true", installer=_access_guard_installer)
    except Exception as exc:  # noqa: BLE001 — judged by the markers below
        log.warning("slot-repos: could not install the access guard in %s: %s", clone, exc)
    return carries()


def _access_guard_installer():
    """`guards/install-access-guard.py` as a module (`lifecycle.access_guard_installer`)."""
    return lifecycle.access_guard_installer()


# ── One repository's clone and worktree ────────────────────────────────────────────────────────────────
def _one_line(text: str) -> str:
    """A refusal's last stderr line, with any URL userinfo masked and bounded — never a credential."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    line = re.sub(r"(://)[^/\s]*@", r"\1***@", lines[-1] if lines else "")
    return line[:_DETAIL_MAX]


def _failure(spec: RepoSpec, reason: str, detail: str) -> dict:
    if reason in (FAILED_CLONE, FAILED_FETCH) and _CREDENTIAL_REFUSAL.search(detail or ""):
        reason = FAILED_CREDENTIAL
    return {"id": spec.id, "repo_path": spec.repo_path, "reason": reason, "detail": _one_line(detail)}


def _prepare_clone(config: PoolConfig, clone: str, repo_id: int) -> bool:
    """A `_repos/<rid>/main` clone's identity and guard: stamp `jaah.repoId` (once) and copy the anchor's
    access guard; False when the guard did not land. The anchor itself is stamped by `stamp_anchor` and
    guarded by the CP's sync."""
    if os.path.realpath(clone) == os.path.realpath(config.clone) or not is_repo(clone):
        return True
    if clone_repo_id(clone) is None:
        core._git(clone, "config", REPO_ID_KEY, str(repo_id))
    return _copy_access_guard(config.clone, clone)


def _ensure_primary_clone(config: PoolConfig) -> None:
    """A primary living off the anchor (`_repos/<rid>/main`) is cloned HERE, at session start, so it is stamped
    and guarded before `claim_mechanics` builds the slot from it. Raises `ResetRefused` (the primary)."""
    primary = _primary(config)
    clone = repo_clone(config, primary.id)
    if clone == config.clone:
        return
    lifecycle.ensure_clone(replace(config, git_clone=clone))
    if not _prepare_clone(config, clone, primary.id):
        raise ResetRefused(f"guard-failed: the access guard could not be installed in {clone}")


def _ensure_repo_clone(config: PoolConfig, spec: RepoSpec) -> tuple[PoolConfig, str]:
    """Clone (when absent), stamp, guard and fetch one non-primary repository. `(its config, "")`, or
    `(its config, <failure detail>)` when nothing can be checked out."""
    cfg = _repo_config(config, spec)
    clone = cfg.source_clone
    try:
        lifecycle.ensure_clone(cfg)
    except ResetRefused as exc:
        return cfg, str(exc)
    if not _prepare_clone(config, clone, spec.id):
        return cfg, "guard-failed: the access guard could not be installed in this repository's clone"
    try:
        # From the repository's own mirror (`<pid>-<rid>.git`, `worktree_mechanics.core.mirror_key`).
        rc, _o, err = core._fetch_from_mirror(cfg, clone, spec.base)
    except ResetRefused as exc:
        rc, err = 1, str(exc)
    if rc != 0:
        # A stale local base still checks out (as the primary's own reset does); only nothing at all fails.
        if core._git(clone, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{spec.base}")[0]:
            return cfg, f"fetch-failed: {err}"
        log.warning("slot-repos: fetch of origin/%s in %s failed — using the local ref: %s",
                    spec.base, clone, _one_line(err))
    return cfg, ""


def _fleet_branch(config: PoolConfig, task_id: object) -> str:
    """`{branch_prefix}fleet/task-<id>` — the spelling `claim_mechanics` derives for the primary."""
    return f"{config.branch_prefix}fleet/task-{task_id}" if task_id is not None and str(task_id).strip() else ""


def _materialise(cfg: PoolConfig, n: int | str, env: "SlotEnv") -> str:
    """Slot N's tree for `cfg`, through the one materialisation primitive (`create_worktree`): the primary's
    with the CP's band, a non-primary's (`cfg.repo_id`) detached at its base with nothing else."""
    band = env.band
    return lifecycle.create_worktree(cfg, n, None, env.node_id, band=band, auto_ports=env.auto_ports)


def _add_worktree(cfg: PoolConfig, spec: RepoSpec, n: int | str, *, mode: str, branch: str) -> str:
    """Slot N's worktree of a non-primary repository, detached at `origin/<base>`, then the session's branch:
    a fresh claim cuts `branch` from the repository's own base (AC3.1), a resume lands the task's existing
    branch, a console stays detached. "" on success, else the refusal."""
    try:
        wt = _materialise(cfg, n, SlotEnv())
    except ResetRefused as exc:
        return str(exc)
    return _land_branch(wt, spec.base, mode=mode, branch=branch)


def _land_branch(wt: str, base: str, *, mode: str, branch: str) -> str:
    """Put a freshly materialised tree on the session's branch: a claim cuts `branch` from `base` (a local
    `branch` a previous attempt left is backed up under `refs/pool-unpushed/` first, as the primary's reset
    does), a resume lands the task's own branch (local, else pushed, else base), a console stays detached.
    "" on success, else the refusal."""
    if not branch or mode == MODE_CONSOLE:
        return ""
    if mode == MODE_CLAIM:
        try:
            reset._backup_unpushed_commits(wt, branch)
        except ResetRefused as exc:
            return str(exc)
        rc, _o, err = core._git(wt, "checkout", "-q", "-B", branch)
        return f"could not create {branch}: {err}" if rc != 0 else ""
    resume_ref = f"origin/{base}"
    for candidate in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if core._git(wt, "rev-parse", "-q", "--verify", candidate)[0] == 0:
            resume_ref = candidate
            break
    return "" if reset._checkout_reset_branch(wt, branch, resume_ref, base) else f"could not land {branch}"


def _reset_for_claim(cfg: PoolConfig, wt: str, branch: str) -> str:
    """A fresh claim of an EXISTING non-primary worktree: `reset_to_base` against its own base (every gate of
    the primary's reset applies), then the task's branch. "" on success, else the refusal."""
    try:
        reset.reset_to_base(cfg, wt, None, detach_base=True, own_branch=branch or None)
    except ResetRefused as exc:
        return str(exc)
    if branch:
        rc, _o, err = core._git(wt, "checkout", "-q", "-B", branch)
        if rc != 0:
            return f"could not create {branch}: {err}"
    return ""


# ── Removal (AS6) ──────────────────────────────────────────────────────────────────────────────────────
def unsaved_work(wt: str) -> str:
    """Why the worktree at `wt` may NOT be removed — `uncommitted`, `unpushed`, `unverified` — or "" when
    it is clean and every commit of HEAD is on some `origin/*` ref (the release rule). Fails closed."""
    rc, out, _e = core._git(wt, "status", "--porcelain", "--untracked-files=normal")
    if rc != 0:
        return KEPT_UNVERIFIED
    if out.strip():
        return KEPT_UNCOMMITTED
    rc, out, _e = core._git(wt, "rev-list", "HEAD", "--not", "--remotes=origin")
    if rc != 0:
        return KEPT_UNVERIFIED
    return KEPT_UNPUSHED if out.strip() else ""


def _slot_worktrees_under(anchor: str, n: int | str) -> dict[int, str]:
    """`{repo id: path}` of every `_repos/<rid>/<N>` directory on disk for slot N (no symlink followed)."""
    out: dict[int, str] = {}
    for repo_dir in ports._child_dirs(repos_root(anchor)):
        name = os.path.basename(repo_dir)
        wt = os.path.join(repo_dir, str(n))
        if name.isdigit() and os.path.isdir(wt) and not os.path.islink(wt):
            out[int(name)] = wt
    return out


def _remove_or_keep(rid: int, wt: str, repo_path: str = "") -> dict | None:
    """Remove a worktree whose repository left the project, or return its `kept` entry (AS6). `git worktree
    remove` WITHOUT `--force`, so git itself refuses anything not clean — never `rm -rf`."""
    reason = unsaved_work(wt)
    if not reason:
        clone = worktree_clone(wt)
        rc, _o, err = core._git(clone or wt, "worktree", "remove", wt) if clone else (1, "", "")
        if rc == 0:
            log.info("slot-repos: removed %s (repository %s left the project, nothing unsaved)", wt, rid)
            return None
        log.warning("slot-repos: kept %s — git worktree remove refused: %s", wt, _one_line(err))
        reason = KEPT_UNCOMMITTED
    if not repo_path:
        origin = _config_get(worktree_clone(wt) or wt, "remote.origin.url")
        repo_path = _repo_identity(origin) if origin else ""
    log.warning("slot-repos: kept %s (repository %s left the project) — it holds %s work", wt, rid, reason)
    return {"id": rid, "path": wt, "repo_path": repo_path, "reason": reason}


# ── Primary swap (lossless) ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SlotEnv:
    """What a primary arriving at the slot path needs to become the slot's runnable tree: the CP's band and
    environment rows, the tier-2 range, this node's id, and the project's stack-down command for the
    primary leaving it. Every field is the caller's (the lease intent or the roster), never invented here."""

    band: dict | None = None
    env_vars: dict | None = None
    auto_ports: tuple[int, int] | None = None
    node_id: str | None = None
    stack_down_cmd: str = ""


def _git_move(clone: str, src: str, dst: str) -> str:
    rc, _o, err = core._git(clone, "worktree", "move", src, dst)
    return "" if rc == 0 else (err or f"git worktree move exited {rc}").strip()


def _swap_primary(
    config: PoolConfig, n: int | str, wt: str, old_id: int, env: SlotEnv, *, mode: str, branch: str,
) -> None:
    """Move the old primary's worktree out of the slot path to `_repos/<old>/N` and the new primary's in.

    Lossless for a held slot: untracked and uncommitted work moves with each tree, and only this slot's
    worktrees move — no clone, `origin` or other slot changes. The old primary's stack is stopped (volumes
    kept) and its managed environment withdrawn first, since its ports are the slot's band. A failure
    puts the old primary back and raises `ResetRefused` (a failed primary refuses the session)."""
    primary = _primary(config)
    old_clone = worktree_clone(wt)
    parked = repo_worktree_path(config.clone, old_id, n)
    if not old_clone:
        raise ResetRefused(f"primary-swap: cannot tell which clone {wt} belongs to")
    if os.path.exists(parked):
        raise ResetRefused(f"primary-swap: {parked} already exists — refusing to move {wt} onto it")
    if ports.has_stack(wt):
        import stack_ops  # noqa: PLC0415 — subprocess-heavy sibling, needed only on a swap

        if stack_ops.stop_stack(wt, env.stack_down_cmd) != "down":
            log.warning("primary-swap: the stack in %s did not come down — moving the tree anyway", wt)
    managed_env._withdraw_managed_env(wt)
    err = session_user.ensure_dir_as_session_user(os.path.dirname(parked))
    if err:
        raise ResetRefused(f"primary-swap: cannot create {os.path.dirname(parked)}: {err}")
    err = _git_move(old_clone, wt, parked)
    if err:
        raise ResetRefused(f"primary-swap: could not move {wt} to {parked}: {err}")
    incoming = repo_worktree_path(config.clone, primary.id, n)
    try:
        if os.path.isdir(incoming):
            err = _git_move(worktree_clone(incoming) or primary_clone(config), incoming, wt)
            if err:
                raise ResetRefused(f"primary-swap: could not move {incoming} to {wt}: {err}")
        else:
            _materialise(config, n, env)
            # A resume lands the task's branch in the primary it never had here (a claim's own reset follows).
            if mode == MODE_RESUME:
                err = _land_branch(wt, primary.base, mode=mode, branch=branch)
                if err:
                    log.warning("primary-swap: %s stays detached: %s", wt, _one_line(err))
    except Exception:
        if not os.path.exists(wt):
            back = _git_move(old_clone, parked, wt)
            log.error("primary-swap: restoring %s to %s %s", parked, wt, "failed: " + back if back else "succeeded")
        raise
    try:
        _arm_primary(config, n, wt, env)
    except ResetRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — a primary that cannot be armed refuses the session, as a claim does
        raise ResetRefused(f"primary-swap: could not set up {wt}'s environment: {exc}") from exc
    log.warning("primary-swap: %s #%s now holds repository %s (repository %s parked at %s)",
                config.name, n, primary.id, old_id, parked)


def _arm_primary(config: PoolConfig, n: int | str, wt: str, env: SlotEnv) -> None:
    """What `create_worktree` does for a slot's tree, for a primary that MOVED in: seed `.env`, harden modes,
    then the managed environment, band, stack and node identity (all idempotent; a claim re-applies them)."""
    env_file, example = os.path.join(wt, ".env"), os.path.join(wt, ".env.example")
    if not os.path.isfile(env_file) and os.path.isfile(example):
        lifecycle._copy(example, env_file)
    wm_env_file.harden_worktree_modes(wt)
    managed_env.apply_managed_env(wt, env.env_vars, n=n, port_vars=config.port_vars, band=env.band)
    ports.apply_port_band(wt, env.band, port_vars=config.port_vars, auto_ports=env.auto_ports)
    ports.apply_stack_identity(wt, config.repo, n)
    ports.apply_node_identity_env(wt, n, env.node_id)


# ── Converge ───────────────────────────────────────────────────────────────────────────────────────────
def needs_converge(config: PoolConfig) -> bool:
    """False for a project this module leaves exactly as today: no repository, an older CP, or one
    repository that is the anchor's with no `_repos` ever made (AC2.1)."""
    if not config.has_repo or not config.repos:
        return False
    if len(config.repos) > 1 or os.path.isdir(repos_root(config.clone)):
        return True
    held = anchor_repo_id(config)
    return held is not None and held != _primary(config).id


def converge(
    config: PoolConfig, n: int | str, *, mode: str, task_id: object = None, env: SlotEnv | None = None,
) -> dict | None:
    """Bring slot N to the roster's repository set, at a session's start; returns the manifest written, or
    None when the project needs none (`needs_converge`).

    1. a primary change: `_swap_primary` (raises `ResetRefused` — the session is refused, as today);
    2. every non-primary repository: cloned, fetched and checked out at `_repos/<rid>/N` — a fresh claim on
       `<prefix>fleet/task-<id>` cut from its own base, a resume on the task's branch; an existing worktree
       is reset on a claim and left exactly as it is otherwise;
    3. a worktree whose repository left the project: removed when nothing in it is unsaved, else kept;
    4. the manifest (`_repos/slot-<N>.json`).
    The primary's own tree (reset, branch, `.env`, ports, stack) stays `claim_mechanics`'."""
    if mode not in MODES:
        raise ValueError(f"converge mode {mode!r}")
    refusal = legacy_refusal(config, n)
    if refusal:
        raise ResetRefused(refusal)
    if not needs_converge(config):
        return None
    # One converge per project at a time: two slots' session starts (a dispatch and a console) would race
    # on a shared clone's creation and `.git/config`.
    with _project_lock(config.clone):
        return _converge_locked(config, n, mode=mode, task_id=task_id, env=env or SlotEnv())


def legacy_refusal(config: PoolConfig, n: int | str) -> str:
    """Why a slot that IS multi-repo may not start a session on a roster that names no repositories (an
    older CP, an unusable list), or "": today's single-repo path would build or fetch its primary from the
    anchor, which may hold another repository since a swap."""
    if not config.has_repo or config.repos or not os.path.isdir(repos_root(config.clone)):
        return ""
    wt = config.wt_path(n)
    stamped = clone_repo_id(config.clone)
    owner = worktree_clone(wt) if os.path.isdir(wt) else ""
    if owner and os.path.realpath(owner) != os.path.realpath(config.clone):
        return (f"repos-unlisted: {wt} holds a repository other than the anchor's, but the roster lists no "
                f"repositories for {config.name} — refusing rather than treating the anchor as its primary")
    if stamped is not None and not owner:
        return (f"repos-unlisted: {config.name} is a multi-repo project (anchor stamped {stamped}) but the roster "
                f"lists no repositories — refusing to build {wt} from a guess")
    return ""


_PROJECT_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


def _project_lock(anchor: str) -> threading.Lock:
    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(os.path.realpath(anchor), threading.Lock())


def _converge_locked(config: PoolConfig, n: int | str, *, mode: str, task_id: object, env: SlotEnv) -> dict:
    stamp_anchor(config)
    primary = _primary(config)
    _ensure_primary_clone(config)
    wt = config.wt_path(n)
    branch = _fleet_branch(config, task_id) if mode != MODE_CONSOLE else ""
    incoming = repo_worktree_path(config.clone, primary.id, n)
    if os.path.isdir(wt):
        current = worktree_repo_id(config, wt)
        if current is not None and current != primary.id:
            _swap_primary(config, n, wt, current, env, mode=mode, branch=branch)
    elif os.path.isdir(incoming):
        # The slot path is gone but the primary's tree is parked: it moves back in rather than being rebuilt
        # beside it (and later read as a repository that left the project).
        err = _git_move(worktree_clone(incoming) or primary_clone(config), incoming, wt)
        if err:
            raise ResetRefused(f"primary-swap: could not move {incoming} to {wt}: {err}")
        _arm_primary(config, n, wt, env)

    present: list[dict] = [{"id": primary.id, "path": wt, "repo_path": primary.repo_path,
                            "base": primary.base, "primary": True}]
    failed: list[dict] = []
    for spec in config.repos:
        if spec.primary:
            continue
        rwt = repo_worktree_path(config.clone, spec.id, n)
        try:
            entry, failure = _converge_one(config, spec, n, rwt, mode=mode, branch=branch)
        except Exception as exc:  # noqa: BLE001 — one repository never stops the session (failure isolation)
            log.exception("slot-repos: converging %s into %s failed", spec.repo_path, rwt)
            entry = None if not os.path.isdir(rwt) else _present(spec, rwt)
            failure = _failure(spec, FAILED_RESET if entry else FAILED_CLONE, f"{type(exc).__name__}: {exc}")
        if failure:
            failed.append(failure)
        if entry:
            present.append(entry)

    wanted = {spec.id for spec in config.repos if not spec.primary}
    # A repository that left the project is named as the slot last knew it (its previous manifest), else by
    # its clone's origin.
    previous = read_manifest(wt) or {}
    known = {r.get("id"): str(r.get("repo_path") or "") for key in ("repos", "kept")
             for r in previous.get(key) or () if isinstance(r, dict)}
    kept = [entry for rid, path in sorted(_slot_worktrees_under(config.clone, n).items())
            if rid not in wanted and rid != primary.id
            and (entry := _remove_or_keep(rid, path, known.get(rid, ""))) is not None]
    try:
        slot_number = int(n)
    except (TypeError, ValueError):
        slot_number = n
    manifest = {"slot": slot_number, "primary_repo_id": primary.id, "repos": present, "kept": kept,
                "failed": failed}
    _write_manifest(manifest_path(config.clone, n), manifest)
    return manifest


def _present(spec: RepoSpec, rwt: str) -> dict:
    return {"id": spec.id, "path": rwt, "repo_path": spec.repo_path, "base": spec.base, "primary": False}


def _converge_one(
    config: PoolConfig, spec: RepoSpec, n: int | str, rwt: str, *, mode: str, branch: str,
) -> tuple[dict | None, dict | None]:
    """One non-primary repository of slot N: `(its repos entry or None, its failed entry or None)`."""
    cfg, err = _ensure_repo_clone(config, spec)
    if err:
        reason = (FAILED_FETCH if err.startswith("fetch-failed")
                  else FAILED_GUARD if err.startswith("guard-failed") else FAILED_CLONE)
        return None, _failure(spec, reason, err)
    if not os.path.isdir(rwt):
        err = _add_worktree(cfg, spec, n, mode=mode, branch=branch)
    elif mode == MODE_CLAIM:
        err = _reset_for_claim(cfg, rwt, branch)
    exists = os.path.isdir(rwt)
    failure = _failure(spec, FAILED_RESET if exists else FAILED_CLONE, err) if err else None
    return (_present(spec, rwt) if exists else None), failure


def release_slot_repos(anchor: str, n: int | str) -> None:
    """A slot's ERASE (`worktree_ops.worktree_erase`): every `_repos/<rid>/N` goes, whatever it holds (the
    erase never refuses), then the manifest, and every `_repos` clone is pruned of the registrations."""
    import slot_residue  # noqa: PLC0415 — deferred: slot_residue → slot_destroy → commands is a cycle

    for _rid, path in sorted(_slot_worktrees_under(anchor, n).items()):
        _down_sub_stack(anchor, path, n)
        clone = worktree_clone(path)
        if not clone or core._git(clone, "worktree", "remove", "--force", "--force", path)[0] != 0:
            err = slot_residue.remove_tree(path)
            if err:
                log.warning("slot-repos: could not remove %s: %s", path, err)
        slot_residue.purge_scratch(path)
    try:
        os.unlink(manifest_path(anchor, n))
    except OSError:
        pass
    for clone in _repo_clone_dirs(anchor):
        core._git(clone, "worktree", "prune", "--expire=now")


def _down_sub_stack(anchor: str, path: str, n: int | str) -> None:
    """Take a sub-tree's compose stack down with its volumes (`slot_destroy.compose_down`), from the anchor so
    nothing in the tree configures the down. Its `.env` names it: only a name ending `-<n>` that no pool
    slot's `.env` names (`disk_reclaim.live_projects`) is taken, so a planted `.env` cannot reach another
    slot's stack."""
    import env_helpers  # noqa: PLC0415

    if sys.platform != "win32":  # `disk_reclaim` imports `pwd`: fenced for the platform import-smoke
        import disk_reclaim  # noqa: PLC0415 — deferred: only the erase reads it
    else:
        return  # no pool stack runs on a Windows node
    import slot_destroy  # noqa: PLC0415 — deferred: slot_destroy → commands is a cycle

    try:
        with open(os.path.join(path, ".env"), encoding="utf-8", errors="replace") as fh:
            name = env_helpers.compose_project_name(fh.read())
    except OSError:
        return
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name) or not name.endswith(f"-{n}"):
        return
    live = disk_reclaim.live_projects(env_helpers.jaah_home())
    if live is None or name in live:
        return
    ok, why = slot_destroy.compose_down(name, anchor)
    if not ok:
        log.warning("slot-repos: %s — left for the residue sweep", why)


def _repo_clone_dirs(anchor: str) -> list[str]:
    """Every `_repos/<rid>/main` clone beside the anchor."""
    return [os.path.join(d, "main") for d in ports._child_dirs(repos_root(anchor))
            if os.path.basename(d).isdigit() and is_repo(os.path.join(d, "main"))]


def _write_manifest(path: str, manifest: dict) -> None:
    """Write the manifest, as the session user where the uid split applies (the directory is `jaah`'s).
    A failure is logged: the session still starts, and the drawer then shows no repositories."""
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if session_user.switch_needed():
        err = session_user.ensure_dir_as_session_user(os.path.dirname(path)) or (
            session_user.write_file_as_session_user(path, text, mode=0o644))
        if err:
            log.warning("slot-repos: could not write %s: %s", path, err)
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("slot-repos: could not write %s: %s", path, exc)


def extra_dirs(manifest: dict | None) -> tuple[str, ...]:
    """Every non-primary path a session in the slot works in (`permissions.additionalDirectories`)."""
    if not manifest:
        return ()
    return tuple(str(r["path"]) for r in manifest.get("repos") or ()
                 if isinstance(r, dict) and not r.get("primary") and r.get("path"))


def observed(manifest: dict | None) -> dict | None:
    """The manifest as the observation's `repos` field carries it to the CP (the drawer's rows)."""
    if not manifest:
        return None
    return {key: manifest.get(key) for key in ("primary_repo_id", "repos", "kept", "failed")}


def converge_for_console(project: str, number: object, node_id: str) -> dict | None:
    """The console's session-start converge: a console about to OPEN A NEW SESSION in a slot that is free or
    held by a user converges it first, from a fresh roster read (`converge`, mode `console`). A task-held slot
    is left to its task's next session start; slot 0, a folder or an unreadable roster converge nothing.

    Never raises: the console opens either way (a refused swap has already put the old primary back)."""
    import control_plane_api  # noqa: PLC0415 — the CP read, only on a console create
    from reconcile import tick_wire  # noqa: PLC0415 — `reconcile` imports this module

    if str(number) == "0":
        return None
    try:
        tick = tick_wire.parse_pool_tick(control_plane_api.agent_api_pool_tick(node_id or ""))
        slot = next((s for s in tick.slots if s.project == project and s.number == str(number)), None)
        if not tick.ok or slot is None or not slot.has_repo:
            return None
        if slot.state not in (tick_wire.SLOT_FREE, tick_wire.SLOT_USER):
            return None
        env = SlotEnv(band=dict(slot.ports), auto_ports=tick.auto_ports, node_id=node_id or None,
                      stack_down_cmd=slot.stack_down_cmd)
        return converge(slot.pool_config(), slot.number, mode=MODE_CONSOLE, env=env)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("slot-repos: console converge of %s #%s failed — opening without it: %s", project, number, exc)
        return None
