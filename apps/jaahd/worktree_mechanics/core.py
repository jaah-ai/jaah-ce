"""Slot identity and git plumbing — `PoolConfig`, slot paths, branch names and the `git` runner every op goes through."""
from __future__ import annotations

import logging
import os
import posixpath
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import clone_identity
import git_env
import git_mirror
import node_settings
import session_user

log = logging.getLogger("jaahd.worktree_mechanics.core")


# A clone takes minutes of network; `_git`'s 120s default would abort it. Read at call time.
def _clone_timeout() -> float:
    return float(node_settings.get("node_clone_timeout_secs"))


class ResetRefused(RuntimeError):
    """`reset_to_base` (or a helper) refuses to touch a worktree; aborts the whole lease, surfaced LOUDLY.
    Callers must let it propagate — catching and proceeding defeats the gate."""


def mirror_key(config: "PoolConfig") -> git_mirror.MirrorKey:
    """The mirror `config`'s network git ops run in: the project's primary (`<pid>.git`) or, for a non-primary
    repository (`config.repo_id`), its own (`<pid>-<rid>.git`), declared with that repository's id."""
    return git_mirror.MirrorKey(project_id=config.project_id, repo_id=config.repo_id, slug=config.name,
                                clone_url=config.clone_url, declared_repo_id=config.repo_id)


def _refresh_mirror(config: "PoolConfig") -> "os.PathLike[str]":
    """`git_mirror.refresh` of `config`'s mirror (the static pool's root). Raises `git_mirror.MirrorError`, and
    translates `ProjectBusy` into `ResetRefused` — the refusal every caller already handles, and one the CP may
    re-issue (nothing was touched)."""
    try:
        return git_mirror.refresh(mirror_key(config), shared=True, timeout=_clone_timeout())
    except clone_identity.ProjectBusy as exc:
        # "clone-busy: " is matched by the CP, which never charges it to the park budget
        # (`routes/agent/slot_intents.py::_CLONE_BUSY_REFUSAL`).
        raise ResetRefused(f"clone-busy: {exc}") from exc


def _fetch_from_mirror(config: "PoolConfig", wt: str, *branches: str, refresh: bool = True) -> tuple[int, str, str]:
    """Refresh `config`'s mirror (unless `refresh` is False: a caller that just did), then fetch `origin/<branch>`
    for each of `branches` into `wt` from it, locally and with no credential (`git_mirror.local_fetch_args`) →
    `_git`'s (rc, stdout, stderr); a failed refresh is rc 1 with its text. Raises `ResetRefused`
    ("clone-busy: ")."""
    if refresh:
        try:
            mirror = _refresh_mirror(config)
        except git_mirror.MirrorError as exc:
            return 1, "", str(exc)
    else:
        mirror = git_mirror.mirror_path(mirror_key(config), shared=True)
    return _run_git("-C", wt, *git_mirror.local_fetch_args(mirror, *map(git_env.tracking_refspec, branches)),
                    env_extra=git_mirror.LOCAL_FETCH_ENV)


def _create_missing_base(config: "PoolConfig", mirror: "os.PathLike[str]") -> bool:
    """Create the integration branch off origin's default branch when origin has no such branch.

    True only when it created the branch. A project may name an integration branch nobody has created yet (an
    onboarding's `prefix/develop`, a typo'd branch); a slot cannot be built off a ref that does not exist, so the
    first materialisation creates it rather than refusing every dispatch. Called after a SUCCESSFUL refresh of
    `mirror`, whose `refs/heads/*` then equal origin's (`--prune`): only a remote that answered and lacks the
    branch is the missing-branch case. A project the CP says may push (`PoolConfig.push_enabled`) gets it ON
    ORIGIN — never read off a clone's `access-guard.push-enabled`, which any runner sharing the clone can set; the
    sha pushed is the mirror's `refs/heads/<default>`, which no runner can move (the mirror is daemon-owned). Any
    other project gets it LOCALLY, as the source clone's `refs/heads/<base>` (which every slot's local-`base`
    fallback resolves), and nothing is pushed. A failure is logged; the caller's refusal then names the branch
    (`missing_base_refusal`)."""
    base = config.base
    local = Path(mirror)  # `git_mirror`'s readers take a `Path`
    if git_mirror.branch_sha(local, base):
        return False
    try:
        default = git_mirror.remote_head(mirror_key(config), shared=True)
        # Origin's own answer, so never read as an option.
        if not default or default == base or default.startswith("-"):
            return False
        sha = git_mirror.branch_sha(local, default)
        if not sha:
            return False
        if config.push_enabled:
            git_mirror.push_ref(mirror_key(config), shared=True, sha=sha, ref=f"refs/heads/{base}")
            log.info("integration branch %s created on origin off its default branch %s", base, default)
            return True
    except clone_identity.ProjectBusy as exc:
        raise ResetRefused(f"clone-busy: {exc}") from exc
    except git_mirror.MirrorError as exc:
        log.warning("integration branch %s is not on origin and could not be created off its default branch: %s",
                    base, exc)
        return False
    clone = config.source_clone
    if _git(clone, "rev-parse", "-q", "--verify", f"refs/heads/{base}")[0] == 0:
        return True  # an earlier dispatch already created it here
    rc, _o, err = _fetch_from_mirror(config, clone, default, refresh=False)
    if rc == 0:
        # `<old>` empty: creates only, never moves a branch that raced in.
        rc, _o, err = _git(clone, "update-ref", f"refs/heads/{base}", sha, "")
    if rc != 0:
        log.warning("integration branch %s could not be created in %s off %s: %s",
                    base, clone, default, (err or "").strip() or "no stderr")
        return False
    log.info("integration branch %s created in %s off origin's default branch %s — local only: this project may "
             "not push", base, clone, default)
    return True


def base_resolves(wt: str, base: str) -> bool:
    """Does `wt` see the integration branch, as `origin/<base>` or as the local `<base>`?"""
    return any(_git(wt, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")[0] == 0
               for ref in (f"refs/remotes/origin/{base}", f"refs/heads/{base}"))


def missing_base_refusal(base: str) -> str:
    """The refusal for an integration branch that is on neither origin nor this node and could not be created.
    Its wording up to the em dash is matched by the CP (`routes/agent/slot_intents.py::_MISSING_BRANCH_REFUSAL_MARKER`)."""
    return (
        f"integration branch '{base}' does not exist on origin and could not be created off the repository's "
        f"default branch — create it, or set the project's integration branch to one that exists"
    )


def ensure_base(config: "PoolConfig", wt: str) -> None:
    """Make `wt` see the integration branch, creating it off origin's default branch when origin lacks it
    (`_create_missing_base`). Raises `ResetRefused`: `missing_base_refusal` only when origin answered without the
    branch and it could not be created; "fetch-failed: " when origin could not be asked or the branch it has could
    not be fetched — never read as a missing branch, whose remedy (another branch) would be wrong."""
    base = config.base
    if base_resolves(wt, base):
        return
    try:
        mirror = _refresh_mirror(config)
    except git_mirror.MirrorError as exc:
        raise ResetRefused(f"fetch-failed: integration branch {base} could not be looked up on origin: {exc}") from None
    on_origin = bool(git_mirror.branch_sha(Path(mirror), base))
    if not on_origin:
        if not _create_missing_base(config, mirror):
            raise ResetRefused(missing_base_refusal(base))
        on_origin = config.push_enabled  # else created locally, in the clone this tree shares
    if on_origin:
        rc, _o, err = _fetch_from_mirror(config, wt, base, refresh=False)
        if rc != 0:
            raise ResetRefused(f"fetch-failed: integration branch {base} could not be fetched: "
                               f"{(err or '').strip() or 'no stderr'}")
    if not base_resolves(wt, base):
        raise ResetRefused(missing_base_refusal(base))


# The branch collision as a TOKEN: it crosses a process boundary (mechanics → `ctl` JSON `reason` →
# `fleet_dispatch`), and this one refusal alone makes the executor retry on the NEXT branch candidate.
BRANCH_HELD_ELSEWHERE = "branch-checked-out-elsewhere"


def slot_path(clone: str, n: int | str) -> str:
    """slot 0 → the main clone; N>=1: `clone` ending in `/main` → sibling `<dirname>/N`, else `<clone>-N`.
    Must agree with the CP's `app/routes/pool_slots.py::_slot_path`. Standalone so a caller holding only a
    clone path (`worktree_ops.worktree_erase`'s containment check) can derive a slot path.

    `posixpath`, never `os.path`, ON EVERY OS: the CP composes a Windows node's clone as
    `C:\\ProgramData\\Jaah\\home/repos/<id>/main` with posixpath, and `ntpath` would split an all-backslash
    spelling where the CP does not — the two must agree byte for byte. Identical to `os.path` on posix."""
    if str(n) == "0":
        return clone
    base = clone.rstrip("/")
    if posixpath.basename(base) == "main":
        return f"{posixpath.dirname(base)}/{n}"
    return f"{base}-{n}"


# ── Pool config (structurally identical to reconcile.PoolConfig; see the module docstring) ──────────────
@dataclass
class PoolConfig:
    name: str
    clone: str
    base: str
    # The credential-free https remote the CP serves (`PoolTickRosterEntry.clone_url`) and the forge path
    # (`o/r`, `grp/sub/repo`, `org/proj/repo`); the node derives neither from the other.
    clone_url: str
    repo_path: str
    port_vars: list[str] = field(default_factory=list)
    # The CP's `projects.branch_prefix`, via `reconcile.tick_wire._mechanics_config`; `""` means none. Used VERBATIM
    # by the only two functions with an opinion on branch shape, which must agree: `claim_mechanics`
    # derives `{branch_prefix}fleet/task-<id>` as `own_branch`, `retire_mechanics`'s `_fleet_branch_re` accepts only
    # that spelling. Defaulted only because tests construct this directly.
    branch_prefix: str = ""
    # The roster's `has_repo` (folder slots): False is a project with no repository, whose slots
    # are plain empty folders — no clone, fetch, worktree, `.env`, port band or stack. Absent on the wire
    # reads True (`reconcile.tick_wire._roster_entry`), so an older CP keeps today's git path byte for byte.
    has_repo: bool = True
    # The roster's `projects.id` — what `roster_from_db` addresses the CP by. None = an older CP's roster
    # (or a test's direct construction): the read then goes by `name` alone.
    project_id: int | None = None
    # The roster's `repos` (`slot_repos.RepoSpec`): every repository of the project, the primary
    # flagged. `()` is a single-repo project or an older CP, and every path below is then today's, byte for
    # byte. Only `slot_repos` reads it.
    repos: tuple = ()
    # The `project_repos.id` this config's network git ops are for, declared beside the project
    # (`clone_identity`); None = the project's primary, today's credential.
    repo_id: int | None = None
    # Where this repo's git objects live when that is not `clone`: a primary swapped off the anchor clone
    # keeps the slot path (`wt_path` is still derived from `clone`) but is cloned and fetched under
    # `_repos/<id>/main`. `""` = `clone`, or the roster's answer (`source_clone`).
    git_clone: str = ""
    # The CP's `projects.push_enabled` (`reconcile.tick_wire.wire_push_enabled`): whether the node may push the missing
    # integration branch (`_create_missing_base`). Never read off a clone, whose config a runner can write.
    push_enabled: bool = False

    def wt_path(self, n: int | str) -> str:
        return slot_path(self.clone, n)

    @property
    def source_clone(self) -> str:
        """The clone `git worktree add`, `ensure_clone` and the integration-branch probes run in: `clone`,
        unless the primary repo lives in another clone (`slot_repos.primary_clone`)."""
        if self.git_clone:
            return self.git_clone
        if not self.repos:
            return self.clone
        import slot_repos  # noqa: PLC0415 — `slot_repos` imports this module
        return slot_repos.primary_clone(self)

    @property
    def repo(self) -> str:
        """`repo_path`'s last segment — the stack identity base for a tree outside the pool layout
        (`stack_base`), unlike the editable display `name`. Falls back to `name`,
        never `""`: an empty `COMPOSE_PROJECT_NAME` makes compose use the directory name, a bare `N` shared
        by every project's slot N."""
        return self.repo_path.rstrip("/").rsplit("/", 1)[-1] or self.name


def _safe_ref_component(s: str) -> str:
    """Sanitize a `refs/pool-reclaim/...` path component: refname-illegal characters become "-", so a
    branch name or an ISO timestamp embeds directly. Slashes stay — they nest as ref hierarchy."""
    out = re.sub(r'[:\s~^?*\[\\]', "-", s)
    while ".." in out:
        out = out.replace("..", "-")
    return out.strip(".") or "x"


# ── git shell-out helpers ───────────────────────────────────────────────────────────────────────────────
# `_run_git`'s rc when the uid switch is required but unavailable — distinct from git's own codes and
# from 127 (absent tool) / 124 (timeout).
SWITCH_UNAVAILABLE_RC = 125


# ── The erase→rebuild interlock ──────────────────────────────────────────────────────────────────────
# `delete_one_slot` deletes the row FIRST and erases the tree SECOND, so a pass on an older roster would
# rebuild an orphan. A short in-process note that THIS daemon erased the path — not a lock or a correctness
# boundary. TTL because the CP commit is never observed here; too short costs the orphan, so generous.
_ERASED_TTL_S = 120.0
_ERASED_LOCK = threading.Lock()
_ERASED: dict[str, float] = {}


def note_erased(path: str) -> None:
    """Record that this daemon just erased `path`, so convergence does not immediately rebuild it."""
    with _ERASED_LOCK:
        now = time.monotonic()
        for stale in [p for p, deadline in _ERASED.items() if deadline <= now]:
            _ERASED.pop(stale, None)
        _ERASED[os.path.realpath(path)] = now + _ERASED_TTL_S


def was_just_erased(path: str) -> bool:
    """True while `path`'s erase is still waiting for the CP to drop its row."""
    with _ERASED_LOCK:
        deadline = _ERASED.get(os.path.realpath(path))
        return deadline is not None and deadline > time.monotonic()


def _run_git(*args: str, timeout: float = 120, env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    """`git <args>` with NO `-C` → (rc, stdout, stderr); the one subprocess site here. rc: 127 absent git,
    124 timeout, 125 switch-unavailable; a nonzero rc is the signal, not an exception.

    RUNS AS THE SESSION USER wherever the uid split applies: a `jaahd`-owned tree makes git's
    safe.directory DISTRUST the repo for `jaah`, every runner's uid. Read-only ops ride the same seam —
    misclassifying one WRITE op would recreate the bug silently. FAILS CLOSED with `SWITCH_UNAVAILABLE_RC`;
    the delegated env is `env_extra` ONLY plus the seam's HOME/PATH defaults. No call here is declared: the
    network ops run in the mirror (`git_mirror`), so the broker serves these nothing, and `GIT_TERMINAL_PROMPT=0`
    keeps an unanswered credential from waiting on a terminal. Repo-local code is pinned off
    (`git_env.NO_REPO_CODE`): a hook planted in a runner's tree must never run as a daemon op."""
    env_extra = {**(env_extra or {}), "GIT_TERMINAL_PROMPT": "0"}
    if session_user.switch_needed():
        if not session_user.can_run_as_session_user():
            return SWITCH_UNAVAILABLE_RC, "", (
                f"session-user switch required but unavailable (socket={session_user.fleet_socket()!r} "
                f"spool={session_user.spool_ready()}) — refusing to run git as {session_user.daemon_user()!r}; "
                f"check jaah-runner@<node name>.service"
            )
        try:
            # Delegation sheds the daemon's uid (the session user's tmux server forks the command), so the
            # op is registered as this daemon's own or the credential broker refuses its credential helper. Scoped
            # to this one call, released whatever happens (`clone_identity`'s delegated-op registry).
            with clone_identity.delegated_op(session_user.session_uid()) as registration:
                proc = session_user.run_as_session_user(
                    ["git", *git_env.NO_REPO_CODE, *args], env=dict(env_extra or {}), timeout=timeout,
                    on_child_pid=registration.claim,
                )
        except subprocess.TimeoutExpired:
            return 124, "", ""
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), OSError
            return SWITCH_UNAVAILABLE_RC, "", f"session-user delegation failed: {exc}"
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    env = {**os.environ, **env_extra}
    try:
        # Registered under this thread's declaration (`clone_identity.run_own`): unregistered, the broker
        # serves its credential helper no project.
        proc = clone_identity.run_own(["git", *git_env.NO_REPO_CODE, *args], text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", ""
    except subprocess.TimeoutExpired:
        return 124, "", ""
    return proc.returncode, proc.stdout, proc.stderr


def _git(wt: str, *args: str, timeout: float = 120) -> tuple[int, str, str]:
    """`git -C <wt> <args>` → (rc, stdout, stderr). See `_run_git` for the rc conventions."""
    return _run_git("-C", wt, *args, timeout=timeout)


def _fetch_base_cp(config: "PoolConfig") -> tuple[int, str, str]:
    """`origin/<base>` in the source clone, fetched from the project's mirror (`_fetch_from_mirror`)."""
    return _fetch_from_mirror(config, config.source_clone, config.base)


def clear_skip_worktree_bits(wt: str) -> None:
    """Return every tracked file in `wt` to a plain checkout by clearing its `skip-worktree` bit.

    A runner launch may set the bit on files it hides from its CLI (an adapter's `exec_line` in
    `agents/`), so `git` inside that launch does not see them as deleted. Left set, the NEXT session in
    this slot — any provider, or a human — would have git silently ignore its edits to those files. Nothing
    in the pool uses sparse checkout, so every such bit is one to clear. Run before a reset's
    `reset --hard`, so the files are written back from the index. Never raises."""
    rc, out, _e = _git(wt, "ls-files", "-v", "-z")
    paths = [entry[2:] for entry in out.split("\0") if entry[:2] in ("S ", "s ")] if rc == 0 else []
    if paths and _git(wt, "update-index", "--no-skip-worktree", "--", *paths)[0] != 0:
        log.warning("clear_skip_worktree_bits: %d file(s) in %s keep their skip-worktree bit (update-index "
                    "failed) — the reset will not rewrite them and git will ignore edits to them", len(paths), wt)


def wt_branch(wt: str) -> str:
    """Current branch of a worktree, or "DETACHED". Never raises."""
    rc, out, _ = _git(wt, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = out.strip()
    return branch if rc == 0 and branch else "DETACHED"
