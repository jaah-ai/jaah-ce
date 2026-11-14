"""Slot identity and git plumbing — `PoolConfig`, slot paths, branch names and the `git` runner every op goes through."""
from __future__ import annotations

import logging
import os
import posixpath
import subprocess
from dataclasses import dataclass, field

import clone_identity
import git_env
import node_settings
import session_user

log = logging.getLogger("jaahd.worktree_mechanics.core")


# A clone takes minutes of network; `_git`'s 120s default would abort it. Read at call time.
def _clone_timeout() -> float:
    return float(node_settings.get("node_clone_timeout_secs"))


def task_branch(prefix: str | None, task_id: object) -> str:
    """A task's branch, `{prefix}fleet/task-<id>`, with a trailing `fleet/` dropped from the prefix so the segment is
    carried once (`fleet/` → `fleet/task-<id>`). The control plane's spelling of the rule: `app/task_branch.py`."""
    text = prefix or ""
    if text == "fleet/" or text.endswith("/fleet/"):
        text = text[: -len("fleet/")]
    return f"{text}fleet/task-{task_id}"


class ResetRefused(RuntimeError):
    """A slot's tree could not be made or used (a clone, guard or folder step refused); aborts the whole lease,
    surfaced LOUDLY. Callers must let it propagate — catching and proceeding defeats the gate."""


def missing_base_refusal(base: str) -> str:
    """The refusal for an integration branch that is on neither origin nor this node and could not be created.
    Its wording up to the em dash is matched by the CP (`routes/agent/slot_intents.py::_MISSING_BRANCH_REFUSAL_MARKER`)."""
    return (
        f"integration branch '{base}' does not exist on origin and could not be created off the repository's "
        f"default branch — create it, or set the project's integration branch to one that exists"
    )


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


def project_anchor(home: str, project_id: int) -> str:
    """`<home>/repos/<project_id>/main`: the anchor `slot_path` derives a project's slot paths from, composed by
    the node from its own Jaah home. Byte for byte the CP's `app/node_clone.py::legacy_clone` (posixpath join on
    a home stripped of its trailing `/`), so a CP that stops sending the legacy `clone` / `repo_root` wire
    fields leaves every path unchanged. `""` when the home is unset."""
    root = (home or "").strip().rstrip("/")
    return f"{root}/repos/{project_id}/main" if root else ""


def slot_anchor(path: object, number: object, project_id: int) -> str:
    """The anchor a slot's CP-sent `path` implies, `<parent>/main`, so the anchor agrees with the slot paths the
    CP sends even when the node's own home differs from the `jaah_home` the CP recorded for it. `""` unless `path` is
    absolute (posix or a drive path), ends in `<project_id>/<number>` joined with `/` as the CP composes it
    (`slot_path` splits with posixpath, so an all-backslash spelling cannot anchor), and holds no `..` or
    multi-repo `_repos` segment under either separator: the caller then falls back to `project_anchor`."""
    raw = str(path or "").rstrip("/\\")
    absolute = raw.startswith("/") or (len(raw) > 2 and raw[0].isalpha() and raw[1] == ":" and raw[2] in "/\\")
    segments = raw.replace("\\", "/").split("/")
    cut = raw.rfind("/")
    if (not absolute or cut < max(raw.rfind("\\"), 1) or len(segments) < 3
            or segments[-2:] != [str(project_id), str(number)] or ".." in segments or "_repos" in segments):
        return ""
    return f"{raw[:cut]}/main"


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
    # The roster's `port_preferred` (`{VAR: port}`), forwarded to `ports.apply_auto_ports`.
    port_preferred: dict[str, int] = field(default_factory=dict)
    # The CP's `projects.branch_prefix`, via `reconcile.tick_wire._mechanics_config`; `""` means none. Used VERBATIM
    # by `claim_mechanics`, the one function with an opinion on branch shape: it derives
    # `task_branch` (`{branch_prefix}fleet/task-<id>`, `fleet/` once). Defaulted only because tests construct
    # this directly.
    branch_prefix: str = ""
    # The roster's `has_repo` (folder slots): False is a plain empty folder — no clone, fetch, worktree,
    # `.env`, ports or stack. A slot's own `has_repo` overrides its entry's, so one project may hold both
    # kinds. Absent on the wire reads True (`reconcile.tick_wire._roster_entry`), so an older CP keeps today's
    # git path byte for byte.
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
    # The CP's `projects.push_enabled` (`reconcile.tick_wire.wire_push_enabled`): whether the node may push the missing
    # integration branch (`slot_clone`'s base creation). Never read off a clone, whose config a runner can write.
    push_enabled: bool = False
    # The CP's `jaah_repo_prefix` (`reconcile.tick_wire.wire_jaah_repo_prefix`), the guard's
    # `access-guard.jaah-repo-prefix` marker. None = an older CP: every install leaves the marker as it is.
    jaah_repo_prefix: str | None = None

    def wt_path(self, n: int | str) -> str:
        return slot_path(self.clone, n)

    @property
    def repo(self) -> str:
        """`repo_path`'s last segment — the stack identity base for a tree outside the pool layout
        (`stack_base`), unlike the editable display `name`. Falls back to `name`,
        never `""`: an empty `COMPOSE_PROJECT_NAME` makes compose use the directory name, a bare `N` shared
        by every project's slot N."""
        return self.repo_path.rstrip("/").rsplit("/", 1)[-1] or self.name


# ── git shell-out helpers ───────────────────────────────────────────────────────────────────────────────
# `_run_git`'s rc when the uid switch is required but unavailable — distinct from git's own codes and
# from 127 (absent tool) / 124 (timeout).
SWITCH_UNAVAILABLE_RC = 125


def _run_git(*args: str, timeout: float = 120, env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    """`git <args>` with NO `-C` → (rc, stdout, stderr); the one subprocess site here. rc: 127 absent git,
    124 timeout, 125 switch-unavailable; a nonzero rc is the signal, not an exception.

    RUNS AS THE SESSION USER wherever the uid split applies: a `jaahd`-owned tree makes git's
    safe.directory DISTRUST the repo for `jaah`, every runner's uid. Read-only ops ride the same seam —
    misclassifying one WRITE op would recreate the bug silently. FAILS CLOSED with `SWITCH_UNAVAILABLE_RC`;
    the delegated env is `env_extra` ONLY plus the seam's HOME/PATH defaults. Nothing here is admitted to the
    credential broker: every network op runs as the daemon in its mirror (`git_mirror`), so the broker serves
    these nothing, and `GIT_TERMINAL_PROMPT=0` keeps an unanswered credential from waiting on a terminal. Repo-local code is pinned off
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
            # Never registered with the credential broker: a process the session user's tmux server forks is
            # never served a login (`clone_identity`'s registry holds the daemon's own children only).
            proc = session_user.run_as_session_user(
                ["git", *git_env.NO_REPO_CODE, *args], env=dict(env_extra or {}), timeout=timeout)
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
