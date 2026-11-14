"""Erase what the retired pool layout left on this node — linked-worktree slot trees, main clones and the old
MicroVM mirror root — without touching held work (worktree-pool-policy.md §Layout). Every repository slot is now
a standalone clone (`slot_clone`); nothing reads these leftovers any more. An older daemon's per-project mirrors
in the slot mirrors' root are `git_mirror.sweep`'s.

Run by every reconcile pass with a usable roster (`reconcile.orchestrate.reconcile_pass`), the first one after
startup included. It is the only deleter of a main clone: the CP never sends `erase_tree` for slot 0.

1. A legacy slot tree — its `.git` is a FILE — is erased once its row is `free` and nothing live sits in it; a
   `task`-held one is left byte for byte until its hold ends. The next lease clones (`slot_clone.prepare`, which
   also erases one it races to).
2. A main clone (`repos/<pid>/main`, `repos/<pid>/_repos/<rid>/main`) is erased once no linked worktree still
   points into it (its own `.git/worktrees` registry, each entry checked on the tree's side) and nothing live
   sits in it. A hub session in `main` keeps it until the session exits.
3. The old MicroVM mirror root (`<daemon state>/mirrors`) is emptied once nothing live sits in it.

FAIL CLOSED: "nothing live" needs a FRESH agents probe (taken for the migration itself, never the cross-tick
cache) that is complete with no session cwd inside the path AND, where the node has `/proc`, a cwd scan that found
no process inside it. A blind probe, a failed scan, an incomplete roster or an unreadable registry erases nothing
this pass; the next pass retries. Nothing is probed or locked on a pass with no candidate.
"""
from __future__ import annotations

import logging
import os
import posixpath
import stat
from dataclasses import dataclass
from typing import Iterable

import env_helpers
import host_platform
import slot_clone
from slot_repos import NON_PRIMARY_DIRNAME
from worktree_mechanics import core, processes

log = logging.getLogger("jaahd.legacy_layout")

# The slot-0 directory of the retired layout.
MAIN_DIRNAME = "main"


@dataclass(frozen=True)
class Slot:
    """What the migration needs of one roster slot: its tree and whether the CP has it `free`."""

    path: str
    free: bool


def slot_trees(path: str) -> list[str]:
    """Slot `path`'s legacy linked-worktree trees: the primary at `path` and each non-primary
    `_repos/<rid>/<N>` beside it."""
    trees = [path] if slot_clone.is_linked_worktree(path) else []
    number = posixpath.basename(path.rstrip("/"))
    root = posixpath.join(posixpath.dirname(path.rstrip("/")), NON_PRIMARY_DIRNAME)
    try:
        repo_ids = sorted(os.listdir(root))
    except OSError:
        return trees
    for rid in repo_ids:
        tree = posixpath.join(root, rid, number)
        if rid.isdigit() and slot_clone.is_linked_worktree(tree):
            trees.append(tree)
    return trees


def main_clones(repos_root: str) -> list[str]:
    """Every `repos/<pid>/main` and `repos/<pid>/_repos/<rid>/main` on disk, rostered project or not."""
    found: list[str] = []
    try:
        projects = sorted(os.listdir(repos_root))
    except OSError:
        return found
    for pid in projects:
        project = os.path.join(repos_root, pid)
        candidates = [os.path.join(project, MAIN_DIRNAME)]
        try:
            candidates += [os.path.join(project, NON_PRIMARY_DIRNAME, rid, MAIN_DIRNAME)
                           for rid in sorted(os.listdir(os.path.join(project, NON_PRIMARY_DIRNAME)))]
        except OSError:
            pass
        found += [path for path in candidates if os.path.isdir(path) and not os.path.islink(path)]
    return found


def _read_path(file: str, prefix: str = "") -> str:
    """The path one line of `file` names (after `prefix`), absolute: a relative one is taken from `file`'s own
    directory, as git does. Raises OSError."""
    with open(file, encoding="utf-8", errors="replace") as fh:
        target = fh.readline().strip().removeprefix(prefix).strip()
    return os.path.normpath(os.path.join(os.path.dirname(file), target)) if target else ""


def has_linked_worktree(main: str) -> bool | None:
    """Does a linked worktree still point into `main`? Read off `main`'s own registry (`.git/worktrees/*/gitdir`),
    each entry confirmed on the tree's side: its `.git` is still a FILE naming this clone (a slot already turned
    standalone has a `.git` directory there; a tree gone from disk is what git itself calls prunable). None when the
    registry cannot be read — unknown, never "no"."""
    registry = os.path.join(main, ".git", "worktrees")
    try:
        entries = sorted(os.listdir(registry))
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("legacy-layout: cannot read %s (%s) — keeping %s", registry, exc, main)
        return None
    common = os.path.realpath(os.path.join(main, ".git"))
    for entry in entries:
        try:
            dot_git = _read_path(os.path.join(registry, entry, "gitdir"))
            if not dot_git:
                continue
            try:
                if not stat.S_ISREG(os.lstat(dot_git).st_mode):
                    continue          # a standalone clone's `.git` directory
            except FileNotFoundError:
                continue              # the tree is gone: what git calls prunable
            # Any other OSError (EACCES) is unknown: handled below, never read as gone.
            if host_platform.is_under(os.path.realpath(_read_path(dot_git, "gitdir:")), common):
                return True
        except OSError as exc:
            log.warning("legacy-layout: cannot read worktree %s of %s (%s) — keeping it", entry, main, exc)
            return None
    return False


def mirror_roots() -> list[str]:
    """Where an older daemon kept its MicroVM sessions' git mirrors, which nothing reads now."""
    return [str(env_helpers.daemon_state_dir("mirrors"))]


def liveness(paths: list[str], agents_ok: bool, agent_cwds: Iterable[str]) -> dict[str, bool | None]:
    """Per path: True live, False provably idle, None unknown. Idle needs the agents probe complete with no
    session cwd inside, and — where `/proc` exists — the cwd scan clear (`processes.scan_cwd_processes_multi`,
    which answers clear on a node without `/proc`, so the agents probe alone decides there)."""
    if not paths:
        return {}
    if not agents_ok:
        return dict.fromkeys(paths)
    cwds = [cwd for cwd in agent_cwds if cwd]
    # `collect=True`: a short-circuiting scan stops at the first hit across ALL paths and reads the rest clear.
    scans = processes.scan_cwd_processes_multi(paths, collect=True)
    verdict: dict[str, bool | None] = {}
    for path, scan in zip(paths, scans, strict=True):
        if scan.status == processes.PROBE_FAILED:
            verdict[path] = None
        else:
            verdict[path] = bool(scan.status) or any(host_platform.is_under(cwd, path) for cwd in cwds)
    return verdict


def _empty_dir(root: str) -> None:
    """Remove everything under the daemon-owned `root`, then `root` itself when it can be (empty is the end state
    where it cannot). Raises OSError."""
    for name in os.listdir(root):
        env_helpers.remove_path(os.path.join(root, name), onexc=slot_clone._clear_readonly)
    try:
        os.rmdir(root)
    except OSError:
        pass


def free_legacy_trees(slots: Iterable[Slot]) -> list[str]:
    """Step 1's candidates: every legacy tree of a slot the roster names `free`."""
    return [tree for slot in slots if slot.free for tree in slot_trees(slot.path)]


def unreferenced_mains(repos_root: str) -> list[str]:
    """Step 2's candidates: every main clone no linked worktree points into (`has_linked_worktree` is False)."""
    return [main for main in main_clones(repos_root) if has_linked_worktree(main) is False]


def nonempty_mirror_roots() -> list[str]:
    """Step 3's candidates: every old mirror root that still holds something."""
    found = []
    for root in mirror_roots():
        try:
            if os.path.isdir(root) and os.listdir(root):
                found.append(root)
        except OSError as exc:
            log.warning("legacy-layout: cannot read %s (%s) — left for the next pass", root, exc)
    return found


def unlocked_candidates(repos_root: str, *, roster_complete: bool) -> list[str]:
    """Steps 2 and 3's candidates, in order: the main clones nothing points into (only on a complete roster: a
    slot the roster dropped could still hold a linked tree), then the old mirror roots with something in them.
    Directory reads only, so a pass with nothing erasable never pays for the fresh agents probe."""
    mains = unreferenced_mains(repos_root) if roster_complete else []
    return mains + nonempty_mirror_roots()


def erase_idle(paths: list[str], agents_ok: bool, agent_cwds: Iterable[str]) -> list[str]:
    """Erase each of `paths` that `liveness` proves idle → the paths erased. A mirror root (daemon-owned) is emptied
    by the daemon; anything else (the session user's) through `slot_clone.erase`. Never raises."""
    roots = set(mirror_roots())
    erased: list[str] = []
    for path, live in liveness(paths, agents_ok, agent_cwds).items():
        if live is not False:
            continue
        try:
            _empty_dir(path) if path in roots else slot_clone.erase(path)
        except (OSError, core.ResetRefused) as exc:
            log.warning("legacy-layout: could not erase %s: %s — retried next pass", path, exc)
            continue
        log.warning("legacy-layout: erased %s (retired pool layout)", path)
        erased.append(path)
    return erased
