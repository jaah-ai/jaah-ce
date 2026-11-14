"""Shared Cockpit-API project resolution for the access-level guard scripts (apps/jaahd/guards/).

Single home for the name/clone-path resolution check-access-guard.py and install-access-guard.py share,
via tools/control_plane_client.py — and for `git_hooks_dir` and `git_config_values`, the clone reads both make.
An exact project-name match wins FIRST; only then is the argument treated as a literal clone path — a
coincidentally-named git directory under cwd must never shadow the real, DB-declared clone.

A matched project's clones are COMPUTED for this node from the project's `id`
(`control_plane_client.resolve_slot_clones`: every primary-repository slot clone under `<JAAH_HOME>/repos/<id>/`,
plus a legacy `main` anchor still on disk); `[]` when this node has no JAAH_HOME, no usable `id`, or no clone yet.

JAAH_ENV_PATH overrides which `.env` supplies ADMIN_EMAIL/ADMIN_PASSWORD (test isolation);
JAAH_API_URL (control_plane_client.api_base) overrides the API base URL.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

HUB_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(HUB_ROOT / "tools"))
import control_plane_client  # noqa: E402

# The `remote.origin.pushurl` sentinel install-access-guard.py sets when push is disabled and
# check-access-guard.py audits for. Never rename: live clones already carry this literal, and the
# installer removes only a sentinel it recognizes, so a rename would leave those clones permanently
# unpushable.
DISABLED_PUSH_URL = "disabled://readonly-guard-see-docs-policies-repo-access-levels-md"


def git_hooks_dir(clone: str) -> Path:
    """`git rev-parse --git-path hooks` for `clone`, as an absolute path; raises `CalledProcessError` when
    git cannot answer. `--git-path` answers relative to cwd in a plain clone (absolute in a linked
    worktree), so run in it."""
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=clone, capture_output=True, text=True, check=True,
    ).stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else Path(clone) / out


def git_config_values(clone: str, key: str, *, local: bool = False) -> list[str] | None:
    """Every value of `key` in `clone`'s git config (`--get-all`, one per line as git prints them), or None
    when git cannot answer: the key is unset, or `clone` is no repository. `local` reads the clone's own config
    file alone — the scope `access-guard-pre-push.py::read_config` reads, so an audit is never satisfied by
    `~/.gitconfig` or `GIT_CONFIG_*`."""
    result = subprocess.run(
        ["git", "-C", clone, "config", *(["--local"] if local else []), "--get-all", key],
        capture_output=True, text=True,
    )
    return result.stdout.splitlines() if result.returncode == 0 else None


_ENV_PATH_OVERRIDE = os.environ.get("JAAH_ENV_PATH")
ENV_PATH = Path(_ENV_PATH_OVERRIDE) if _ENV_PATH_OVERRIDE else control_plane_client.DEFAULT_ENV_PATH


def _is_git_clone(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))


def _project_clones(project: dict) -> list[str]:
    return [os.path.expanduser(c) for c in control_plane_client.resolve_slot_clones(project.get("id"))]


def resolve_one(arg: str) -> tuple[str, list[str]] | None:
    """<name-or-path> -> (name, clone paths), or None if it resolves to neither an exact console
    project name nor a local git clone; the list is empty for a project with no clone on this node.
    Raises control_plane_client.ControlPlaneApiError if the API is unreachable — callers must propagate
    that loudly, never as a silent 'not found'."""
    projects = control_plane_client.fetch_projects(env_path=ENV_PATH)
    for p in projects:
        if p.get("name") == arg:
            return arg, _project_clones(p)
    if _is_git_clone(arg):
        name = os.path.basename(arg.rstrip("/")) or arg
        return name, [arg]
    return None


def resolve_integration_branch(arg: str) -> str:
    """The project's `integration_branch`, or `""` when `arg` names no Jaah project (a bare clone
    path, a repo we do not manage). Never guesses a branch: `""` means "unknown", and every caller
    treats it as "say nothing" rather than substituting a default.

    Informational only (it shapes a refusal's advice). Raises `ControlPlaneApiError` — an unreachable API
    is never a silent empty answer."""
    for p in control_plane_client.fetch_projects(env_path=ENV_PATH):
        if p.get("name") == arg:
            return str(p.get("integration_branch") or "")
    return ""


def resolve_jaah_repo_prefix(arg: str) -> str | None:
    """The project's `jaah_repo_prefix` (`<jaah org>/<org slug>-`, `""` when the control plane has none), or None
    when `arg` names no Jaah project. Raises `ControlPlaneApiError`, as `resolve_integration_branch` does."""
    for p in control_plane_client.fetch_projects(env_path=ENV_PATH):
        if p.get("name") == arg:
            return str(p.get("jaah_repo_prefix") or "")
    return None


class DeclaredGovernance(NamedTuple):
    """The two columns the clone-side guard enforces: `branch_prefix` (with its trailing slash, or
    `""` for none) and `push_enabled`. Nothing about PRs: no layer inside a git clone sees a PR, so the
    prefix half of the PR rule is enforced at the tool layer, by a guard on `gh pr` commands.

    `None` in place of a whole `DeclaredGovernance` means "not a Jaah project", never to be conflated
    with a project declaring NO restriction: only the second can be compared against what is installed,
    and conflating them let a clone left unpushable after its restriction was lifted read as conforming."""

    branch_prefix: str
    push_enabled: bool

    @property
    def restricted(self) -> bool:
        """Whether anything in the clone has to be enforced at all — the one definition the hook, the
        installer and the checker share."""
        return bool(self.branch_prefix) or not self.push_enabled


def parse_git_bool(raw: str | None) -> bool | None:
    """Git boolean spellings -> bool: `true`/`yes`/`on`/`1` and `false`/`no`/`off`/`0`, case-insensitive;
    None for unset or anything else — the single home of that parse for the guard scripts (the pre-push
    hook restates it: it is copied alone into a clone). Never a truthiness test: `bool('false')` is True."""
    if raw is None:
        return None
    low = raw.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    return None


def _declared_governance(project: dict) -> DeclaredGovernance:
    """A project row's `branch_prefix` + `push_enabled` as the clone-side guard must compare them.

    No em-dash split: `branch_prefix` is a validated git ref prefix, so splitting would mangle a legal
    value. A missing/NULL prefix reads as `""`.

    A non-empty prefix is CANONICALIZED to its trailing slash (`sn` -> `sn/`), as
    `install-access-guard.py::normalize_prefix` and `tools/project_config.py::_config_from_row` do.
    Otherwise a row holding `sn` compares against an installed `sn/` and fails forever, and the hook
    reading it raw would let `refs/heads/sn` match `refs/heads/sneaky/foo` — every layer must agree.

    `push_enabled` is read STRICTLY; anything unrecognizable reads as False — the restrictive side, so an
    unreadable flag never hides a real restriction from the audit."""
    prefix = str(project.get("branch_prefix") or "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    raw = project.get("push_enabled")
    if isinstance(raw, bool):
        push_enabled = raw
    elif isinstance(raw, str):
        push_enabled = parse_git_bool(raw) is True
    else:
        push_enabled = False
    return DeclaredGovernance(branch_prefix=prefix, push_enabled=push_enabled)


def resolve_one_with_governance(arg: str) -> tuple[str, list[str], DeclaredGovernance | None] | None:
    """`resolve_one` plus the project's DECLARED governance — (name, clone paths, governance), where
    governance is None ONLY for a bare local clone the API knows nothing about (a caller must not
    invent one). Same priority and per-node clones as `resolve_one`."""
    projects = control_plane_client.fetch_projects(env_path=ENV_PATH)
    for p in projects:
        if p.get("name") == arg:
            return arg, _project_clones(p), _declared_governance(p)
    if _is_git_clone(arg):
        name = os.path.basename(arg.rstrip("/")) or arg
        return name, [arg], None
    return None


def sweep_all() -> list[tuple[str, list[str], DeclaredGovernance]]:
    """EVERY project in the console as (name, clone paths, declared governance) triples — governance per
    `_declared_governance`, the clones this node holds (possibly none).

    Deliberately UNfiltered: filtering to restricted projects would hide a clone still carrying a
    restrictive marker after its restriction was lifted."""
    projects = control_plane_client.fetch_projects(env_path=ENV_PATH)
    return [
        (p.get("name", ""), _project_clones(p), _declared_governance(p))
        for p in projects
    ]
