"""`origin` follows `repo_url`: `repo_url` is only where a project is cloned from, so when it names a
different repo (a rename, a move) the clone's `origin` follows it. Run once per reconcile pass, before
anything fetches (`reconcile`)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING

import git_env
import slot_repos
from worktree_mechanics import core

if TYPE_CHECKING:  # the types live in `reconcile`, which imports this module
    from reconcile.tick_wire import PoolTick, RosterProject

log = logging.getLogger("jaahd.pool_origins")


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


def _origin_url(clone: str) -> str | None:
    """`remote.origin.url` of the clone at CLONE, read off its config file in-process; None when unreadable."""
    import subprocess  # noqa: PLC0415 — a read of one file, never a network op

    try:
        proc = subprocess.run(
            ["git", "config", "-f", os.path.join(clone, ".git", "config"), "--get", "remote.origin.url"],
            # From `/`: git still discovers a repository from its cwd, and a broken one there (a worktree
            # whose main clone is gone) fails even a `-f` read.
            capture_output=True, text=True, timeout=10, check=False, cwd=os.sep,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


# Two or more segments: `o/r`, nested GitLab groups `grp/sub/repo`, Azure `org/proj/repo`.
_REPO_PATH_SHAPE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")


def _repoint_clone(project_name: str, clone: str, repo_path: str, clone_url: str) -> bool:
    """Point the clone at CLONE's `origin` at CLONE_URL when they name DIFFERENT repos. A mere respelling
    (ssh vs https, a `.git` suffix) is left alone: it is the same repo, and the existing remote may carry
    the auth this host uses. Returns whether it changed."""
    if not _REPO_PATH_SHAPE.fullmatch(repo_path or "") or not clone_url or not slot_repos.is_repo(clone):
        return False
    have = _origin_url(clone)
    if have is None or _repo_identity(have) == _repo_identity(clone_url):
        return False
    rc, _out, err = core._git(clone, "remote", "set-url", "origin", clone_url)
    if rc != 0:
        log.warning("pool-origins: could not re-point %s origin %s → %s: %s", project_name, git_env.redact_userinfo(have),
                    git_env.redact_userinfo(clone_url), err.strip())
        return False
    log.info("pool-origins: re-pointed %s origin %s → %s", project_name, git_env.redact_userinfo(have),
             git_env.redact_userinfo(clone_url))
    return True


def _repoint_origin(project: "RosterProject") -> bool:
    """Point PROJECT's clone's `origin` at the CP's repo when they name DIFFERENT repos — `repo_url` is only
    where a project is cloned from, so a repo rename (or a move) is an edit of that URL. Every linked
    worktree shares the clone's config. Returns whether any clone changed.

    A multi-repo project (`repos` on the roster) re-points PER CLONE, by the repository each clone
    is stamped with (`slot_repos`): a clone follows only its OWN row's URL, never another row's — so a
    primary change moves slots' worktrees (`slot_repos.converge`) and never retargets a clone that live
    slots push through. The anchor is stamped here first: a clone, not a slot, so a pass may."""
    if not project.repos:
        # A roster naming no repositories (an older CP, an unusable list) never re-points an anchor that is
        # already one clone among several: since a primary swap the anchor may hold ANOTHER repository than
        # `clone_url`, and its parked worktrees would follow the wrong remote.
        if slot_repos.clone_repo_id(project.clone) is not None or os.path.isdir(slot_repos.repos_root(project.clone)):
            return False
        return _repoint_clone(project.project, project.clone, project.repo_path, project.clone_url)
    config = project.pool_config()
    slot_repos.stamp_anchor(config)
    changed = False
    for spec in project.repos:
        clone = slot_repos.repo_clone(config, spec.id)
        if clone == project.clone and slot_repos.anchor_repo_id(config) != spec.id:
            continue  # the anchor is no clone of this row yet (no anchor at all)
        changed = _repoint_clone(project.project, clone, spec.repo_path, spec.clone_url) or changed
    return changed


async def repoint_origins(tick: "PoolTick") -> None:
    """`_repoint_origin` for every project on the tick. Never raises into the pass."""
    if not tick.ok:
        return
    for project in tick.projects.values():
        try:
            await asyncio.to_thread(_repoint_origin, project)
        except Exception as exc:  # noqa: BLE001 — a failed re-point must never wedge the pass
            log.warning("pool-origins: %s origin check failed: %s", project.project, exc)
