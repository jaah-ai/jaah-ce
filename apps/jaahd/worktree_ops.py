"""Worktree-misc command family — native in-process handlers. `run` (the op allowlist) is
the docker→host shell boundary. Importing this module (once, at the bottom of `commands.py`)
self-registers the family into `commands.NATIVE_HANDLERS`.

Handler contract: a SYNCHRONOUS `(command_type, args, run) -> {"exit_code", "stdout", "stderr"}`
callable. `run` is `commands._native_run`, a bounded output-capped subprocess closure run off the event
loop via `asyncio.to_thread`; a `to_thread` handler is UNKILLABLE, so `_native_run` clamps any timeout
DOWN to the ~170s bus budget (`EXECUTE_TIMEOUT_SECONDS`) and handlers pass none of their own.

`worktree_erase`, `org_transfer_purge` and `trigger_preflight` are DISPATCH-ONLY, absent from `app/routes/commands.py`'s
`VALID_TYPES`, so the generic `POST /api/commands` surface cannot reach a destructive disk op or run a repo
script. Every tmux client call passes
`_tmux_safe_env()` (`env_helpers`): this process's env must never freeze into a tmux server it
auto-starts. Asserted by the tests.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import clone_identity  # which project a network git op belongs to, for the node's credential helper
import fleet_launch   # slot_window_name(): the ONE composer of a slot's tmux window name
import git_env
import git_mirror   # the project's daemon-owned mirror: the one place a network git op holds a credential
import host_platform
import node_settings   # node_clone_timeout_secs: a mirror bootstrap's budget
from jaah_env import env_get  # renamed env vars
import runner_env   # docker_env(): the node-managed engine's endpoint
import session_user   # which tmux server a client call must name (`-S`)
import slot_repos     # a multi-repo slot's other trees: the clone that owns a slot tree, removed with it on erase
import slot_residue   # what a slot leaves outside its tree: its scratch dirs

from commands import EXECUTE_TIMEOUT_SECONDS, native_command
import env_helpers
from env_helpers import _tmux_safe_env, jaah_home

log = logging.getLogger("jaahd.worktree_ops")

# Read through the accessors below so a test can monkeypatch `_REPO_ROOT`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# The NON-agent windows a slot can also have (`console.py`'s plain-shell and web-console tabs). Both are
# resolved BY NAME, so renaming one orphans the live window and duplicates it on the next attach.
# `_tmux_window_id_for` returns the FIRST pane at the worktree path, so the skip must be explicit.
_NON_AGENT_WINDOW_SUFFIXES = ("|term", "|web")
#: A docker compose project name (`docker compose -p`): lowercase alphanumerics, `-` and `_`, as the CP mints them
#: (`p<project>-<number>`, `app/slot_allocation.py::_mint_destroy`).
_COMPOSE_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def _tmux_window_id_for(path: str, run, *, skip_name_suffixes: tuple[str, ...] = ()) -> tuple[str, str]:
    """`(socket, window_id)` of the live tmux window whose pane cwd is `path` (exact match wins, else the
    first match UNDER it), skipping any window whose name ends in one of `skip_name_suffixes`. `("", "")`
    when nothing matches, and then the rename is skipped.

    Uses `#{window_id}`, not `session:index`, because an index is unstable under `renumber-windows on`.
    THE SOCKET TRAVELS WITH THE ID: window ids are per-SERVER and both servers are live during an
    in-place-upgrade drain, so renaming on the wrong socket relabels a stranger's window. An absent socket
    path is skipped rather than probed: a tmux client with no server starts one AS ITSELF."""
    if not path:
        return "", ""
    for socket in session_user.manage_sockets():
        if socket and not os.path.exists(socket):
            continue
        reply = run(*session_user.tmux_cmd(
            socket, "list-panes", "-a", "-F", "#{pane_current_path}\t#{window_id}\t#{window_name}"),
            env=_tmux_safe_env())
        if reply.get("exit_code") != 0:
            continue
        prefix = ""
        for line in (reply.get("stdout") or "").splitlines():
            pane_path, _, rest = line.partition("\t")
            target, _, name = rest.partition("\t")
            if not pane_path or not target:
                continue
            if any(name.endswith(suffix) for suffix in skip_name_suffixes):
                continue
            if host_platform.path_key(pane_path) == host_platform.path_key(path):
                return socket, target
            if not prefix and host_platform.is_under(pane_path, path):
                prefix = target
        if prefix:
            return socket, prefix
    return "", ""


def _agent_windows_under(path: str, run) -> list[tuple[str, str]]:
    """EVERY distinct `(socket, window_id)` with a pane at — or under — `path`, minus the non-agent tabs.

    Unlike `_tmux_window_id_for`, which arbitrates ONE window (right for a rename), a teardown needs them
    all: `kill_gates`' `expect_cwd` fence cannot tell sibling windows in one tree apart. The same set as
    `reconcile/context.py::wt_tmux_targets` (which needs a pass snapshot this sync command lacks). Scans BOTH
    sockets: during an in-place-upgrade drain a tree's windows can be split across servers."""
    if not path:
        return []
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for socket in session_user.manage_sockets():
        if socket and not os.path.exists(socket):
            continue
        reply = run(*session_user.tmux_cmd(
            socket, "list-panes", "-a", "-F", "#{pane_current_path}\t#{window_id}\t#{window_name}"),
            env=_tmux_safe_env())
        if reply.get("exit_code") != 0:
            continue
        for line in (reply.get("stdout") or "").splitlines():
            pane_path, _, rest = line.partition("\t")
            target, _, name = rest.partition("\t")
            if not pane_path or not target:
                continue
            if not host_platform.is_under(pane_path, path):
                continue
            if any(name.endswith(suffix) for suffix in _NON_AGENT_WINDOW_SUFFIXES):
                continue
            # A multi-pane window lists once per pane; dedupe so the reported count is not overstated.
            if (socket, target) in seen:
                continue
            seen.add((socket, target))
            found.append((socket, target))
    return found


def _clear_the_tree_before_it_goes(wt: str, what: str, run) -> tuple[int, int]:
    """Force-kill every process rooted in `wt` (docker excepted) and every agent tmux window in it.
    Answers `(processes signalled, windows torn down)`. NEVER RAISES.

    Lives here, not in an intent: the CP deletes the `worktrees` row and commits before pushing its
    `release_slot` intent, which the reconcile loop then refuses (slot absent from the roster), and the
    tree is erased by then anyway. This awaited handler runs while the tree still exists. The same
    enactment as `reconcile.enact_reset._clear_tree` (the single home of why it asks nothing and why docker is
    exempt — the stack is stopped properly by `stack_down_cmd` below); processes before windows.
    Never raises: it runs past the erase's risk gate on an already-deleted row. The two halves fail
    INDEPENDENTLY — a process-sweep failure must not abandon the window teardown; only a failed import
    of `kill_gates` skips both."""
    swept: list = []
    windows = 0
    try:
        # Deferred (subprocess-heavy, this path only) and guarded: a deferred import can raise on a broken
        # install, in a handler whose caller has already committed the row deletion.
        import kill_gates  # noqa: PLC0415
        from worktree_mechanics import reap  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — see the docstring's never-raises paragraph
        print(f"erase: cannot clear {wt} — teardown modules unavailable: {exc}", file=sys.stderr)
        return 0, 0
    try:
        swept = reap.reap_orphan_processes(wt, orphans_only=False) or []
    except Exception as exc:  # noqa: BLE001 — as above
        # No early return: the windows are a separate obligation.
        print(f"erase: sweeping processes in {wt} failed: {exc}", file=sys.stderr)
    try:
        for _socket, target in _agent_windows_under(wt, run):
            # `expect_cwd` re-proves the window is still under this path (tmux renumbering). The socket
            # is not passed: `kill_gates.kill_session` resolves ids across sockets, the single choke point.
            if kill_gates.guarded_window_kill(str(target), what, expect_cwd=wt):
                windows += 1
    except Exception as exc:  # noqa: BLE001 — as above
        print(f"erase: tearing down tmux windows in {wt} failed: {exc}", file=sys.stderr)
    return len(swept), windows


def _check_pool_ports() -> str:
    return str(_REPO_ROOT / "tools" / "check_pool_ports.py")


def _fleet_doctor() -> str:
    return str(_REPO_ROOT / "tools" / "fleet_doctor.py")


def _open_term() -> str:
    return str(_REPO_ROOT / "apps" / "jaahd" / "open_term.py")


def _install_access_guard() -> str:
    return str(_REPO_ROOT / "apps" / "jaahd" / "guards" / "install-access-guard.py")


# `run` ops whose script AUTHENTICATES TO THE CP, so they inherit the daemon's real env (see `run_op`),
# never the strict tmux-safe allowlist, which strips the credential. Governs the SAME-UID branch only;
# under the uid switch the two ports ops are delegated with `_PORT_GRID_ENV_KEYS` (`_PORT_GRID_OPS`).
_CTL_BUS_OPS = frozenset({"ports_audit", "ports_reband", "doctor"})

# The two ops that shell the pool port audit, which reads (audit) and writes (`--fix`) every
# pool slot's `.env` — `0600 jaah:jaah`, holding real secrets — so both are EACCES for uid `jaahd`
# and are delegated to the session user. That also gives `--restart`'s `./dc up -d` the only docker
# engine a fleet uid may reach (`jaah`'s rootless one).
_PORT_GRID_OPS = frozenset({"ports_audit", "ports_reband"})

# What a DELEGATED port-grid run may carry — an allowlist, since tmux `run-shell` otherwise hands over
# its own frozen env. `HOME`/`PATH` absent: the seam fills the session user's. JAAH_HOME is required
# (resolve_clone_root has no default); the socket overrides only if non-default; locale/TZ because the
# script prints non-ASCII. NODE_TOKEN and the CP base are WITHHELD: every read it makes is a socket verb
# on the `0660` group-`jaah` credential socket, and the token is the one secret
# `jaah-runner@.service` refuses to list (see `check`).
_PORT_GRID_ENV_KEYS: tuple[str, ...] = (
    "JAAH_HOME", "JAAH_CRED_SOCKET", "JAAHD_CTL", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)


@native_command("rename_tmux")
def rename_tmux(command_type: str, args: dict, run) -> dict:
    """Relabel a slot's LIVE tmux window to the recomposed `{Project}-{N}|{Title}`.

    Args `{project, number, path, title}`; the name comes from `fleet_launch.slot_window_name` and `path`
    finds the window. The CP-owned Title travels one way — never forwarded back. `|term`/`|web` windows
    are skipped (`_NON_AGENT_WINDOW_SUFFIXES`)."""
    project = args.get("project")
    number = args.get("number")
    title = (args.get("title") or "").strip()
    path = str(args.get("path") or "").strip()
    if project is None or number is None:
        return {"exit_code": 1, "stdout": "", "stderr": "rename_tmux needs project + number"}
    if not title:
        return {"exit_code": 1, "stdout": "", "stderr": "title is required"}
    if not path:
        # Loud: success here would report a rename that never happened.
        return {"exit_code": 1, "stdout": "", "stderr": "path is required to find the live window"}
    if any(c in title for c in ("\n", "\r", "\0")):
        return {"exit_code": 1, "stdout": "", "stderr": "title contains invalid characters"}

    name = fleet_launch.slot_window_name(project, number, title)
    socket, window = _tmux_window_id_for(path, run, skip_name_suffixes=_NON_AGENT_WINDOW_SUFFIXES)
    if not window:
        # No live window is not an error: the next window this slot opens is named from the stored Title.
        return {"exit_code": 0, "stdout": f"no live window at {path} — nothing to rename", "stderr": ""}
    # A tmux client call on the SAME socket the id came from (ids are per-server).
    return run(*session_user.tmux_cmd(socket, "rename-window", "-t", window, "--", name),
               env=_tmux_safe_env())


@native_command("logs")
def logs(command_type: str, args: dict, run) -> dict:
    # Pop a desktop terminal tailing the worktree's stack logs (the per-stack `./dc logs`).
    path = args.get("path", "")
    if not path:
        return {"exit_code": -1, "stdout": "", "stderr": "logs needs a worktree path"}
    title = f"logs {Path(path).name}"
    inner = (f"exec {shlex.quote(sys.executable)} {shlex.quote(_open_term())} {shlex.quote(title)} "
             f"{shlex.quote(path)} {shlex.quote('./dc logs -f --tail=200')}")
    # `_tmux_safe_env()`: a long-lived desktop terminal; `./dc` reads the worktree's own .env.
    return run("bash", "-lc", inner, env=_tmux_safe_env())


@native_command("sync_develop")
def sync_develop(command_type: str, args: dict, run) -> dict:
    # Clean-rebase-ONLY sync of the integration base. The executor holds the whole safety decision:
    # decline as a quiet no-op (exit 0, tree untouched, never a reset) when it would need to stash or
    # merge. Never `--autostash` — it stashes a live session's work out from under it.
    path = args.get("path", "")
    if not path:
        return {"exit_code": -1, "stdout": "", "stderr": "sync_develop needs a worktree path"}
    # The base comes from the CP (`projects.integration_branch`); a node may not author it. FAILS CLOSED,
    # no git probe and no `"develop"` default (absence-is-not-a-state). Refused before any git call.
    base = str(args.get("base") or "").strip()
    if not base:
        return {"exit_code": -1, "stdout": "",
                "stderr": ("sync_develop needs base (the project's integration_branch, sent by the CP) — "
                           "the node does not derive it; nothing was synced")}
    # The remote too: the project's mirror refreshes from the CP's URL, never the tree's `origin` (`git_mirror`).
    clone_url = str(args.get("clone_url") or "").strip()
    if not clone_url:
        return {"exit_code": -1, "stdout": "",
                "stderr": ("sync_develop needs clone_url (the project's remote, sent by the CP) — the node "
                           "does not read it from the tree; nothing was synced")}
    project = str(args.get("project") or "").strip()
    project_id = args.get("project_id") if isinstance(args.get("project_id"), int) else None
    # The mirror is keyed by the id, never the editable name; the CP always sends it (it names the slot).
    if project_id is None or isinstance(project_id, bool):
        return {"exit_code": -1, "stdout": "",
                "stderr": ("sync_develop needs project_id (the project's id, sent by the CP with the slot) — the "
                           "node keys the project's git mirror by it; nothing was synced. Update the control "
                           "plane if it sent none")}
    return _sync_develop_body(path, base, clone_url, run, project, project_id)


def _sync_develop_body(path: str, base: str, clone_url: str, run, project: str, project_id: int) -> dict:
    """`sync_develop`'s git calls. Only the mirror's REFRESH is declared (`git_mirror.refresh`, run here as the
    daemon, never through `run`, which delegates git to the session user): every call in this tree — `status`
    runs clean filters, the local fetch, `rebase` checks out (smudge filters, hooks) — runs undeclared, so a
    driver planted in this tree's own config sees no token. Each is still registered as this daemon's op
    (`_native_run`) with no declaration of its own (`clone_identity._own_declaration`), so the broker serves
    it nothing. `_native_run` pins repo-local code off. A host with no uid split has no such boundary to keep:
    there runners share the daemon's uid."""
    git = ("git", "-C", path)
    # A `.git` DIRECTORY is never genuinely bare — core.bare=true there is corruption. Linked worktrees
    # have a `.git` FILE, so this never fires for them.
    repair_failed = False
    if (Path(path) / ".git").is_dir():
        bare = run(*git, "config", "--get", "core.bare")
        if bare["stdout"].strip() == "true":
            repair = run(*git, "config", "core.bare", "false")
            repair_failed = repair["exit_code"] != 0

    def _with_repair_note(text: str) -> str:
        return text + "\n[sync_develop] core.bare self-heal failed" if repair_failed else text

    def _skip(reason: str) -> dict:
        return {"exit_code": 0, "stdout": "", "stderr": _with_repair_note(f"[sync_develop] skipped: {reason}")}

    # Any uncommitted TRACKED change blocks (a stash would take it). Untracked files never block a
    # rebase, so this is deliberately narrower than `Worktree.dirty` (`reconcile/context.py::wt_dirty`); safe
    # because Sync-All pre-filters on the wider column.
    dirty = run(*git, "status", "--porcelain", "--untracked-files=no")
    if dirty["exit_code"] != 0:
        # "Could not ask" is not "clean": an errored probe must not fall through to the rebase.
        return {"exit_code": dirty["exit_code"], "stdout": "",
                "stderr": _with_repair_note("[sync_develop] could not read the worktree's status — "
                                            "not synced\n" + (dirty.get("stderr") or "").strip())}
    if dirty["stdout"].strip():
        return _skip("worktree has uncommitted changes (a clean sync would require stashing) — not synced")
    # FETCH failure (credential, network) is a real, visible failure with git's stderr; REBASE failure
    # (divergence) is the deliberate quiet no-op after an abort.
    # The node's credential helper serves a credential chosen by the project declared in flight, in
    # the refresh alone. The cost of the declaration: a sync queued behind another project's clone is refused
    # with `ProjectBusy`.
    key = git_mirror.MirrorKey(project_id=project_id, repo_id=None, slug=project, clone_url=clone_url)
    try:
        # The bus budget `run` is clamped to: a handler cannot be killed once its git runs (`_native_run`).
        mirror = git_mirror.refresh(key, shared=True, timeout=EXECUTE_TIMEOUT_SECONDS)
    except clone_identity.ProjectBusy as exc:
        return {"exit_code": -1, "stdout": "",
                "stderr": _with_repair_note(
                    f"[sync_develop] another git operation for a different project is in flight on this node — "
                    f"the tree is untouched and NOT synced\n{exc}")}
    except git_mirror.MirrorError as exc:
        fetched = {"exit_code": 1, "stdout": "", "stderr": str(exc)}
    else:
        fetched = run(*git, *git_mirror.local_fetch_args(mirror, git_env.tracking_refspec(base)),
                      env=git_mirror.LOCAL_FETCH_ENV)
    if fetched["exit_code"] != 0:
        detail = (fetched.get("stderr") or "").strip()
        return {
            "exit_code": fetched["exit_code"], "stdout": "",
            "stderr": _with_repair_note(
                f"[sync_develop] fetch of origin/{base} failed — the tree is untouched and NOT synced"
                + (f"\n{detail}" if detail else "")
            ),
        }
    # FETCH_HEAD, not origin/<base>: exactly what the fetch wrote, whatever the tracking-ref behaviour.
    res = run(*git, "rebase", "FETCH_HEAD")
    if res["exit_code"] != 0:
        run(*git, "rebase", "--abort")   # never leave the tree half-applied
        detail = (res.get("stderr") or "").strip()
        return _skip(
            "rebase is not clean (conflicts / would require merging) — tree restored intact, not synced"
            + (f"\n{detail}" if detail else "")
        )
    # Keep git's "From <mirror>" line: the audit-log receipt of what the sync fetched from.
    res["stderr"] = _with_repair_note(
        "\n".join(part for part in ((fetched.get("stderr") or "").strip(),
                                    (res.get("stderr") or "").strip()) if part)
    ).strip()
    return res


def erase_lock_path(project: str, number: int | str) -> Path:
    """The per-slot flock `worktree_erase` holds. `project` is not format-validated, so `/` must not make this filename a path."""
    return env_helpers.fleet_locks_dir() / f"worktree-erase-{project.replace('/', '_')}-{number}.lock"


def open_erase_lock(project: str, number: int | str):
    """`erase_lock_path` opened for `flock`, NEVER through a symlink: `run/locks` is group-writable, and a
    planted link would have this uid create a file wherever it may write. Raises `OSError`.

    Windows has neither flag (`os` lacks both names) and needs neither: its handles are not inherited by
    default, and the directory there is the daemon-only `state\\locks` (`env_helpers.fleet_locks_dir`),
    where no runner can plant a link or a junction."""
    return _open_lock(erase_lock_path(project, number))


def _open_lock(path: Path):
    """`path` opened for `flock`, never through a symlink (see `open_erase_lock`). Raises `OSError`."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o660)
    return os.fdopen(fd, "a+")


def _python() -> str:
    """The interpreter a delegated repo tool runs under: `runner_env.python_cmd()` on posix (the node's own on
    Linux, PATH `python3` on macOS); on Windows the bundle's own `python.exe`, since no `python3` is on its PATH."""
    return host_platform.paths().python if host_platform.is_windows() else runner_env.python_cmd()


def _ensure_locks_dir(locks_dir: Path) -> str:
    """Create the erase lock's directory, or say why not ("" = ready). A refusal refuses the erase.

    As the session user on posix (`session_user`'s ownership contract: `run/locks` is shared with it). On
    Windows by the daemon itself: the directory is the daemon-only `state\\locks`
    (`env_helpers.fleet_locks_dir`), which the runner account cannot even list."""
    if host_platform.is_windows():
        try:
            locks_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return str(exc)
        return ""
    return session_user.ensure_dir_as_session_user(str(locks_dir))


def _purge_volumes(compose_project: str, cwd: str) -> tuple[bool, str]:
    """`slot_destroy.compose_down`: the stack down WITH its volumes. `(ok, why)`."""
    import slot_destroy  # noqa: PLC0415 — deferred: slot_destroy → commands → worktree_ops is a cycle

    return slot_destroy.compose_down(compose_project, cwd)


def _tree_compose_project(wt: str, clone: str, number: int) -> str:
    """The slot's own compose project when the tree's `.env` names it, else "". The name is DERIVED
    (`worktree_mechanics.ports.stack_base`, as `apply_stack_identity` writes it) and the runner-writable `.env`
    only confirms it: a `.env` naming another slot's project must never take that slot's volumes down."""
    from worktree_mechanics import ports  # noqa: PLC0415 — deferred, as every caller here

    try:
        text = (Path(wt) / ".env").read_text(errors="replace")
    except OSError:
        return ""
    base = ports.stack_base(wt, os.path.basename(os.path.dirname(clone)))
    expected = base if number == 0 else f"{base}-{number}"
    return expected if env_helpers.compose_project_name(text) == expected else ""


@native_command("org_transfer_purge")
def org_transfer_purge(command_type: str, args: dict, run) -> dict:
    """Purge what the node's old org left outside its slots (`slot_residue.transfer_purge`) — the node half of
    the node-move cleanup (`app/durable/node_transfer.py`), which keeps the node out of placement until this
    exits 0. `held_tasks` / `keep_projects`: the task ids and compose projects the NEW org holds on this node,
    whose staging dirs and images stay."""
    raw = args.get("held_tasks")
    if not isinstance(raw, list) or not all(str(t).isdigit() and not isinstance(t, bool) for t in raw):
        return {"exit_code": -1, "stdout": "",
                "stderr": f"org_transfer_purge: held_tasks must be a list of task ids (got {raw!r})"}
    keep = args.get("keep_projects")
    if not isinstance(keep, list) or not all(isinstance(p, str) and _COMPOSE_PROJECT.fullmatch(p) for p in keep):
        return {"exit_code": -1, "stdout": "",
                "stderr": f"org_transfer_purge: keep_projects must be a list of compose project names (got {keep!r})"}
    result = slot_residue.transfer_purge({str(t) for t in raw}, set(keep))
    return {"exit_code": 1 if result["failed"] else 0, "stdout": json.dumps(result),
            "stderr": "; ".join(result["failed"])[:2000]}


@native_command("worktree_erase")
def worktree_erase(command_type: str, args: dict, run) -> dict:
    """PHYSICALLY erase one pool slot's directory on the CP's instruction — the node half of
    `POST /api/worktrees/{project}/{number}/erase`. An already-absent target is SUCCESS so a retry
    converges, and a per-target flock keeps two operators off the same slot.

    The target is RECOMPUTED from CP-injected `clone_path` via `worktree_mechanics.core.slot_path`, never
    accepted as a path, behind two containment checks: the clone must resolve under this node's OWN
    `jaah_home()` (a caller-settable `repo_url` must not steer a `number=0` erase at an arbitrary
    directory), and slot 0 must resolve to the clone exactly while N>=1 must be a direct sibling.

    THE ERASE NEVER REFUSES, AND NOTHING THE SLOT LEFT SURVIVES IT: the tree — whatever it holds, dirty,
    unpushed or stashed — its compose stack (containers, network, volumes, built images) and its scratch dirs
    (`slot_residue`). Nothing is kept for later. Only the checks
    that the target IS this slot refuse (exit -1).

    The stack goes down before the tree: live containers bind-mounted onto an erased tree keep its ports and
    keep writing into it. `stack_down_cmd` (the project's own stop) runs first when given, then
    `slot_destroy.compose_down` for the project — `compose_project` when `purge_volumes` names it, else the
    slot's own derived name when the tree's `.env` carries it (`_tree_compose_project`). Best effort:
    `stack_stopped` in the reply says
    which happened, and the residue sweep collects a stack a failed down left. A failed down of a NAMED
    project still exits 1 after the tree is gone, so the CP re-serves it and the already-absent arm purges
    again (the org transfer, whose volumes hold the old org's data)."""
    import shutil

    from worktree_mechanics import core
    from worktree_mechanics.core import _git, slot_path

    project = str(args.get("project") or "").strip()
    raw_number = args.get("number")
    clone_path = str(args.get("clone_path") or "").strip()
    # Absent means "skip the stop", never a guessed command.
    stack_down_cmd = str(args.get("stack_down_cmd") or "").strip()
    # `purge_volumes` + `compose_project`: take the slot's stack down WITH its volumes (`slot_destroy.compose_down`,
    # destroy_slot's own down) — the org-transfer erase, whose volumes and `.env` hold the old org's data.
    compose_project = str(args.get("compose_project") or "").strip() if args.get("purge_volumes") is True else ""
    if compose_project and not _COMPOSE_PROJECT.fullmatch(compose_project):
        return {"exit_code": -1, "stdout": "",
                "stderr": f"worktree_erase: compose_project {compose_project!r} is not a compose project name"}
    if not project or raw_number is None or not clone_path:
        return {"exit_code": -1, "stdout": "",
                "stderr": "worktree_erase needs project + number + clone_path"}
    try:
        number = int(raw_number)
    except (TypeError, ValueError):
        return {"exit_code": -1, "stdout": "",
                "stderr": f"worktree_erase: number must be an integer (got {raw_number!r})"}
    if args.get("has_repo") is False:
        # A FOLDER slot: its own erase, never the git one below — run from a folder, git would climb
        # into an ancestor repository. Absent keeps the git path.
        raw_pid = args.get("project_id")
        project_id = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
        return folder_erase(project, number, clone_path, project_id)

    home_resolved = Path(os.path.realpath(jaah_home()))
    clone_resolved = Path(os.path.realpath(clone_path))
    # STRICTLY under the home: a clone equal to it would make slot 0 the home itself and slot N a sibling of it.
    if home_resolved not in clone_resolved.parents:
        return {"exit_code": -1, "stdout": "",
                "stderr": (f"worktree_erase: clone {clone_resolved} is not under this node's Jaah home "
                           f"{home_resolved} — refusing to touch it")}
    resolved = Path(os.path.realpath(slot_path(clone_path, number)))
    if number == 0:
        contained = resolved == clone_resolved
    elif number >= 1:
        contained = resolved.parent == clone_resolved.parent and resolved != clone_resolved
    else:
        contained = False
    if not contained:
        return {"exit_code": -1, "stdout": "",
                "stderr": (f"worktree_erase: {resolved} is not slot {number} of the clone at "
                           f"{clone_resolved} — refusing to touch it")}

    if not resolved.exists():
        # A volume purge still owes the stack: its volumes outlive the tree, and nothing else will take them.
        if compose_project:
            purged, why = _purge_volumes(compose_project, str(home_resolved))
            if not purged:
                return {"exit_code": 1, "stdout": "", "stderr": f"worktree_erase: {why}"}
        # No lock: a retry must converge. The prune matters — a stale registration fails the next
        # `git worktree add`.
        if number != 0:
            _git(str(clone_resolved), "worktree", "prune", "--expire=now")
            # A multi-repo slot's other trees, their stacks and scratch, and its manifest (`slot_repos`).
            slot_repos.release_slot_repos(str(clone_resolved), number)
        # Noted here too: a retry lands here while the row is still declared.
        core.note_erased(str(resolved))
        slot_residue.purge_scratch(str(resolved))
        return {"exit_code": 0,
                "stdout": json.dumps({"deleted": True, "already_absent": True, "path": str(resolved)}),
                "stderr": ""}

    locks_dir = env_helpers.fleet_locks_dir()
    locks_err = _ensure_locks_dir(locks_dir)
    if locks_err:
        return {"exit_code": 1, "stdout": "",
                "stderr": f"worktree_erase: could not create {locks_dir}: {locks_err}"}
    try:
        lock_fh = open_erase_lock(project, number)
    except OSError as exc:
        # ELOOP: something planted a symlink where the lock lives.
        return {"exit_code": 1, "stdout": "",
                "stderr": f"worktree_erase: cannot open this slot's lock ({exc}) — refusing"}
    try:
        host_platform.lock(lock_fh, blocking=False)
    except OSError:
        lock_fh.close()
        return {"exit_code": 1, "stdout": "",
                "stderr": "another worktree operation is in progress for this slot"}

    try:
        wt = str(resolved)
        is_valid_repo = _git(wt, "rev-parse", "--git-dir")[0] == 0

        # ── CLEAR THE TREE, then stop the stack, then remove the directory ──
        # The sweep is first because it exempts docker (the stack's own shutdown stops those) and because a
        # process cannot be found by its cwd once the cwd is gone.
        killed_procs, killed_windows = _clear_the_tree_before_it_goes(
            wt, f"worktree_erase {project} #{number}", run,
        )

        # `None` = not attempted (no command injected), distinct from `False` = attempted and still up.
        stack_stopped = None
        if stack_down_cmd and not compose_project:
            import stack_ops  # noqa: PLC0415 — deferred: stack_ops → commands → worktree_ops is a cycle

            stack_stopped = stack_ops.stop_stack(wt, stack_down_cmd) == "down"
        # Read before the tree goes: the `.env` is the only record of which project is this slot's.
        down_project = compose_project or _tree_compose_project(wt, str(clone_resolved), number)
        down_failed = ""
        if down_project:
            # From the home, never the tree: a compose file or `.env` planted there must not steer the down
            # (`compose_down` then removes what carries the project's label).
            purged, why = _purge_volumes(down_project, str(home_resolved))
            stack_stopped = purged and stack_stopped is not False
            if not purged:
                down_failed = why
                log.warning("worktree_erase %s #%s: %s — left for the residue sweep", project, number, why)

        try:
            removed = False
            # The clone that OWNS the tree: the anchor, or — after a multi-repo primary swap — the primary's own
            # `_repos/<rid>/main` (`slot_repos`); removing or pruning through any other leaves its registration.
            owner = (slot_repos.worktree_clone(wt) if is_valid_repo else "") or str(clone_resolved)
            if number != 0 and is_valid_repo:
                # `git worktree remove` also clears the `.git/worktrees/N` registration (else a
                # re-declared slot N fails). `--force --force`: past git's dirty-tree refusal and its
                # LOCKED-worktree refusal — the erase never refuses.
                removed = _git(owner, "worktree", "remove", "--force", "--force", wt)[0] == 0
            if not removed:
                if host_platform.is_windows():
                    # The runner account removes its own tree there: the daemon deletes nothing under the
                    # runner-writable home itself.
                    err = session_user.remove_tree_as_session_user(str(resolved))
                    if err:
                        raise OSError(err)
                else:
                    shutil.rmtree(resolved, ignore_errors=False)
            if number != 0:
                # Best-effort: covers a `remove` fallback that left the registration, or a broken `.git`.
                _git(str(clone_resolved), "worktree", "prune", "--expire=now")
                # A multi-repo slot's other trees, their stacks and scratch, and its manifest (`slot_repos`).
                slot_repos.release_slot_repos(str(clone_resolved), number)
        except OSError as exc:
            # The PermissionError hint names the one cause this unprivileged uid cannot fix: legacy
            # root-owned residue from containers under the old rootful engine. The tree is left
            # part-deleted; a retry after cleanup converges.
            hint = ""
            if isinstance(exc, PermissionError):
                hint = (" — this uid cannot unlink inside a root-owned directory; this is legacy residue "
                        "of a container that wrote into the bind-mounted worktree as root under the "
                        "rootful engine (the rootless one cannot create it). Reclaim it as root on the "
                        f"node — `find {wt} -xdev -user 0 -exec chown jaah:jaah {{}} +` AND `chmod g+w` "
                        "on each such directory (this uid is in GROUP jaah, so ownership alone does not "
                        "reopen a 2755 directory), or re-run the reprovision layer, which does both — and retry")
            return {"exit_code": 1,
                    "stdout": json.dumps({"deleted": False, "path": wt,
                                          "stack_stopped": stack_stopped,
                                          "killed_processes": killed_procs,
                                          "killed_windows": killed_windows}),
                    "stderr": f"failed to erase {wt}: {exc}{hint}"}
        # The CP has not dropped the row yet, so this looks like a slot convergence should rebuild;
        # `note_erased` makes `reconcile.orchestrate._unmaterialised_candidates` skip it meanwhile.
        core.note_erased(wt)
        # Claude Code's per-cwd scratch under /tmp: keyed by this path, so a later slot at it would inherit it.
        scratch = slot_residue.purge_scratch(wt)
        return {"exit_code": 1 if (down_failed and compose_project) else 0,
                "stdout": json.dumps({"deleted": True, "path": wt,
                                      "scratch_removed": scratch,
                                      "stack_stopped": stack_stopped,
                                      # Reported: the only record that a Delete ended live work here.
                                      "killed_processes": killed_procs,
                                      "killed_windows": killed_windows}),
                "stderr": ""}
    finally:
        host_platform.unlock(lock_fh)
        lock_fh.close()


def _remove_folder(path: str) -> str:
    """`rm -rf` one folder slot → "" or the error. As the session user wherever the uid split applies (it
    owns the folder, owner-only) and always on Windows (the daemon deletes nothing under the
    runner-writable home there); in-process otherwise."""
    if session_user.switch_needed() or host_platform.is_windows():
        return session_user.remove_tree_as_session_user(path)
    import shutil  # noqa: PLC0415 — as `worktree_erase`'s

    try:
        shutil.rmtree(path)
    except OSError as exc:
        return str(exc)
    return ""


def folder_erase(project: str, number: int, clone_path: str, project_id: int | None) -> dict:
    """PHYSICALLY erase one FOLDER slot (a project with no repository) →
    `{"exit_code", "stdout", "stderr"}`, shaped like `worktree_erase`'s reply so `slot_destroy` reads both
    alike. `exit_code` -1 is a refusal no retry changes; 1 a failure a retry may clear; 0 erased (an
    already-absent folder included, so a retry converges).

    Its own branch, never `worktree_erase`: run from a folder, git would climb into an ancestor repository.
    Every containment check is kept and tightened to the folder layout — the target must be
    `<jaah_home>/repos/<project_id>/<number>` EXACTLY, after resolving links; slot 0 is never a folder; the
    per-slot flock is held. Whatever the folder holds goes."""
    from worktree_mechanics.core import note_erased, slot_path  # noqa: PLC0415 — deferred, as every caller here

    def _refuse(why: str) -> dict:
        return {"exit_code": -1, "stdout": "", "stderr": f"folder_erase: {why}"}

    if not project or project_id is None or isinstance(project_id, bool):
        return _refuse(f"needs a project and its id (got {project!r}, {project_id!r})")
    if number < 1:
        return _refuse(f"slot {number} is never a folder slot — refusing to touch it")
    home_resolved = Path(os.path.realpath(jaah_home()))
    expected = home_resolved / "repos" / str(project_id) / str(number)
    target = Path(slot_path(clone_path, number))
    if os.path.islink(target):
        return _refuse(f"{target} is a symlink — refusing to follow it")
    resolved = Path(os.path.realpath(target))
    if resolved != expected:
        return _refuse(f"{resolved} is not {expected}, folder slot {number} of project {project_id} — "
                       f"refusing to touch it")

    if not resolved.exists():
        note_erased(str(resolved))
        return {"exit_code": 0,
                "stdout": json.dumps({"deleted": True, "already_absent": True, "path": str(resolved)}),
                "stderr": ""}

    locks_dir = env_helpers.fleet_locks_dir()
    locks_err = _ensure_locks_dir(locks_dir)
    if locks_err:
        return {"exit_code": 1, "stdout": "", "stderr": f"folder_erase: could not create {locks_dir}: {locks_err}"}
    try:
        lock_fh = open_erase_lock(project, number)
    except OSError as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"folder_erase: cannot open this slot's lock ({exc})"}
    try:
        host_platform.lock(lock_fh, blocking=False)
    except OSError:
        lock_fh.close()
        return {"exit_code": 1, "stdout": "", "stderr": "another worktree operation is in progress for this slot"}
    try:
        err = _remove_folder(str(resolved))
        if err:
            return {"exit_code": 1, "stdout": "", "stderr": f"failed to erase folder {resolved}: {err}"}
        note_erased(str(resolved))
        slot_residue.purge_scratch(str(resolved))
        return {"exit_code": 0, "stdout": json.dumps({"deleted": True, "path": str(resolved)}), "stderr": ""}
    finally:
        host_platform.unlock(lock_fh)
        lock_fh.close()


@native_command("open_finder")
def open_finder(command_type: str, args: dict, run) -> dict:
    # Open the worktree dir in the Ubuntu file manager (xdg-open).
    path = args.get("path", "")
    if not path:
        return {"exit_code": -1, "stdout": "", "stderr": "open_finder needs a worktree path"}
    inner = ('export DISPLAY="${DISPLAY:-:0}"; '
             '[ -S "/run/user/$(id -u)/bus" ] && export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"; '
             f"{session_user.new_session_cmd()} xdg-open {shlex.quote(path)} >/dev/null 2>&1 || true")
    return run("bash", "-lc", inner)


@native_command("open_cursor")
def open_cursor(command_type: str, args: dict, run) -> dict:
    # Pop a desktop terminal running `cursor .` in the worktree.
    path = args.get("path", "")
    if not path:
        return {"exit_code": -1, "stdout": "", "stderr": "open_cursor needs a worktree path"}
    title = f"cursor {Path(path).name}"
    # `setsid -f`, not `& disown`: a disowned job stays in the terminal's session and dies with the
    # SIGHUP on close. macOS has no `setsid` binary (`session_user.new_session_cmd`).
    cursor_cmd = f"{session_user.new_session_cmd(fork=True)} cursor . </dev/null >/dev/null 2>&1"
    inner = (f"exec {shlex.quote(sys.executable)} {shlex.quote(_open_term())} {shlex.quote(title)} "
             f"{shlex.quote(path)} {shlex.quote(cursor_cmd)}")
    # `_tmux_safe_env()`: `open_term.py` pops a long-lived desktop terminal; `cursor` needs no .env.
    return run("bash", "-lc", inner, env=_tmux_safe_env())


def _port_grid_env() -> dict:
    """What THIS CALLER adds to the delegated port-grid run's environment — NEVER `os.environ`. See
    `_PORT_GRID_ENV_KEYS`.

    Additive, not a replacement: `tmux run-shell` still inherits the tmux server's env underneath
    (which holds no NODE_TOKEN because `jaah-runner@.service` refuses `node.env`). Withholding the
    token here keeps it out of that process's `/proc/<pid>/environ`."""
    env: dict[str, str] = {}
    for key in _PORT_GRID_ENV_KEYS:
        value = env_get(key)
        if value is not None:
            env[key] = value
    return env


def _run_port_grid_op(argv: list[str], run) -> dict:
    """The pool port audit, AS THE SESSION USER where the uid split applies (the slot `.env`s
    are `0600 jaah:jaah`; see `_PORT_GRID_OPS`).

    Returns `_native_run`'s shape and caps, so the reply and the exit code (0 clean, 1 drift) are
    byte-stable across both branches. Switch needed but unavailable is a REFUSAL, not a fall-through
    that would report the fix as having run."""
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    if not session_user.switch_needed():
        # No uid split: in process with the daemon's real env, like every other `_CTL_BUS_OPS` op.
        return run(*argv, env=None)
    if not session_user.can_run_as_session_user():
        return {"exit_code": SWITCH_UNAVAILABLE_RC, "stdout": "",
                "stderr": ("port grid: session-user switch required but unavailable — refusing to run "
                           "the pool port audit as the daemon uid, which cannot read (let alone "
                           "rewrite) a pool slot's 0600 jaah:jaah .env; nothing was audited or "
                           "fixed. Check jaah-runner@<node name>.service")}
    # `cwd` stated (the session user must be able to enter it); the ~170s bus budget, not the seam's
    # 120s default.
    return session_user.run_capped(
        argv, cwd=str(_REPO_ROOT), env=_port_grid_env(), timeout=EXECUTE_TIMEOUT_SECONDS,
        as_session_user=True, failure_rc=SWITCH_UNAVAILABLE_RC, failure_prefix="port grid: ")


@native_command("run")
def run_op(command_type: str, args: dict, run) -> dict:
    # Generic worktree op, allowlisted: `args.op` selects from a fixed registry — this IS the
    # docker→host shell boundary. A new button adds a registry entry, never a new command_type.
    op = args.get("op", "")
    project = args.get("project", "")

    # Dispatch rides the CP's `lease_slot` intents; there is no node-side pass to poke.
    ops: dict[str, list[str]] = {
        "doctor": [_python(), _fleet_doctor()],
        # `--fix` rewrites every slot's band, `--restart` brings back only the stacks that were live.
        # Unscoped by default: moving a range start moves every project's band on this node (this org's).
        "ports_reband": (
            [_python(), _check_pool_ports(), "--fix", "--restart"] + ([project] if project else [])
        ),
    }
    if project:
        ops["ports_audit"] = [_python(), _check_pool_ports(), project]
    argv = ops.get(op)
    if argv is None:
        return {"exit_code": -1, "stdout": "",
                "stderr": f"unknown or unavailable run op: {op!r}; allowed: {sorted(ops)}"}
    # The port-grid ops need the session-user seam (see `_PORT_GRID_OPS`).
    if op in _PORT_GRID_OPS:
        return _run_port_grid_op(argv, run)
    # CP-authenticating ops inherit the daemon's env; `_tmux_safe_env()` would strip it. Pinned by the
    # `test_worktree_ops.py` canary. None spawns a tmux server.
    env = None if op in _CTL_BUS_OPS else _tmux_safe_env()
    return run(*argv, env=env)


# A gate command's environment is an ALLOWLIST, never `os.environ` under a denylist: the string is
# operator-authored. A gate takes the rest from its clone's `.env`; locale/TZ so UTF-8 output works.
# HOME/PATH are supplied per branch for the right uid.
_GATE_ENV_KEYS: tuple[str, ...] = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")


def _gate_env() -> dict:
    """The uid-independent half of a gate command's environment — spelled out, NEVER `os.environ`. Plus
    the node-managed engine's endpoint where the CLI default is not it (macOS, `runner_env.docker_env`)."""
    env = runner_env.docker_env()
    env.update({key: os.environ[key] for key in _GATE_ENV_KEYS if key in os.environ})
    return env


@native_command("check")
def check(command_type: str, args: dict, run) -> dict:
    # Run the PROJECT'S CONFIGURED gate at the resolved clone path, within `_native_run`'s ~170s cap.
    # One tier, the CP's call (`gate_cmd` from `projects.gate_quick_cmd`). The CP resolver writes
    # `clone_path` and pops `repo_root`, so neither can smuggle a path; `_REPO_ROOT` only when neither is set.
    clone_path = args.get("clone_path") or args.get("repo_root") or str(_REPO_ROOT)
    # Fails closed when the CP sent no gate (no default gate here). A SENT blank is the project declaring
    # no quick gate — a deliberate "no gate", so nothing runs and nothing fails.
    if "gate_cmd" in args and not str(args["gate_cmd"] or "").strip():
        message = "project declares no quick gate — nothing to run"
        return {"exit_code": 0, "stdout": message, "stderr": message}
    gate_cmd = str(args.get("gate_cmd") or "").strip()
    if not gate_cmd:
        return {"exit_code": 1, "stdout": "",
                "stderr": ("check needs gate_cmd (the project's gate command, sent by the CP from "
                           "projects.gate_quick_cmd) — the node has no gate command of its own; "
                           "nothing was run")}
    # A SHELL: the value is a command line, not an argv; `-l` puts docker/node on PATH. The string is
    # NOT sanitised — what bounds it is PROVENANCE (`gate_cmd` is overwritten server-side from the project
    # row, authored by a governance-authorized author or seeded from the org's Projects > Defaults settings,
    # which an operator or the tenant's Org Admin writes) plus the curated env and session-user delegation.
    argv = ["bash", "-lc", gate_cmd]
    # AS THE SESSION USER where the uid split applies: `_native_run`'s seam keys on `argv[0] == "git"`,
    # so a `bash` gate would run as the daemon (full env with `NODE_TOKEN`; root on cloud). Not fixed by
    # widening that test: `logs`/`open_*` need their desktop session. Env is curated on both branches.
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    if session_user.switch_needed():
        if not session_user.can_run_as_session_user():
            return {"exit_code": SWITCH_UNAVAILABLE_RC, "stdout": "",
                    "stderr": "check: session-user switch required but unavailable — refusing to run "
                              "the project's gate command as the daemon uid; "
                              "check jaah-runner@<node name>.service"}
        # Two legs keep the token out: `_gate_env()`, and the session user's tmux server env —
        # `jaah-runner@.service` refusing `node.env` is a security requirement. ~170s budget.
        return session_user.run_capped(
            argv, cwd=clone_path, env=_gate_env(), timeout=EXECUTE_TIMEOUT_SECONDS,
            as_session_user=True, failure_rc=SWITCH_UNAVAILABLE_RC, failure_prefix="check: ")
    # No uid split: in process, env still curated — a root cloud node lands here, and a gate reading
    # `/proc/self/environ` must not find `NODE_TOKEN`.
    return run(*argv, cwd=clone_path, env={
        **_gate_env(),
        # This process is the runner here. PATH must be present: `subprocess.run(env=…)` resolves
        # argv[0] against the PATH it is handed.
        "HOME": os.environ.get("HOME") or os.path.expanduser("~"),
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
    })


# Git's refname rules, mirrored only to keep a malformed DB value out of the installer's argv; the
# installer is the authority.
_BAD_PREFIX_CHARS = re.compile(r"[\s~^:?*\[\\\x00-\x1f]")


# ── Trigger preflight (the "Repo script" trigger) ───────────────────────────────────────────────────────
#
# CP-INTERNAL, like `worktree_erase`: absent from `app/routes/commands.py`'s `VALID_TYPES`, so only the
# trigger processor (`app/trigger_preflight_script.py`) reaches it. It runs the OWNER'S script from the
# project's repo — the trust `check` already extends to the gate command — against the integration branch,
# in a dedicated checkout `<repos>/<project id>/preflight` beside the main clone: no `worktrees` row, never
# leased, so neither a pool slot nor the main clone's working copy is touched. The script runs as the
# session user, with no credential admitted and a curated env (`_gate_env()` plus the input path).
#
# The script can write its checkout's `.git` (same uid), so that `.git` is never trusted: the checkout is
# recreated on every run, ONLY the fetch runs credential-admitted (inside `clone_identity.cloning`), and
# every git call pins repo-local code off (`_NO_REPO_CODE`) — a hook or fsmonitor planted by one run cannot
# execute under the next run's credential.

#: The env var naming the file that holds the preflight input (`{"event","state","seen_keys"}`), also on stdin.
PREFLIGHT_INPUT_ENV = "JAAH_PREFLIGHT_INPUT"
#: One byte over the CP's 64 KiB output cap, so an oversized answer still reads as "over 64 KiB" there.
PREFLIGHT_STDOUT_CAP = 64 * 1024 + 1
_PREFLIGHT_STDERR_LINE_CAP = 500
_PREFLIGHT_LOCK_WAIT_S = 30.0
# A script without its executable bit still runs when its suffix names the interpreter (`.py`: the node's own,
# `runner_env.python_cmd()`, resolved at call time).
_PREFLIGHT_INTERPRETERS = {".sh": "bash", ".js": "node", ".mjs": "node"}
# Copies stdin to a private per-run file (`mktemp` is 0600), names it in the env, runs the script with that
# file as its stdin, and removes it on exit: the event must not outlive the run on disk.
_PREFLIGHT_WRAPPER = (
    f'F=$(mktemp) || exit 111; trap \'rm -f "$F"\' EXIT; trap \'exit 143\' TERM INT; '
    f'export {PREFLIGHT_INPUT_ENV}="$F"; cat >"$F" || exit 111; "$@" <"$F"'
)
# Repo-local code a git call must never run: hooks, fsmonitor, templates, the `ext::` transport.
_NO_REPO_CODE = git_env.NO_REPO_CODE
# `flock` contention; anything else from the lock call is a real fault.
_LOCK_BUSY_ERRNOS = frozenset({errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, getattr(errno, "EDEADLK", -1)})


def _preflight_result(exit_code: int, stderr: str, *, stdout: str = "", **flags) -> dict:
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, **flags}


def _first_line(text: str) -> str:
    """The first non-blank line, capped — what the deliveries log shows as the reason."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:_PREFLIGHT_STDERR_LINE_CAP]
    return ""


def _cap_stdout(text: str) -> str:
    return (text or "").encode()[:PREFLIGHT_STDOUT_CAP].decode(errors="ignore")


def _preflight_args(args: dict) -> tuple[dict, str]:
    """The validated arguments, or `({}, reason)`. Every value is CP-supplied; re-checked so a malformed one
    never reaches a path or an argv."""
    clone_path = str(args.get("clone_path") or "")
    branch = str(args.get("integration_branch") or "").strip()
    script = str(args.get("script_path") or "").strip()
    clone_url = str(args.get("clone_url") or "").strip()
    try:
        timeout = int(args.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        timeout = 0
    if not clone_path or not os.path.isabs(clone_path) or Path(clone_path).name != "main":
        return {}, "needs clone_path (the project's main clone, …/main)"
    if not branch or branch.startswith("-") or _BAD_PREFIX_CHARS.search(branch) or ".." in branch:
        return {}, "needs integration_branch (a valid branch name)"
    if not clone_url:
        return {}, "needs clone_url (the project's remote, sent by the CP)"
    if not script or script.startswith(("/", "-")) or "\\" in script or ".." in script.split("/"):
        return {}, "needs script_path (a path inside the repository, with no '..')"
    if not 1 <= timeout <= EXECUTE_TIMEOUT_SECONDS:
        return {}, f"needs timeout_seconds between 1 and {int(EXECUTE_TIMEOUT_SECONDS)}"
    if not isinstance(args.get("input"), str):
        return {}, "needs input (the JSON document the script reads)"
    project_id = args.get("project_id")
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        # Without it the mirror would be a second, URL-keyed full clone beside the project's `<pid>.git`.
        return {}, "needs project_id (the project's id, which keys the node's mirror)"
    return {"clone_path": clone_path, "clone_url": clone_url, "branch": branch, "script": script, "timeout": timeout,
            "input": args["input"], "project": str(args.get("project") or ""), "project_id": project_id}, ""


class _OutOfTime(Exception):
    """The run's whole budget (checkout refresh + script) is spent."""


def _left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise _OutOfTime
    return remaining


@native_command("trigger_preflight")
def trigger_preflight(command_type: str, args: dict, run) -> dict:
    """Recreate the preflight checkout at `origin/<integration_branch>`, run the script there, and return
    `{exit_code, stdout (≤64 KiB + 1), stderr (its first line)}`. A reply flagged `unavailable` (lock or git
    identity busy, session-user switch down) is the node's "not now", never the script's "no"; one flagged
    `setup` failed before the script ran; `timed_out` spent the whole `timeout_seconds` budget, which the
    checkout refresh and the script share so the CP's wait is never outrun."""
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    parsed, why = _preflight_args(args)
    if why:
        return _preflight_result(1, f"trigger_preflight {why}; nothing was run", setup=True)
    deadline = time.monotonic() + parsed["timeout"]
    if session_user.switch_needed() and not session_user.can_run_as_session_user():
        return _preflight_result(
            SWITCH_UNAVAILABLE_RC, "the node cannot switch to its session user right now", unavailable=True)
    checkout = str(Path(parsed["clone_path"]).parent / "preflight")
    locks_dir = env_helpers.fleet_locks_dir()
    locks_err = _ensure_locks_dir(locks_dir)
    if locks_err:
        return _preflight_result(1, f"could not create {locks_dir}: {locks_err}", setup=True)
    try:
        lock_fh = _open_lock(locks_dir / f"trigger-preflight-{Path(checkout).parent.name}.lock")
    except OSError as exc:
        return _preflight_result(1, f"cannot open the preflight lock ({exc})", setup=True)
    lock_until = time.monotonic() + min(_PREFLIGHT_LOCK_WAIT_S, parsed["timeout"] - 1)
    try:
        while True:
            try:
                host_platform.lock(lock_fh, blocking=False)
                break
            except OSError as exc:
                if exc.errno not in _LOCK_BUSY_ERRNOS:
                    return _preflight_result(1, f"cannot take the preflight lock ({exc})", setup=True)
                if time.monotonic() >= lock_until:
                    return _preflight_result(
                        -1, "another preflight for this project is running on this node", unavailable=True)
                time.sleep(0.5)
        try:
            refused = _refresh_preflight_checkout(parsed, checkout, deadline, run)
            if refused:
                return refused
            return _run_preflight_script(parsed, checkout, deadline)
        except _OutOfTime:
            return _timed_out(parsed)
    finally:
        lock_fh.close()


def _timed_out(parsed: dict) -> dict:
    return _preflight_result(124, f"timed out after {parsed['timeout']}s", timed_out=True)


def _unadmitted(argv: list[str], *, cwd: str | None, timeout: float, stdin: str | None = None,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """ARGV as the session user (or, with no uid split, in its own process group) with a curated env and NO
    credential admitted. Raises `TimeoutExpired`, and `RuntimeError` when the switch went away."""
    env = {**_gate_env(), **(env or {})}
    if session_user.switch_needed():
        # No `on_child_pid`: nothing is admitted to the credential helper. The far side's `timeout` fires 5s
        # inside the deadline it is given, so the budget handed over is `timeout + 5`.
        return session_user.run_as_session_user(argv, cwd=cwd, env=env, timeout=timeout + 5, input=stdin)
    return _run_in_own_group(argv, stdin, cwd=cwd, timeout=timeout, env={
        **env, "HOME": os.environ.get("HOME") or os.path.expanduser("~"),
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"})


#: Replies carrying it set `mirror_bootstrapping`, so the CP retries them for the clone budget, not its short window.
_MIRROR_BOOTSTRAPPING = "the project's mirror is being created on this node; retry shortly"
#: Mirror paths a background first clone is running for: a full clone outlasts any preflight budget, and one
#: cut off by it leaves nothing to resume, so it runs on a clone's budget and preflights answer "not now".
_bootstrapping: set[str] = set()
#: Mirror path → why its last background clone failed: the next preflight reports it once, then retries the clone.
_bootstrap_failed: dict[str, str] = {}
_bootstrapping_guard = threading.Lock()


def _bootstrap_running(key: git_mirror.MirrorKey) -> bool:
    with _bootstrapping_guard:
        return str(git_mirror.mirror_path(key, shared=True)) in _bootstrapping


def _start_mirror_bootstrap(key: git_mirror.MirrorKey) -> None:
    """Create `key`'s pool mirror in a daemon thread, at most one per mirror."""
    path = str(git_mirror.mirror_path(key, shared=True))
    with _bootstrapping_guard:
        if path in _bootstrapping:
            return
        _bootstrapping.add(path)

    def create() -> None:
        try:
            git_mirror.refresh(key, shared=True, timeout=float(node_settings.get("node_clone_timeout_secs")))
        except Exception as exc:   # noqa: BLE001 — a daemon thread: every failure is logged and reported
            log.warning("trigger_preflight: creating the mirror %s failed: %s", Path(path).name, exc)
            with _bootstrapping_guard:
                _bootstrap_failed[path] = str(exc)
        finally:
            with _bootstrapping_guard:
                _bootstrapping.discard(path)

    threading.Thread(target=create, name=f"mirror-bootstrap-{Path(path).name}", daemon=True).start()


def _refresh_preflight_checkout(parsed: dict, checkout: str, deadline: float, run) -> dict | None:
    """Recreate the checkout: `init` → origin = the CP's `clone_url` → refresh the project's mirror from that URL
    (`git_mirror.refresh`, never the checkout's `origin`) → shallow local `fetch` from it → `checkout --detach --force origin/<branch>` → `clean -fdx`. None when ready, else the setup
    failure, or `unavailable` while the mirror's first clone runs in the background. Raises `_OutOfTime`."""
    branch = parsed["branch"]

    def failed(step: str, rc: int, stderr: str) -> dict:
        detail = _first_line(stderr) or f"exit {rc}"
        return _preflight_result(rc or 1, f"preflight checkout: {step} failed: {detail}", setup=True)

    def local(step: str, *argv: str) -> tuple[subprocess.CompletedProcess | None, dict | None]:
        try:
            proc = _unadmitted(list(argv), cwd=None, timeout=_left(deadline))
        except subprocess.TimeoutExpired:
            raise _OutOfTime from None
        except (RuntimeError, OSError) as exc:
            return None, failed(step, 1, str(exc))
        return proc, (failed(step, proc.returncode, proc.stderr) if proc.returncode != 0 else None)

    for step, argv in (
        ("removing the previous checkout", ("rm", "-rf", "--", checkout)),
        ("git init", ("git", *_NO_REPO_CODE, "init", "-q", checkout)),
        ("setting origin", ("git", *_NO_REPO_CODE, "-C", checkout, "config", "remote.origin.url",
                            parsed["clone_url"])),
    ):
        _proc, refused = local(step, *argv)
        if refused:
            return refused
    # The one credential-admitted call: the mirror's refresh, inside the project's git-identity declaration.
    key = git_mirror.MirrorKey(project_id=parsed["project_id"], repo_id=None, slug=parsed["project"],
                               clone_url=parsed["clone_url"])
    if _bootstrap_running(key):
        return _preflight_result(-1, _MIRROR_BOOTSTRAPPING, unavailable=True, mirror_bootstrapping=True)
    try:
        mirror = git_mirror.refresh(key, shared=True, timeout=_left(deadline),
                                    wait_s=min(_PREFLIGHT_LOCK_WAIT_S, _left(deadline)), create=False)
        if mirror is None:
            with _bootstrapping_guard:
                last_failure = _bootstrap_failed.pop(str(git_mirror.mirror_path(key, shared=True)), None)
            if last_failure is not None:
                return failed("git clone", 1, last_failure)
            _start_mirror_bootstrap(key)
            return _preflight_result(-1, _MIRROR_BOOTSTRAPPING, unavailable=True, mirror_bootstrapping=True)
    except clone_identity.ProjectBusy as exc:
        return _preflight_result(-1, f"another project's git operation is in flight: {exc}", unavailable=True)
    except git_mirror.MirrorError as exc:
        _left(deadline)
        return failed("git fetch", 1, str(exc))
    fetched = run("git", "-C", checkout, *git_mirror.local_fetch_args(
        mirror, git_env.tracking_refspec(branch), depth=1), timeout=_left(deadline), env=git_mirror.LOCAL_FETCH_ENV)
    if fetched["exit_code"] != 0:
        _left(deadline)   # a fetch cut off by the budget is a timeout, not a checkout fault
        return failed("git fetch", fetched["exit_code"], fetched.get("stderr") or "")
    for step, argv in (
        ("git checkout", ("git", *_NO_REPO_CODE, "-C", checkout, "checkout", "--quiet", "--detach", "--force",
                          f"origin/{branch}")),
        ("git clean", ("git", *_NO_REPO_CODE, "-C", checkout, "clean", "-fdxq")),
    ):
        _proc, refused = local(step, *argv)
        if refused:
            return refused
    return None


def _preflight_script_argv(checkout: str, script: str) -> tuple[list[str], str]:
    """The argv that runs `script`, or `([], reason)`: inside the checkout (symlinks resolved), a file, and
    either executable or of a known interpreter's suffix."""
    root = Path(checkout).resolve()
    target = (root / script).resolve()
    if target != root and root not in target.parents:
        return [], f"{script} resolves outside the repository"
    if not target.is_file():
        return [], f"{script} is not a file on the integration branch"
    if os.access(target, os.X_OK):
        return [str(target)], ""
    suffix = target.suffix
    interpreter = runner_env.python_cmd() if suffix == ".py" else _PREFLIGHT_INTERPRETERS.get(suffix)
    if interpreter:
        return [interpreter, str(target)], ""
    return [], (f"{script} is not executable — commit it with the executable bit "
                f"(git update-index --chmod=+x) or use a .py, .sh or .js file")


def _run_preflight_script(parsed: dict, checkout: str, deadline: float) -> dict:
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    script_argv, why = _preflight_script_argv(checkout, parsed["script"])
    if why:
        return _preflight_result(1, why, setup=True)
    budget = _left(deadline)
    started = time.monotonic()
    try:
        proc = _unadmitted(["sh", "-c", _PREFLIGHT_WRAPPER, "preflight", *script_argv], cwd=checkout,
                           timeout=budget, stdin=parsed["input"])
    except subprocess.TimeoutExpired:
        return _timed_out(parsed)
    except RuntimeError as exc:   # the session-user switch went away between the check and the run
        return _preflight_result(SWITCH_UNAVAILABLE_RC, f"could not start the script: {exc}", unavailable=True)
    except OSError as exc:
        return _preflight_result(1, f"could not start the script: {exc}", setup=True)
    # The far side's `timeout` exits 124 (TERM) or 137 (`-k`'s KILL) when it ends the script; a script's own
    # 124 or 137 comes back early.
    if proc.returncode in (124, 137) and time.monotonic() - started >= budget - 1:
        return _timed_out(parsed)
    return _preflight_result(proc.returncode, _first_line(proc.stderr), stdout=_cap_stdout(proc.stdout))


def _run_in_own_group(argv: list[str], stdin: str | None, *, cwd: str | None, timeout: float,
                      env: dict) -> subprocess.CompletedProcess:
    """`subprocess.run(input=…)`, but a timeout kills the whole process group: the script is the wrapper
    shell's child, and killing only the shell would leave it running. (Delegated, the far side's `timeout`
    signals its own group; Windows, which has no group to kill and ignores `start_new_session`, kills the shell.)"""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=cwd, env=env, start_new_session=hasattr(os, "killpg"))
    try:
        out, err = proc.communicate(stdin or "", timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, 9)
            else:
                proc.kill()
        except OSError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


@native_command("sync_access_guard")
def sync_access_guard(command_type: str, args: dict, run) -> dict:
    # Re-install the git-native push guard with the declared branch prefix and push flag,
    # fired by `PUT /api/projects/{id}`. `repo_root` is CP-resolved. Idempotent; revert is not an
    # uninstall — empty prefix + push enabled is the declared unrestricted pair, a no-op hook.
    clone_path = args.get("repo_root")
    if not clone_path:
        return {"exit_code": 1, "stdout": "",
                "stderr": "sync_access_guard needs repo_root (no local clone set for this project)"}
    # PRESENCE, not truthiness: `""` is a legitimate prefix value, so an absent key cannot mean it.
    if "branch_prefix" not in args:
        return {"exit_code": 1, "stdout": "",
                "stderr": ("sync_access_guard needs branch_prefix (the project's, sent by the CP; '' is a "
                           "legal value, an absent key is not)")}
    branch_prefix = str(args.get("branch_prefix") or "")
    if _BAD_PREFIX_CHARS.search(branch_prefix) or ".." in branch_prefix or branch_prefix.startswith("/"):
        return {"exit_code": 1, "stdout": "",
                "stderr": f"sync_access_guard: '{branch_prefix}' is not a legal git ref prefix"}
    # A REAL BOOLEAN or nothing: `bool()` reads absent/None/"false" wrongly and silently — fail-open
    # unfences a push URL, fail-closed wedges a repo.
    push_enabled = args.get("push_enabled")
    if not isinstance(push_enabled, bool):
        return {"exit_code": 1, "stdout": "",
                "stderr": (f"sync_access_guard needs push_enabled as a boolean (got "
                           f"{push_enabled!r}) — the node does not infer it")}
    # AS THE SESSION USER: the installer writes new inodes into `.git`, and as the daemon they would be
    # `jaahd`-owned and unwritable for `jaah`. Fails closed.
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    # `integration_branch` is informational (names where work goes in a refusal), so optional — and the
    # primary's, so only the anchor clone is told it.
    integration_branch = str(args.get("integration_branch") or "")
    # `branch_prefix`/`push_enabled` are project-level (AS1): every clone of the project gets the same pair —
    # the anchor and each `_repos/<rid>/main` of a multi-repo project (`slot_repos`).
    clones = [str(clone_path), *_repo_clones(str(clone_path))]
    results = []
    for i, clone in enumerate(clones):
        argv = access_guard_argv(clone, branch_prefix, push_enabled,
                                 integration_branch if i == 0 else "")
        if session_user.switch_needed():
            if not session_user.can_run_as_session_user():
                return {"exit_code": SWITCH_UNAVAILABLE_RC, "stdout": "",
                        "stderr": "sync_access_guard: session-user switch required but unavailable — "
                                  "refusing to write the clone's .git as the daemon uid; "
                                  "check jaah-runner@<node name>.service"}
            try:
                proc = session_user.run_as_session_user(argv, timeout=120)
            except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired
                # Not `run_capped`: a failed delegation ends the LOOP, not one clone's row.
                return {"exit_code": SWITCH_UNAVAILABLE_RC, "stdout": "",
                        "stderr": f"sync_access_guard: session-user delegation failed: {exc}"}
            results.append(session_user.capped_result(proc))
        else:
            results.append(run(*argv))
    if len(results) == 1:
        return results[0]
    # The first nonzero exit wins; every clone's output is kept, each under its path.
    return {"exit_code": next((r["exit_code"] for r in results if r.get("exit_code")), 0),
            "stdout": "\n".join(f"{c}: {(r.get('stdout') or '').strip()}" for c, r in zip(clones, results))[-8000:],
            "stderr": "\n".join(f"{c}: {(r.get('stderr') or '').strip()}" for c, r in zip(clones, results)
                                if r.get("stderr"))[-2000:]}


def access_guard_argv(clone: str, branch_prefix: str, push_enabled: bool, integration_branch: str = "") -> list[str]:
    """The installer argv for one clone. Flags, not positionals: an empty positional prefix is easily
    dropped, and `--push-enabled true|false` must never be readable from absence."""
    argv = [_python(), _install_access_guard(), clone,
            "--branch-prefix", branch_prefix,
            "--push-enabled", "true" if push_enabled else "false"]
    if integration_branch:
        argv += ["--integration-branch", integration_branch]
    return argv


def _repo_clones(anchor: str) -> list[str]:
    """Every `_repos/<rid>/main` clone beside the anchor `<home>/repos/<pid>/main` (`slot_repos`' layout)."""
    root = slot_repos.repos_root(anchor)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [clone for name in names if name.isdigit()
            and os.path.isdir(os.path.join(clone := os.path.join(root, name, "main"), ".git"))]
