"""Multi-repository slots: every repository of a project, checked out side by side in one slot, and
the converge that brings a slot to the project's current repository set at the START of a session.

LAYOUT (`apps/api/app/node_clone.py` mirrors it for the CP):

    <home>/repos/<pid>/<N>                   slot N — ALWAYS the PRIMARY repository, a standalone clone
    <home>/repos/<pid>/_repos/<rid>/<N>      slot N's standalone clone of non-primary repository <rid>
    <home>/repos/<pid>/_repos/slot-<N>.json  slot N's manifest (what the last converge left there)

Every tree is its own `git clone` straight from the forge with the leasing task's credential (`slot_clone`):
no anchor clone and no `git worktree`. A `main` anchor or `_repos/<rid>/main` clone an older node left is
ignored, never written. Each clone carries `git config jaah.repoId` naming its repository. A primary change
RENAMES the slot's trees (`slot_clone.move`), so a held slot's uncommitted work travels with its tree and no
other slot of the project is touched. `_repos` is not a slot name: every slot scan reads only digit names.

WHEN: only at the start of a session in the slot — a fresh dispatch (`claim`), a resume (`resume`) or a
console that opens a new session (`console`) — never from the reconcile pass, so nothing changes under a
running session (N8). The primary alone gets the gate, stack, ports and `.env` (AC3.5): a non-primary
tree gets none of them. One non-primary repository failing (a refused credential, a failed clone) is
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
from pathlib import Path

import env_helpers
import file_io
import session_user
import slot_clone
from clone_identity import Acting
from worktree_mechanics import core, env_file as wm_env_file, ports, managed_env, lifecycle
from worktree_mechanics.core import PoolConfig, ResetRefused

log = logging.getLogger("jaahd.slot_repos")

NON_PRIMARY_DIRNAME = "_repos"
# The clone's own identity: which `project_repos.id` it holds. Written on every clone the converge makes or
# refreshes (`_guard_and_stamp`); read by `clone_repo_id`.
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
    r"\b(401|403)\b|permission denied \(publickey|access denied|not authorized",
    re.IGNORECASE,
)
_DETAIL_MAX = 300

# Git's refname rules, mirrored only to keep a malformed value out of the access-guard installer's argv; the
# installer is the authority.
BAD_REF_CHARS = re.compile(r"[\s~^:?*\[\\\x00-\x1f]")


def legal_ref_prefix(prefix: str) -> bool:
    """Whether a branch prefix may reach the installer's argv (`worktree_ops.sync_access_guard`, `repos_from_wire`)."""
    return not (BAD_REF_CHARS.search(prefix) or ".." in prefix or prefix.startswith("/"))


@dataclass(frozen=True)
class RepoSpec:
    """One repository of the project, as the roster's `repos[]` carries it (S4). The last three are its own Branch
    policy; None from a CP older than them, where the project's values apply."""

    id: int
    clone_url: str
    repo_path: str
    forge: str
    base: str
    primary: bool
    branch_prefix: str | None = None
    push_enabled: bool | None = None
    allow_force_push: bool | None = None


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
        # Branch policy: absent is None (the project's); present but not a legal prefix or a bool drops the row,
        # never guessed.
        prefix, push, force = row.get("branch_prefix"), row.get("push_enabled"), row.get("allow_force_push")
        if ((prefix is not None and (not isinstance(prefix, str) or not legal_ref_prefix(prefix)))
                or any(v is not None and not isinstance(v, bool) for v in (push, force))):
            log.warning("slot-repos: roster repos row carries an unreadable Branch policy, dropped: %r", row)
            continue
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        out.append(RepoSpec(
            id=rid, clone_url=clone_url, repo_path=str(row.get("repo_path") or ""),
            forge=str(row.get("forge") or ""), base=base, primary=row.get("primary") is True,
            branch_prefix=prefix, push_enabled=push, allow_force_push=force,
        ))
    if sum(1 for spec in out if spec.primary) != 1 or len({spec.id for spec in out}) != len(out):
        log.warning("slot-repos: roster repos list has no single primary or repeats an id — ignored: %r", raw)
        return ()
    return tuple(out)


# ── Paths ──────────────────────────────────────────────────────────────────────────────────────────────
def repos_root(clone: str) -> str:
    """`<home>/repos/<pid>/_repos` for the project path `<home>/repos/<pid>/main` (the slot-0 path the roster names)."""
    return posixpath.join(posixpath.dirname(clone.rstrip("/")), NON_PRIMARY_DIRNAME)


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
    """The manifest the last converge wrote for the slot at `wt`, or None (none, unreadable, not a JSON object).
    Read like any session-user file the daemon opens (`file_io.read_bounded`): a planted `/dev/zero` or FIFO
    must not stall or exhaust the daemon."""
    try:
        data = json.loads(file_io.read_bounded(manifest_path_for_slot(wt), _MANIFEST_READ_CAP))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_untrusted_text(path: str, cap: int) -> str:
    """A small session-user file's text, "" when there is none to read — `read_manifest`'s open rules, for the
    one-line git pointer files `checkout_repo_id` reads."""
    return file_io.read_bounded(path, cap).decode("utf-8", "replace")


def checkout_repo_id(slot_wt: str, named: str) -> int | None:
    """Which repository the checkout holding `named` checks out, judged only among the checkouts of the slot
    at `slot_wt` (`<home>/repos/<pid>/<N>`, the runner's recorded worktree): the credential broker's answer to
    "which of this task's repositories is the caller working in" (S5).

    The LAYOUT answers, never a caller-supplied id and never a clone's `jaah.repoId` (a file the session user can
    rewrite): slot `N` itself is always the PRIMARY (None — the primary, as before) and `_repos/<rid>/<N>` is
    repository `<rid>`'s when it holds a checkout there: its `.git` a real directory (a standalone clone) or a regular
    file (a linked worktree of the retired layout, still held until its next lease), read by `lstat` and never
    followed, and never through git in the session user's tree: what a git command there would open, or run, is
    theirs to choose."""
    if not slot_wt or not named:
        return None
    slot = os.path.realpath(slot_wt)
    n = os.path.basename(slot)
    if not n.isdigit():
        return None
    root = os.path.join(os.path.dirname(slot), NON_PRIMARY_DIRNAME)
    here = os.path.realpath(named)
    if here == slot or here.startswith(slot + os.sep):
        return None
    if not here.startswith(root + os.sep):
        return None
    parts = here[len(root) + 1:].split(os.sep)
    if len(parts) < 2 or not re.fullmatch(r"[0-9]+", parts[0]) or parts[1] != n:
        return None
    try:
        info = os.lstat(os.path.join(root, parts[0], n, ".git"))
    except OSError:
        return None
    return int(parts[0]) if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) else None


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
    """The host-relative repo path (`owner/repo`, `grp/sub/repo`, `org/proj/_git/repo`), lower-cased, off
    any spelling of a remote (`https://host/…`, `ssh://git@host/…`, `git@host:…`, a host alias, `.git`)."""
    value = url.strip()
    for prefix in ("https://", "http://", "ssh://"):
        if value.startswith(prefix):
            value = value[len(prefix):].split("/", 1)[-1]
    if "@" in value.split("/", 1)[0] and ":" in value:
        value = value.split(":", 1)[1]
    if value.endswith(".git"):
        value = value[:-4]
    return value.strip("/").lower()


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


def worktree_clone(wt: str) -> str:
    """The repository a tree's git belongs to (its git common dir's parent: the tree itself for a standalone
    clone, the owning clone for a legacy linked worktree), or "" when unreadable."""
    rc, out, _e = core._git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = out.strip()
    if rc != 0 or not common:
        return ""
    return os.path.dirname(common.rstrip("/"))


def worktree_repo_id(config: PoolConfig, wt: str) -> int | None:
    """Which repository the tree at `wt` holds: a standalone clone's `jaah.repoId` stamp, else the roster
    repository its `origin` names; a legacy linked worktree by its owning clone (the anchor's
    `anchor_repo_id`, a clone's stamp, the `<rid>` of `_repos/<rid>/main`); None when it cannot be told."""
    if slot_clone.is_standalone(wt):
        stamped = clone_repo_id(wt)
        if stamped is not None:
            return stamped
        origin = _repo_identity(_config_get(wt, "remote.origin.url"))
        return next((spec.id for spec in config.repos if origin and _repo_identity(spec.clone_url) == origin), None)
    if not slot_clone.is_linked_worktree(wt):
        return None
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


# ── Guards (per clone, AS1/N2) ─────────────────────────────────────────────────────────────────────────
def _guard_and_stamp(config: PoolConfig, spec: RepoSpec, tree: str) -> bool:
    """Stamp `jaah.repoId` and (re)install the repository's access guard in a clone the converge just made or
    refreshed, always with the CURRENT values: the roster row's own Branch policy, else the project's (Allow
    force push off), integration branch = its base. Returns whether the markers landed: a False is a clone the
    session must NOT get — it could push where the project may not (fails closed)."""
    core._git(tree, "config", REPO_ID_KEY, str(spec.id))
    prefix = config.branch_prefix if spec.branch_prefix is None else spec.branch_prefix
    push = config.push_enabled if spec.push_enabled is None else spec.push_enabled
    force = spec.allow_force_push is True
    try:
        lifecycle.install_access_guard(tree, prefix, push, spec.base, installer=_access_guard_installer,
                                       allow_force_push=force, jaah_repo_prefix=config.jaah_repo_prefix)
    except Exception as exc:  # noqa: BLE001 — judged by the markers below
        log.warning("slot-repos: could not install the access guard in %s: %s", tree, exc)
    return (_config_get(tree, "access-guard.push-enabled") == ("true" if push else "false")
            and _config_get(tree, "access-guard.branch-prefix") == prefix
            and (_config_get(tree, "access-guard.allow-force-push") == "true") == force
            and _config_get(tree, "access-guard.integration-branch") == spec.base)


def _access_guard_installer():
    """`guards/install-access-guard.py` as a module (`lifecycle.access_guard_installer`)."""
    return lifecycle.access_guard_installer()


# ── One repository's clone ─────────────────────────────────────────────────────────────────────────────
def _one_line(text: str) -> str:
    """A refusal's last stderr line, with any URL userinfo masked and bounded — never a credential."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    line = re.sub(r"(://)[^/\s]*@", r"\1***@", lines[-1] if lines else "")
    return line[:_DETAIL_MAX]


def _failure(spec: RepoSpec, reason: str, detail: str) -> dict:
    if reason in (FAILED_CLONE, FAILED_FETCH) and _CREDENTIAL_REFUSAL.search(detail or ""):
        reason = FAILED_CREDENTIAL
    return {"id": spec.id, "repo_path": spec.repo_path, "reason": reason, "detail": _one_line(detail)}


def _fleet_branch(config: PoolConfig, task_id: object) -> str:
    """`core.task_branch` — the spelling `claim_mechanics` derives for the primary."""
    return core.task_branch(config.branch_prefix, task_id) if task_id is not None and str(task_id).strip() else ""


def _push_enabled(config: PoolConfig, spec: RepoSpec) -> bool:
    """The repository's effective push flag: its own row's, else the project's."""
    return config.push_enabled if spec.push_enabled is None else spec.push_enabled


class _GuardFailed(ResetRefused):
    """A clone erased because its access guard did not land (`FAILED_GUARD`)."""


def _clone_tree(config: PoolConfig, spec: RepoSpec, tree: str, acting: Acting, branch: str) -> None:
    """Clone `spec` into the absent `tree` for `acting` (its own repository's credential), land `branch` and
    guard it; a clone whose guard does not land is erased. Raises `CloneFailed` or `ResetRefused`."""
    slot_clone.clone(tree, spec.clone_url, spec.base, replace(acting, repo_id=None if spec.primary else spec.id),
                     push_missing_base=_push_enabled(config, spec))
    try:
        slot_clone.land(tree, spec.base, branch)
    except BaseException:
        slot_clone.erase(tree)
        raise
    slot_clone.hide_harness_scratch(tree)
    _require_guard(config, spec, tree)


def _require_guard(config: PoolConfig, spec: RepoSpec, tree: str) -> None:
    if not _guard_and_stamp(config, spec, tree):
        slot_clone.erase(tree)
        raise _GuardFailed(f"guard-failed: the access guard could not be installed in {tree} — withheld")


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
    """Remove a tree whose repository left the project, or return its `kept` entry (AS6): erased only when
    `unsaved_work` finds it clean and fully pushed."""
    reason = unsaved_work(wt)
    if not reason:
        try:
            slot_clone.erase(wt)
            log.info("slot-repos: removed %s (repository %s left the project, nothing unsaved)", wt, rid)
            return None
        except ResetRefused as exc:
            log.warning("slot-repos: kept %s — could not remove it: %s", wt, _one_line(str(exc)))
            reason = KEPT_UNVERIFIED
    if not repo_path:
        origin = _config_get(worktree_clone(wt) or wt, "remote.origin.url")
        repo_path = _repo_identity(origin) if origin else ""
    log.warning("slot-repos: kept %s (repository %s left the project) — it holds %s work", wt, rid, reason)
    return {"id": rid, "path": wt, "repo_path": repo_path, "reason": reason}


# ── Primary swap (lossless) ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SlotEnv:
    """What a primary arriving at the slot path needs to become the slot's runnable tree: the CP's
    environment rows, the node-assigned port range, this node's id, and the project's stack-down command for the
    primary leaving it. Every field is the caller's (the lease intent or the roster), never invented here."""

    env_vars: dict | None = None
    auto_ports: tuple[int, int] | None = None
    node_id: str | None = None
    stack_down_cmd: str = ""


def _swap_primary(
    config: PoolConfig, n: int | str, wt: str, old_id: int, env: SlotEnv, *, mode: str, branch: str,
    acting: Acting | None,
) -> None:
    """Rename the old primary's tree out of the slot path to `_repos/<old>/N` and the new primary's in.

    Lossless for a held slot: untracked and uncommitted work moves with each tree, and only this slot's trees
    move. The old primary's stack is stopped (volumes kept) and its managed environment withdrawn first, since
    its ports are the slot's. With no `_repos/<new>/N` to move in, a claim leaves the slot path absent
    (`claim_mechanics` clones the primary next) and a resume clones it here for its task; a console, which no
    task acts for, is refused before anything moves. A failure puts the old primary back and raises
    `ResetRefused` (a failed primary refuses the session)."""
    primary = _primary(config)
    parked = repo_worktree_path(config.clone, old_id, n)
    incoming = repo_worktree_path(config.clone, primary.id, n)
    if os.path.lexists(parked):
        raise ResetRefused(f"primary-swap: {parked} already exists — refusing to move {wt} onto it")
    if slot_clone.is_linked_worktree(incoming):
        slot_clone.erase(incoming)   # the retired layout: never moved, never refreshed
    moving_in = slot_clone.is_standalone(incoming)
    if not moving_in and mode != MODE_CLAIM and acting is None:
        raise ResetRefused(f"primary-swap: repository {primary.id} is cloned at a task's lease — no task acts "
                           f"for this console session")
    if ports.has_stack(wt):
        import stack_ops  # noqa: PLC0415 — subprocess-heavy sibling, needed only on a swap

        if stack_ops.stop_stack(wt, env.stack_down_cmd) != "down":
            log.warning("primary-swap: the stack in %s did not come down — moving the tree anyway", wt)
    managed_env._withdraw_managed_env(wt)
    err = session_user.ensure_dir_as_session_user(os.path.dirname(parked))
    if err:
        raise ResetRefused(f"primary-swap: cannot create {os.path.dirname(parked)}: {err}")
    slot_clone.move(wt, parked)
    try:
        if moving_in:
            slot_clone.move(incoming, wt)
        elif mode == MODE_RESUME and acting is not None:   # a resume with no acting task was refused above
            try:
                _clone_tree(config, primary, wt, acting, branch)
            except slot_clone.CloneFailed as exc:
                raise ResetRefused(f"primary-swap: {exc}") from exc
    except Exception:
        if not os.path.lexists(wt):
            try:
                slot_clone.move(parked, wt)
                log.error("primary-swap: restored %s to %s", parked, wt)
            except ResetRefused as back:
                log.error("primary-swap: restoring %s to %s failed: %s", parked, wt, back)
        raise
    if os.path.isdir(wt):
        try:
            _arm_primary(config, n, wt, env)
        except ResetRefused:
            raise
        except Exception as exc:  # noqa: BLE001 — a primary that cannot be armed refuses the session, as a claim does
            raise ResetRefused(f"primary-swap: could not set up {wt}'s environment: {exc}") from exc
    log.warning("primary-swap: %s #%s now holds repository %s (repository %s parked at %s)",
                config.name, n, primary.id, old_id, parked)


def _arm_primary(config: PoolConfig, n: int | str, wt: str, env: SlotEnv) -> None:
    """What `claim_mechanics` does for a slot's tree, for a primary that MOVED in: seed `.env`, harden modes,
    then the managed environment, ports, stack and node identity (all idempotent; a claim re-applies them)."""
    env_file, example = os.path.join(wt, ".env"), os.path.join(wt, ".env.example")
    if not os.path.isfile(env_file) and os.path.isfile(example):
        lifecycle._copy(example, env_file)
    wm_env_file.harden_worktree_modes(wt)
    managed_env.apply_managed_env(wt, env.env_vars, n=n, port_vars=config.port_vars)
    floor, ceiling = env.auto_ports or (ports.AUTO_PORT_FLOOR, ports.AUTO_PORT_CEILING)
    ports.apply_auto_ports(wt, port_vars=config.port_vars, floor=floor, ceiling=ceiling,
                           preferred=config.port_preferred)
    ports.apply_stack_identity(wt, config.repo, n)
    ports.apply_node_identity_env(wt, n, env.node_id)


# ── Converge ───────────────────────────────────────────────────────────────────────────────────────────
def needs_converge(config: PoolConfig, n: int | str) -> bool:
    """False for a project this module leaves exactly as today: no repository, an older CP, or one repository
    whose tree is the slot's with no `_repos` ever made (AC2.1)."""
    if not config.has_repo or not config.repos:
        return False
    if len(config.repos) > 1 or os.path.isdir(repos_root(config.clone)):
        return True
    wt = config.wt_path(n)
    held = worktree_repo_id(config, wt) if os.path.isdir(wt) else None
    return held is not None and held != _primary(config).id


def converge(
    config: PoolConfig, n: int | str, *, mode: str, task_id: object = None, env: SlotEnv | None = None,
    acting: Acting | None = None,
) -> dict | None:
    """Bring slot N to the roster's repository set, at a session's start; returns the manifest written, or
    None when the project needs none (`needs_converge`). `acting` is the lease's task (`repo_id` None): every
    clone and refresh spends ITS credential, each repository's own (`repo_id`); None (a console) clones nothing.

    1. a primary change: `_swap_primary` (raises `ResetRefused` — the session is refused, as today);
    2. every non-primary repository at `_repos/<rid>/N`: cloned when absent (a legacy linked worktree is erased
       first), on its `core.task_branch` from `origin/<branch>` or its own base; an existing clone is
       refreshed on a claim and left exactly as it is otherwise;
    3. a tree whose repository left the project: removed when nothing in it is unsaved, else kept;
    4. the manifest (`_repos/slot-<N>.json`).
    The primary's own tree (clone, reset, branch, `.env`, ports, stack) stays `claim_mechanics`'."""
    if mode not in MODES:
        raise ValueError(f"converge mode {mode!r}")
    refusal = legacy_refusal(config, n)
    if refusal:
        raise ResetRefused(refusal)
    if not needs_converge(config, n):
        return None
    # One converge per project at a time: two slots' session starts (a dispatch and a console) would race on
    # the shared `_repos` directories.
    with _project_lock(config.clone):
        return _converge_locked(config, n, mode=mode, task_id=task_id, env=env or SlotEnv(), acting=acting)


def legacy_refusal(config: PoolConfig, n: int | str) -> str:
    """Why a slot that IS multi-repo may not start a session on a roster that names no repositories (an
    older CP, an unusable list), or "": today's single-repo path would treat the slot's tree as the project's
    `clone_url`, which it may not be since a swap."""
    if not config.has_repo or config.repos or not os.path.isdir(repos_root(config.clone)):
        return ""
    wt = config.wt_path(n)
    stamped = clone_repo_id(wt) if slot_clone.is_standalone(wt) else None
    if stamped is not None:
        return (f"repos-unlisted: {config.name} is a multi-repo project ({wt} holds repository {stamped}) but the "
                f"roster lists no repositories — refusing rather than guessing its primary")
    return ""


_PROJECT_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


def _project_lock(anchor: str) -> threading.Lock:
    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(os.path.realpath(anchor), threading.Lock())


def _converge_locked(config: PoolConfig, n: int | str, *, mode: str, task_id: object, env: SlotEnv,
                     acting: Acting | None) -> dict:
    primary = _primary(config)
    wt = config.wt_path(n)
    branch = _fleet_branch(config, task_id) if mode != MODE_CONSOLE else ""
    incoming = repo_worktree_path(config.clone, primary.id, n)
    if os.path.isdir(wt):
        current = worktree_repo_id(config, wt)
        if current is not None and current != primary.id:
            _swap_primary(config, n, wt, current, env, mode=mode, branch=branch, acting=acting)
    elif slot_clone.is_linked_worktree(incoming):
        slot_clone.erase(incoming)   # the retired layout: the claim clones the primary afresh
    elif slot_clone.is_standalone(incoming):
        # The slot path is gone but the primary's tree is parked: it moves back in rather than being cloned
        # beside it (and later read as a repository that left the project).
        slot_clone.move(incoming, wt)
        _arm_primary(config, n, wt, env)

    present: list[dict] = [{"id": primary.id, "path": wt, "repo_path": primary.repo_path,
                            "base": primary.base, "primary": True}]
    failed: list[dict] = []
    for spec in config.repos:
        if spec.primary:
            continue
        rwt = repo_worktree_path(config.clone, spec.id, n)
        # Its own prefix names its task branch (the prompt's `task_prompt` spelling), else the primary's.
        own = (_fleet_branch(replace(config, branch_prefix=spec.branch_prefix), task_id)
               if branch and spec.branch_prefix is not None else branch)
        try:
            entry, failure = _converge_one(config, spec, rwt, mode=mode, branch=own, acting=acting)
        except Exception as exc:  # noqa: BLE001 — one repository never stops the session (failure isolation)
            log.exception("slot-repos: converging %s into %s failed", spec.repo_path, rwt)
            # Withheld whatever is on disk: an unexpected error leaves no proof the tree was refreshed and guarded.
            entry = None
            failure = _failure(spec, FAILED_RESET if os.path.isdir(rwt) else FAILED_CLONE,
                               f"{type(exc).__name__}: {exc}")
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
    config: PoolConfig, spec: RepoSpec, rwt: str, *, mode: str, branch: str, acting: Acting | None,
) -> tuple[dict | None, dict | None]:
    """One non-primary repository of slot N: `(its repos entry or None, its failed entry or None)`."""
    linked = slot_clone.is_linked_worktree(rwt)
    absent = linked or not os.path.lexists(rwt)
    if not absent and mode != MODE_CLAIM:
        return _present(spec, rwt), None
    if acting is None:
        who = "console session" if mode == MODE_CONSOLE else "session"
        return ((None if absent else _present(spec, rwt)),
                _failure(spec, FAILED_CLONE, f"cloned at a task's lease — no task acts for this {who}"))
    # Every path below is withheld unless the tree was cloned or refreshed AND guarded with current values: a tree
    # whose refresh failed carries the last hold's state and guard, and is never handed to the session.
    try:
        slot_clone.prepare(rwt, spec.clone_url, spec.base, branch, replace(acting, repo_id=spec.id),
                           push_missing_base=_push_enabled(config, spec))
        _require_guard(config, spec, rwt)
    except slot_clone.CloneFailed as exc:
        return None, _failure(spec, FAILED_CREDENTIAL if exc.refused else FAILED_CLONE, str(exc))
    except _GuardFailed as exc:
        return None, _failure(spec, FAILED_GUARD, str(exc))
    except ResetRefused as exc:
        return None, _failure(spec, FAILED_CLONE if absent else FAILED_RESET, str(exc))
    return _present(spec, rwt), None


def release_slot_repos(anchor: str, n: int | str) -> None:
    """A slot's ERASE (`worktree_ops.worktree_erase`): every `_repos/<rid>/N` goes, whatever it holds (the
    erase never refuses), then the manifest."""
    import slot_residue  # noqa: PLC0415 — deferred: slot_residue → slot_destroy → commands is a cycle

    for _rid, path in sorted(_slot_worktrees_under(anchor, n).items()):
        _down_sub_stack(anchor, path, n)
        try:
            slot_clone.erase(path)
        except ResetRefused as exc:
            log.warning("slot-repos: %s — falling back to the residue remover", exc)
            err = slot_residue.remove_tree(path)
            if err:
                log.warning("slot-repos: could not remove %s: %s", path, err)
        slot_residue.purge_scratch(path)
    try:
        os.unlink(manifest_path(anchor, n))
    except OSError:
        pass


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
        env_helpers.atomic_write_text(Path(path), text, mode=0o644)
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
    """The console's session-start converge: a console about to OPEN A NEW SESSION in a free slot
    converges it first, from a fresh roster read (`converge`, mode `console`). A task-held slot
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
        if slot.state != tick_wire.SLOT_FREE:
            return None
        env = SlotEnv(auto_ports=tick.auto_ports, node_id=node_id or None,
                      stack_down_cmd=slot.stack_down_cmd)
        return converge(slot.pool_config(), slot.number, mode=MODE_CONSOLE, env=env)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("slot-repos: console converge of %s #%s failed — opening without it: %s", project, number, exc)
        return None
